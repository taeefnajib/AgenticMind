"""Trigger endpoints (ADR-0035).

* ``GET /api/projects/{id}/triggers``: status of the project's active triggers (for the IDE).
* ``POST /api/triggers/{project}/{node}``: inbound webhooks and chat webhooks (Slack, Telegram, WhatsApp).
* ``GET /api/triggers/{project}/{node}``: WhatsApp's webhook verification handshake.

``/api/triggers/`` is public (no session): each trigger verifies its own caller (token / HMAC / Slack signing /
Telegram secret header / WhatsApp signature) and unknown or inactive triggers answer 404 / 503.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from ..deps import Services, services
from ..triggers import channels as ch

router = APIRouter(prefix="/api", tags=["triggers"])
MAX_BODY_BYTES = 2 * 1024 * 1024


@router.get("/projects/{project_id}/triggers")
def list_triggers(project_id: str, svc: Services = Depends(services)) -> list[dict[str, Any]]:
    if svc.store.get_project(project_id) is None:
        raise HTTPException(404, f"Project {project_id!r} not found")
    return svc.triggers.status(project_id)


def _trigger(svc: Services, project_id: str, node_id: str) -> Any:
    trig = svc.triggers.get(project_id, node_id)
    if trig is None or trig.kind not in ("webhook", "chat"):
        raise HTTPException(404, "No active webhook trigger here (is it enabled in the latest saved revision?)")
    if trig.problem:
        raise HTTPException(503, f"This trigger is not ready: {trig.problem}")
    return trig


def _reject(exc: ch.Rejected) -> JSONResponse:
    return JSONResponse({"detail": exc.message}, status_code=exc.status)


@router.get("/triggers/{project_id}/{node_id}")
def verify(project_id: str, node_id: str, request: Request, svc: Services = Depends(services)) -> Response:
    trig = _trigger(svc, project_id, node_id)
    if trig.kind != "chat" or trig.config.channel != "whatsapp":
        raise HTTPException(405, "Send a POST request")
    try:
        challenge = ch.whatsapp_challenge(svc.triggers.secret(trig.config.whatsapp_verify_token),
                                          dict(request.query_params))  # fmt: skip
    except ch.Rejected as exc:
        return _reject(exc)
    return PlainTextResponse(challenge)


@router.post("/triggers/{project_id}/{node_id}")
async def receive(project_id: str, node_id: str, request: Request, svc: Services = Depends(services)) -> Response:
    trig = _trigger(svc, project_id, node_id)
    if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
        raise HTTPException(413, "Request body too large")
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(413, "Request body too large")
    headers = {k.lower(): v for k, v in request.headers.items()}
    tsvc = svc.triggers
    c = trig.config
    try:
        if trig.kind == "webhook":
            ch.verify_webhook(c.auth, tsvc.secret(c.secret), headers, body, c.signature_header)
            payload = ch.parse_body(body, headers.get("content-type", ""))
            inbound = ch.Inbound(payload=payload, text=payload if isinstance(payload, str) else "",
                                 meta={"method": "POST", "content_type": headers.get("content-type", "")})  # fmt: skip
            run_id = await tsvc.fire(trig, inbound)
            if c.respond == "wait" and run_id:
                status, outputs, error = await tsvc.wait_outputs(run_id, c.wait_seconds)
                if status == "running":
                    return JSONResponse({"run_id": run_id, "status": status}, status_code=202)
                result = outputs.get(c.response_output) if c.response_output else outputs
                code = 200 if status == "succeeded" else 500
                return JSONResponse({"run_id": run_id, "status": status, "output": result, "error": error},
                                    status_code=code)  # fmt: skip
            return JSONResponse({"run_id": run_id, "status": "accepted"}, status_code=202)
        if c.channel == "slack":
            ch.verify_slack(tsvc.secret(c.slack_signing_secret), headers, body)
            data = json.loads(body or b"{}")
            if data.get("type") == "url_verification":
                return JSONResponse({"challenge": data.get("challenge", "")})
            inbound = ch.parse_slack(data)
            if inbound is not None:
                await tsvc.fire(trig, inbound)  # Slack retries are deduplicated by event_id
            return Response(status_code=200)  # acknowledge within Slack's 3 seconds; the reply comes later
        if c.channel == "telegram":
            ch.verify_telegram(tsvc.secret(c.telegram_secret_token), headers)
            inbound = ch.parse_telegram(json.loads(body or b"{}"))
            if inbound is not None:
                await tsvc.fire(trig, inbound)
            return Response(status_code=200)
        if c.channel == "whatsapp":
            ch.verify_whatsapp(tsvc.secret(c.whatsapp_app_secret), headers, body)
            for inbound in ch.parse_whatsapp(json.loads(body or b"{}")):
                await tsvc.fire(trig, inbound)  # Meta retries for days: deduplicated by message id
            return Response(status_code=200)
    except ch.Rejected as exc:
        return _reject(exc)
    except json.JSONDecodeError:
        raise HTTPException(400, "Body is not JSON") from None
    raise HTTPException(405, "Email triggers poll a mailbox; they have no webhook")
