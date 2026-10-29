"""Latest confirmed source cache, scoped to one supplier and quote series."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3

from .schemas import PricingBundle
from .table_diff import PARSER_VERSION, fingerprint


def initialize_supplier_baselines(conn: sqlite3.Connection) -> None:
    # Individual statements retain WorkspaceStore's outer migration transaction.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pricing_supplier_baseline (
            supplier_id TEXT NOT NULL REFERENCES pricing_supplier(supplier_id),
            source_scope TEXT NOT NULL, revision INTEGER NOT NULL,
            import_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(supplier_id,source_scope))
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pricing_supplier_baseline_import (
            supplier_id TEXT NOT NULL REFERENCES pricing_supplier(supplier_id),
            source_scope TEXT NOT NULL, import_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY(supplier_id,source_scope,import_id))
    """)


class SupplierBaselineStore:
    def get_supplier_baseline(self, supplier_id: str, source_scope: str = "default") -> dict | None:
        with self._lock:
            self._profile_supplier(supplier_id)
            row = self._conn.execute(
                "SELECT * FROM pricing_supplier_baseline WHERE supplier_id=? AND source_scope=?",
                (supplier_id, source_scope)).fetchone()
            if row is None:
                return None
            result = json.loads(row["payload"])
            result.update(supplier_id=supplier_id, source_scope=source_scope, revision=row["revision"],
                          import_id=row["import_id"], created_at=row["created_at"])
            return result

    def record_supplier_baseline(self, supplier_id: str, source_scope: str, import_id: str,
                                 payload: dict, expected_revision: int | None = None) -> dict:
        """Only call after a successful human-confirmed quote transaction.

        Replaying an older receipt must never roll the latest baseline backwards.
        Source originals remain in data/; only their references and parsed cache
        are stored here. No history of every source workbook is duplicated.
        """
        from .store import IdempotencyConflictError, RevisionConflictError
        source_scope, import_id = str(source_scope).strip(), str(import_id).strip()
        if not source_scope or len(source_scope) > 255 or not import_id or len(import_id) > 255:
            raise ValueError("baseline requires a bounded source_scope and import_id")
        required = {"snapshot", "bundle", "file_refs", "profile_revision",
                    "profile_fingerprint", "prompt_fingerprint", "parser_version"}
        if not required.issubset(payload):
            raise ValueError("baseline is missing snapshot, confirmed candidate or interpretation metadata")
        payload = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        if payload["parser_version"] != PARSER_VERSION or payload["snapshot"].get("version") != 1:
            raise ValueError("baseline parser version is unsupported")
        candidate = PricingBundle.model_validate(payload["bundle"])
        if candidate.update or candidate.delete:
            raise ValueError("baseline stores full source candidates, never database operation recipes")
        payload["bundle"] = candidate.model_dump(mode="json")
        digest = fingerprint(payload)
        with self._lock, self._transaction():
            old = self.get_supplier_baseline(supplier_id, source_scope)
            recorded = self._conn.execute(
                "SELECT payload_hash FROM pricing_supplier_baseline_import "
                "WHERE supplier_id=? AND source_scope=? AND import_id=?",
                (supplier_id, source_scope, import_id)).fetchone()
            if recorded:
                if recorded["payload_hash"] != digest:
                    raise IdempotencyConflictError("confirmed baseline import was already recorded with other content")
                return old
            revision = old["revision"] if old else 0
            if expected_revision is not None and revision != expected_revision:
                raise RevisionConflictError("supplier source baseline changed; compare its latest revision")
            now = datetime.now(timezone.utc).isoformat()
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            self._conn.execute(
                "INSERT INTO pricing_supplier_baseline VALUES (?,?,?,?,?,?) "
                "ON CONFLICT(supplier_id,source_scope) DO UPDATE SET revision=excluded.revision,"
                "import_id=excluded.import_id,payload=excluded.payload,created_at=excluded.created_at",
                (supplier_id, source_scope, revision + 1, import_id, text, now))
            self._conn.execute("INSERT INTO pricing_supplier_baseline_import VALUES (?,?,?,?,?)",
                               (supplier_id, source_scope, import_id, digest, now))
            result = self.get_supplier_baseline(supplier_id, source_scope)
            # Keep the audit lightweight; archived files and current cache carry source detail.
            self._audit("supplier_baseline", supplier_id + ":" + source_scope, "confirmed", "review:" + import_id,
                None if old is None else {key: old[key] for key in
                    ("revision", "import_id", "profile_fingerprint", "prompt_fingerprint")},
                {key: result[key] for key in ("revision", "import_id", "profile_fingerprint", "prompt_fingerprint")})
            return result
