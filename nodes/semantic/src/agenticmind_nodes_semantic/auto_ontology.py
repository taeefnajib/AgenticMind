"""Client for an NVIDIA Auto Ontology deployment's public REST API (ADR-0019).

Pinned to upstream commit ``UPSTREAM_COMMIT``, whose gateway API is OpenAPI ``API_VERSION``. Only the
operations in ``contract/auto-ontology-api.subset.json`` (extracted from that spec) are used, and the
contract tests check every request against it. Auto Ontology is a deployed service, not a library: this
module talks to its authenticated Next.js gateway (``APP_URL``) with an API token sent as ``x-api-key``.
Never point it at the internal FastAPI port, which trusts an identity header without verifying it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
from agenticmind_node_sdk import NodeError, TransientNodeError

UPSTREAM_REPO = "https://github.com/NVIDIA/auto-ontology"
UPSTREAM_COMMIT = "778ccdef75536bee5653306f8ded655924e4ada8"
API_VERSION = "1.3.0"

_SSE_DATA = "data:"
_SSE_DONE = "[DONE]"


@dataclass
class OntologyAnswer:
    answer: str
    sql: str
    rows: list[dict[str, Any]]
    steps: list[dict[str, Any]] = field(default_factory=list)

    @property
    def columns(self) -> list[str]:
        cols: list[str] = []
        for row in self.rows:
            cols.extend(k for k in row if k not in cols)
        return cols


def parse_rows(raw: Any) -> list[dict[str, Any]]:
    """Rows arrive as a one-item list holding a JSON-records string, or as the records themselves
    (mirrors upstream ``mcp/auto_ontology_mcp/chat.py::_parse_rows`` at the pinned commit)."""
    if raw is None:
        return []
    if isinstance(raw, list) and len(raw) == 1 and isinstance(raw[0], str):
        try:
            raw = json.loads(raw[0])
        except json.JSONDecodeError:
            return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def decode_sse_line(line: str) -> dict[str, Any] | None:
    if not line.startswith(_SSE_DATA):
        return None
    payload = line[len(_SSE_DATA) :].strip()
    if not payload or payload == _SSE_DONE:
        return None
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        return None  # one lost progress update is better than a lost answer
    return event if isinstance(event, dict) else None


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        if isinstance(body, dict):
            return str(body.get("detail") or body.get("error") or body)[:300]
    except ValueError:
        pass
    return response.text.strip()[:300]


def raise_for_status(response: httpx.Response, *, what: str) -> None:
    status = response.status_code
    if status < 400:
        return
    detail = _detail(response)
    if not detail:
        detail = f"HTTP {status}"
    if status == 401:
        raise NodeError(
            "ONTOLOGY_UNAUTHORIZED",
            f"Auto Ontology rejected the API token ({what}). Mint one under the user menu → API Tokens "
            f"and reference it as a secret. ({detail})",
        )
    if status == 403:
        raise NodeError("ONTOLOGY_FORBIDDEN", f"The token's owner lacks permission for {what}. ({detail})")
    if status == 404:
        raise NodeError("ONTOLOGY_NOT_FOUND", f"Not found: {what}. ({detail})")
    if status == 409:
        busy = "in progress" in detail.lower() or "in flight" in detail.lower()
        raise NodeError(
            "ONTOLOGY_BUSY" if busy else "ONTOLOGY_NOT_READY",
            f"Auto Ontology cannot answer right now: {detail}"
            + ("" if busy else ". Turn on Settings → Semantic Compilation and wait until it is ready."),
        )
    if status in (502, 503, 504):
        raise TransientNodeError("ONTOLOGY_UNAVAILABLE", f"Auto Ontology is unavailable (HTTP {status}): {detail}")
    raise NodeError("ONTOLOGY_API_ERROR", f"Auto Ontology returned HTTP {status} for {what}: {detail}")


class AutoOntologyClient:
    """Thin async client. ``transport`` lets tests substitute a contract-checked fake server."""

    def __init__(
        self,
        base_url: str,
        token: str | None,
        *,
        timeout: float = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"Accept": "application/json", "User-Agent": "AgenticMind-AutoOntology-Adapter"}
        if token:
            headers["x-api-key"] = token
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=timeout, transport=transport)

    async def __aenter__(self) -> AutoOntologyClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def _get(self, path: str, *, what: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = await self._client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise TransientNodeError("ONTOLOGY_TIMEOUT", f"Auto Ontology did not answer {what} in time") from exc
        except httpx.HTTPError as exc:
            raise TransientNodeError(
                "ONTOLOGY_UNREACHABLE", f"Could not reach Auto Ontology at {self.base_url}: {exc}"
            ) from exc
        raise_for_status(response, what=what)
        try:
            return response.json()
        except ValueError as exc:
            raise NodeError(
                "ONTOLOGY_API_ERROR",
                f"{what} did not return JSON. Is {self.base_url} the Auto Ontology gateway (APP_URL)?",
            ) from exc

    # ---- operations (each is in the pinned contract) ----
    async def health(self) -> dict[str, Any]:
        """``GET /api/health`` (unauthenticated): 200 ok, 503 degraded."""
        try:
            response = await self._client.get("/api/health")
        except httpx.HTTPError as exc:
            raise TransientNodeError(
                "ONTOLOGY_UNREACHABLE", f"Could not reach Auto Ontology at {self.base_url}: {exc}"
            ) from exc
        if response.status_code in (200, 503):
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict) and "status" in body:
                return body
        raise NodeError(
            "ONTOLOGY_API_ERROR",
            f"{self.base_url}/api/health is not an Auto Ontology health probe (HTTP {response.status_code}). "
            "Use the gateway URL (APP_URL, port 3000 in Docker Compose).",
        )

    async def semantic_status(self) -> dict[str, Any]:
        """``GET /api/semantic-compilation/status``: {calculated, running, last_success_at, last_failure_at}."""
        return await self._get("/api/semantic-compilation/status", what="the semantic-compilation status")

    async def list_terms(self, query: str | None = None, limit: int | None = None) -> dict[str, Any]:
        """``GET /api/terms``: {terms: [TermListItem], total, …}."""
        params: dict[str, Any] = {}
        if query:
            params["query"] = query
        if limit is not None:
            params["limit"] = max(1, min(int(limit), 100))
        return await self._get("/api/terms", what="the term list", params=params)

    async def get_term(self, term_id: str) -> dict[str, Any]:
        """``GET /api/terms/{term_id}`` → ``{data: TermDetail}``."""
        body = await self._get(f"/api/terms/{_segment(term_id)}", what=f"term {term_id!r}")
        return body.get("data", body) if isinstance(body, dict) else {}

    async def term_sql_attributes(self, term_id: str) -> list[dict[str, Any]]:
        """``GET /api/terms/{term_id}/sql-attributes`` → paged list."""
        body = await self._get(
            f"/api/terms/{_segment(term_id)}/sql-attributes", what=f"SQL attributes of term {term_id!r}"
        )
        if isinstance(body, dict):
            items = body.get("data", body.get("items", []))
            return items if isinstance(items, list) else []
        return []

    async def ask(
        self,
        question: str,
        *,
        evidence: str | None = None,
        target_db: str | None = None,
        timeout: float = 300,
        on_step: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> OntologyAnswer:
        """``POST /api/chat/completions`` (SSE): stateless, one question, read until ``result``/``error``."""
        body: dict[str, Any] = {"question": question}
        if evidence:
            body["evidence"] = evidence
        if target_db:
            body["target_db"] = target_db
        answer: OntologyAnswer | None = None
        steps: list[dict[str, Any]] = []
        try:
            # httpx timeouts bound each read, and a run keeps sending step events, so a stuck upstream run
            # (e.g. its embedding endpoint hanging and being retried) would never time out without a
            # deadline on the whole request.
            async with (
                asyncio.timeout(timeout),
                self._client.stream(
                    "POST",
                    "/api/chat/completions",
                    json=body,
                    headers={"Accept": "text/event-stream"},
                    timeout=httpx.Timeout(timeout, connect=10),
                ) as response,
            ):
                if response.status_code >= 400:
                    await response.aread()
                    raise_for_status(response, what="the question")
                async for event in _events(response.aiter_lines()):
                    kind = event.get("type")
                    if kind == "step":
                        step = {k: event.get(k) for k in ("node", "label", "phase", "thought") if event.get(k)}
                        if step.get("phase") != "start":
                            steps.append(step)
                        if on_step is not None:
                            await on_step(step)
                    elif kind == "result":
                        raw = event.get("answer") or {}
                        answer = OntologyAnswer(
                            answer=str(raw.get("response") or ""),
                            sql=str(raw.get("sql_code") or ""),
                            rows=parse_rows(raw.get("sql_response_from_db") or raw.get("result")),
                            steps=steps,
                        )
                    elif kind == "error":
                        raise NodeError(
                            "ONTOLOGY_ANSWER_FAILED",
                            f"Auto Ontology's agent failed: {event.get('message') or 'unknown'}",
                        )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise NodeError(
                "ONTOLOGY_TIMEOUT",
                f"Auto Ontology did not answer within {timeout:g}s. Check its model endpoints (its logs show "
                "retries), or raise the timeout.",
            ) from exc
        except httpx.HTTPError as exc:
            raise TransientNodeError(
                "ONTOLOGY_UNREACHABLE", f"Could not reach Auto Ontology at {self.base_url}: {exc}"
            ) from exc
        if answer is None:
            raise NodeError(
                "ONTOLOGY_ANSWER_FAILED",
                "Auto Ontology ended the run without an answer (cancelled, or the question does not map to the data)",
            )
        return answer


async def _events(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    async for line in lines:
        event = decode_sse_line(line)
        if event is not None:
            yield event


def _segment(value: str) -> str:
    from urllib.parse import quote

    return quote(str(value), safe="")
