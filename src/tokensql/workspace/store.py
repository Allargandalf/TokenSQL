"""Transactional SQLite store for the browser quote workspace."""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any

from tokensql.workspace.schemas import QuoteCreate, QuoteResponse

EDITABLE_FIELDS = (
    "model_name", "model_version", "manufacturer", "provider", "provider_type",
    "amount", "currency", "unit", "unit_quantity", "metric", "dimension",
    "direction", "component_type", "discount_rate", "conditions",
    "effective_date", "source", "source_ref", "note",
)


class WorkspaceStoreError(RuntimeError):
    pass


class QuoteNotFoundError(WorkspaceStoreError):
    pass


class RevisionConflictError(WorkspaceStoreError):
    pass


class IdempotencyConflictError(WorkspaceStoreError):
    pass


def _utc_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class WorkspaceStore:
    """One shared SQLite connection, protected for API worker threads."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._conn = sqlite3.connect(
            self.path, timeout=10, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=10000")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _initialize(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS workspace_quote (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_name TEXT NOT NULL CHECK(trim(model_name) <> ''),
                    model_version TEXT,
                    manufacturer TEXT,
                    provider TEXT NOT NULL CHECK(trim(provider) <> ''),
                    provider_type TEXT NOT NULL DEFAULT 'unknown'
                        CHECK(provider_type IN (
                            'official','reseller','aggregator','internal','unknown'
                        )),
                    amount TEXT,
                    currency TEXT CHECK(currency IS NULL OR length(currency) = 3),
                    unit TEXT,
                    unit_quantity TEXT NOT NULL CHECK(CAST(unit_quantity AS REAL) > 0),
                    metric TEXT,
                    dimension TEXT,
                    direction TEXT,
                    component_type TEXT NOT NULL
                        CHECK(component_type IN ('monetary','discount')),
                    discount_rate TEXT,
                    conditions TEXT NOT NULL DEFAULT '{}',
                    effective_date TEXT,
                    source TEXT,
                    source_ref TEXT,
                    note TEXT,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','archived')),
                    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK(
                        (component_type = 'monetary'
                         AND amount IS NOT NULL AND CAST(amount AS REAL) >= 0
                         AND currency IS NOT NULL AND unit IS NOT NULL
                         AND discount_rate IS NULL)
                        OR
                        (component_type = 'discount'
                         AND amount IS NULL AND discount_rate IS NOT NULL
                         AND CAST(discount_rate AS REAL) >= 0
                         AND CAST(discount_rate AS REAL) <= 1)
                    )
                );
                CREATE INDEX IF NOT EXISTS ix_workspace_quote_model
                    ON workspace_quote(model_name);
                CREATE INDEX IF NOT EXISTS ix_workspace_quote_filters
                    ON workspace_quote(status, provider, currency, metric);
                CREATE TABLE IF NOT EXISTS workspace_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    quote_id INTEGER NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('added','updated','deleted')),
                    actor TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_workspace_audit_actor
                    ON workspace_audit(actor, action, quote_id);
                CREATE TABLE IF NOT EXISTS workspace_request (
                    idempotency_key TEXT PRIMARY KEY,
                    payload_sha256 TEXT NOT NULL,
                    result_ids TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    @staticmethod
    def _write_values(item: QuoteCreate) -> dict[str, Any]:
        values = item.model_dump(mode="python")
        values["amount"] = _decimal_text(item.amount)
        values["discount_rate"] = _decimal_text(item.discount_rate)
        values["unit_quantity"] = _decimal_text(item.unit_quantity)
        values["conditions"] = _json(item.conditions)
        values["effective_date"] = (
            None if item.effective_date is None else item.effective_date.isoformat()
        )
        return values

    @staticmethod
    def _public_row(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["conditions"] = json.loads(value["conditions"])
        return value

    @classmethod
    def _response(cls, row: sqlite3.Row) -> QuoteResponse:
        return QuoteResponse.model_validate(cls._public_row(row))

    def get_quote(self, quote_id: int) -> QuoteResponse:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workspace_quote WHERE id = ?", (quote_id,)
            ).fetchone()
        if row is None:
            raise QuoteNotFoundError(f"quote {quote_id} was not found")
        return self._response(row)

    @staticmethod
    def _where(
        *,
        q: str | None = None,
        model_name: str | None = None,
        manufacturer: str | None = None,
        provider: str | None = None,
        currency: str | None = None,
        metric: str | None = None,
        dimension: str | None = None,
        status: str | None = "active",
        ids: Sequence[int] | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if q:
            clauses.append(
                "(model_name LIKE ? ESCAPE '\\' OR manufacturer LIKE ? ESCAPE '\\' "
                "OR provider LIKE ? ESCAPE '\\' OR metric LIKE ? ESCAPE '\\')"
            )
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.extend([f"%{escaped}%"] * 4)
        for column, value in (
            ("model_name", model_name),
            ("manufacturer", manufacturer),
            ("provider", provider),
            ("currency", currency.upper() if currency else None),
            ("metric", metric),
            ("dimension", dimension),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if ids is not None:
            if not ids:
                clauses.append("0")
            else:
                clauses.append(f"id IN ({','.join('?' for _ in ids)})")
                params.extend(ids)
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), params

    def list_quotes(
        self,
        *,
        q: str | None = None,
        model_name: str | None = None,
        manufacturer: str | None = None,
        provider: str | None = None,
        currency: str | None = None,
        metric: str | None = None,
        dimension: str | None = None,
        status: str | None = "active",
        ids: Sequence[int] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[QuoteResponse], int]:
        where, params = self._where(
            q=q, model_name=model_name, manufacturer=manufacturer,
            provider=provider, currency=currency, metric=metric,
            dimension=dimension,
            status=status, ids=ids,
        )
        with self._lock:
            total = int(self._conn.execute(
                "SELECT count(*) FROM workspace_quote" + where, params
            ).fetchone()[0])
            rows = self._conn.execute(
                "SELECT * FROM workspace_quote" + where
                + " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return [self._response(row) for row in rows], total


    def filter_options(self) -> dict[str, list[str]]:
        columns = {
            "model_names": "model_name",
            "manufacturers": "manufacturer",
            "providers": "provider",
            "currencies": "currency",
            "metrics": "metric",
            "dimensions": "dimension",
        }
        result: dict[str, list[str]] = {}
        with self._lock:
            for key, column in columns.items():
                rows = self._conn.execute(
                    f"SELECT DISTINCT {column} FROM workspace_quote "
                    f"WHERE status = 'active' AND {column} IS NOT NULL "
                    f"AND trim({column}) <> '' ORDER BY {column}"
                ).fetchall()
                result[key] = [str(row[0]) for row in rows]
        return result

    def _audit(
        self,
        quote_id: int,
        action: str,
        actor: str,
        old_value: dict[str, Any] | None,
        new_value: dict[str, Any] | None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO workspace_audit(
                quote_id, action, actor, old_value, new_value, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                quote_id, action, actor.strip() or "unknown",
                None if old_value is None else _json(old_value),
                None if new_value is None else _json(new_value),
                _utc_text(),
            ),
        )

    def _insert_quote(self, item: QuoteCreate, actor: str) -> int:
        values = self._write_values(item)
        now = _utc_text()
        columns = ", ".join(EDITABLE_FIELDS)
        placeholders = ", ".join("?" for _ in EDITABLE_FIELDS)
        cursor = self._conn.execute(
            f"INSERT INTO workspace_quote({columns}, created_at, updated_at) "
            f"VALUES ({placeholders}, ?, ?)",
            [*(values[name] for name in EDITABLE_FIELDS), now, now],
        )
        quote_id = int(cursor.lastrowid)
        row = self._conn.execute(
            "SELECT * FROM workspace_quote WHERE id = ?", (quote_id,)
        ).fetchone()
        assert row is not None
        self._audit(quote_id, "added", actor, None, self._public_row(row))
        return quote_id

    def create_many(
        self,
        items: Sequence[QuoteCreate],
        *,
        actor: str = "manual/cli",
        idempotency_key: str | None = None,
    ) -> tuple[list[QuoteResponse], bool]:
        if not items:
            raise ValueError("at least one quote is required")
        payload = [item.model_dump(mode="json") for item in items]
        payload_hash = hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()
        with self._lock:
            with self._transaction():
                if idempotency_key:
                    key = idempotency_key.strip()
                    if not key:
                        raise ValueError("idempotency_key must not be blank")
                    previous = self._conn.execute(
                        "SELECT payload_sha256, result_ids FROM workspace_request "
                        "WHERE idempotency_key = ?", (key,)
                    ).fetchone()
                    if previous is not None:
                        if previous["payload_sha256"] != payload_hash:
                            raise IdempotencyConflictError(
                                "idempotency key was already used for another payload"
                            )
                        ids = json.loads(previous["result_ids"])
                        rows = [
                            self._conn.execute(
                                "SELECT * FROM workspace_quote WHERE id = ?", (item_id,)
                            ).fetchone()
                            for item_id in ids
                        ]
                        if any(row is None for row in rows):
                            raise WorkspaceStoreError(
                                "idempotent result references a missing quote"
                            )
                        return [self._response(row) for row in rows], True
                ids = [self._insert_quote(item, actor) for item in items]
                if idempotency_key:
                    self._conn.execute(
                        """
                        INSERT INTO workspace_request(
                            idempotency_key, payload_sha256, result_ids, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (idempotency_key.strip(), payload_hash, _json(ids), _utc_text()),
                    )
                rows = [
                    self._conn.execute(
                        "SELECT * FROM workspace_quote WHERE id = ?", (item_id,)
                    ).fetchone()
                    for item_id in ids
                ]
        return [self._response(row) for row in rows], False

    def update_quote(
        self,
        quote_id: int,
        changes: dict[str, Any],
        *,
        expected_revision: int,
        actor: str = "workspace-ui",
    ) -> QuoteResponse:
        unknown = set(changes) - set(EDITABLE_FIELDS)
        if unknown:
            raise ValueError(f"unknown quote fields: {', '.join(sorted(unknown))}")
        if not changes:
            return self.get_quote(quote_id)
        with self._lock:
            with self._transaction():
                old_row = self._conn.execute(
                    "SELECT * FROM workspace_quote WHERE id = ?", (quote_id,)
                ).fetchone()
                if old_row is None:
                    raise QuoteNotFoundError(f"quote {quote_id} was not found")
                if int(old_row["revision"]) != expected_revision:
                    raise RevisionConflictError(
                        f"quote {quote_id} is at revision {old_row['revision']}"
                    )
                public_old = self._public_row(old_row)
                current = {name: public_old[name] for name in EDITABLE_FIELDS}
                current.update(changes)
                values = self._write_values(QuoteCreate.model_validate(current))
                assignments = ", ".join(f"{name} = ?" for name in EDITABLE_FIELDS)
                cursor = self._conn.execute(
                    f"UPDATE workspace_quote SET {assignments}, "
                    "revision = revision + 1, updated_at = ? "
                    "WHERE id = ? AND revision = ?",
                    [
                        *(values[name] for name in EDITABLE_FIELDS),
                        _utc_text(), quote_id, expected_revision,
                    ],
                )
                if cursor.rowcount != 1:
                    raise RevisionConflictError(f"quote {quote_id} changed concurrently")
                new_row = self._conn.execute(
                    "SELECT * FROM workspace_quote WHERE id = ?", (quote_id,)
                ).fetchone()
                assert new_row is not None
                self._audit(
                    quote_id, "updated", actor, public_old, self._public_row(new_row)
                )
        return self._response(new_row)

    def archive_quote(
        self,
        quote_id: int,
        *,
        expected_revision: int,
        actor: str = "workspace-ui",
    ) -> QuoteResponse:
        with self._lock:
            with self._transaction():
                old_row = self._conn.execute(
                    "SELECT * FROM workspace_quote WHERE id = ?", (quote_id,)
                ).fetchone()
                if old_row is None:
                    raise QuoteNotFoundError(f"quote {quote_id} was not found")
                if int(old_row["revision"]) != expected_revision:
                    raise RevisionConflictError(
                        f"quote {quote_id} is at revision {old_row['revision']}"
                    )
                cursor = self._conn.execute(
                    "UPDATE workspace_quote SET status = 'archived', "
                    "revision = revision + 1, updated_at = ? "
                    "WHERE id = ? AND revision = ?",
                    (_utc_text(), quote_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise RevisionConflictError(f"quote {quote_id} changed concurrently")
                new_row = self._conn.execute(
                    "SELECT * FROM workspace_quote WHERE id = ?", (quote_id,)
                ).fetchone()
                assert new_row is not None
                self._audit(
                    quote_id, "deleted", actor,
                    self._public_row(old_row), self._public_row(new_row),
                )
        return self._response(new_row)

    def change_summary(self, actor: str) -> dict[str, int]:
        """Return distinct quote counts for one actor, grouped by audit action.

        A quote changed repeatedly counts once in an action. Total is distinct
        quote IDs across all actions, so it can be less than the action sum.
        """
        with self._lock:
            row = self._conn.execute(
                """
                SELECT
                    count(DISTINCT CASE WHEN action = 'added' THEN quote_id END),
                    count(DISTINCT CASE WHEN action = 'updated' THEN quote_id END),
                    count(DISTINCT CASE WHEN action = 'deleted' THEN quote_id END),
                    count(DISTINCT quote_id)
                FROM workspace_audit WHERE actor = ?
                """,
                (actor.strip() or "unknown",),
            ).fetchone()
        assert row is not None
        return {
            "added": int(row[0]), "updated": int(row[1]),
            "deleted": int(row[2]), "total": int(row[3]),
        }

    def apply_changes(
        self,
        *,
        creates: Sequence[QuoteCreate] = (),
        updates: Sequence[tuple[int, int, dict[str, Any]]] = (),
        deletes: Sequence[tuple[int, int]] = (),
        actor: str = "manual/cli",
    ) -> list[QuoteResponse]:
        """Apply create/update/archive operations in one all-or-nothing transaction."""
        target_ids = [item[0] for item in updates] + [item[0] for item in deletes]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("a quote may be updated or deleted only once per apply")
        result_ids: list[int] = []
        with self._lock:
            with self._transaction():
                for item in creates:
                    result_ids.append(self._insert_quote(item, actor))
                for quote_id, expected_revision, changes in updates:
                    unknown = set(changes) - set(EDITABLE_FIELDS)
                    if unknown:
                        raise ValueError(
                            f"unknown quote fields: {', '.join(sorted(unknown))}"
                        )
                    old_row = self._conn.execute(
                        "SELECT * FROM workspace_quote WHERE id = ?", (quote_id,)
                    ).fetchone()
                    if old_row is None:
                        raise QuoteNotFoundError(f"quote {quote_id} was not found")
                    if int(old_row["revision"]) != expected_revision:
                        raise RevisionConflictError(
                            f"quote {quote_id} is at revision {old_row['revision']}"
                        )
                    public_old = self._public_row(old_row)
                    current = {name: public_old[name] for name in EDITABLE_FIELDS}
                    current.update(changes)
                    values = self._write_values(QuoteCreate.model_validate(current))
                    assignments = ", ".join(
                        f"{name} = ?" for name in EDITABLE_FIELDS
                    )
                    self._conn.execute(
                        f"UPDATE workspace_quote SET {assignments}, "
                        "revision = revision + 1, updated_at = ? "
                        "WHERE id = ? AND revision = ?",
                        [
                            *(values[name] for name in EDITABLE_FIELDS),
                            _utc_text(), quote_id, expected_revision,
                        ],
                    )
                    new_row = self._conn.execute(
                        "SELECT * FROM workspace_quote WHERE id = ?", (quote_id,)
                    ).fetchone()
                    assert new_row is not None
                    self._audit(
                        quote_id, "updated", actor,
                        public_old, self._public_row(new_row),
                    )
                    result_ids.append(quote_id)
                for quote_id, expected_revision in deletes:
                    old_row = self._conn.execute(
                        "SELECT * FROM workspace_quote WHERE id = ?", (quote_id,)
                    ).fetchone()
                    if old_row is None:
                        raise QuoteNotFoundError(f"quote {quote_id} was not found")
                    if int(old_row["revision"]) != expected_revision:
                        raise RevisionConflictError(
                            f"quote {quote_id} is at revision {old_row['revision']}"
                        )
                    self._conn.execute(
                        "UPDATE workspace_quote SET status = 'archived', "
                        "revision = revision + 1, updated_at = ? "
                        "WHERE id = ? AND revision = ?",
                        (_utc_text(), quote_id, expected_revision),
                    )
                    new_row = self._conn.execute(
                        "SELECT * FROM workspace_quote WHERE id = ?", (quote_id,)
                    ).fetchone()
                    assert new_row is not None
                    self._audit(
                        quote_id, "deleted", actor,
                        self._public_row(old_row), self._public_row(new_row),
                    )
                    result_ids.append(quote_id)
                rows = [
                    self._conn.execute(
                        "SELECT * FROM workspace_quote WHERE id = ?", (quote_id,)
                    ).fetchone()
                    for quote_id in result_ids
                ]
        return [self._response(row) for row in rows]
