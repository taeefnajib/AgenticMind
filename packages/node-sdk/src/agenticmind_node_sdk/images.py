"""Images on data edges (``PortType.IMAGE``).

An image value is a small reference to an uploaded project file, never the pixels themselves, so traces,
checkpoints and run records stay small and images are read (with the ``artifacts`` permission) only by the
node that needs the bytes, e.g. an LLM sending them to a vision model or the OCR node.

    {"kind": "image", "artifact_id": "art_...", "filename": "receipt.png", "media_type": "image/png",
     "size": 48213}
"""

from __future__ import annotations

from typing import Any

from .artifacts import ArtifactInfo

#: Uploadable image formats. SVG is deliberately absent (it can carry scripts).
IMAGE_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
#: Formats vision models accept (OpenAI-style APIs); BMP/TIFF are for OCR.
VISION_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})


def is_image_media_type(media_type: str) -> bool:
    return media_type in IMAGE_MEDIA_TYPES.values()


def image_ref(info: ArtifactInfo) -> dict[str, Any]:
    return {
        "kind": "image",
        "artifact_id": info.id,
        "filename": info.filename,
        "media_type": info.media_type,
        "size": info.size,
    }


def is_image(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and value.get("kind") == "image"
        and isinstance(value.get("artifact_id"), str)
        and isinstance(value.get("media_type"), str)
    )


def coerce_images(value: Any) -> list[dict[str, Any]]:
    """One image or a list of images (e.g. an Image Loader's ``images``) → a list; anything else → error."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    if not all(is_image(v) for v in items):
        raise ValueError("expected image references (from Image Input or Image Loader)")
    return items
