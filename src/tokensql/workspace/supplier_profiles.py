"""Confirmed supplier business meaning, isolated by stable supplier and source scope.

Profiles are deliberately separate from quotes and workbook baselines. Only an
explicit human edit or the post-confirmation adapter may write them.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any
from uuid import uuid4

from .schemas import SupplierProfileContent, SupplierProfileWrite


def initialize_supplier_profiles(conn: sqlite3.Connection) -> None:
    schema = """
        CREATE TABLE IF NOT EXISTS pricing_supplier_profile (
            supplier_id TEXT PRIMARY KEY REFERENCES pricing_supplier(supplier_id),
            revision INTEGER NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS pricing_supplier_profile_history (
            supplier_id TEXT NOT NULL REFERENCES pricing_supplier(supplier_id),
            revision INTEGER NOT NULL, payload TEXT NOT NULL, actor TEXT NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY(supplier_id,revision));
        CREATE TABLE IF NOT EXISTS pricing_supplier_profile_import (
            supplier_id TEXT NOT NULL REFERENCES pricing_supplier(supplier_id),
            import_id TEXT NOT NULL, source_scope TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(supplier_id,import_id));
    """
    for statement in schema.split(";"):
        if statement.strip():
            conn.execute(statement)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _names(mapping: dict) -> set[str]:
    return {value.casefold() for value in [mapping["raw_name"], *mapping.get("aliases", [])]}


def _same_context(left: dict, right: dict) -> bool:
    return (left.get("supplier_model_id") or "") == (right.get("supplier_model_id") or "")


def _clean_content(value: dict) -> dict:
    result = SupplierProfileContent.model_validate(value).model_dump(mode="json")
    for group in [result, *result["sources"].values()]:
        for mapping in group["model_mappings"]:
            mapping["id"] = mapping["id"] or "mapping-" + uuid4().hex
        for rule in group["rules"]:
            rule["id"] = rule["id"] or "rule-" + uuid4().hex
        for key in ("model_mappings", "rules"):
            ids = [item["id"] for item in group[key]]
            if len(ids) != len(set(ids)):
                raise ValueError("profile entry IDs must be unique within each scope")
        mappings = group["model_mappings"]
        for index, mapping in enumerate(mappings):
            if not mapping["enabled"]:
                continue
            for other in mappings[index + 1:]:
                if other["enabled"] and _same_context(mapping, other) and _names(mapping) & _names(other):
                    raise ValueError("profile names or aliases overlap within the same channel context")
    if len(_json(result)) > 2000000:
        raise ValueError("supplier profile is too large")
    return result


def _effective(profile: dict, source_scope: str) -> dict:
    scope = profile["sources"].get(source_scope, {})
    scoped = [deepcopy(item) for item in scope.get("model_mappings", []) if item["enabled"]]
    mappings = [deepcopy(item) for item in profile["model_mappings"] if item["enabled"]
                and not any(_same_context(item, override) and _names(item) & _names(override)
                            for override in scope.get("model_mappings", []))]
    mappings.extend(scoped)
    # A disabled scoped rule can deliberately suppress an inherited rule by ID.
    scoped_rule_ids = {item["id"] for item in scope.get("rules", [])}
    rules = [deepcopy(item) for item in profile["rules"]
             if item["enabled"] and item["id"] not in scoped_rule_ids]
    rules.extend(deepcopy(item) for item in scope.get("rules", []) if item["enabled"])
    return {"source_scope": source_scope, "model_mappings": mappings, "rules": rules,
            "default_prompt": scope.get("default_prompt") if scope.get("default_prompt") is not None
                              else profile["default_prompt"]}


def profile_meaning_fingerprint(effective: dict) -> str:
    """Cache identity excludes edit order, IDs and confirmation provenance."""
    mappings = []
    for item in effective["model_mappings"]:
        mappings.append({key: (sorted(set(item.get(key, []))) if key == "aliases" else item.get(key))
                         for key in ("raw_name", "model_id", "supplier_model_id", "aliases")})
    rules = [{"kind": item["kind"], "text": item["text"]} for item in effective["rules"]]
    meaning = {"default_prompt": effective["default_prompt"],
               "model_mappings": sorted({_json(item) for item in mappings}),
               "rules": sorted({_json(item) for item in rules})}
    return hashlib.sha256(_json(meaning).encode("utf-8")).hexdigest()


class SupplierProfileStore:
    """Mixin sharing WorkspaceStore's lock, transaction and audit trail."""

    def _profile_supplier(self, supplier_id: str) -> sqlite3.Row:
        supplier = self._conn.execute(
            "SELECT supplier_id,name FROM pricing_supplier WHERE supplier_id=?", (supplier_id,)).fetchone()
        if supplier is None:
            raise ValueError("supplier_id was not found")
        return supplier

    def get_supplier_profile(self, supplier_id: str, source_scope: str | None = None) -> dict:
        with self._lock:
            supplier = self._profile_supplier(supplier_id)
            row = self._conn.execute("SELECT * FROM pricing_supplier_profile WHERE supplier_id=?",
                                     (supplier_id,)).fetchone()
            result = (json.loads(row["payload"]) if row else
                      SupplierProfileContent().model_dump(mode="json"))
            result.update(supplier_id=supplier_id, supplier_name=supplier["name"],
                          revision=row["revision"] if row else 0,
                          updated_at=row["updated_at"] if row else None,
                          confirmed_imports=[item["import_id"] for item in self._conn.execute(
                              "SELECT import_id FROM pricing_supplier_profile_import WHERE supplier_id=? "
                              "ORDER BY created_at,import_id", (supplier_id,))])
            if source_scope is not None:
                result["effective"] = _effective(result, source_scope)
                result["effective_fingerprint"] = profile_meaning_fingerprint(result["effective"])
            return result

    def supplier_profile_history(self, supplier_id: str) -> list[dict]:
        with self._lock:
            self._profile_supplier(supplier_id)
            return [{"revision": row["revision"], "profile": json.loads(row["payload"]),
                     "actor": row["actor"], "created_at": row["created_at"]}
                    for row in self._conn.execute(
                        "SELECT * FROM pricing_supplier_profile_history WHERE supplier_id=? "
                        "ORDER BY revision DESC", (supplier_id,))]

    def _validate_profile_models(self, content: dict, allowed_model_ids: list[str] | None = None) -> None:
        ids = {item["model_id"] for group in [content, *content["sources"].values()]
               for item in group["model_mappings"]}
        for model_id in ids - set(allowed_model_ids or []):
            if not self._conn.execute("SELECT 1 FROM pricing_model WHERE model_id=?", (model_id,)).fetchone():
                raise ValueError("profile model_id must reference an existing canonical model: " + model_id)

    def _write_supplier_profile(self, supplier_id: str, content: dict, actor: str,
                                old: dict) -> dict:
        content = _clean_content(content)
        self._validate_profile_models(content)
        revision = old["revision"] + 1
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO pricing_supplier_profile(supplier_id,revision,payload,updated_at) VALUES (?,?,?,?) "
            "ON CONFLICT(supplier_id) DO UPDATE SET revision=excluded.revision,payload=excluded.payload,"
            "updated_at=excluded.updated_at", (supplier_id, revision, _json(content), now))
        result = self.get_supplier_profile(supplier_id)
        self._conn.execute(
            "INSERT INTO pricing_supplier_profile_history VALUES (?,?,?,?,?)",
            (supplier_id, revision, _json(result), actor, now))
        self._audit("supplier_profile", supplier_id, "confirmed" if old["revision"] == 0 else "updated",
                    actor, old, result)
        return result

    def update_supplier_profile(self, supplier_id: str, request: SupplierProfileWrite | dict,
                                actor: str = "workspace-ui") -> dict:
        from .store import RevisionConflictError
        request = SupplierProfileWrite.model_validate(request)
        with self._lock, self._transaction():
            old = self.get_supplier_profile(supplier_id)
            if old["revision"] != request.expected_revision:
                raise RevisionConflictError("supplier profile changed; refresh its current revision")
            content = {key: old[key] for key in SupplierProfileContent.model_fields}
            content.update(request.model_dump(mode="json", exclude_unset=True,
                                               exclude={"expected_revision", "actor"}))
            return self._write_supplier_profile(supplier_id, content, request.actor or actor, old)

    def _confirmed_profile_content(self, supplier_id: str, source_scope: str | None,
                                   import_id: str, *, model_mappings: list[dict] | None,
                                   rules: list[dict] | None, default_prompt: str | None,
                                   evidence: dict | None) -> tuple[dict, dict, str, bool]:
        from .store import IdempotencyConflictError
        import_id = str(import_id).strip()
        if not import_id or len(import_id) > 255:
            raise ValueError("confirmed profile requires a bounded import_id")
        source_scope = (source_scope or "").strip()
        if len(source_scope) > 255:
            raise ValueError("source_scope is too long")
        old = self.get_supplier_profile(supplier_id)
        prior = self._conn.execute(
            "SELECT source_scope FROM pricing_supplier_profile_import WHERE supplier_id=? AND import_id=?",
            (supplier_id, import_id)).fetchone()
        content = {key: deepcopy(old[key]) for key in SupplierProfileContent.model_fields}
        if prior:
            if prior["source_scope"] != source_scope:
                raise IdempotencyConflictError("confirmed import already belongs to another profile source scope")
            return old, content, source_scope, True
        target = content
        if source_scope:
            target = content["sources"].setdefault(source_scope, {
                "default_prompt": None, "model_mappings": [], "rules": []})
        provenance = {**(evidence or {}), "import_id": import_id, "source": "confirmed_review"}
        for key, incoming in (("model_mappings", model_mappings or []), ("rules", rules or [])):
            for raw in incoming:
                item = deepcopy(raw)
                # Receipt provenance is host-owned; quoted evidence cannot replace it.
                item["evidence"] = {**item.get("evidence", {}), **provenance}
                if key == "model_mappings":
                    names = _names(item)
                    matches = [entry for entry in target[key]
                               if _same_context(entry, item) and _names(entry) & names]
                else:
                    matches = [entry for entry in target[key]
                               if (item.get("id") and entry["id"] == item["id"])
                               or (entry["text"] == item.get("text")
                                   and entry["kind"] == item.get("kind", "interpretation"))]
                if matches:
                    item.setdefault("id", matches[0]["id"])
                    if key == "model_mappings":
                        # Automatic captures never erase human-confirmed spellings.
                        known = [value for entry in matches
                                 for value in [entry["raw_name"], *entry.get("aliases", [])]]
                        item["aliases"] = list(dict.fromkeys([
                            *item.get("aliases", []), *(value for value in known
                                                      if value != item["raw_name"])]))
                    target[key] = [entry for entry in target[key] if entry not in matches]
                target[key].append(item)
        if default_prompt is not None:
            target["default_prompt"] = default_prompt
        return old, _clean_content(content), source_scope, False

    def preview_confirmed_supplier_profile(self, supplier_id: str, source_scope: str | None,
                                           import_id: str, *, model_mappings: list[dict] | None = None,
                                           rules: list[dict] | None = None,
                                           default_prompt: str | None = None,
                                           evidence: dict | None = None,
                                           actor: str = "confirmed-review",
                                           allowed_model_ids: list[str] | None = None) -> dict:
        """Preflight the exact post-confirm payload before a quote transaction."""
        with self._lock:
            old, content, _, recorded = self._confirmed_profile_content(
                supplier_id, source_scope, import_id, model_mappings=model_mappings,
                rules=rules, default_prompt=default_prompt, evidence=evidence)
            if not recorded:
                self._validate_profile_models(content, allowed_model_ids or [])
            effective = _effective(content, (source_scope or "").strip())
            return {"valid": True, "base_revision": old["revision"],
                    "already_recorded": recorded, "profile": content, "effective": effective,
                    "effective_fingerprint": profile_meaning_fingerprint(effective)}

    def record_confirmed_supplier_profile(self, supplier_id: str, source_scope: str | None,
                                          import_id: str, *, model_mappings: list[dict] | None = None,
                                          rules: list[dict] | None = None,
                                          default_prompt: str | None = None,
                                          evidence: dict | None = None,
                                          actor: str = "confirmed-review") -> dict:
        with self._lock, self._transaction():
            old, content, source_scope, recorded = self._confirmed_profile_content(
                supplier_id, source_scope, import_id, model_mappings=model_mappings,
                rules=rules, default_prompt=default_prompt, evidence=evidence)
            if recorded:
                return old
            self._conn.execute("INSERT INTO pricing_supplier_profile_import VALUES (?,?,?,?)",
                               (supplier_id, import_id.strip(), source_scope,
                                datetime.now(timezone.utc).isoformat()))
            return self._write_supplier_profile(supplier_id, content, actor, old)
