from pathlib import Path

from fastapi.testclient import TestClient

from orch.api import create_app
from orch.config import Config
from orch.db import TaskStore
from orch.logging_utils import task_log_dir
from orch.result import write_result_json
from orch.models import Risk, TaskStatus


def make_client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(Config(runtime_root=tmp_path)))


def test_health(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_create_list_and_show_task(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    created = client.post(
        "/tasks",
        json={"repo": str(tmp_path), "task": "READMEを更新して", "risk": "read_only", "priority": "high"},
    )

    assert created.status_code == 201
    payload = created.json()["task"]
    assert payload["id"] == "task-0001"
    assert payload["risk"] == "read_only"
    assert payload["priority"] == "high"

    listed = client.get("/tasks")
    assert listed.status_code == 200
    assert len(listed.json()["tasks"]) == 1

    shown = client.get("/tasks/task-0001")
    assert shown.status_code == 200
    assert shown.json()["task"]["task"] == "READMEを更新して"


def test_missing_task(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get("/tasks/task-9999")

    assert response.status_code == 404


def test_missing_log_diff_and_result_are_neutral_404(tmp_path: Path) -> None:
    store = TaskStore(Config(runtime_root=tmp_path))
    store.add_task(tmp_path, "task", risk=Risk.NORMAL)
    client = make_client(tmp_path)

    for path in [
        "/tasks/task-0001/logs/agent",
        "/tasks/task-0001/diff",
        "/tasks/task-0001/result",
    ]:
        response = client.get(path)
        assert response.status_code == 404
        assert response.json()["detail"]["message"] == "not available yet"


def test_reads_log_diff_and_result(tmp_path: Path) -> None:
    store = TaskStore(Config(runtime_root=tmp_path))
    task = store.add_task(tmp_path, "task", risk=Risk.NORMAL)
    log_dir = task_log_dir(Config(runtime_root=tmp_path), task.id)
    (log_dir / "agent.log").write_text("agent output\n", encoding="utf-8")
    (log_dir / "diff.patch").write_text("diff --git\n", encoding="utf-8")
    write_result_json(Config(runtime_root=tmp_path), task, TaskStatus.NEEDS_REVIEW, "done")
    client = make_client(tmp_path)

    assert client.get("/tasks/task-0001/logs/agent").json()["content"] == "agent output\n"
    assert client.get("/tasks/task-0001/diff").json()["content"] == "diff --git\n"
    assert client.get("/tasks/task-0001/result").json()["result"]["summary"] == "done"


def test_process_one_with_empty_queue(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.post("/daemon/process-one")

    assert response.status_code == 200
    assert response.json() == {"processed": False}


def test_task_payload_carries_engine_kind_and_cost(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    created = client.post(
        "/tasks",
        json={
            "repo": str(tmp_path),
            "task": "レビューして",
            "kind": "review",
            "engine": "grok",
            "risk": "read_only",
        },
    )

    assert created.status_code == 201
    payload = created.json()["task"]
    assert payload["kind"] == "review"
    assert payload["engine"] == "grok"
    assert payload["cost_usd"] is None

    store = TaskStore(Config(runtime_root=tmp_path))
    store.update_task("task-0001", cost_usd=0.125, exit_code=0)

    assert client.get("/tasks/task-0001").json()["task"]["cost_usd"] == 0.125


def test_engines_endpoint_reports_the_routing_table(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.get("/engines")

    assert response.status_code == 200
    routing = {entry["kind"]: entry["engine"] for entry in response.json()["routing"]}
    assert routing["implement"] == "codex"
    assert routing["review"] == "grok"


def test_cancel_marks_the_task_blocked(tmp_path: Path) -> None:
    client = make_client(tmp_path)
    client.post("/tasks", json={"repo": str(tmp_path), "task": "work"})

    response = client.post("/tasks/task-0001/cancel")

    assert response.status_code == 200
    assert response.json()["task"]["status"] == "blocked"


def test_cancelling_an_unknown_task_is_404(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    assert client.post("/tasks/task-9999/cancel").status_code == 404


def test_rejects_an_unknown_kind(tmp_path: Path) -> None:
    client = make_client(tmp_path)

    response = client.post(
        "/tasks", json={"repo": str(tmp_path), "task": "work", "kind": "telepathy"}
    )

    assert response.status_code == 422
