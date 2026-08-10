/// <reference types="vite/client" />

import React, { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  AlertCircle,
  FileText,
  ListRestart,
  Play,
  Plus,
  RefreshCw,
  TerminalSquare,
} from "lucide-react";
import { api, ApiError, Priority, Risk, Task } from "./api.ts";
import "./styles.css";

type ArtifactState = {
  agentLog: string;
  stdoutLog: string;
  stderrLog: string;
  diff: string;
  result: string;
};

const emptyArtifacts: ArtifactState = {
  agentLog: "",
  stdoutLog: "",
  stderrLog: "",
  diff: "",
  result: "",
};

const risks: Risk[] = ["read_only", "normal", "high"];
const priorities: Priority[] = ["low", "normal", "high"];

function App() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [repo, setRepo] = useState("");
  const [taskText, setTaskText] = useState("");
  const [risk, setRisk] = useState<Risk>("normal");
  const [priority, setPriority] = useState<Priority>("normal");
  const [artifacts, setArtifacts] = useState<ArtifactState>(emptyArtifacts);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [isBusy, setIsBusy] = useState(false);

  const selectedTask = useMemo(
    () => tasks.find((task) => task.id === selectedId) ?? tasks[0] ?? null,
    [selectedId, tasks],
  );

  const loadTasks = useCallback(async () => {
    try {
      const nextTasks = await api.listTasks();
      setTasks(nextTasks);
      setSelectedId((current) => current ?? nextTasks[0]?.id ?? null);
      setError("");
    } catch (caught) {
      setError(messageFromError(caught));
    }
  }, []);

  const loadArtifacts = useCallback(async (taskId: string) => {
    const [agentLog, stdoutLog, stderrLog, diff, result] = await Promise.all([
      optionalText(() => api.getLog(taskId, "agent")),
      optionalText(() => api.getLog(taskId, "stdout")),
      optionalText(() => api.getLog(taskId, "stderr")),
      optionalText(() => api.getDiff(taskId)),
      optionalResult(() => api.getResult(taskId)),
    ]);
    setArtifacts({ agentLog, stdoutLog, stderrLog, diff, result });
  }, []);

  useEffect(() => {
    void loadTasks();
    const interval = window.setInterval(() => {
      void loadTasks();
    }, 4000);
    return () => window.clearInterval(interval);
  }, [loadTasks]);

  useEffect(() => {
    if (selectedTask) {
      void loadArtifacts(selectedTask.id);
    } else {
      setArtifacts(emptyArtifacts);
    }
  }, [loadArtifacts, selectedTask]);

  async function submitTask(event: FormEvent) {
    event.preventDefault();
    const trimmedRepo = repo.trim();
    const trimmedTask = taskText.trim();
    if (!trimmedRepo || !trimmedTask) {
      setError("Repository path and task are required.");
      return;
    }
    setIsBusy(true);
    try {
      const created = await api.createTask({
        repo: trimmedRepo,
        task: trimmedTask,
        risk,
        priority,
      });
      setRepo("");
      setTaskText("");
      setSelectedId(created.id);
      setNotice(`Added ${created.id}`);
      await loadTasks();
    } catch (caught) {
      setError(messageFromError(caught));
    } finally {
      setIsBusy(false);
    }
  }

  async function processOne() {
    setIsBusy(true);
    try {
      const response = await api.processOne();
      setNotice(response.processed ? "Processed one queued task." : "No queued task to process.");
      await loadTasks();
      if (selectedTask) {
        await loadArtifacts(selectedTask.id);
      }
    } catch (caught) {
      setError(messageFromError(caught));
    } finally {
      setIsBusy(false);
    }
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <h1>Agent Orchestrator</h1>
          <p>Local task queue for isolated Claude workers</p>
        </div>
        <div className="topbar-actions">
          <button className="icon-button" title="Refresh tasks" onClick={() => void loadTasks()}>
            <RefreshCw size={18} />
          </button>
          <button className="primary-button" onClick={() => void processOne()} disabled={isBusy}>
            <Play size={17} />
            Process one
          </button>
        </div>
      </header>

      {(notice || error) && (
        <section className={error ? "banner error" : "banner"} aria-live="polite">
          {error ? <AlertCircle size={18} /> : <Activity size={18} />}
          <span>{error || notice}</span>
        </section>
      )}

      <section className="workspace">
        <aside className="sidebar">
          <form className="task-form" onSubmit={(event) => void submitTask(event)}>
            <div className="section-title">
              <Plus size={17} />
              <h2>Add task</h2>
            </div>
            <label>
              Repository
              <input
                value={repo}
                onChange={(event) => setRepo(event.target.value)}
                placeholder="/Users/me/dev/project"
              />
            </label>
            <label>
              Task
              <textarea
                value={taskText}
                onChange={(event) => setTaskText(event.target.value)}
                placeholder="READMEのセットアップ手順を更新して"
              />
            </label>
            <div className="field-row">
              <label>
                Risk
                <select value={risk} onChange={(event) => setRisk(event.target.value as Risk)}>
                  {risks.map((item) => <option key={item}>{item}</option>)}
                </select>
              </label>
              <label>
                Priority
                <select
                  value={priority}
                  onChange={(event) => setPriority(event.target.value as Priority)}
                >
                  {priorities.map((item) => <option key={item}>{item}</option>)}
                </select>
              </label>
            </div>
            <button className="primary-button full-width" disabled={isBusy}>
              <Plus size={17} />
              Add task
            </button>
          </form>

          <div className="task-list">
            <div className="section-title">
              <ListRestart size={17} />
              <h2>Tasks</h2>
            </div>
            {tasks.length === 0 ? (
              <p className="empty-state">No tasks yet</p>
            ) : (
              tasks.map((task) => (
                <button
                  key={task.id}
                  className={task.id === selectedTask?.id ? "task-row active" : "task-row"}
                  onClick={() => setSelectedId(task.id)}
                >
                  <span className={`status-dot ${task.status}`} />
                  <span>
                    <strong>{task.id}</strong>
                    <small>{task.task}</small>
                  </span>
                </button>
              ))
            )}
          </div>
        </aside>

        <section className="detail-pane">
          {selectedTask ? (
            <TaskDetail task={selectedTask} artifacts={artifacts} />
          ) : (
            <div className="empty-detail">
              <FileText size={28} />
              <p>Select or add a task.</p>
            </div>
          )}
        </section>
      </section>
    </main>
  );
}

