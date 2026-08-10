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
