"""Images on edges: Image Input, LLM 1.2.0 with vision inputs, and the image port type."""

from __future__ import annotations

import base64

import pytest
from agenticmind_node_sdk import NodeError, PortType, value_matches_type
from agenticmind_node_sdk.testing import MemoryArtifacts, invoke_node
from agenticmind_nodes_core import models
from agenticmind_nodes_core.io import image_input
from agenticmind_nodes_core.models import llm_v12
from agenticmind_providers import ChatResponse, ModelUsage

PNG = b"\x89PNG\r\n\x1a\nfake-png"
FILES = MemoryArtifacts(
    {
        "art_photo": ("photo.png", PNG),
        "art_scan": ("scan.tiff", b"II*\x00tiff"),
        "art_notes": ("notes.txt", b"hello"),
        "art_cat": ("cat.jpg", b"\xff\xd8\xffjpeg"),
    }
)
MOCK = {"provider": "mock", "model": "vision-1"}


async def test_image_input_reads_the_run_input_or_its_default():
    r = await invoke_node(image_input, config={"key": "photo"}, run_input={"photo": "art_photo"}, artifacts=FILES)
    assert r.outputs["image"] == {
        "kind": "image", "artifact_id": "art_photo", "filename": "photo.png", "media_type": "image/png", "size": 16,
    }  # fmt: skip
    assert value_matches_type(r.outputs["image"], PortType.IMAGE)
    r = await invoke_node(image_input, config={"default": ["art_cat"]}, artifacts=FILES)
    assert r.outputs["image"]["media_type"] == "image/jpeg"
    r = await invoke_node(image_input, run_input={"image": {"artifact_id": "art_photo"}}, artifacts=FILES)
    assert r.outputs["image"]["artifact_id"] == "art_photo"
    with pytest.raises(NodeError) as exc:
        await invoke_node(image_input, artifacts=FILES)
    assert exc.value.code == "INPUT_MISSING"
    with pytest.raises(NodeError) as exc:
        await invoke_node(image_input, run_input={"image": "art_notes"}, artifacts=FILES)
    assert exc.value.code == "INPUT_TYPE_MISMATCH" and "not an image" in str(exc.value)


async def test_llm_sends_images_to_vision_models():
    image = {"kind": "image", "artifact_id": "art_photo", "filename": "photo.png", "media_type": "image/png"}
    cat = {"kind": "image", "artifact_id": "art_cat", "filename": "cat.jpg", "media_type": "image/jpeg"}
    r = await invoke_node(
        llm_v12, inputs={"prompt": "What is this?", "image": image, "images": [cat, image]}, config=MOCK,
        artifacts=FILES,
    )  # fmt: skip
    # the duplicate is sent once; the mock reports what it was given
    assert r.outputs["text"] == "[mock:vision-1] What is this? [saw 2 images: image/jpeg, image/png]"
    plain = await invoke_node(llm_v12, inputs={"prompt": "hi"}, config=MOCK, artifacts=FILES)
    assert plain.outputs["text"] == "[mock:vision-1] hi"


async def test_llm_image_bytes_reach_the_provider(monkeypatch):
    seen = []

    class Capture:
        async def chat(self, request, *, api_key=None, base_url=None):
            seen.append(request)
            return ChatResponse(text="a cat", usage=ModelUsage(provider="x", model=request.model))

    monkeypatch.setattr(models, "get_provider", lambda name: Capture())
    cat = {"kind": "image", "artifact_id": "art_cat", "filename": "cat.jpg", "media_type": "image/jpeg"}
    await invoke_node(llm_v12, inputs={"prompt": "?", "image": cat}, config=MOCK, artifacts=FILES)
    [part] = seen[0].messages[0].images
    assert part.media_type == "image/jpeg" and base64.b64decode(part.data_base64) == b"\xff\xd8\xffjpeg"


async def test_llm_refuses_formats_vision_models_cannot_read():
    scan = {"kind": "image", "artifact_id": "art_scan", "filename": "scan.tiff", "media_type": "image/tiff"}
    with pytest.raises(NodeError) as exc:
        await invoke_node(llm_v12, inputs={"prompt": "?", "image": scan}, config=MOCK, artifacts=FILES)
    assert exc.value.code == "IMAGE_FORMAT_UNSUPPORTED" and "OCR" in str(exc.value)
    with pytest.raises(NodeError) as exc:
        await invoke_node(llm_v12, inputs={"prompt": "?", "images": ["art_photo"]}, config=MOCK, artifacts=FILES)
    assert exc.value.code == "INPUT_TYPE_MISMATCH"
    many = [{"kind": "image", "artifact_id": f"a{i}", "media_type": "image/png"} for i in range(17)]
    with pytest.raises(NodeError) as exc:
        await invoke_node(llm_v12, inputs={"prompt": "?", "images": many}, config=MOCK, artifacts=FILES)
    assert exc.value.code == "TOO_MANY_IMAGES"


def test_providers_offered_and_validated_everywhere():
    cfg = models.LLMConfig
    assert cfg(provider="ollama", model="llava").base_url is None  # local server, no key
    with pytest.raises(ValueError, match="HF_TOKEN"):
        cfg(provider="huggingface", model="Qwen/Qwen3-VL-8B-Instruct")
    assert cfg(provider="huggingface", model="m", api_key={"$secret": "HF_TOKEN"}).provider == "huggingface"
    assert llm_v12.manifest.permissions.artifacts and llm_v12.manifest.version == "1.2.0"
