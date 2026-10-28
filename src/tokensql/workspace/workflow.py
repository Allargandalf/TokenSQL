"""Durable file and proposal lifecycle; formal writes require explicit confirmation."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
from threading import RLock
from typing import Any, Sequence
from uuid import uuid4

from .memory import AgentMemory, atomic_json
from .discount import DISCOUNT_POLICY_VERSION, normalize_discount_rate
from .clarification import NeedsInput
from .store import RevisionConflictError
from .official_lookup import validate_lookup_bundle

_SAFE = re.compile(r"[^\w.()\uFF08\uFF09\[\]{} +\-@]", re.UNICODE)
_ZERO = {"added": 0, "updated": 0, "deleted": 0, "total": 0}
PRICING_POLICY_VERSION = 1

def now() -> str:
    return datetime.now(timezone.utc).isoformat()

def clean_filename(name: str) -> str:
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _SAFE.sub("_", "".join(c for c in name if ord(c) >= 32)).strip(" .")
    name = name or "upload"
    suffix = Path(name).suffix[:20]
    stem = name[:-len(suffix)] if suffix else name
    while len((stem + suffix).encode("utf-8")) > 180:
        stem = stem[:-1]
    return stem + suffix

def current_model_fields(value: Any) -> Any:
    """Drop only the retired structural model field, never source text/conditions."""
    result = deepcopy(value)
    if not isinstance(result, dict):
        return result
    models = result.get("models")
    if isinstance(models, list):
        for item in models:
            if isinstance(item, dict):
                item.pop("physical_key", None)
    # Preview/receipt catalog snapshots are known application structures.
    for key in ("catalog", "preview", "commit_result"):
        if isinstance(result.get(key), dict):
            result[key] = current_model_fields(result[key])
    return result


def legacy_bundle_fields(value: Any) -> Any:
    """Retain old missing currency semantics rather than apply the new CNY default."""
    result = current_model_fields(value)
    if not isinstance(result, dict):
        return result
    for key in ("create", "official_prices"):
        for item in result.get(key) or []:
            if isinstance(item, dict) and "currency" not in item and not item.get("pricing_unit_id"):
                item["currency"] = None
    return result


def normalize_bundle_discounts(value: Any) -> Any:
    """Apply the fixed-two-place raw ratio policy, not monetary derivation."""
    result = current_model_fields(value)
    if not isinstance(result, dict):
        return result
    for key in ("create", "update"):
        rows = result.get(key)
        if not isinstance(rows, list):
            continue
        for item in rows:
            if not isinstance(item, dict):
                continue
            payload = item if key == "create" else item.get("changes")
            if isinstance(payload, dict) and "discount_rate" in payload:
                rate = normalize_discount_rate(payload["discount_rate"])
                payload["discount_rate"] = None if rate is None else format(rate, "f")
    return result


def json_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return json.loads(json.dumps(value, default=str))

class WorkflowService:
    def __init__(self, config: Any, store: Any, memory: AgentMemory) -> None:
        self.config, self.store, self.memory = config, store, memory
        self.agent_dir = config.agent_dir
        self.input_dir, self.progress_dir, self.data_dir = config.input_dir, config.progress_dir, config.data_dir
        for directory in (self.input_dir, self.progress_dir, self.data_dir,
                          self.agent_dir / "runs", self.agent_dir / "catalogs"):
            directory.mkdir(parents=True, exist_ok=True)
        self.path = self.agent_dir / "workflow.json"
        self._lock = RLock()
        self._job_locks: dict[str, asyncio.Lock] = {}
        self.manager: Any = None
        if self.path.exists():
            self.state = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.state = {"uploads": {}, "jobs": {}, "model_id_schema_version": 1}
            self._save()
        self._upgrade_model_fields()
        self._upgrade_discount_policy()
        self._upgrade_pricing_policy()

    def _upgrade_model_fields(self) -> None:
        if self.state.get("model_id_schema_version") == 1:
            return
        for job in self.state.get("jobs", {}).values():
            if job.get("status") != "ready":
                # Historical receipts and in-flight idempotency hashes are immutable.
                continue
            for key in ("bundle", "original_bundle"):
                if isinstance(job.get(key), dict):
                    job[key] = legacy_bundle_fields(job[key])
            for key in ("preview", "commit_result"):
                if isinstance(job.get(key), dict):
                    job[key] = current_model_fields(job[key])
            if job.get("status") == "ready" and isinstance(job.get("bundle"), dict):
                # Refresh in RAM after the data schema changes. No automatic confirmation.
                try:
                    preview = json_value(self.store.preview_bundle(job["bundle"]))
                except (ValueError, RevisionConflictError) as error:
                    job["schema_cleanup_warning"] = "Legacy draft needs manual revision: " + type(error).__name__
                else:
                    job.update(preview=preview, issues=preview.get("issues", []) + job.get("proposal_issues", []))
                job["revision"] = job.get("revision", 1) + 1
                job["updated_at"] = now()
        self.state["model_id_schema_version"] = 1
        self._save()

    def _upgrade_discount_policy(self) -> None:
        """Refresh only unconfirmed reviews once, without changing files or history."""
        changed = False
        for job in self.state.get("jobs", {}).values():
            if job.get("status") != "ready" or job.get("discount_policy_version") == DISCOUNT_POLICY_VERSION:
                continue
            try:
                bundle = normalize_bundle_discounts(job.get("bundle"))
                original = normalize_bundle_discounts(job.get("original_bundle"))
                if not isinstance(bundle, dict):
                    raise ValueError("legacy review has no valid proposal")
                # Normalize both sides before remembering later human edits.
                job.update(bundle=bundle, original_bundle=original if isinstance(original, dict) else deepcopy(bundle))
                preview = json_value(self.store.preview_bundle(bundle))
            except (ValueError, RevisionConflictError) as error:
                # Invalid old proposals remain reviewable, never implicitly committed.
                job.update(preview=None, discount_policy_warning=(
                    "Legacy draft requires manual revision under the two-decimal discount policy: "
                    + type(error).__name__))
            else:
                job.update(preview=preview, issues=preview.get("issues", []) + job.get("proposal_issues", []))
                job.pop("discount_policy_warning", None)
            job.update(discount_policy_version=DISCOUNT_POLICY_VERSION,
                       revision=job.get("revision", 1) + 1, updated_at=now())
            changed = True
        if changed:
            self._save()

    def _upgrade_pricing_policy(self) -> None:
        """Refresh pending reviews without inferring scope or rewriting raw proposals."""
        changed = False
        for job in self.state.get("jobs", {}).values():
            if job.get("status") != "ready" or job.get("pricing_policy_version") == PRICING_POLICY_VERSION:
                continue
            try:
                bundle = job.get("bundle")
                if not isinstance(bundle, dict):
                    raise ValueError("legacy review has no valid proposal")
                validate_lookup_bundle(job, bundle, self.store.catalog_snapshot())
                preview = json_value(self.store.preview_bundle(bundle))
            except (ValueError, RevisionConflictError) as error:
                job.update(preview=None, pricing_policy_warning=(
                    "Legacy draft requires manual revision under the structured pricing policy: "
                    + type(error).__name__))
            else:
                job.update(preview=preview, issues=preview.get("issues", []) + job.get("proposal_issues", []))
                job.pop("pricing_policy_warning", None)
            # Both raw bundle baselines stay byte-for-byte business-equivalent. Defaults
            # projected by the new preview are not confirmed human corrections.
            job.update(pricing_policy_version=PRICING_POLICY_VERSION,
                       revision=job.get("revision", 1) + 1, updated_at=now())
            changed = True
        if changed:
            self._save()

    def _save(self) -> None:
        atomic_json(self.path, self.state)

    def _job(self, identifier: str) -> dict[str, Any]:
        if identifier not in self.state["jobs"]:
            raise KeyError("review was not found")
        return self.state["jobs"][identifier]

    def _location(self, item: dict[str, Any]) -> Path:
        stage = item.get("status", "input")
        if stage == "input":
            return self.input_dir / item["stored_name"]
        base = self.data_dir if stage == "data" else self.progress_dir
        return base / item["job_id"] / item["stored_name"]

    def _file_public(self, item: dict[str, Any]) -> dict[str, Any]:
        return {key: item.get(key) for key in ("id", "filename", "size", "sha256", "status", "job_id")}

    def list_files(self) -> list[dict[str, Any]]:
        with self._lock:
            known = {item["stored_name"] for item in self.state["uploads"].values() if item["status"] == "input"}
            for path in sorted(self.input_dir.iterdir()):
                if path.is_file() and not path.is_symlink() and path.name not in known and not path.name.startswith("."):
                    identifier = uuid4().hex
                    self.state["uploads"][identifier] = {"id": identifier, "filename": path.name,
                        "stored_name": path.name, "size": path.stat().st_size, "sha256": None, "status": "input"}
            self._save()
            return [self._file_public(item) for item in self.state["uploads"].values()]

    def upload(self, files: Sequence[Any]) -> list[dict[str, Any]]:
        if not files or len(files) > self.config.max_files:
            raise ValueError("invalid number of uploads")
        if any(len(item.content) > self.config.max_file_bytes for item in files):
            raise ValueError("an upload exceeds the per-file size limit")
        if sum(len(item.content) for item in files) > self.config.max_total_bytes:
            raise ValueError("uploads exceed the total size limit")
        with self._lock:
            saved: list[dict[str, Any]] = []
            try:
                for file in files:
                    identifier = uuid4().hex
                    name = clean_filename(file.filename)
                    item = {"id": identifier, "filename": name, "stored_name": identifier + "_" + name,
                            "size": len(file.content), "sha256": hashlib.sha256(file.content).hexdigest(), "status": "input"}
                    path = self._location(item)
                    with path.open("xb") as handle:
                        handle.write(file.content)
                    self.state["uploads"][identifier] = item
                    saved.append(item)
                self._save()
            except Exception:
                for item in saved:
                    self.state["uploads"].pop(item["id"], None)
                    self._location(item).unlink(missing_ok=True)
                raise
            return [self._file_public(item) for item in saved]

    def ensure_no_waiting_session(self, conversation_id: str | None) -> None:
        if conversation_id and any(job.get("status") == "waiting_for_input"
                and job.get("conversation_id") == conversation_id for job in self.state["jobs"].values()):
            raise RevisionConflictError("Conversation is waiting for input; respond to its existing import job")

    async def start(self, *, upload_ids: Sequence[str], prompt: str,
                    conversation_id: str | None = None, intent: str = "import",
                    lookup_model_id: str | None = None, lookup_billing_item: str | None = None) -> dict[str, Any]:
        if intent not in {"import", "official_price_lookup"}:
            raise ValueError("unknown workflow intent")
        self.ensure_no_waiting_session(conversation_id)
        session_scope = getattr(self.manager, "_sessions", {}).get(conversation_id, {})
        continuation = session_scope.get("intent") == "official_price_lookup"
        if continuation:
            if upload_ids:
                raise ValueError("official lookup conversations accept text clarification only, not attachments")
            # A public ordinary-start request cannot downgrade a network-enabled official session.
            intent = "official_price_lookup"
            lookup_model_id = session_scope["lookup_model_id"]
            lookup_billing_item = session_scope.get("lookup_billing_item")
        if intent == "official_price_lookup" and (
                upload_ids or not lookup_model_id or conversation_id is not None and not continuation):
            raise ValueError("official lookup requires a new independent session or its scoped continuation")
        prompt = prompt.strip()
        if not prompt:
            raise ValueError("prompt must not be blank")
        if len(upload_ids) != len(set(upload_ids)) or len(upload_ids) > self.config.max_files:
            raise ValueError("invalid upload selection")
        if conversation_id is not None and not re.fullmatch(r"ses[A-Za-z0-9_-]+", conversation_id):
            raise ValueError("invalid conversation_id")
        with self._lock:
            items = []
            for identifier in upload_ids:
                item = self.state["uploads"].get(identifier)
                if item is None or item["status"] != "input":
                    raise ValueError("selected upload is not available in input")
                if self._location(item).is_symlink() or not self._location(item).is_file():
                    raise ValueError("selected upload is unavailable")
                if item["size"] > self.config.max_file_bytes:
                    raise ValueError("an upload exceeds the per-file size limit")
                items.append(item)
            if sum(item["size"] for item in items) > self.config.max_total_bytes:
                raise ValueError("uploads exceed the total size limit")
            job = self._new_job(prompt, conversation_id, list(upload_ids))
            job.update(intent=intent, lookup_model_id=lookup_model_id, lookup_billing_item=lookup_billing_item)
            job["status"] = "preparing"
            self._save()
            try:
                destination = self.progress_dir / job["id"]
                destination.mkdir(exist_ok=True)
                for item in items:
                    source = self._location(item)
                    item["job_id"] = job["id"]
                    target = destination / item["stored_name"]
                    source.rename(target)
                    item["status"] = "progress"
                    if item["sha256"] is None:
                        item["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
                    self._save()
                job["status"] = "queued"
                self._save()
            except Exception:
                self._return_files(job)
                job.update(status="failed", error="Files could not enter the processing directory")
                self._save()
                raise
        self.manager.schedule(job["id"])
        return self.get(job["id"])

    def _new_job(self, prompt: str, conversation_id: str | None, upload_ids: list[str]) -> dict[str, Any]:
        identifier = uuid4().hex
        job = {"id": identifier, "run_id": identifier, "import_id": identifier,
               "conversation_id": conversation_id, "status": "queued", "revision": 1,
               "discount_policy_version": DISCOUNT_POLICY_VERSION,
               "pricing_policy_version": PRICING_POLICY_VERSION,
               "attempt": 1, "clarifications": [], "questions": [], "question_summary": None,
               "intent": "import", "lookup_model_id": None, "lookup_billing_item": None,
               "upload_ids": upload_ids, "prompt": prompt, "bundle": None, "original_bundle": None,
               "preview": None, "issues": [], "proposal_issues": [], "error": None, "assistant_message": None,
               "messages": [], "changes": dict(_ZERO), "created_at": now(), "updated_at": now()}
        self.state["jobs"][identifier] = job
        return job

    def create_draft(self, bundle: dict[str, Any], prompt: str = "") -> dict[str, Any]:
        bundle = normalize_bundle_discounts(bundle)
        preview = json_value(self.store.preview_bundle(bundle))
        with self._lock:
            job = self._new_job(prompt or "Human proposal", None, [])
            job.update(status="ready", bundle=deepcopy(bundle), original_bundle=deepcopy(bundle),
                       preview=preview, issues=preview.get("issues", []), revision=2)
            self._save()
            return self.get(job["id"])

    def get(self, identifier: str) -> dict[str, Any]:
        with self._lock:
            job = deepcopy(self._job(identifier))
            job.pop("original_bundle", None)
            for key, default in (("attempt", 1), ("clarifications", []), ("questions", []), ("question_summary", None)):
                job.setdefault(key, default)
            job["files"] = [self._file_public(self.state["uploads"][key]) for key in job["upload_ids"]]
            job["review_required"] = job["status"] == "ready"
            return job

    def list_jobs(self) -> list[dict[str, Any]]:
        return [self.get(key) for key in reversed(list(self.state["jobs"]))]

    def update_job(self, identifier: str, **values: Any) -> None:
        with self._lock:
            job = self._job(identifier)
            if job["status"] == "cancelled":
                return
            job.update(values, updated_at=now())
            self._save()

    def accept_proposal(self, identifier: str, bundle: dict[str, Any], issues: list[Any] | None = None) -> None:
        bundle = normalize_bundle_discounts(bundle)
        validate_lookup_bundle(self._job(identifier), bundle, self.store.catalog_snapshot())
        preview = json_value(self.store.preview_bundle(bundle))
        with self._lock:
            job = self._job(identifier)
            if job["status"] == "cancelled":
                return
            proposal_issues = json_value(issues or [])[:2000]
            job.update(bundle=deepcopy(bundle), original_bundle=deepcopy(bundle), proposal_issues=proposal_issues,
                       questions=[], question_summary=None, error=None,
                       preview=preview, issues=preview.get("issues", []) + proposal_issues, revision=job["revision"] + 1,
                       status="ready", updated_at=now())
            self._save()

    def accept_questions(self, identifier: str, payload: dict[str, Any]) -> None:
        request = NeedsInput.model_validate(payload)
        with self._lock:
            job = self._job(identifier)
            if job["status"] == "cancelled":
                return
            if job["status"] != "running":
                raise RevisionConflictError("Question response status conflict")
            job.update(status="waiting_for_input", questions=list(request.questions),
                       question_summary=request.summary, bundle=None, original_bundle=None, preview=None,
                       issues=[], proposal_issues=[], error=None, changes=dict(_ZERO),
                       revision=job["revision"] + 1, updated_at=now())
            self._save()

    async def respond(self, identifier: str, expected_revision: int, prompt: str) -> dict[str, Any]:
        prompt = prompt.strip()
        if not prompt or len(prompt) > 16000:
            raise ValueError("clarification response must contain 1 to 16000 characters")
        async with self._job_locks.setdefault(identifier, asyncio.Lock()):
            with self._lock:
                job = self._job(identifier)
                if job["status"] != "waiting_for_input" or job["revision"] != expected_revision:
                    raise RevisionConflictError("Clarification status or revision conflict")
                next_attempt = job.get("attempt", 1) + 1
                clarification = {"attempt": next_attempt, "prompt": prompt, "created_at": now(),
                                 "question_attempt": job.get("attempt", 1),
                                 "questions": deepcopy(job.get("questions", [])),
                                 "question_summary": job.get("question_summary"),
                                 "assistant_message": job.get("assistant_message")}
                job.update(status="queued", attempt=next_attempt,
                           clarifications=[*job.get("clarifications", []), clarification],
                           questions=[], question_summary=None, error=None, assistant_message=None, messages=[],
                           bundle=None, original_bundle=None, preview=None, issues=[], proposal_issues=[],
                           changes=dict(_ZERO), revision=job["revision"] + 1, updated_at=now())
                self._save()
            self.manager.schedule(identifier)
            return self.get(identifier)

    async def revise(self, identifier: str, expected_revision: int, bundle: dict[str, Any]) -> dict[str, Any]:
        bundle = normalize_bundle_discounts(bundle)
        async with self._job_locks.setdefault(identifier, asyncio.Lock()):
            with self._lock:
                job = self._job(identifier)
                if job["status"] != "ready" or job["revision"] != expected_revision:
                    raise ValueError("review status or revision conflict")
                validate_lookup_bundle(job, bundle, self.store.catalog_snapshot())
                preview = json_value(self.store.preview_bundle(bundle))
                job.pop("discount_policy_warning", None)
                job.pop("pricing_policy_warning", None)
                job.update(bundle=deepcopy(bundle), preview=preview, issues=preview.get("issues", []) + job.get("proposal_issues", []),
                           revision=job["revision"] + 1, updated_at=now())
                self._save()
            return self.get(identifier)

    def _move_files(self, job: dict[str, Any], stage: str) -> None:
        base = self.data_dir if stage == "data" else self.progress_dir
        destination = base / job["id"]
        destination.mkdir(exist_ok=True)
        for identifier in job["upload_ids"]:
            item = self.state["uploads"][identifier]
            source = self._location(item)
            target = destination / item["stored_name"]
            # A crash between rename and manifest save can leave the destination already present.
            actual = source if source.is_file() else target
            if stage == "data" and item.get("sha256") and hashlib.sha256(actual.read_bytes()).hexdigest() != item["sha256"]:
                raise OSError("original upload hash changed; confirmation was refused")
            if source != target and source.exists():
                if target.exists():
                    raise OSError("archive destination already exists")
                source.rename(target)
            elif not target.is_file():
                raise OSError("source file is missing")
            item.update(status=stage, job_id=job["id"])
            self._save()

    def _return_files(self, job: dict[str, Any]) -> None:
        for identifier in job["upload_ids"]:
            item = self.state["uploads"][identifier]
            target = self.input_dir / item["stored_name"]
            candidates = [self._location(item), self.progress_dir / job["id"] / item["stored_name"],
                          self.data_dir / job["id"] / item["stored_name"]]
            source = next((path for path in candidates if path.is_file()), None)
            if source is not None and source != target:
                if target.exists():
                    raise OSError("input destination already exists")
                source.rename(target)
            if not target.is_file():
                raise OSError("original upload could not be recovered")
            item.update(status="input")
            item.pop("job_id", None)
            self._save()

    async def confirm(self, identifier: str, expected_revision: int,
                      idempotency_key: str | None = None, *, recovering: bool = False) -> dict[str, Any]:
        async with self._job_locks.setdefault(identifier, asyncio.Lock()):
            with self._lock:
                job = self._job(identifier)
                if job["status"] == "confirmed":
                    return self.get(identifier)
                if job["status"] == "committed":
                    self._finish_confirmation(job)
                    return self.get(identifier)
                if job["status"] == "confirming":
                    recovering = True
                if not recovering and (job["status"] != "ready" or job["revision"] != expected_revision):
                    raise ValueError("review status or revision conflict")
                if recovering and job["status"] != "confirming":
                    raise ValueError("review is not awaiting confirmation recovery")
                if job.get("discount_policy_warning") or job.get("pricing_policy_warning"):
                    raise ValueError("legacy draft requires manual revision before confirmation")
                # Recheck raw contracts and dedicated lookup scope; never derive missing raw prices.
                validate_lookup_bundle(job, job["bundle"], self.store.catalog_snapshot())
                if not recovering:
                    latest = json_value(self.store.preview_bundle(job["bundle"]))
                    if latest.get("base_catalog_token") != (job["preview"] or {}).get("base_catalog_token"):
                        job.update(preview=latest, issues=latest.get("issues", []) + job.get("proposal_issues", []),
                                   revision=job["revision"] + 1, error="Catalog changed; review the refreshed calculation and confirm again")
                        self._save()
                        raise RevisionConflictError(job["error"])
                    job["preview"] = latest
                job.update(status="confirming", confirmation_key=idempotency_key or identifier, updated_at=now())
                self._save()
                try:
                    self._move_files(job, "data")
                    result = json_value(self.store.apply_bundle(job["bundle"], actor="review:" + identifier,
                                                                import_id="review:" + identifier,
                                                                expected_catalog_token=job["preview"].get("base_catalog_token")))
                except Exception as error:
                    if isinstance(error, RevisionConflictError):
                        self._move_files(job, "progress")
                        try:
                            job["preview"] = json_value(self.store.preview_bundle(job["bundle"]))
                            job["issues"] = job["preview"].get("issues", []) + job.get("proposal_issues", [])
                        except (ValueError, RevisionConflictError):
                            pass
                        job.update(status="ready", revision=job["revision"] + 1,
                                   error="Catalog changed during confirmation; review and confirm again")
                        self._save()
                    elif not recovering:
                        try:
                            self._move_files(job, "progress")
                        except OSError:
                            job.update(status="confirming", error="Confirmation requires file recovery; retry after restart")
                            self._save()
                            raise
                        job.update(status="ready", error="Confirmation failed; no unconfirmed proposal was accepted")
                        self._save()
                    raise
                # Persist the committed receipt before post-commit memory/file work.
                job.update(commit_result=result, status="committed", error=None)
                self._save()
                self._finish_confirmation(job)
            return self.get(identifier)

    def _finish_confirmation(self, job: dict[str, Any]) -> None:
        result = job["commit_result"]
        self.memory.remember_confirmed(job["id"], job["original_bundle"], job["bundle"],
                                       [self.state["uploads"][key]["filename"] for key in job["upload_ids"]])
        changes = dict(result.get("changes", {}))
        changes.setdefault("total", sum(int(changes.get(key, 0)) for key in ("added", "updated", "deleted")))
        job.update(status="confirmed", changes=changes, preview=result, error=None,
                   revision=job["revision"] + 1, updated_at=now())
        self._save()
        self._remove_draft(job["id"])

    async def cancel(self, identifier: str) -> dict[str, Any]:
        async with self._job_locks.setdefault(identifier, asyncio.Lock()):
            job = self._job(identifier)
            if job["status"] == "cancelled":
                return self.get(identifier)
            if job["status"] in {"confirmed", "confirming", "committed"}:
                raise ValueError("a confirmed or committing review cannot be cancelled")
            await self.manager.abort(identifier)
            with self._lock:
                self._return_files(job)
                self._remove_draft(identifier)
                job.update(status="cancelled", bundle=None, original_bundle=None, preview=None,
                           issues=[], questions=[], question_summary=None, error=None, changes=dict(_ZERO), revision=job["revision"] + 1, updated_at=now())
                self._save()
            return self.get(identifier)

    def _remove_draft(self, identifier: str) -> None:
        directory = self.agent_dir / "runs" / identifier
        if directory.parent == self.agent_dir / "runs" and directory.is_dir():
            shutil.rmtree(directory)
        catalogs = self.agent_dir / "catalogs"
        for pattern in (identifier + ".json", identifier + ".official-sources.md",
                        identifier + ".attempt-*.json", identifier + ".attempt-*.official-sources.md"):
            for path in catalogs.glob(pattern):
                path.unlink(missing_ok=True)

    async def recover(self) -> None:
        for identifier in list(self.state["jobs"]):
            job = self._job(identifier)
            try:
                if job["status"] == "confirming":
                    await self.confirm(identifier, job["revision"], recovering=True)
                elif job["status"] == "committed":
                    self._finish_confirmation(job)
                elif job["status"] in {"preparing", "queued", "running", "cancelling"}:
                    await self.manager.abort(identifier)
                    self._return_files(job)
                    self._remove_draft(identifier)
                    job.update(status="failed", error="Processing was interrupted; originals were returned to input",
                               bundle=None, preview=None, changes=dict(_ZERO))
                    self._save()
            except Exception as error:
                job["error"] = "Recovery pending: " + type(error).__name__
                self._save()
