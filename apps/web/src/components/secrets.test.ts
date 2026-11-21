import { describe, expect, it } from "vitest";

import { fieldsFromSchema, fieldVisible } from "../lib/schemaForm";
import { normaliseSecretName, SECRET_NAME } from "./SecretsSection";

describe("secrets", () => {
  it("nudges names into env-var style and validates them", () => {
    expect(normaliseSecretName(" supabase password")).toBe("SUPABASE_PASSWORD");
    // typed one key at a time: the space survives until the next word arrives
    let typed = "";
    for (const ch of "db pass") typed = normaliseSecretName(typed + ch);
    expect(typed).toBe("DB_PASS");
    expect(normaliseSecretName("hf-token!")).toBe("HF_TOKEN");
    expect(SECRET_NAME.test("HF_TOKEN")).toBe(true);
    expect(SECRET_NAME.test("1TOKEN")).toBe(false);
  });
});

describe("show_when", () => {
  const fields = fieldsFromSchema({
    properties: {
      engine: { type: "string", enum: ["sqlite", "snowflake", "databricks"], default: "sqlite" },
      warehouse: { type: "string", "x-ui": { show_when: { engine: ["snowflake", "databricks"] } } },
      file: { type: "array", "x-ui": { widget: "artifacts", show_when: { engine: ["sqlite"] } } },
    },
  });
  const [, warehouse, file] = fields;
  it("shows fields only for the chosen option, using defaults when unset", () => {
    expect(fieldVisible(warehouse!, {}, fields)).toBe(false);
    expect(fieldVisible(file!, {}, fields)).toBe(true);
    expect(fieldVisible(warehouse!, { engine: "databricks" }, fields)).toBe(true);
    expect(fieldVisible(file!, { engine: "databricks" }, fields)).toBe(false);
  });
});
