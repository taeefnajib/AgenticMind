import { describe, expect, it } from "vitest";

import { isCompatible, PORT_TYPES } from "../lib/graph";
import { isImageFile, isImageRef } from "./ImagePicker";

describe("images", () => {
  it("recognises image references and image files", () => {
    expect(isImageRef({ kind: "image", artifact_id: "art_1", media_type: "image/png" })).toBe(true);
    expect(isImageRef({ kind: "document", artifact_id: "art_1" })).toBe(false);
    expect(isImageRef("art_1")).toBe(false);
    expect(isImageFile({ media_type: "image/jpeg" })).toBe(true);
    expect(isImageFile({ media_type: "image/svg+xml" })).toBe(false);
    expect(isImageFile({ media_type: "application/pdf" })).toBe(false);
  });

  it("treats Image as its own port type (mirrors the backend)", () => {
    expect(PORT_TYPES.has("Image")).toBe(true);
    expect(isCompatible("Image", "Image")).toBe(true);
    expect(isCompatible("Image", "Any")).toBe(true);
    expect(isCompatible("Image", "JSON")).toBe(false);
    expect(isCompatible("String", "Image")).toBe(false);
  });
});
