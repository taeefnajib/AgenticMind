import { describe, expect, it } from "vitest";

import { isDocument, isDocumentList } from "../components/DocumentViewer";
import { formatBytes } from "../components/Files";
import { fieldsFromSchema } from "./schemaForm";

describe("document detection", () => {
  const doc = { id: "a#c0", content: "hello", source: { uri: "artifact://a/x.md" } };

  it("recognises document values and lists", () => {
    expect(isDocument(doc)).toBe(true);
    expect(isDocumentList([doc, { ...doc, id: "a#c1" }])).toBe(true);
    expect(isDocumentList([])).toBe(false);
    expect(isDocumentList([doc, { id: "x" }])).toBe(false);
    expect(isDocument("text")).toBe(false);
  });
});

describe("file helpers", () => {
  it("formats sizes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });

  it("maps the artifacts widget to a file picker field", () => {
    const [field] = fieldsFromSchema({
      properties: { files: { type: "array", "x-ui": { widget: "artifacts" } } },
    });
    expect(field!.kind).toBe("artifacts");
  });
});
