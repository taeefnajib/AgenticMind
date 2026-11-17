import { afterEach, describe, expect, it, vi } from "vitest";

import { api, ApiError, isConnectionError } from "./client";

const realFetch = globalThis.fetch;
afterEach(() => {
  globalThis.fetch = realFetch;
  vi.restoreAllMocks();
});

function mockFetch(impl: () => Promise<Response>) {
  globalThis.fetch = vi.fn(impl) as unknown as typeof fetch;
}

describe("api client failure handling", () => {
  it("turns a timeout into a connection error", async () => {
    mockFetch(() => Promise.reject(new DOMException("timed out", "TimeoutError")));
    const err = await api.nodes().catch((e) => e);
    expect(isConnectionError(err)).toBe(true);
    expect((err as Error).message).toMatch(/did not respond/);
  });

  it("treats proxy gateway errors as the API being unreachable", async () => {
    mockFetch(() => Promise.resolve(new Response("proxy error", { status: 502 })));
    expect(isConnectionError(await api.projects().catch((e) => e))).toBe(true);
    mockFetch(() => Promise.resolve(new Response("Internal Server Error", { status: 500 })));
    expect(isConnectionError(await api.projects().catch((e) => e))).toBe(true);
  });

  it("keeps real API errors distinct", async () => {
    mockFetch(() =>
      Promise.resolve(
        new Response(JSON.stringify({ detail: "Project 'x' not found" }), {
          status: 404,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    const err = await api.getProject("x").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(isConnectionError(err)).toBe(false);
    expect((err as ApiError).status).toBe(404);
    expect((err as Error).message).toBe("Project 'x' not found");
  });
});
