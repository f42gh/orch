/// <reference types="vite/client" />

export type Risk = "read_only" | "normal" | "high";
export type Priority = "low" | "normal" | "high";
export type TaskStatus =
  | "queued"
  | "running"
  | "blocked"
  | "failed"
  | "succeeded"
  | "needs_review";

export type Task = {
  id: string;
  repo_path: string;
  workspace_path: string | null;
  branch_name: string | null;
  session_id: string | null;
  task: string;
  risk: Risk;
  priority: Priority;
  status: TaskStatus;
  created_at: string;
  updated_at: string;
  result_summary: string | null;
  error: string | null;
  log_path: string;
  diff_path: string;
  result_path: string;
};

export type CreateTaskInput = {
  repo: string;
  task: string;
  risk: Risk;
  priority: Priority;
};

export type Artifact = {
  path: string;
  content: string;
};

export type ResultArtifact = {
  path: string;
  result: unknown;
};

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const API_BASE = import.meta.env.VITE_AGENT_API_BASE ?? "http://127.0.0.1:8765";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
    ...init,
  });
  if (!response.ok) {
    let message = `Request failed with ${response.status}`;
    try {
      const body = await response.json();
      message = body.detail?.message ?? body.detail ?? message;
    } catch {
      // Keep the generic message when the server sends no JSON body.
    }
    throw new ApiError(String(message), response.status);
  }
  return await response.json() as T;
}

export const api = {
  async health(): Promise<{ status: string; runtime_root: string }> {
    return await request("/health");
  },

  async listTasks(): Promise<Task[]> {
    const payload = await request<{ tasks: Task[] }>("/tasks");
    return payload.tasks;
  },

  async createTask(input: CreateTaskInput): Promise<Task> {
    const payload = await request<{ task: Task }>("/tasks", {
      method: "POST",
      body: JSON.stringify(input),
    });
    return payload.task;
  },

  async getTask(taskId: string): Promise<Task> {
    const payload = await request<{ task: Task }>(`/tasks/${taskId}`);
    return payload.task;
  },

  async processOne(): Promise<{ processed: boolean }> {
    return await request("/daemon/process-one", { method: "POST" });
  },

  async getLog(taskId: string, kind: "agent" | "stdout" | "stderr"): Promise<Artifact> {
    return await request(`/tasks/${taskId}/logs/${kind}`);
  },

  async getDiff(taskId: string): Promise<Artifact> {
    return await request(`/tasks/${taskId}/diff`);
  },

  async getResult(taskId: string): Promise<ResultArtifact> {
    return await request(`/tasks/${taskId}/result`);
  },
};
