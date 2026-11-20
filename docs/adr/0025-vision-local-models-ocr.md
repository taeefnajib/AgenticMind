# ADR-0025: Images, vision models, Ollama and Hugging Face, open-source OCR

**Status:** Accepted. This is step 1 of the "build any agentic workflow" plan. Later steps cover data
sources, chunking, vector databases, reasoning and agent architectures, and guardrails.

## Context
Users asked for:
- images as inputs to vision models: an image data loader and an image manual input;
- LLMs through Ollama and Hugging Face;
- OCR with open-source tools.

The runtime had no image type. Providers sent text only, and the Document Loader stopped on scanned
PDFs.

## Decision

### Images on edges
- **New port type `Image`.**
  - Node SDK `PortType.IMAGE`, mirrored in `graph.ts`. It stays strict: no implicit conversion to or from
    JSON.
  - An image value is a *reference* to an uploaded project file:
    `{"kind": "image", "artifact_id", "filename", "media_type", "size"}`.
  - Traces, checkpoints and run records never contain pixels. Only the node that needs the bytes reads
    them, and it must declare `artifacts`.
- **Uploads** now accept PNG, JPEG, WebP, GIF, BMP and TIFF. SVG is refused, because it can carry
  scripts. Downloads stay attachments with `nosniff`; thumbnails use `<img>`, which never runs scripts.
- **`agenticmind.core.image_input@1.0.0`:** the run input `<key>` holds an uploaded image's file ID (or an
  image reference). It has an optional default image, and checks that the file really is an image.
- **`agenticmind.data.image_loader@1.0.0`:** selected images become `image` (the first) and `images` (a
  JSON list).

### Vision models
- **`agenticmind.models.llm@1.2.0`** adds optional `image` (Image) and `images` (JSON list) inputs.
  - It needs the `artifacts` permission to read the files, so it is a new version. 1.1.0 stays unchanged
    for existing graphs.
  - Images are de-duplicated; at most 16 images and 20 MB each.
  - Formats vision APIs accept: PNG, JPEG, WebP and GIF. BMP and TIFF get `IMAGE_FORMAT_UNSUPPORTED`,
    pointing to OCR.
- **Providers:** `ChatMessage.images: list[ImagePart]` is sent as OpenAI content parts: `text` plus
  `image_url` with a base64 data URL. The mock provider reports how many images it saw, for offline tests.

### Ollama and Hugging Face
These are new chat providers on the OpenAI-compatible client.
- **`ollama`:** default `http://localhost:11434/v1`, no key. Sends `max_tokens`. A connection error says
  to start `ollama serve`.
- **`huggingface`:** Inference Providers at `https://router.huggingface.co/v1`, with a token (`HF_TOKEN`).
  Sends `max_tokens`. Model ids may end in `:fastest`, `:cheapest` or `:<provider>`.
- **Docs check:** both were verified against their docs in Nov 2026. Self-hosted TGI, vLLM or Hugging Face
  Inference Endpoints use `openai_compatible` with a base URL.
- **One provider list:** `ChatProviderName` and `check_chat_provider` are shared by every model-using node
  (LLM, Model Router, Agent, Planner, Router (LLM mode), RAG LLM helpers, NL-to-SQL) and the
  Evaluation judge. Adding enum options to those configs is a backward-compatible change, so existing node
  versions keep their numbers.
- **AI assistant settings** list the new providers. `huggingface` needs a key secret; `ollama` does not.

### OCR
- **Engine:** RapidOCR (PaddleOCR models on ONNX Runtime, Apache-2.0) via the
  `agenticmind-nodes-data[ocr]` extra, which the API installs.
  - It runs locally on CPU. Models load on first use; `rapidocr download_models` pre-fetches them for
    offline machines.
  - Verified against RapidOCR 3.x docs (Nov 2026).
  - Lines below a confidence threshold are dropped and the rest kept in reading order: rows by position,
    then left to right.
- **`agenticmind.data.ocr@1.0.0`:** images (inputs or files) and scanned PDFs (every page) become `text`
  and one document per image or page, with OCR metadata (engine, lines, confidence).
- **Document Loader `ocr: auto` (default):** image files are OCR'd, and PDF pages without a text layer are
  filled from their embedded page images (read with pypdf). Text PDFs never run OCR. `off` keeps the old
  behaviour, and the error message now explains how to turn OCR on.

### Web
- **Run inputs:** an image picker (upload or choose an uploaded image) for each Image Input.
- **Chat:** an attach-image button when the graph has an Image Input; the thumbnail shows in the message.
- **Outputs:** image references render as thumbnails.
- **Port colour** for `Image`.
- **New template:** "Describe an image (vision)" (Image Input → vision LLM and OCR).

## Verification
- **Unit tests:**
  - providers: content parts; `max_tokens` for Ollama and Hugging Face; default URLs; a missing HF token;
    the Ollama connect hint;
  - Image Input: run input, default, non-image refused;
  - LLM 1.2.0: images reach the provider as base64, duplicates are removed, unsupported formats and too
    many images are refused;
  - Image Loader; OCR reading order and confidence; the OCR node on images and scanned PDFs (a fake
    engine);
  - Document Loader OCR for images, scanned pages and joined pages; text PDFs skip OCR; `ocr: off`.
- **Opt-in live OCR test** (`AGENTICMIND_TEST_LIVE_OCR=1`): real RapidOCR read "Invoice 4821 / Total
  due: 129.50 USD" from a rendered image at confidence 1.0.

## Consequences
- The API environment grows by OpenCV (headless), RapidOCR and Pillow (about 75 MB). OCR is CPU-bound and
  runs in a worker thread.
- No image resizing: very large photos should be downscaled before upload. No hand-writing models, and
  RapidOCR's default models cover Chinese and English. Other languages need other models, which is
  future work.
- Exported standalone apps do not yet accept image uploads.

## Revisit when
Image generation, audio, other OCR engines (Tesseract, docTR) or languages, or images in exported apps are
needed.
