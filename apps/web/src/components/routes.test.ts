import { describe, expect, it } from "vitest";

import { asRoutes, nextRouteName, parseKeywords } from "./RoutesControl";
import { fieldsFromSchema } from "../lib/schemaForm";

describe("model router routes", () => {
  it("reads routes defensively and parses keywords", () => {
    expect(asRoutes(null)).toEqual([]);
    expect(asRoutes([{ name: "code", model: "m", keywords: ["a", 3] }, "junk"])).toEqual([
      { name: "code", description: "", keywords: ["a"], model: "m" },
    ]);
    expect(parseKeywords(" code, python ,, step by step ")).toEqual(["code", "python", "step by step"]);
  });

  it("suggests a free route name", () => {
    expect(nextRouteName([{ name: "route-2", model: "m" }])).toBe("route-3");
    expect(nextRouteName([{ name: "a", model: "m" }, { name: "route-3", model: "m" }])).toBe("route-4");
  });

  it("maps the router widgets to field kinds", () => {
    const fields = fieldsFromSchema({
      properties: {
        routes: { type: "array", "x-ui": { widget: "model_routes" } },
        default_route: { type: "string", "x-ui": { widget: "route_ref" } },
      },
    });
    expect(fields.map((f) => f.kind)).toEqual(["routes", "route_ref"]);
  });
});

describe("field titles", () => {
  it("names referenced types after the field, not the type", () => {
    const [f] = fieldsFromSchema({
      $defs: { SecretRef: { title: "SecretRef", type: "object" } },
      properties: { api_key: { anyOf: [{ $ref: "#/$defs/SecretRef" }, { type: "null" }], "x-secret": true } },
    });
    expect(f?.title).toBe("Api key");
  });
});
