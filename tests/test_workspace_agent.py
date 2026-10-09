from __future__ import annotations

import asyncio
from pathlib import Path
from threading import Event

import httpx

from tokensql.workspace.agent import (
    AgentConfig,
    AgentUpload,
    OpenCodeAgentManager,
)


class FakeStore:
    def __init__(self) -> None:
        self.actors: list[str] = []

    def change_summary(self, actor: str) -> dict[str, int]:
        self.actors.append(actor)
        return {"added": 2, "updated": 1, "deleted": 0, "total": 2}


def make_config(tmp_path: Path) -> AgentConfig:
    workspace = tmp_path / "workspace"
    return AgentConfig(
        workspace_dir=workspace,
        database_path=workspace / "quotes.sqlite3",
        python_bin=tmp_path / ".venv" / "bin" / "python",
    )


def test_agent_run_creates_session_preserves_history_and_uses_audit(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_test"})
        if request.method == "POST" and request.url.path.endswith("/message"):
            return httpx.Response(
                200,
                json={"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "已导入 2 条报价"}]},
            )
        if request.method == "GET" and request.url.path.endswith("/message"):
            return httpx.Response(
                200,
                json=[
                    {"info": {"role": "user"}, "parts": [{"type": "text", "text": "导入"}]},
                    {"info": {"role": "assistant"}, "parts": [
                        {"type": "tool", "tool": "bash", "state": {"status": "completed"}},
                        {"type": "text", "text": "已导入 2 条报价"},
                    ]},
                ],
            )
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async def scenario() -> None:
        store = FakeStore()
        manager = OpenCodeAgentManager(
            config=make_config(tmp_path),
            store=store,
            transport=httpx.MockTransport(handler),
        )
        run = await manager.submit(
            prompt="供应商是甲公司，默认币种 CNY",
            files=[
                AgentUpload(
                    filename="../模型 报价（十月）.xlsx",
                    content=b"workbook",
                )
            ],
        )
        result = await manager.wait(run.id)
        assert result.status == "completed"
        assert result.conversation_id == "ses_test"
        assert result.assistant_message == "已导入 2 条报价"
        assert result.changes == {
            "added": 2, "updated": 1, "deleted": 0, "total": 2
        }
        assert len(result.messages) == 3
        tool = next(item for item in result.messages if item["kind"] == "tool")
        assert tool == {"role": "tool", "kind": "tool", "name": "bash", "status": "completed", "text": "bash"}
        assert "output" not in tool
        assert store.actors == [f"opencode:{run.id}"]
        saved = list((tmp_path / "workspace" / "inbox" / run.id).iterdir())
        assert [item.name for item in saved] == ["模型 报价（十月）.xlsx"]
        prompt_request = next(
            item for item in requests
            if item.method == "POST" and item.url.path.endswith("/message")
        )
        body = __import__("json").loads(prompt_request.content)
        assert f"--actor opencode:{run.id}" in body["system"]
        assert str(saved[0]) in body["parts"][0]["text"]

    asyncio.run(scenario())


def test_agent_reuses_conversation_without_creating_session(tmp_path: Path) -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.method == "POST":
            return httpx.Response(200, json={"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "完成"}]})
        return httpx.Response(200, json=[])

    async def scenario() -> None:
        manager = OpenCodeAgentManager(
            config=make_config(tmp_path),
            store=FakeStore(),
            transport=httpx.MockTransport(handler),
        )
        run = await manager.submit(prompt="列出当前报价", conversation_id="ses_existing")
        result = await manager.wait(run.id)
        assert result.status == "completed"
        assert "/session" not in paths

    asyncio.run(scenario())


def test_agent_status_reports_unavailable_and_invalid_input(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    async def scenario() -> None:
        manager = OpenCodeAgentManager(
            config=make_config(tmp_path),
            store=FakeStore(),
            transport=httpx.MockTransport(handler),
        )
        assert await manager.status() == {
            "available": False,
            "model": "qwen38-flash-next-local/qwen3.8-flash-next-iq3_s",
            "status": "unavailable",
        }
        try:
            await manager.submit(prompt=" ", conversation_id="bad")
        except ValueError as error:
            assert "blank" in str(error)
        else:
            raise AssertionError("blank prompt should fail")

    asyncio.run(scenario())


def test_agent_router_accepts_multipart_and_exposes_runs(tmp_path: Path) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tokensql.workspace.agent import AgentRun
    from tokensql.workspace.agent_routes import create_agent_router

    class FakeManager:
        config = make_config(tmp_path)
        submitted: tuple[str, list[AgentUpload], str | None] | None = None
        run = AgentRun(id="run1", conversation_id="ses_router")

        async def submit(self, *, prompt, files, conversation_id):
            self.submitted = (prompt, list(files), conversation_id)
            return self.run

        def get_run(self, run_id):
            if run_id != "run1":
                raise KeyError(run_id)
            return self.run

        async def status(self):
            return {"available": True, "model": "provider/model", "status": "ready"}

    manager = FakeManager()
    api = FastAPI()
    api.include_router(create_agent_router(manager))
    with TestClient(api) as client:
        response = client.post(
            "/api/workspace/agent/messages",
            data={"prompt": "读取报价", "conversation_id": "ses_router"},
            files={"files": ("价格 表.csv", b"model,price\na,1\n", "text/csv")},
        )
        assert response.status_code == 202
        assert response.json()["run_id"] == "run1"
        assert manager.submitted is not None
        assert manager.submitted[1][0].filename == "价格 表.csv"
        assert client.get("/api/workspace/agent/runs/run1").status_code == 200
        assert client.get("/api/workspace/agent/runs/missing").status_code == 404
        assert client.get("/api/workspace/agent/status").json()["available"] is True


def test_http_200_model_error_marks_run_failed_and_keeps_audit_counts(tmp_path: Path) -> None:
    store = FakeStore()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_error"})
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "info": {
                        "role": "assistant",
                        "error": {"name": "APIError", "data": {"message": "private"}},
                    },
                    "parts": [],
                },
            )
        raise AssertionError("history must not be requested after a model error")

    async def scenario() -> None:
        manager = OpenCodeAgentManager(
            config=make_config(tmp_path),
            store=store,
            transport=httpx.MockTransport(handler),
        )
        run = await manager.submit(prompt="导入报价")
        result = await manager.wait(run.id)
        assert result.status == "failed"
        assert result.error == "OpenCode model response failed (APIError)"
        assert result.changes["added"] == 2
        assert store.actors == [f"opencode:{run.id}"]

    asyncio.run(scenario())


def test_agent_config_preserves_virtualenv_symlink_path(tmp_path: Path) -> None:
    import sys

    python_bin = tmp_path / ".venv" / "bin" / "python"
    python_bin.parent.mkdir(parents=True)
    python_bin.symlink_to(sys.executable)
    workspace = tmp_path / "workspace"
    config = AgentConfig(
        workspace_dir=workspace,
        database_path=workspace / "quotes.sqlite3",
        python_bin=python_bin,
    )
    assert config.python_bin == python_bin.absolute()


def test_upload_write_failure_does_not_leave_a_queued_run(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"healthy": True})

    async def scenario() -> None:
        manager = OpenCodeAgentManager(
            config=make_config(tmp_path),
            store=FakeStore(),
            transport=httpx.MockTransport(handler),
        )

        def fail_write(run_id, files):
            raise OSError("/private/workspace/path is not writable")

        manager._save_uploads = fail_write
        try:
            await manager.submit(
                prompt="导入",
                files=[AgentUpload(filename="quote.csv", content=b"a,b")],
            )
        except OSError:
            pass
        else:
            raise AssertionError("write failure should propagate")
        assert manager._runs == {}
        assert manager._tasks == {}
        assert (await manager.status())["status"] == "ready"

    asyncio.run(scenario())


def test_agent_router_maps_storage_error_without_leaking_path(tmp_path: Path) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tokensql.workspace.agent_routes import create_agent_router

    class FailingManager:
        config = make_config(tmp_path)

        async def submit(self, **kwargs):
            raise OSError("/private/workspace/path")

        def get_run(self, run_id):
            raise KeyError(run_id)

        async def status(self):
            return {"available": True, "model": "provider/model", "status": "ready"}

    api = FastAPI()
    api.include_router(create_agent_router(FailingManager()))
    with TestClient(api) as client:
        response = client.post(
            "/api/workspace/agent/messages", data={"prompt": "导入"}
        )
    assert response.status_code == 503
    assert response.json() == {"detail": "uploaded files could not be stored"}
    assert "/private" not in response.text


def test_agent_router_enforces_total_upload_limit_while_reading(tmp_path: Path) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from tokensql.workspace.agent_routes import create_agent_router

    class LimitedManager:
        config = AgentConfig(
            workspace_dir=tmp_path / "workspace",
            database_path=tmp_path / "workspace" / "quotes.sqlite3",
            python_bin=tmp_path / "python",
            max_file_bytes=10,
            max_total_bytes=5,
            max_files=3,
        )

        async def submit(self, **kwargs):
            raise AssertionError("oversized uploads must not reach submit")

        def get_run(self, run_id):
            raise KeyError(run_id)

        async def status(self):
            return {"available": True, "model": "provider/model", "status": "ready"}

    api = FastAPI()
    api.include_router(create_agent_router(LimitedManager()))
    with TestClient(api) as client:
        response = client.post(
            "/api/workspace/agent/messages",
            data={"prompt": "导入"},
            files=[
                ("files", ("a.csv", b"abc", "text/csv")),
                ("files", ("b.csv", b"def", "text/csv")),
            ],
        )
    assert response.status_code == 413
    assert response.json()["detail"] == "uploads exceed the total size limit"


def test_manager_rejects_total_upload_size_before_writing(tmp_path: Path) -> None:
    async def scenario() -> None:
        workspace = tmp_path / "workspace"
        manager = OpenCodeAgentManager(
            config=AgentConfig(
                workspace_dir=workspace,
                database_path=workspace / "quotes.sqlite3",
                python_bin=tmp_path / "python",
                max_file_bytes=10,
                max_total_bytes=5,
            ),
            store=FakeStore(),
        )
        try:
            await manager.submit(
                prompt="导入",
                files=[
                    AgentUpload(filename="a.csv", content=b"abc"),
                    AgentUpload(filename="b.csv", content=b"def"),
                ],
            )
        except ValueError as error:
            assert "total size" in str(error)
        else:
            raise AssertionError("oversized uploads should fail")
        assert manager._runs == {}
        assert not list((workspace / "inbox").iterdir())

    asyncio.run(scenario())


def test_run_stays_running_until_audit_summary_is_ready(tmp_path: Path) -> None:
    entered = Event()
    release = Event()

    class SlowStore:
        def change_summary(self, actor: str) -> dict[str, int]:
            entered.set()
            if not release.wait(timeout=2):
                raise TimeoutError("test did not release audit summary")
            return {"added": 3, "updated": 1, "deleted": 0, "total": 4}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_slow_audit"})
        if request.method == "POST" and request.url.path.endswith("/message"):
            return httpx.Response(
                200,
                json={
                    "info": {"role": "assistant"},
                    "parts": [{"type": "text", "text": "完成"}],
                },
            )
        if request.method == "GET" and request.url.path.endswith("/message"):
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async def scenario() -> None:
        manager = OpenCodeAgentManager(
            config=make_config(tmp_path),
            store=SlowStore(),
            transport=httpx.MockTransport(handler),
        )
        run = await manager.submit(prompt="导入报价")
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            assert run.status == "running"
            assert run.changes == {
                "added": 0, "updated": 0, "deleted": 0, "total": 0
            }
        finally:
            release.set()
        result = await manager.wait(run.id)
        assert result.status == "completed"
        assert result.changes == {
            "added": 3, "updated": 1, "deleted": 0, "total": 4
        }

    asyncio.run(scenario())


def test_shutdown_cancellation_keeps_audit_changes_and_never_completes(
    tmp_path: Path,
) -> None:
    entered = Event()
    release = Event()

    class SlowStore:
        def change_summary(self, actor: str) -> dict[str, int]:
            entered.set()
            if not release.wait(timeout=2):
                raise TimeoutError("test did not release audit summary")
            return {"added": 1, "updated": 2, "deleted": 1, "total": 4}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/session":
            return httpx.Response(200, json={"id": "ses_cancel"})
        if request.method == "POST" and request.url.path.endswith("/message"):
            return httpx.Response(
                200,
                json={
                    "info": {"role": "assistant"},
                    "parts": [{"type": "text", "text": "已写入"}],
                },
            )
        if request.method == "GET" and request.url.path.endswith("/message"):
            return httpx.Response(200, json=[])
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    async def scenario() -> None:
        manager = OpenCodeAgentManager(
            config=make_config(tmp_path),
            store=SlowStore(),
            transport=httpx.MockTransport(handler),
        )
        run = await manager.submit(prompt="导入报价")
        assert await asyncio.to_thread(entered.wait, 2)
        shutdown = asyncio.create_task(manager.shutdown())
        await asyncio.sleep(0)
        assert run.status == "running"
        release.set()
        await shutdown
        assert run.status == "failed"
        assert run.error == "The workspace agent was cancelled"
        assert run.changes == {
            "added": 1, "updated": 2, "deleted": 1, "total": 4
        }

    asyncio.run(scenario())
