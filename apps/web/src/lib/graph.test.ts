import { describe, expect, it } from "vitest";

import type { NodeManifest, ProjectDocument } from "../api/types";
import {
  checkConnection,
  dataIn,
  dataOut,
  docToFlow,
  flowToDoc,
  isCompatible,
  manifestKey,
  parseHandle,
  uniqueNodeId,
} from "./graph";
import { fieldsFromSchema } from "./schemaForm";

const doc: ProjectDocument = {
  ir_version: "1.0",
  project: { id: "p", name: "P", description: "" },
  runtime: { engine: "agenticmind", profile: "development" },
  nodes: [
    { id: "in", type: "agenticmind.core.manual_input", version: "1.0.0", config: { key: "q" }, ui: { position: { x: 1, y: 2 } } },
    { id: "out", type: "agenticmind.core.output", version: "1.0.0", config: {}, ui: { position: { x: 300, y: 2 }, label: "Answer" } },
  ],
  edges: [
    { id: "e1", kind: "data", source: { node: "in", port: "value" }, target: { node: "out", port: "value" } },
    { id: "c1", kind: "control", source: { node: "in", port: "done" }, target: { node: "out", port: "trigger" } },
  ],
  metadata: {},
};

const port = (name: string, type: string) => ({ name, type, description: "", type_from_config: null });
const manifest = (type: string, inputs: NodeManifest["inputs"], outputs: NodeManifest["outputs"]): NodeManifest => ({
  type, version: "1.0.0", display_name: type, category: "t", package: "p", description: "", inputs, outputs,
  branches: ["done"], config_schema: {}, permissions: {}, execution: { timeout_seconds: 30, max_attempts: 1, idempotent: true },
  modes: [], tags: [], trust: "core", source: "x",
});

describe("graph conversions", () => {
  it("round-trips IR through the canvas model", () => {
    const { nodes, edges } = docToFlow(doc);
    expect(nodes.map((n) => n.position)).toEqual([{ x: 1, y: 2 }, { x: 300, y: 2 }]);
    const back = flowToDoc(doc, { name: "P", description: "" }, nodes, edges);
    expect(back.edges).toEqual(doc.edges);
    expect(back.nodes).toEqual(doc.nodes);
  });

  it("parses handles", () => {
    expect(parseHandle("ctrl-in")).toEqual({ kind: "control", dir: "in", port: "trigger" });
    expect(parseHandle("ctrl-out:true")).toEqual({ kind: "control", dir: "out", port: "true" });
    expect(parseHandle("out:text")).toEqual({ kind: "data", dir: "out", port: "text" });
    expect(parseHandle("bogus")).toBeNull();
  });

  it("mirrors backend type compatibility", () => {
    expect(isCompatible("String", "Any")).toBe(true);
    expect(isCompatible("Number", "JSON")).toBe(true);
    expect(isCompatible("JSON", "String")).toBe(false);
    expect(isCompatible("Any", "String")).toBe(false);
  });

  it("generates unique node ids", () => {
    expect(uniqueNodeId("agenticmind.core.output", ["output_1"])).toBe("output_2");
  });

  it("checks connections like the validator", () => {
    const { nodes } = docToFlow({
      ...doc,
      nodes: [
        { id: "j", type: "t.json", version: "1.0.0", config: {} },
        { id: "s", type: "t.str", version: "1.0.0", config: {} },
      ],
    });
    const manifests = new Map([
      [manifestKey("t.json", "1.0.0"), manifest("t.json", [], [port("v", "JSON")])],
      [manifestKey("t.str", "1.0.0"), manifest("t.str", [{ ...port("text", "String"), required: true, cardinality: "single" }], [])],
    ]);
    const res = checkConnection(
      { source: "j", target: "s", sourceHandle: dataOut("v"), targetHandle: dataIn("text") },
      nodes, [], manifests,
    );
    expect(res).toEqual({ ok: false, reason: "JSON → String needs a conversion node" });
  });
});

describe("dynamic branches", () => {
  it("mirrors effective_branches for routers", async () => {
    const { effectiveBranches } = await import("./graph");
    const router = manifest("agenticmind.core.router", [], []);
    router.branches = ["default"];
    router.branches_from_config = "routes";
    router.config_schema = { properties: { routes: { default: [{ name: "billing" }, { name: "technical" }] } } };
    expect(effectiveBranches(router, {})).toEqual(["billing", "technical", "default"]);
    expect(effectiveBranches(router, { routes: [{ name: "sales" }, { name: "Bad Name" }, "support"] })).toEqual([
      "sales", "support", "default",
    ]);
    expect(effectiveBranches(manifest("x.y.z", [], []), {})).toEqual(["done"]);
  });
});

describe("schema form", () => {
  it("derives fields from pydantic schemas", () => {
    const fields = fieldsFromSchema({
      $defs: { JsonValue: {}, SecretRef: { type: "object" } },
      properties: {
        provider: { const: "mock", default: "mock", type: "string" },
        temperature: { type: "number", minimum: 0, maximum: 2, "x-ui": { advanced: true } },
        default: { $ref: "#/$defs/JsonValue", default: null },
        api_key: { anyOf: [{ $ref: "#/$defs/SecretRef" }, { type: "null" }], "x-secret": true },
        template: { type: "string", "x-ui": { widget: "textarea" } },
      },
    });
    expect(fields.map((f) => f.kind)).toEqual(["enum", "number", "json", "secret", "textarea"]);
    expect(fields[1]!.advanced).toBe(true);
    expect(fields[2]!.title).toBe("Default");
  });
});
