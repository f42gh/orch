import { assertEquals, assertRejects } from "jsr:@std/assert";
import { api, ApiError } from "./api.ts";

Deno.test("api client returns task list", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (() =>
    Promise.resolve(new Response(JSON.stringify({ tasks: [] }), { status: 200 }))) as typeof fetch;
  try {
    assertEquals(await api.listTasks(), []);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

Deno.test("api client raises ApiError with server detail", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (() =>
    Promise.resolve(
      new Response(JSON.stringify({ detail: { message: "not available yet" } }), { status: 404 }),
    )) as typeof fetch;
  try {
    const error = await assertRejects(() => api.getDiff("task-0001"), ApiError);
    assertEquals(error.status, 404);
    assertEquals(error.message, "not available yet");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

Deno.test("createTask sends kind and engine", async () => {
  const originalFetch = globalThis.fetch;
  let sentBody = "";
  globalThis.fetch = ((_url: string, init?: RequestInit) => {
    sentBody = String(init?.body ?? "");
    return Promise.resolve(
      new Response(JSON.stringify({ task: { id: "task-0001" } }), { status: 201 }),
    );
  }) as typeof fetch;
  try {
    await api.createTask({
      repo: "/repo",
      task: "review it",
      risk: "read_only",
      priority: "normal",
      kind: "review",
      engine: "grok",
    });
    const parsed = JSON.parse(sentBody);
    assertEquals(parsed.kind, "review");
    assertEquals(parsed.engine, "grok");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

Deno.test("listEngines returns capabilities and routing", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = (() =>
    Promise.resolve(
      new Response(
        JSON.stringify({
          engines: [{
            engine: "codex",
            path: "/usr/bin/codex",
            version: "0.147.0",
            structured_output: true,
            reports_cost: false,
            notes: [],
          }],
          routing: [{ kind: "implement", engine: "codex", fallbacks: [], writes: true }],
        }),
        { status: 200 },
      ),
    )) as typeof fetch;
  try {
    const payload = await api.listEngines();
    assertEquals(payload.engines[0].engine, "codex");
    assertEquals(payload.engines[0].reports_cost, false);
    assertEquals(payload.routing[0].kind, "implement");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
