import { describe, expect, it } from "vitest";

import { fieldsFromSchema } from "./schemaForm";

describe("fieldsFromSchema widgets", () => {
  it("maps ui widgets to field kinds", () => {
    const fields = fieldsFromSchema({
      type: "object",
      properties: {
        code: { type: "string", "x-ui": { widget: "code" } },
        notes: { type: "string", "x-ui": { widget: "textarea" } },
        name: { type: "string" },
      },
    } as never);
    expect(Object.fromEntries(fields.map((f) => [f.name, f.kind]))).toEqual({ code: "code", notes: "textarea", name: "string" });
  });
});
