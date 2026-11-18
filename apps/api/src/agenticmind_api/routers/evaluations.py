"""Evaluation Lab API (ADR-0017): datasets (versioned), evaluation runs, per-case results, comparison."""

from __future__ import annotations

from typing import Any

from agenticmind_evaluation import DatasetContent, EvaluationConfig, catalogue, compare
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, ValidationError

from ..db import DatasetRow, EvalRunRow
from ..deps import Services, services
from ..store import iso

router = APIRouter(prefix="/api", tags=["evaluation"])


class DatasetIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    cases: list[dict[str, Any]] = Field(default_factory=list)


class EvaluationIn(BaseModel):
    dataset_id: str
    dataset_version: int | None = None
    revision: int | None = Field(default=None, description="Project revision to evaluate (default: latest)")
    label: str = Field(default="", max_length=200, description="Experiment name, e.g. 'hybrid + rerank'")
    config: EvaluationConfig


def _content(cases: list[dict[str, Any]]) -> DatasetContent:
    try:
        return DatasetContent.model_validate({"cases": cases})
    except ValidationError as exc:
        problems = [
            {"path": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors(include_url=False)
        ]
        raise HTTPException(422, {"message": "Invalid dataset", "problems": problems[:20]}) from exc


def _dataset_out(row: DatasetRow, full: bool = True) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": row.id,
        "project_id": row.project_id,
        "version": row.version,
        "name": row.name,
        "description": row.description,
        "content_hash": row.content_hash,
        "cases_count": len(row.content.get("cases", [])),
        "created_at": iso(row.created_at),
    }
    if full:
        out["cases"] = row.content.get("cases", [])
    return out


def _eval_out(row: EvalRunRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "project_id": row.project_id,
        "dataset_id": row.dataset_id,
        "dataset_version": row.dataset_version,
        "dataset_hash": row.dataset_hash,
        "revision": row.revision,
        "content_hash": row.content_hash,
        "label": row.label,
        "config": row.config,
        "status": row.status,
        "total": row.total,
        "done": row.done,
        "summary": row.summary,
        "setup": row.setup,
        "error": row.error,
        "baseline": row.baseline,
        "created_at": iso(row.created_at),
        "finished_at": iso(row.finished_at),
    }


def _project(svc: Services, project_id: str) -> None:
    if svc.store.get_project(project_id) is None:
        raise HTTPException(404, f"Project {project_id!r} not found")


def _eval(svc: Services, eval_id: str) -> EvalRunRow:
    row = svc.evals.get_eval_run(eval_id)
    if row is None:
        raise HTTPException(404, "Evaluation not found")
    return row


@router.get("/evaluators")
def evaluators() -> list[dict[str, Any]]:
    return [
        {
            "name": s.name,
            "version": s.version,
            "area": s.area,
            "title": s.title,
            "description": s.description,
            "requires": list(s.requires),
            "higher_is_better": s.higher_is_better,
            "uses_judge": s.uses_judge,
            "aggregation": s.aggregation,
            "unit": s.unit,
        }
        for s in catalogue()
    ]


# ---- datasets -------------------------------------------------------------------------------------
@router.get("/projects/{project_id}/datasets")
def list_datasets(project_id: str, svc: Services = Depends(services)) -> list[dict[str, Any]]:
    _project(svc, project_id)
    return [_dataset_out(r, full=False) for r in svc.evals.list_datasets(project_id)]


@router.post("/projects/{project_id}/datasets", status_code=201)
def create_dataset(project_id: str, body: DatasetIn, svc: Services = Depends(services)) -> dict[str, Any]:
    _project(svc, project_id)
    content = _content(body.cases)
    row = svc.evals.create_dataset(
        project_id,
        body.name,
        body.description,
        content.model_dump(mode="json", exclude_defaults=True),
        content.content_hash(),
    )
    return _dataset_out(row)


@router.get("/datasets/{dataset_id}")
def get_dataset(dataset_id: str, version: int | None = None, svc: Services = Depends(services)) -> dict[str, Any]:
    row = svc.evals.get_dataset(dataset_id, version)
    if row is None:
        raise HTTPException(404, "Dataset not found")
    return _dataset_out(row)


@router.put("/datasets/{dataset_id}")
def update_dataset(dataset_id: str, body: DatasetIn, svc: Services = Depends(services)) -> dict[str, Any]:
    """Saving creates a new version (unless nothing changed); earlier versions stay for reproducibility."""
    if svc.evals.get_dataset(dataset_id) is None:
        raise HTTPException(404, "Dataset not found")
    content = _content(body.cases)
    row, created = svc.evals.new_dataset_version(
        dataset_id,
        body.name,
        body.description,
        content.model_dump(mode="json", exclude_defaults=True),
        content.content_hash(),
    )
    return {**_dataset_out(row), "created_version": created}


