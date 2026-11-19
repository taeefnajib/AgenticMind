from __future__ import annotations

from typing import Any

from agenticmind_ir import IR_VERSION, ir_json_schema
from fastapi import APIRouter, Depends, HTTPException

from ..deps import Services, services
from ..templates import list_templates, load_template

router = APIRouter(prefix="/api", tags=["registry"])


@router.get("/health")
def health(svc: Services = Depends(services)) -> dict[str, Any]:
    return {"status": "ok", "ir_version": IR_VERSION, "node_types": len(svc.registry.entries())}


@router.get("/nodes")
def list_nodes(svc: Services = Depends(services)) -> list[dict[str, Any]]:
    """All installed node manifests with registry trust metadata."""
    return [
        {
            **entry.manifest.model_dump(mode="json"),
            "trust": entry.trust.value,
            "source": entry.source,
            "isolated": entry.isolated,
            "permitted": entry.permitted,
        }
        for entry in svc.registry.entries()
    ]


@router.get("/ir/schema")
def get_ir_schema() -> dict[str, Any]:
    return ir_json_schema()


@router.get("/templates")
def get_templates() -> list[dict[str, Any]]:
    return list_templates()


@router.get("/templates/{name}")
def get_template(name: str) -> dict[str, Any]:
    try:
        return load_template(name)
    except KeyError as exc:
        raise HTTPException(404, f"Unknown template {name!r}") from exc