function TaskDetail({ task, artifacts }: { task: Task; artifacts: ArtifactState }) {
  return (
    <div className="detail-stack">
      <section className="detail-header">
        <div>
          <h2>{task.id}</h2>
          <p>{task.task}</p>
        </div>
        <span className={`status-pill ${task.status}`}>{task.status}</span>
      </section>

      {task.error && (
        <section className="error-box">
          <AlertCircle size={18} />
          <span>{task.error}</span>
        </section>
      )}

      <section className="meta-grid">
        <Meta label="Repo" value={task.repo_path} />
        <Meta label="Workspace" value={task.workspace_path ?? "Not created yet"} />
        <Meta label="Branch" value={task.branch_name ?? "Not created yet"} />
        <Meta label="Risk" value={task.risk} />
        <Meta label="Priority" value={task.priority} />
        <Meta label="Updated" value={formatDate(task.updated_at)} />
      </section>

      <section className="summary-panel">
        <h3>Summary</h3>
        <p>{task.result_summary ?? "Not available yet"}</p>
      </section>

      <section className="artifact-grid">
        <ArtifactPanel title="Agent log" icon={<TerminalSquare size={17} />} value={artifacts.agentLog} />
        <ArtifactPanel title="Stdout" icon={<TerminalSquare size={17} />} value={artifacts.stdoutLog} />
        <ArtifactPanel title="Stderr" icon={<TerminalSquare size={17} />} value={artifacts.stderrLog} />
        <ArtifactPanel title="Diff" icon={<FileText size={17} />} value={artifacts.diff} />
        <ArtifactPanel title="Result JSON" icon={<FileText size={17} />} value={artifacts.result} />
      </section>
    </div>
  );
}

function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="meta-item">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function ArtifactPanel({ title, icon, value }: { title: string; icon: React.ReactNode; value: string }) {
  return (
    <section className="artifact-panel">
      <div className="section-title">
        {icon}
        <h3>{title}</h3>
      </div>
      <pre>{value || "Not available yet"}</pre>
    </section>
  );
}

async function optionalText(loader: () => Promise<{ content: string }>): Promise<string> {
  try {
    return (await loader()).content;
  } catch (caught) {
    if (caught instanceof ApiError && caught.status === 404) {
      return "";
    }
    throw caught;
  }
}

async function optionalResult(loader: () => Promise<{ result: unknown }>): Promise<string> {
  try {
    return JSON.stringify((await loader()).result, null, 2);
  } catch (caught) {
    if (caught instanceof ApiError && caught.status === 404) {
      return "";
    }
    throw caught;
  }
}

function messageFromError(caught: unknown): string {
  return caught instanceof Error ? caught.message : String(caught);
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
