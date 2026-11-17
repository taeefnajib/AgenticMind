"""Inspect and delete a project's memory (ADR-0012): long-term memories and conversations."""

from __future__ import annotations

from agenticmind_node_sdk import ConversationTurn, MemoryRecord, valid_key
from fastapi import APIRouter, Depends, HTTPException, Query, Response

from ..deps import Services, services
from ..memory_store import SqlMemory

router = APIRouter(prefix="/api/projects/{project_id}", tags=["memory"])


def _memory(svc: Services, project_id: str) -> SqlMemory:
    if svc.store.get_project(project_id) is None:
        raise HTTPException(404, f"Project {project_id!r} not found")
    return svc.memory.scoped(project_id)


def _check_key(value: str, what: str) -> str:
    if not valid_key(value):
        raise HTTPException(400, f"Invalid {what}")
    return value


@router.get("/memories", response_model=list[MemoryRecord])
def list_memories(
    project_id: str,
    namespace: str | None = None,
    q: str | None = Query(default=None, max_length=200),
    svc: Services = Depends(services),
) -> list[MemoryRecord]:
    return _memory(svc, project_id).list_memories(namespace, q)


@router.delete("/memories/{memory_id}", status_code=204)
def delete_memory(project_id: str, memory_id: str, svc: Services = Depends(services)) -> Response:
    if not _memory(svc, project_id).delete_memory(memory_id):
        raise HTTPException(404, "Memory not found")
    return Response(status_code=204)


@router.delete("/memories")
def clear_memories(project_id: str, namespace: str | None = None, svc: Services = Depends(services)) -> dict:
    if namespace is not None:
        _check_key(namespace, "namespace")
    return {"deleted": _memory(svc, project_id).clear_memories(namespace)}


@router.get("/conversations")
def list_conversations(project_id: str, svc: Services = Depends(services)) -> list[dict]:
    return _memory(svc, project_id).list_conversations()


@router.get("/conversations/{conversation_id}", response_model=list[ConversationTurn])
def get_conversation(
    project_id: str, conversation_id: str, svc: Services = Depends(services)
) -> list[ConversationTurn]:
    return _memory(svc, project_id).conversation(_check_key(conversation_id, "conversation id"))


@router.delete("/conversations/{conversation_id}")
def delete_conversation(project_id: str, conversation_id: str, svc: Services = Depends(services)) -> dict:
    deleted = _memory(svc, project_id).delete_conversation(_check_key(conversation_id, "conversation id"))
    if not deleted:
        raise HTTPException(404, "Conversation not found")
    return {"deleted": deleted}
