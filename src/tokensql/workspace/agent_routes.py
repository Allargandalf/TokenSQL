"""Same-origin agent, review, file and human-memory endpoints."""
from __future__ import annotations

import json
from typing import Any
from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .agent import AgentUpload, OpenCodeAgentManager
from .agent_routes_upload import read_uploads, _read_upload
from .store import RevisionConflictError, IdempotencyConflictError, QuoteNotFoundError

class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload_ids: list[str] = Field(default_factory=list)
    prompt: str
    conversation_id: str | None = None
    supplier_id: str | None = None
    update_request: dict[str, Any] | None = None
    source_scope: str | None = Field(default=None, min_length=1, max_length=255)
    persist_profile: bool = True
    remember_prompt: bool = False
    full_reparse: bool = False

class SupplierSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_scope: str = Field(default="default", min_length=1, max_length=255)
    new_round: bool = False


class OfficialLookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1, max_length=1000)
    billing_item: str | None = Field(default=None, max_length=1000)
    prompt: str = Field(default="", max_length=16000)

class ProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    bundle: dict[str, Any] | None = None
    reconciliation_request: dict[str, Any] | None = None
    decisions: list[dict[str, Any]] | None = None
    persist_profile: bool | None = None
    remember_prompt: bool | None = None

class DraftRequest(BaseModel):
    bundle: dict[str, Any]
    prompt: str = ""

class ConfirmRequest(BaseModel):
    expected_revision: int
    idempotency_key: str | None = None

class RespondRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    prompt: str = Field(min_length=1, max_length=16000)
    supplier_id: str | None = None
    update_request: dict[str, Any] | None = None
    persist_profile: bool | None = None
    remember_prompt: bool | None = None

    @field_validator("prompt")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("prompt must not be blank")
        return value.strip()


class MemoryRequest(BaseModel):
    expected_revision: int
    rules: list[dict[str, Any]]

def problem(error: Exception) -> HTTPException:
    if isinstance(error, (RevisionConflictError, IdempotencyConflictError)):
        return HTTPException(409, detail=str(error))
    if isinstance(error, OSError):
        return HTTPException(503, detail="File storage could not complete the operation")
    if isinstance(error, (KeyError, QuoteNotFoundError)):
        return HTTPException(404, detail=str(error).strip("'"))
    if isinstance(error, ValueError):
        return HTTPException(409 if "conflict" in str(error) else 422, detail=str(error))
    return HTTPException(503, detail=str(error)[:1000] or "Operation could not be completed")

def create_agent_router(manager: OpenCodeAgentManager) -> APIRouter:
    router = APIRouter(prefix="/api/workspace", tags=["workspace-review"])
    workflow = getattr(manager, "workflow", None)

    @router.post("/agent/messages", status_code=202)
    async def submit_message(prompt: str = Form(...), conversation_id: str | None = Form(None),
                             supplier_id: str | None = Form(None), update_request: str | None = Form(None),
                             source_scope: str | None = Form(None), persist_profile: bool = Form(True),
                             remember_prompt: bool = Form(False), full_reparse: bool = Form(False),
                             files: list[UploadFile] = File(default=[])):
        uploads = await read_uploads(files, manager.config)
        try:
            context = json.loads(update_request) if update_request else None
            if context is not None and not isinstance(context, dict):
                raise ValueError("update_request must be a JSON object")
            run = await manager.submit(prompt=prompt, files=uploads, conversation_id=conversation_id,
                                       supplier_id=supplier_id, update_request=context, source_scope=source_scope,
                                       persist_profile=persist_profile, remember_prompt=remember_prompt, full_reparse=full_reparse)
            return run.public_dict()
        except (ValueError, KeyError, OSError, RevisionConflictError) as error:
            raise problem(error) from None

    @router.post("/official-prices/lookup", status_code=202)
    async def lookup_official_prices(payload: OfficialLookupRequest):
        try:
            return await manager.lookup_official_prices(**payload.model_dump())
        except Exception as error:
            raise problem(error) from None

    @router.get("/agent/runs/{run_id}")
    async def run(run_id: str):
        try:
            return manager.get_run(run_id).public_dict()
        except KeyError as error:
            raise problem(error) from None

    @router.get("/agent/status")
    async def status():
        return await manager.status()

    @router.get("/agent/sessions")
    async def sessions():
        return {"items": await manager.sessions()}

    @router.post("/agent/suppliers/{supplier_id}/session")
    async def supplier_session(supplier_id: str, payload: SupplierSessionRequest):
        try:
            return await manager.supplier_session(supplier_id, **payload.model_dump())
        except Exception as error:
            raise problem(error) from None

    @router.get("/agent/sessions/{session_id}/messages")
    async def history(session_id: str):
        try:
            return {"conversation_id": session_id, "messages": await manager.history(session_id)}
        except Exception as error:
            raise problem(error) from None

    @router.get("/agent/memory")
    async def memory():
        return manager.memory.read()

    @router.patch("/agent/memory")
    async def update_memory(payload: MemoryRequest):
        try:
            return manager.memory.update(payload.rules, payload.expected_revision)
        except ValueError as error:
            raise problem(error) from None

    @router.get("/imports/files")
    async def files():
        return {"items": workflow.list_files()}

    @router.post("/imports/upload", status_code=201)
    async def upload(files: list[UploadFile] = File(...)):
        uploads = await read_uploads(files, manager.config)
        try:
            return {"items": workflow.upload(uploads)}
        except (ValueError, OSError) as error:
            raise problem(error) from None

    @router.post("/imports/start", status_code=202)
    async def start(payload: StartRequest):
        try:
            return await workflow.start(**payload.model_dump())
        except Exception as error:
            raise problem(error) from None

    @router.post("/imports/drafts", status_code=201)
    async def draft(payload: DraftRequest):
        try:
            return workflow.create_draft(payload.bundle, payload.prompt)
        except Exception as error:
            raise problem(error) from None

    @router.get("/imports")
    async def imports():
        return {"items": workflow.list_jobs()}

    @router.get("/imports/{identifier}")
    async def get_import(identifier: str):
        try:
            return workflow.get(identifier)
        except KeyError as error:
            raise problem(error) from None

    @router.patch("/imports/{identifier}/proposal")
    async def revise(identifier: str, payload: ProposalRequest):
        try:
            return await workflow.revise(identifier, **payload.model_dump())
        except Exception as error:
            raise problem(error) from None

    @router.post("/imports/{identifier}/respond", status_code=202)
    async def respond(identifier: str, payload: RespondRequest):
        try:
            return await workflow.respond(identifier, **payload.model_dump())
        except Exception as error:
            raise problem(error) from None

    @router.post("/imports/{identifier}/confirm")
    async def confirm(identifier: str, payload: ConfirmRequest):
        try:
            return await workflow.confirm(identifier, payload.expected_revision, payload.idempotency_key)
        except Exception as error:
            raise problem(error) from None

    @router.post("/imports/{identifier}/cancel")
    async def cancel(identifier: str):
        try:
            return await workflow.cancel(identifier)
        except Exception as error:
            raise problem(error) from None
    return router