@router.get("/datasets/{dataset_id}/versions")
def dataset_versions(dataset_id: str, svc: Services = Depends(services)) -> list[dict[str, Any]]:
    return [_dataset_out(r, full=False) for r in svc.evals.dataset_versions(dataset_id)]


@router.delete("/datasets/{dataset_id}", status_code=204)
def delete_dataset(dataset_id: str, svc: Services = Depends(services)) -> Response:
    if not svc.evals.delete_dataset(dataset_id):
        raise HTTPException(404, "Dataset not found")
    return Response(status_code=204)


# ---- evaluation runs ------------------------------------------------------------------------------
@router.post("/projects/{project_id}/evaluations", status_code=202)
async def start_evaluation(project_id: str, body: EvaluationIn, svc: Services = Depends(services)) -> dict[str, Any]:
    _project(svc, project_id)
    dataset = svc.evals.get_dataset(body.dataset_id, body.dataset_version)
    if dataset is None or dataset.project_id != project_id:
        raise HTTPException(404, "Dataset not found in this project")
    if not dataset.content.get("cases"):
        raise HTTPException(422, "The dataset has no cases")
    rev = svc.store.get_revision(project_id, body.revision)
    if rev is None:
        raise HTTPException(404, "Revision not found")
    row = svc.eval_runs.start(project_id, rev, dataset, body.config, body.label)
    return _eval_out(row)


@router.get("/projects/{project_id}/evaluations")
def list_evaluations(project_id: str, svc: Services = Depends(services)) -> list[dict[str, Any]]:
    _project(svc, project_id)
    return [_eval_out(r) for r in svc.evals.list_eval_runs(project_id)]


@router.get("/evaluations/compare")
def compare_evaluations(a: str, b: str, svc: Services = Depends(services)) -> dict[str, Any]:
    ra, rb = _eval(svc, a), _eval(svc, b)
    if ra.summary is None or rb.summary is None:
        raise HTTPException(409, "Both evaluations must have finished")
    result = compare(
        {"summary": ra.summary, "cases": [c.payload for c in svc.evals.list_cases(a)]},
        {"summary": rb.summary, "cases": [c.payload for c in svc.evals.list_cases(b)]},
    )
    warnings = []
    if (ra.dataset_id, ra.dataset_hash) != (rb.dataset_id, rb.dataset_hash):
        warnings.append("The two runs used different datasets or dataset versions; scores are not directly comparable.")
    if (ra.config.get("k"), ra.config.get("judge")) != (rb.config.get("k"), rb.config.get("judge")):
        warnings.append("K or the judge configuration differs between the runs.")
    return {"a": _eval_out(ra), "b": _eval_out(rb), "comparable": not warnings, "warnings": warnings, **result}


@router.get("/evaluations/{eval_id}")
def get_evaluation(eval_id: str, svc: Services = Depends(services)) -> dict[str, Any]:
    return _eval_out(_eval(svc, eval_id))


@router.get("/evaluations/{eval_id}/cases")
def evaluation_cases(eval_id: str, svc: Services = Depends(services)) -> list[dict[str, Any]]:
    _eval(svc, eval_id)
    return [c.payload for c in svc.evals.list_cases(eval_id)]


@router.post("/evaluations/{eval_id}/cancel")
def cancel_evaluation(eval_id: str, svc: Services = Depends(services)) -> dict[str, Any]:
    row = _eval(svc, eval_id)
    if row.status not in ("queued", "running") or not svc.eval_runs.cancel(eval_id):
        raise HTTPException(409, f"Evaluation is {row.status}")
    return _eval_out(_eval(svc, eval_id))


@router.put("/evaluations/{eval_id}/baseline")
def set_baseline(eval_id: str, svc: Services = Depends(services)) -> dict[str, Any]:
    row = _eval(svc, eval_id)
    if row.status != "succeeded":
        raise HTTPException(409, "Only a finished evaluation can be the baseline")
    svc.evals.set_baseline(eval_id)
    return _eval_out(_eval(svc, eval_id))


@router.delete("/evaluations/{eval_id}", status_code=204)
def delete_evaluation(eval_id: str, svc: Services = Depends(services)) -> Response:
    row = _eval(svc, eval_id)
    if svc.eval_runs.is_active(eval_id):
        raise HTTPException(409, f"Evaluation is {row.status}; cancel it first")
    svc.evals.delete_eval_run(eval_id)
    return Response(status_code=204)
