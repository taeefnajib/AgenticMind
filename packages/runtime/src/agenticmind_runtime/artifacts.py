"""Project-scoped file storage for uploads and generated artifacts.

``LocalArtifactStore`` keeps files on local disk::

    <root>/<project_id>/<artifact_id>/blob
    <root>/<project_id>/<artifact_id>/meta.json

It needs no database, so the API and the CLI share it. An S3-compatible store will implement the
same methods (ADR-0007). IDs are generated server-side and validated on every access, so client
input can never become a filesystem path.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
import unicodedata
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Protocol

from agenticmind_node_sdk import ArtifactInfo, ArtifactNotFoundError, ArtifactReader

_PROJECT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ARTIFACT_ID = re.compile(r"^art_[a-f0-9]{16}$")
_CHUNK = 1024 * 1024

#: Extensions accepted for upload, mapped to the media type we record (client types are not trusted).
ALLOWED_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".ndjson": "application/x-ndjson",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".epub": "application/epub+zip",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".rst": "text/x-rst",
    ".log": "text/plain",
    # images: vision models and OCR (no SVG: it can carry scripts)
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".sqlite": "application/vnd.sqlite3",
    ".sqlite3": "application/vnd.sqlite3",
    ".db": "application/vnd.sqlite3",
    ".duckdb": "application/vnd.duckdb",
}


class ArtifactError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ArtifactStore(Protocol):
    def scoped(self, project_id: str) -> ArtifactReader: ...


def sanitize_filename(name: str) -> str:
    name = unicodedata.normalize("NFKC", name).replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable()).strip().strip(".")
    return name[:200] or "file"


class LocalArtifactStore:
    def __init__(self, root: Path | str, *, max_bytes: int = 50 * 1024 * 1024) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes

    # -- paths ------------------------------------------------------------------------------
    def _project_dir(self, project_id: str) -> Path:
        if not _PROJECT_ID.match(project_id):
            raise ArtifactError("ARTIFACT_INVALID_PROJECT", "Invalid project id")
        return self.root / project_id

    def _dir(self, project_id: str, artifact_id: str) -> Path:
        if not _ARTIFACT_ID.match(artifact_id):
            raise ArtifactNotFoundError(artifact_id)
        return self._project_dir(project_id) / artifact_id

    # -- writes -----------------------------------------------------------------------------
    def put_stream(self, project_id: str, filename: str, stream: BinaryIO) -> ArtifactInfo:
        clean = sanitize_filename(filename)
        ext = Path(clean).suffix.lower()
        if ext not in ALLOWED_TYPES:
            allowed = ", ".join(sorted(ALLOWED_TYPES))
            raise ArtifactError(
                "ARTIFACT_TYPE_NOT_ALLOWED", f"File type '{ext or '(none)'}' is not allowed ({allowed})"
            )
        artifact_id = f"art_{uuid.uuid4().hex[:16]}"
        target = self._dir(project_id, artifact_id)
        target.mkdir(parents=True, exist_ok=False)
        digest = hashlib.sha256()
        size = 0
        try:
            with open(target / "blob", "wb") as out:
                while chunk := stream.read(_CHUNK):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ArtifactError(
                            "ARTIFACT_TOO_LARGE", f"File exceeds the {self.max_bytes // (1024 * 1024)} MB limit"
                        )
                    digest.update(chunk)
                    out.write(chunk)
            if size == 0:
                raise ArtifactError("ARTIFACT_EMPTY", "File is empty")
            info = ArtifactInfo(
                id=artifact_id,
                filename=clean,
                media_type=ALLOWED_TYPES[ext],
                size=size,
                sha256=digest.hexdigest(),
                created_at=datetime.now(UTC).isoformat(),
            )
            (target / "meta.json").write_text(info.model_dump_json(), encoding="utf-8")
            return info
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise

    def put_bytes(self, project_id: str, filename: str, data: bytes) -> ArtifactInfo:
        import io

        return self.put_stream(project_id, filename, io.BytesIO(data))

    def delete(self, project_id: str, artifact_id: str) -> bool:
        try:
            target = self._dir(project_id, artifact_id)
        except ArtifactNotFoundError:
            return False
        if not target.is_dir():
            return False
        shutil.rmtree(target)
        return True

    def delete_project(self, project_id: str) -> None:
        shutil.rmtree(self._project_dir(project_id), ignore_errors=True)

    # -- reads ------------------------------------------------------------------------------
    def info(self, project_id: str, artifact_id: str) -> ArtifactInfo:
        meta = self._dir(project_id, artifact_id) / "meta.json"
        if not meta.is_file():
            raise ArtifactNotFoundError(artifact_id)
        return ArtifactInfo.model_validate(json.loads(meta.read_text(encoding="utf-8")))

    def path(self, project_id: str, artifact_id: str) -> Path:
        blob = self._dir(project_id, artifact_id) / "blob"
        if not blob.is_file():
            raise ArtifactNotFoundError(artifact_id)
        return blob

    def read_bytes(self, project_id: str, artifact_id: str) -> bytes:
        return self.path(project_id, artifact_id).read_bytes()

    def list(self, project_id: str) -> list[ArtifactInfo]:
        base = self._project_dir(project_id)
        if not base.is_dir():
            return []
        out = []
        for d in base.iterdir():
            if _ARTIFACT_ID.match(d.name) and (d / "meta.json").is_file():
                out.append(ArtifactInfo.model_validate(json.loads((d / "meta.json").read_text(encoding="utf-8"))))
        return sorted(out, key=lambda a: (a.created_at or "", a.id))

    def scoped(self, project_id: str) -> ProjectArtifacts:
        self._project_dir(project_id)  # validates
        return ProjectArtifacts(self, project_id)


class ProjectArtifacts:
    """``ArtifactReader`` bound to one project (what nodes receive)."""

    def __init__(self, store: LocalArtifactStore, project_id: str) -> None:
        self._store = store
        self._project_id = project_id

    def info(self, artifact_id: str) -> ArtifactInfo:
        return self._store.info(self._project_id, artifact_id)

    def read_bytes(self, artifact_id: str) -> bytes:
        return self._store.read_bytes(self._project_id, artifact_id)

    def list(self) -> list[ArtifactInfo]:
        return self._store.list(self._project_id)


def guess_media_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return ALLOWED_TYPES.get(ext) or mimetypes.guess_type(filename)[0] or "application/octet-stream"
