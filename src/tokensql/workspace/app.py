"""Same-origin quote grid and OpenCode chat application.

Run with ``python -m uvicorn tokensql.workspace.app:app --host 127.0.0.1 --port 8765``.
"""
from contextlib import asynccontextmanager
import asyncio
from pathlib import Path
import sys
from typing import Any

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from tokensql import __version__
from .agent import AgentConfig, OpenCodeAgentManager
from .agent_routes import create_agent_router
from .config import PROJECT_ROOT, WorkspaceSettings
from .data_routes import create_data_router
from .store import WorkspaceStore
from .fx_routes import create_fx_router, refresh_loop


STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app(
    settings: WorkspaceSettings | None = None,
    *,
    store: Any | None = None,
    agent_manager: Any | None = None,
) -> FastAPI:
    settings = settings or WorkspaceSettings()
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    store = store or WorkspaceStore(settings.database_path)
    manager = agent_manager or OpenCodeAgentManager(
        config=AgentConfig(
            workspace_dir=settings.workspace_dir.resolve(),
            database_path=settings.database_path,
            agent_dir=settings.agent_dir.resolve(),
            input_dir=settings.input_dir.resolve(),
            progress_dir=settings.progress_dir.resolve(),
            data_dir=settings.data_dir.resolve(),
            official_sources_path=PROJECT_ROOT / "docs" / "official_price_sources.md",
            python_bin=Path(sys.executable),
            server_url=settings.opencode_url,
            provider_id=settings.opencode_provider,
            model_id=settings.opencode_model,
            timeout_seconds=settings.opencode_timeout_seconds,
            max_file_bytes=settings.workspace_max_file_mb * 1024 * 1024,
            max_total_bytes=settings.workspace_max_total_mb * 1024 * 1024,
            max_files=settings.workspace_max_files,
        ),
        store=store,
    )

    @asynccontextmanager
    async def lifespan(api: FastAPI):
        refresh_task = None
        try:
            recover = getattr(manager, "recover", None)
            if recover:
                await recover()
            if settings.fx_auto_refresh:
                refresh_task = asyncio.create_task(refresh_loop(store, settings.fx_refresh_seconds))
            yield
        finally:
            if refresh_task:
                refresh_task.cancel()
                await asyncio.gather(refresh_task, return_exceptions=True)
            await manager.shutdown()
            store.close()

    api = FastAPI(title="TokenSQL Workspace", version=__version__, lifespan=lifespan)
    api.state.workspace_store = store
    api.state.agent_manager = manager
    api.include_router(create_data_router(store))
    api.include_router(create_fx_router(store))
    api.include_router(create_agent_router(manager))
    api.mount("/static", StaticFiles(directory=STATIC_DIR), name="workspace-static")

    @api.get("/", include_in_schema=False)
    @api.get("/workspace", include_in_schema=False)
    def workspace_page():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @api.get("/health")
    def health():
        result = {"status": "ok", "application": "tokensql-workspace"}
        if settings.test_mode:
            result["isolated_test"] = True
        return result

    return api


def __getattr__(name: str):
    # Importing the app factory in tests or runtime must not open a second DB.
    if name == "app":
        instance = create_app()
        globals()[name] = instance
        return instance
    raise AttributeError(name)
