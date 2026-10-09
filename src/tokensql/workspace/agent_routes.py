"""FastAPI routes for asynchronous OpenCode workspace conversations."""
from __future__ import annotations

from typing import Any, Literal
from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from tokensql.workspace.agent import AgentUpload, OpenCodeAgentManager


class AgentChangesResponse(BaseModel):
    added: int = Field(ge=0)
    updated: int = Field(ge=0)
    deleted: int = Field(ge=0)
    total: int = Field(ge=0)


class AgentRunResponse(BaseModel):
    run_id: str
    conversation_id: str | None
    status: Literal["queued", "running", "completed", "failed"]
    assistant_message: str | None
    messages: list[dict[str, Any]]
    changes: AgentChangesResponse
    error: str | None


class AgentStatusResponse(BaseModel):
    available: bool
    model: str
    status: Literal["ready", "busy", "unavailable"]


async def _read_upload(
    upload: UploadFile, max_bytes: int, total_remaining: int
) -> AgentUpload:
    chunks: list[bytes] = []
    size = 0
    read_limit = min(max_bytes, max(0, total_remaining))
    try:
        while True:
            chunk = await upload.read(min(1024 * 1024, read_limit + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"{upload.filename or 'upload'} exceeds the file size limit",
                )
            if size > total_remaining:
                raise HTTPException(
                    status_code=413, detail="uploads exceed the total size limit"
                )
            chunks.append(chunk)
    finally:
        await upload.close()
    return AgentUpload(
        filename=upload.filename or "upload",
        content=b"".join(chunks),
        content_type=upload.content_type,
    )


def create_agent_router(manager: OpenCodeAgentManager) -> APIRouter:
    router = APIRouter(prefix="/api/workspace/agent", tags=["workspace-agent"])

    @router.post("/messages", response_model=AgentRunResponse, status_code=status.HTTP_202_ACCEPTED)
    async def submit_message(
        prompt: str = Form(...),
        conversation_id: str | None = Form(default=None),
        files: list[UploadFile] = File(default=[]),
    ) -> AgentRunResponse:
        if len(files) > manager.config.max_files:
            raise HTTPException(status_code=413, detail=f"at most {manager.config.max_files} files may be uploaded")
        uploads: list[AgentUpload] = []
        total_bytes = 0
        for position, item in enumerate(files):
            try:
                upload = await _read_upload(
                    item,
                    manager.config.max_file_bytes,
                    manager.config.max_total_bytes - total_bytes,
                )
            except Exception:
                for pending in files[position + 1:]:
                    await pending.close()
                raise
            uploads.append(upload)
            total_bytes += len(upload.content)
        try:
            run = await manager.submit(
                prompt=prompt, files=uploads, conversation_id=conversation_id
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from None
        except OSError:
            raise HTTPException(
                status_code=503, detail="uploaded files could not be stored"
            ) from None
        return AgentRunResponse.model_validate(run.public_dict())

    @router.get("/runs/{run_id}", response_model=AgentRunResponse)
    async def get_run(run_id: str) -> AgentRunResponse:
        try:
            run = manager.get_run(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="agent run was not found") from None
        return AgentRunResponse.model_validate(run.public_dict())

    @router.get("/status", response_model=AgentStatusResponse)
    async def agent_status() -> AgentStatusResponse:
        return AgentStatusResponse.model_validate(await manager.status())

    return router
