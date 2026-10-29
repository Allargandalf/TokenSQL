"""Normalized SQLite pricing catalog, optimistic revisions and atomic bundles."""
from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from threading import RLock
from typing import Any
from uuid import uuid4

from .discount import normalize_discount_rate
from .pricing_conditions import compact_conditions, legacy_metric, legacy_official_semantics, legacy_quote_scope, match_official_prices, scenario_key
from .pricing import canonical_unit, identity_key
from .units import canonical_price_payload, expand_price_payload
from .conversion import quote_projection
from .schemas import (ExchangeRate, TokenEquivalence, ModelCreate, OfficialPriceCreate,
                      BulkQuoteDeleteRequest, PricingBundle, QuoteCreate, QuoteResponse,
                      SupplierWrite, SupplierReconciliationRequest)
from .reconciliation import build_reconciliation, canonical_json, digest, pricing_basis_issue

from .supplier_profiles import SupplierProfileStore, initialize_supplier_profiles
from .supplier_baselines import SupplierBaselineStore, initialize_supplier_baselines

EDITABLE_FIELDS = tuple(QuoteCreate.model_fields)


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


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


class WorkspaceStore(SupplierProfileStore, SupplierBaselineStore):
    """The legacy workspace_quote table is preserved, never rewritten or cleared."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._valuation_at: datetime | None = None
        self._conn = sqlite3.connect(self.path, timeout=10, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=10000")
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        try:
            self._initialize()
        except Exception:
            self._conn.close()
            raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _initialize(self) -> None:
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS pricing_vendor (
                    vendor_id TEXT PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pricing_model (
                    model_id TEXT PRIMARY KEY, vendor_id TEXT REFERENCES pricing_vendor(vendor_id),
                    payload TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pricing_alias (
                    vendor_id TEXT NOT NULL REFERENCES pricing_vendor(vendor_id),
                    alias_key TEXT NOT NULL, model_id TEXT NOT NULL REFERENCES pricing_model(model_id),
                    PRIMARY KEY(vendor_id,alias_key));
                CREATE TABLE IF NOT EXISTS pricing_official (
                    official_price_id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL REFERENCES pricing_model(model_id),
                    billing_item TEXT, payload TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pricing_supplier (
                    supplier_id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
                    provider_type TEXT NOT NULL DEFAULT 'unknown', revision INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS pricing_channel (
                    channel_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    supplier_id TEXT NOT NULL REFERENCES pricing_supplier(supplier_id),
                    model_id TEXT NOT NULL REFERENCES pricing_model(model_id),
                    supplier_model_id TEXT, channel_key TEXT NOT NULL,
                    UNIQUE(supplier_id,model_id,channel_key));
                CREATE TABLE IF NOT EXISTS pricing_quote (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    model_id TEXT NOT NULL REFERENCES pricing_model(model_id),
                    billing_item TEXT,
                    supplier_id TEXT REFERENCES pricing_supplier(supplier_id),
                    channel_id INTEGER REFERENCES pricing_channel(channel_id),
                    payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                    revision INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS ix_pricing_quote_model ON pricing_quote(model_id,billing_item,status);
                CREATE TABLE IF NOT EXISTS pricing_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
                    action TEXT NOT NULL, actor TEXT NOT NULL, old_value TEXT, new_value TEXT,
                    created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pricing_context (
                    id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL,
                    payload TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pricing_context_history (
                    revision INTEGER PRIMARY KEY, payload TEXT NOT NULL,
                    actor TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pricing_import (
                    import_id TEXT PRIMARY KEY, payload_sha256 TEXT NOT NULL,
                    result TEXT NOT NULL, created_at TEXT NOT NULL);
            """)
            self._conn.execute("PRAGMA foreign_keys=OFF")
            try:
                with self._transaction():
                    self._migrate_nullable_schema()
                    self._migrate_discount_precision()
                    self._migrate_pricing_conditions()
                    self._conn.execute('CREATE TABLE IF NOT EXISTS pricing_supplier_alias (\n                    alias_key TEXT PRIMARY KEY, alias TEXT NOT NULL,\n                    supplier_id TEXT NOT NULL REFERENCES pricing_supplier(supplier_id))')
                    self._conn.execute('CREATE TABLE IF NOT EXISTS pricing_supplier_mapping (\n                    supplier_id TEXT NOT NULL REFERENCES pricing_supplier(supplier_id),\n                    source_scope TEXT NOT NULL, entry_key TEXT NOT NULL,\n                    quote_id INTEGER NOT NULL REFERENCES pricing_quote(id) ON DELETE CASCADE,\n                    identity_hash TEXT NOT NULL,\n                    PRIMARY KEY(supplier_id,source_scope,entry_key))')
                    self._conn.execute('CREATE TABLE IF NOT EXISTS pricing_supplier_update (\n                    import_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,\n                    metadata TEXT NOT NULL, created_at TEXT NOT NULL)')
                    initialize_supplier_profiles(self._conn)
                    initialize_supplier_baselines(self._conn)
                    initial = self._default_context()
                    self._conn.execute("INSERT OR IGNORE INTO pricing_context VALUES (1,1,?,?)",
                                       (_json(initial), "1970-01-01T00:00:00+00:00"))
                    self._conn.execute("INSERT OR IGNORE INTO pricing_context_history VALUES (1,?,?,?)",
                                       (_json(initial), "system", "1970-01-01T00:00:00+00:00"))
            finally:
                self._conn.execute("PRAGMA foreign_keys=ON")

    def _migrate_nullable_schema(self) -> None:
        """Atomically relax nullable fields and remove the redundant physical identity."""
        import re
        columns = {
            "pricing_model": {"vendor_id"},
            "pricing_official": {"billing_item"},
            "pricing_quote": {"billing_item", "supplier_id", "channel_id"},
        }
        needed = {table: names for table, names in columns.items()
                  if any(row["name"] in names and row["notnull"]
                         for row in self._conn.execute(f"PRAGMA table_info({table})"))}
        remove_physical = any(row["name"] == "physical_key" for row in
                              self._conn.execute("PRAGMA table_info(pricing_model)"))
        if remove_physical:
            needed.setdefault("pricing_model", set())
        payload_cleanup = [(row["model_id"], json.loads(row["payload"])) for row in
                           self._conn.execute("SELECT model_id,payload FROM pricing_model")
                           if "physical_key" in json.loads(row["payload"])]
        if not needed and not payload_cleanup:
            self._conn.execute("CREATE INDEX IF NOT EXISTS ix_pricing_official_unclassified "
                               "ON pricing_official(model_id) WHERE billing_item IS NULL")
            return
        manage_foreign_keys = not self._conn.in_transaction
        if manage_foreign_keys:
            self._conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with self._transaction():
                for table, names in needed.items():
                    original_sql = self._conn.execute(
                        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                    ).fetchone()["sql"]
                    auxiliary = [row["sql"] for row in self._conn.execute(
                        "SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') "
                        "AND sql IS NOT NULL", (table,))]
                    sequence = self._conn.execute(
                        "SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
                    temporary = "__nullable_" + table
                    sql = re.sub(r"(CREATE TABLE(?: IF NOT EXISTS)?\s+)[^\s(]+",
                                 lambda match: match.group(1) + temporary, original_sql, count=1,
                                 flags=re.IGNORECASE)
                    if table == "pricing_model" and remove_physical:
                        # Keep all other column/table declarations, including custom constraints.
                        prefix, body = sql.split("(", 1)
                        body, suffix = body.rsplit(")", 1)
                        parts, start, depth, quoted = [], 0, 0, None
                        for index, character in enumerate(body):
                            if quoted:
                                if character == quoted:
                                    quoted = None
                            elif character in ("'", '"', "`"):
                                quoted = character
                            elif character == "(":
                                depth += 1
                            elif character == ")":
                                depth -= 1
                            elif character == "," and depth == 0:
                                parts.append(body[start:index])
                                start = index + 1
                        parts.append(body[start:])
                        parts = [part for part in parts if not re.search(r"\bphysical_key\b", part, re.I)]
                        sql = prefix + "(" + ",".join(parts) + ")" + suffix
                        auxiliary = [statement for statement in auxiliary
                                     if not re.search(r"\bphysical_key\b", statement, re.I)]
                    for name in names:
                        sql = re.sub(r"\b" + re.escape(name) + r"\s+(TEXT|INTEGER)\s+NOT NULL",
                                     lambda match: name + " " + match.group(1), sql, flags=re.IGNORECASE)
                    self._conn.execute(sql)
                    fields = [row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")
                              if not (table == "pricing_model" and remove_physical and row["name"] == "physical_key")]
                    target = ",".join('"' + field + '"' for field in fields)
                    selected = [
                        "CASE WHEN json_type(payload,'$.physical_key') IS NOT NULL "
                        "THEN json_remove(payload,'$.physical_key') ELSE payload END"
                        if table == "pricing_model" and remove_physical and field == "payload"
                        else '"' + field + '"' for field in fields]
                    self._conn.execute(f"INSERT INTO {temporary} ({target}) SELECT {','.join(selected)} FROM {table}")
                    self._conn.execute(f"DROP TABLE {table}")
                    self._conn.execute(f"ALTER TABLE {temporary} RENAME TO {table}")
                    for statement in auxiliary:
                        self._conn.execute(statement)
                    if sequence is not None:
                        self._conn.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?",
                                           (sequence["seq"], table))
                if not remove_physical:
                    for model_id, payload in payload_cleanup:
                        payload.pop("physical_key")
                        self._conn.execute("UPDATE pricing_model SET payload=? WHERE model_id=?",
                                           (_json(payload), model_id))
                # Old releases indexed model IDs, names and physical keys automatically.
                # Only payload.aliases is an explicit equivalence assertion in the new model.
                self._conn.execute("DELETE FROM pricing_alias")
                for row in self._conn.execute("SELECT model_id,vendor_id,payload FROM pricing_model"):
                    if row["vendor_id"] is not None:
                        for alias in {identity_key(value) for value in json.loads(row["payload"]).get("aliases", [])} - {""}:
                            self._conn.execute("INSERT INTO pricing_alias VALUES (?,?,?)",
                                               (row["vendor_id"], alias, row["model_id"]))
                self._conn.execute("CREATE INDEX IF NOT EXISTS ix_pricing_official_unclassified "
                                   "ON pricing_official(model_id) WHERE billing_item IS NULL")
                if self._conn.execute("PRAGMA foreign_key_check").fetchall():
                    raise RuntimeError("schema migration would violate existing foreign keys")
        finally:
            if manage_foreign_keys:
                self._conn.execute("PRAGMA foreign_keys=ON")

    def _migrate_discount_precision(self) -> None:
        """Normalize only stored source ratios, with atomic revisioned before/after audits."""
        with self._transaction():
            rows = self._conn.execute("SELECT * FROM pricing_quote ORDER BY id").fetchall()
            for row in rows:
                previous = json.loads(row["payload"])
                if previous.get("discount_rate") is None:
                    continue
                normalized = normalize_discount_rate(previous["discount_rate"])
                value = None if normalized is None else format(normalized, "f")
                if previous["discount_rate"] == value:
                    continue
                current = {**previous, "discount_rate": value}
                now = _utc_text()
                metadata = {key: row[key] for key in ("id", "model_id", "billing_item", "supplier_id",
                            "channel_id", "status", "revision", "created_at", "updated_at")}
                old = {**previous, **metadata}
                new = {**current, **metadata, "revision": row["revision"] + 1, "updated_at": now}
                self._conn.execute(
                    "UPDATE pricing_quote SET payload=?,revision=revision+1,updated_at=? WHERE id=?",
                    (_json(current), now, row["id"]))
                self._audit("quote", row["id"], "updated", "migration:discount-2dp", old, new)

    def _migrate_pricing_conditions(self) -> None:
        """Migrate IDs and explicit legacy scope in one revisioned, fully rollbackable transaction."""
        fields = {row["name"] for row in self._conn.execute("PRAGMA table_info(pricing_official)")}
        rebuild = "official_price_id" not in fields
        auxiliary = [row["sql"] for row in self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='pricing_official' AND type IN ('index','trigger') "
            "AND sql IS NOT NULL") if row["sql"] and "ix_pricing_official_unclassified" not in row["sql"]]
        official = self._conn.execute("SELECT rowid AS migration_rowid,* FROM pricing_official").fetchall()
        quotes = self._conn.execute("SELECT * FROM pricing_quote ORDER BY id").fetchall()
        with self._transaction():
            if rebuild:
                self._conn.execute("""CREATE TABLE __conditions_pricing_official (
                    official_price_id TEXT PRIMARY KEY,
                    model_id TEXT NOT NULL REFERENCES pricing_model(model_id),
                    billing_item TEXT, payload TEXT NOT NULL, revision INTEGER NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            for row in official:
                previous = json.loads(row["payload"])
                identifier = (row["official_price_id"] if not rebuild else
                    "op-" + hashlib.sha256(_json([row["model_id"], row["billing_item"],
                    row["created_at"], row["migration_rowid"]]).encode()).hexdigest()[:32])
                current = dict(previous)
                current.setdefault("official_price_id", identifier)
                inferred_metric, inferred_conditions = legacy_official_semantics(row["billing_item"])
                current.setdefault("billing_metric", inferred_metric)
                current.setdefault("pricing_conditions", inferred_conditions)
                current.setdefault("conditions", {})
                if previous.get("conditions") and "pricing_conditions" not in previous:
                    current["pricing_conditions"] = {**inferred_conditions, "raw": previous["conditions"]}
                changed = current != previous
                now = _utc_text() if changed else row["updated_at"]
                revision = row["revision"] + int(changed)
                metadata = {key: row[key] for key in ("revision", "created_at", "updated_at")}
                if rebuild:
                    self._conn.execute("INSERT INTO __conditions_pricing_official VALUES (?,?,?,?,?,?,?)",
                        (identifier, row["model_id"], row["billing_item"], _json(current),
                         revision, row["created_at"], now))
                elif changed:
                    self._conn.execute("UPDATE pricing_official SET payload=?,revision=?,updated_at=? WHERE official_price_id=?",
                                       (_json(current), revision, now, identifier))
                if changed:
                    self._audit("official_price", identifier, "updated", "migration:pricing-conditions",
                        {**previous, **metadata}, {**current, **metadata, "revision": revision, "updated_at": now})
            if rebuild:
                # No other table references this formerly composite primary key.
                self._conn.execute("DROP TABLE pricing_official")
                self._conn.execute("ALTER TABLE __conditions_pricing_official RENAME TO pricing_official")
                for statement in auxiliary:
                    self._conn.execute(statement)
            self._conn.execute("CREATE INDEX IF NOT EXISTS ix_pricing_official_model ON pricing_official(model_id,billing_item)")
            for row in quotes:
                previous = json.loads(row["payload"])
                current = dict(previous)
                current.setdefault("billing_metric", legacy_metric(row["billing_item"]))
                current.setdefault("pricing_conditions", {})
                current.setdefault("discount_scope", legacy_quote_scope(previous))
                current.setdefault("discount_items", [])
                if current == previous:
                    continue
                now = _utc_text()
                metadata = {key: row[key] for key in ("id", "model_id", "billing_item", "supplier_id",
                            "channel_id", "status", "revision", "created_at", "updated_at")}
                self._conn.execute("UPDATE pricing_quote SET payload=?,revision=revision+1,updated_at=? WHERE id=?",
                                   (_json(current), now, row["id"]))
                self._audit("quote", row["id"], "updated", "migration:pricing-conditions",
                            {**previous, **metadata},
                            {**current, **metadata, "revision": row["revision"] + 1, "updated_at": now})
            if self._conn.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("pricing-condition migration would violate foreign keys")

    @staticmethod
    def _default_context() -> dict[str, Any]:
        return {"fx_rates": [{"currency": "CNY", "cny_per_unit": "1",
            "source": "CNY identity", "source_url": None,
            "observed_at": "1970-01-01T00:00:00+00:00", "fetched_at": None, "expires_at": None}],
            "token_equivalences": []}

    def pricing_context(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM pricing_context WHERE id=1").fetchone()
            data = json.loads(row["payload"])
            return {**data, "revision": row["revision"], "updated_at": row["updated_at"]}

    def pricing_context_history(self) -> list[dict[str, Any]]:
        with self._lock:
            return [{**json.loads(r["payload"]), "revision": r["revision"],
                     "actor": r["actor"], "updated_at": r["created_at"]}
                    for r in self._conn.execute("SELECT * FROM pricing_context_history ORDER BY revision DESC")]

    def set_pricing_context(self, *, fx_rates: list[dict], token_equivalences: list[dict],
                            expected_revision: int, actor: str = "workspace-ui") -> dict[str, Any]:
        rates = [ExchangeRate.model_validate(v).model_dump(mode="json") for v in fx_rates]
        factors = [TokenEquivalence.model_validate(v).model_dump(mode="json") for v in token_equivalences]
        if len({r["currency"] for r in rates}) != len(rates):
            raise ValueError("each currency may have only one current FX rate")
        if not any(r["currency"] == "CNY" for r in rates):
            rates.append(self._default_context()["fx_rates"][0])
        for factor in factors:
            factor["unit"] = canonical_unit(factor["unit"])
            factor["billing_item"] = factor["billing_item"].casefold()
            if factor["unit"] == "token":
                raise ValueError("token identity is fixed at one; token equivalence estimates are for other units")
            if factor["unit"] not in {"second", "image"}:
                raise ValueError("token equivalence unit must be second or image from the pricing-unit dictionary")
        keys = [(f["model_id"], f["billing_item"], f["unit"]) for f in factors]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate model/billing_item/unit token equivalence")
        payload = {"fx_rates": sorted(rates, key=lambda r: r["currency"]),
                   "token_equivalences": sorted(factors, key=lambda f: (f["model_id"], f["billing_item"], f["unit"]))}
        with self._lock, self._transaction():
            old = self.pricing_context()
            if old["revision"] != expected_revision:
                raise RevisionConflictError(f"pricing context is at revision {old['revision']}")
            for factor in factors:
                self._require_model(factor["model_id"])
            now, revision = _utc_text(), old["revision"] + 1
            self._conn.execute("UPDATE pricing_context SET payload=?,revision=?,updated_at=? WHERE id=1",
                               (_json(payload), revision, now))
            self._conn.execute("INSERT INTO pricing_context_history VALUES (?,?,?,?)",
                               (revision, _json(payload), actor.strip() or "unknown", now))
            result = self.pricing_context()
            self._audit("pricing_context", 1, "updated", actor, old, result)
            return result

    @contextmanager
    def _valuation(self) -> Iterator[None]:
        previous = self._valuation_at
        if previous is None:
            self._valuation_at = datetime.now(timezone.utc)
        try:
            yield
        finally:
            self._valuation_at = previous

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        nested = self._conn.in_transaction
        savepoint = "workspace_" + uuid4().hex
        self._conn.execute("SAVEPOINT " + savepoint if nested else "BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            if nested:
                self._conn.execute("ROLLBACK TO SAVEPOINT " + savepoint)
                self._conn.execute("RELEASE SAVEPOINT " + savepoint)
            else:
                self._conn.rollback()
            raise
        else:
            if nested:
                self._conn.execute("RELEASE SAVEPOINT " + savepoint)
            else:
                self._conn.commit()

    def _audit(self, kind: str, key: Any, action: str, actor: str,
               old: dict | None, new: dict | None) -> None:
        self._conn.execute(
            "INSERT INTO pricing_audit(entity_type,entity_id,action,actor,old_value,new_value,created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (kind, str(key), action, actor.strip() or "unknown",
             None if old is None else _json(old), None if new is None else _json(new), _utc_text()),
        )

    @staticmethod
    def _catalog_row(row: sqlite3.Row) -> dict[str, Any]:
        result = json.loads(row["payload"])
        result.update(revision=row["revision"], created_at=row["created_at"], updated_at=row["updated_at"])
        return result

    @staticmethod
    def _check_revision(old: sqlite3.Row | None, expected: int | None, label: str) -> None:
        if old is None:
            if expected is not None:
                raise RevisionConflictError(f"{label} does not yet exist")
        elif expected is None or int(old["revision"]) != expected:
            raise RevisionConflictError(f"{label} is at revision {old['revision']}; expected_revision required")

    def _put_model(self, item: ModelCreate, actor: str) -> None:
        old = self._conn.execute("SELECT * FROM pricing_model WHERE model_id=?", (item.model_id,)).fetchone()
        data = item.model_dump(mode="json", exclude={"expected_revision"})
        vendor_id = (identity_key(item.vendor) or None) if item.vendor is not None else None
        # Only explicit aliases assert equivalence; display names never identify a model.
        aliases = {identity_key(value) for value in item.aliases} - {""}
        if old and json.loads(old["payload"]) == data:
            if item.expected_revision is not None and item.expected_revision != old["revision"]:
                raise RevisionConflictError(f"model {item.model_id} is at revision {old['revision']}")
            return
        self._check_revision(old, item.expected_revision, f"model {item.model_id}")
        if old:
            previous = json.loads(old["payload"])
            data["aliases"] = list(dict.fromkeys([*previous.get("aliases", []), *data["aliases"]]))
            aliases.update(identity_key(value) for value in data["aliases"])
        if vendor_id is not None:
            # Explicit aliases cannot claim another existing model ID, in either direction.
            claimed_id = self._conn.execute(
                "SELECT model_id FROM pricing_alias WHERE vendor_id=? AND alias_key=? AND model_id<>?",
                (vendor_id, identity_key(item.model_id), item.model_id)).fetchone()
            if claimed_id:
                raise ValueError(f"model_id is already an alias for model_id {claimed_id['model_id']}; do not split")
            other_ids = [row["model_id"] for row in self._conn.execute(
                "SELECT model_id FROM pricing_model WHERE vendor_id=? AND model_id<>?",
                (vendor_id, item.model_id))]
            for other_id in other_ids:
                if identity_key(other_id) in aliases:
                    raise ValueError(f"alias is already assigned to model_id {other_id}; do not split")
            for alias in aliases:
                duplicate = self._conn.execute(
                    "SELECT model_id FROM pricing_alias WHERE vendor_id=? AND alias_key=? AND model_id<>?",
                    (vendor_id, alias, item.model_id)).fetchone()
                if duplicate:
                    raise ValueError(f"alias is already assigned to model_id {duplicate['model_id']}; do not split")
            self._conn.execute("INSERT OR IGNORE INTO pricing_vendor(vendor_id,name) VALUES (?,?)",
                               (vendor_id, item.vendor))
        now = _utc_text()
        if old:
            self._conn.execute("DELETE FROM pricing_alias WHERE model_id=?", (item.model_id,))
            self._conn.execute(
                "UPDATE pricing_model SET vendor_id=?,payload=?,revision=revision+1,updated_at=? WHERE model_id=?",
                (vendor_id, _json(data), now, item.model_id))
        else:
            self._conn.execute("INSERT INTO pricing_model VALUES (?,?,?,1,?,?)",
                               (item.model_id, vendor_id, _json(data), now, now))
        # Unknown vendor aliases stay in source metadata, without a fabricated namespace.
        if vendor_id is not None:
            for alias in aliases - {""}:
                self._conn.execute("INSERT OR IGNORE INTO pricing_alias VALUES (?,?,?)",
                                   (vendor_id, alias, item.model_id))
        new = self._conn.execute("SELECT * FROM pricing_model WHERE model_id=?", (item.model_id,)).fetchone()
        self._audit("model", item.model_id, "updated" if old else "added", actor,
                    self._catalog_row(old) if old else None, self._catalog_row(new))

    def _ensure_model(self, model_id: str, actor: str) -> None:
        if self._conn.execute("SELECT 1 FROM pricing_model WHERE model_id=?", (model_id,)).fetchone() is None:
            self._put_model(ModelCreate(model_id=model_id), actor)

    @staticmethod
    def _official_payload(item: OfficialPriceCreate) -> dict[str, Any]:
        data = canonical_price_payload(item.model_dump(mode="json", exclude={"expected_revision"}))
        data["pricing_conditions"] = compact_conditions(data.get("pricing_conditions"))
        inferred_metric, inferred_conditions = legacy_official_semantics(item.billing_item)
        if data.get("billing_metric") is None:
            data["billing_metric"] = inferred_metric
        if any(key in data["pricing_conditions"] and data["pricing_conditions"][key] != value
               for key, value in inferred_conditions.items()):
            raise ValueError("explicit pricing_conditions conflict with the original billing_item")
        data["pricing_conditions"] = {**inferred_conditions, **data["pricing_conditions"]}
        if item.conditions and "pricing_conditions" not in item.model_fields_set:
            data["pricing_conditions"]["raw"] = dict(item.conditions)
        return data

    def _put_official(self, item: OfficialPriceCreate, actor: str, skip_quote_ids: Sequence[int] = ()) -> None:
        self._ensure_model(item.model_id, actor)
        data = self._official_payload(item)
        old = None
        if item.official_price_id is not None:
            old = self._conn.execute("SELECT * FROM pricing_official WHERE official_price_id=?",
                                     (item.official_price_id,)).fetchone()
        else:
            candidates = self._conn.execute(
                "SELECT * FROM pricing_official WHERE model_id=? AND billing_item IS ?",
                (item.model_id, item.billing_item)).fetchall()
            if item.expected_revision is not None:
                if len(candidates) > 1:
                    raise RevisionConflictError("multiple official scenarios; official_price_id required")
                old = candidates[0] if candidates else None
            else:
                same = [row for row in candidates if scenario_key(json.loads(row["payload"])) == scenario_key(data)]
                if len(same) > 1:
                    raise RevisionConflictError("conflicting official scenarios; official_price_id required")
                old = same[0] if same else None
        identifier = old["official_price_id"] if old else item.official_price_id or "op-" + uuid4().hex
        data["official_price_id"] = identifier
        if old and json.loads(old["payload"]) == data:
            if item.expected_revision is not None and item.expected_revision != old["revision"]:
                raise RevisionConflictError("official price revision changed")
            return
        self._check_revision(old, item.expected_revision, f"official {identifier}")
        # New scenarios may share a metric. Identical structured scenarios are never silently overwritten.
        conflicts = [row for row in self._conn.execute("SELECT * FROM pricing_official WHERE model_id=?",
                     (item.model_id,)) if row["official_price_id"] != identifier and
                     scenario_key(json.loads(row["payload"])) == scenario_key(data)]
        if conflicts:
            raise RevisionConflictError("official scenario already exists; update its official_price_id and revision")
        now = _utc_text()
        if old:
            self._conn.execute(
                "UPDATE pricing_official SET model_id=?,billing_item=?,payload=?,revision=revision+1,updated_at=?"
                " WHERE official_price_id=?",
                (item.model_id, item.billing_item, _json(data), now, identifier))
        else:
            self._conn.execute("INSERT INTO pricing_official VALUES (?,?,?,?,1,?,?)",
                               (identifier, item.model_id, item.billing_item, _json(data), now, now))
        new = self._conn.execute("SELECT * FROM pricing_official WHERE official_price_id=?", (identifier,)).fetchone()
        self._audit("official_price", identifier, "updated" if old else "added", actor,
                    self._catalog_row(old) if old else None, self._catalog_row(new))

    def _require_model(self, model_id: str) -> sqlite3.Row:
        row = self._conn.execute("SELECT * FROM pricing_model WHERE model_id=?", (model_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown model_id {model_id}")
        return row

    def _official_prices(self, model_id: str) -> list[dict[str, Any]]:
        return [expand_price_payload(self._catalog_row(row), allow_legacy=True)
                for row in self._conn.execute("SELECT * FROM pricing_official WHERE model_id=? ORDER BY official_price_id",
                                              (model_id,))]

    def _official(self, model_id: str, billing_item: str | None) -> dict[str, Any] | None:
        candidates = [item for item in self._official_prices(model_id) if item.get("billing_item") == billing_item]
        if len(candidates) > 1:
            raise RevisionConflictError("multiple official scenarios; official_price_id required")
        return candidates[0] if candidates else None

    @staticmethod
    def _supplier_key(name: str) -> str:
        # Only spelling/case whitespace normalization; punctuation is meaningful.
        return " ".join(name.split()).casefold()

    @staticmethod
    def _confirmed_supplier_name(name: str) -> str:
        name = " ".join(name.split())
        if not name or name.casefold() in {
            "unknown", "unknown supplier", "n/a", "none", "null",
            "\u672a\u77e5", "\u672a\u77e5\u4f9b\u5e94\u5546", "\u5f85\u786e\u8ba4", "\u5f85\u8865\u5145"}:
            raise ValueError("supplier must be a confirmed supplier name, not a placeholder")
        return name

    def list_suppliers(self) -> list[dict[str, Any]]:
        with self._lock:
            items = [dict(row) for row in self._conn.execute(
                "SELECT supplier_id,name,provider_type,revision FROM pricing_supplier ORDER BY name")]
            for item in items:
                item["aliases"] = [row["alias"] for row in self._conn.execute(
                    "SELECT alias FROM pricing_supplier_alias WHERE supplier_id=? ORDER BY alias",
                    (item["supplier_id"],))]
            return items

    def _find_supplier_name(self, name: str) -> sqlite3.Row | None:
        key = self._supplier_key(name)
        row = self._conn.execute(
            "SELECT s.* FROM pricing_supplier s LEFT JOIN pricing_supplier_alias a "
            "ON a.supplier_id=s.supplier_id WHERE s.name_key=? OR a.alias_key=?", (key, key)).fetchone()
        if row is not None:
            return row
        # Preserve stable IDs from older releases without their lossy punctuation key.
        return next((row for row in self._conn.execute("SELECT * FROM pricing_supplier")
                     if self._supplier_key(row["name"]) == key), None)

    def confirm_supplier(self, request: SupplierWrite | dict, actor: str = "workspace-ui") -> dict[str, Any]:
        request = SupplierWrite.model_validate(request)
        name = self._confirmed_supplier_name(request.name)
        with self._lock, self._transaction():
            old = (self._conn.execute("SELECT * FROM pricing_supplier WHERE supplier_id=?",
                                     (request.supplier_id,)).fetchone() if request.supplier_id else
                   self._find_supplier_name(name))
            if request.supplier_id and old is None:
                raise ValueError("supplier_id was not found")
            if old is not None and request.supplier_id and request.expected_revision != old["revision"]:
                raise RevisionConflictError("supplier changed; refresh before renaming or confirming aliases")
            if old is not None and request.supplier_id is None:
                # Confirming an existing name/alias selects that identity; a rename
                # always names its stable ID and expected revision explicitly.
                name = old["name"]
                existing = next(item for item in self.list_suppliers() if item["supplier_id"] == old["supplier_id"])
                additions = {self._supplier_key(value) for value in request.aliases} - {
                    self._supplier_key(name), *(self._supplier_key(value) for value in existing["aliases"])}
                provider_change = request.provider_type is not None and request.provider_type != old["provider_type"]
                if not additions and not provider_change:
                    return existing
                if request.expected_revision != old["revision"]:
                    raise RevisionConflictError("supplier changed; confirm its current revision before adding aliases")
            supplier_id = old["supplier_id"] if old is not None else "supplier-" + uuid4().hex
            aliases = list(request.aliases)
            if old is not None and self._supplier_key(old["name"]) != self._supplier_key(name):
                aliases.append(old["name"])
            for value in [name, *aliases]:
                found = self._find_supplier_name(self._confirmed_supplier_name(value))
                if found is not None and found["supplier_id"] != supplier_id:
                    raise ValueError("supplier name or alias already belongs to another supplier; choose an explicit identity")
            before = next((item for item in self.list_suppliers() if item["supplier_id"] == supplier_id), None)
            if old is None:
                self._conn.execute(
                    "INSERT INTO pricing_supplier(supplier_id,name,name_key,provider_type) VALUES (?,?,?,?)",
                    (supplier_id, name, self._supplier_key(name), request.provider_type or "unknown"))
            else:
                self._conn.execute(
                    "UPDATE pricing_supplier SET name=?,name_key=?,provider_type=?,revision=revision+1 WHERE supplier_id=?",
                    (name, self._supplier_key(name), request.provider_type or old["provider_type"], supplier_id))
            for alias in aliases:
                key = self._supplier_key(alias)
                if key != self._supplier_key(name):
                    self._conn.execute(
                        "INSERT OR IGNORE INTO pricing_supplier_alias(alias_key,alias,supplier_id) VALUES (?,?,?)",
                        (key, alias, supplier_id))
            after = next(item for item in self.list_suppliers() if item["supplier_id"] == supplier_id)
            self._audit("supplier", supplier_id, "added" if old is None else "updated", actor, before, after)
            return after

    def _prepare_quote(self, item: QuoteCreate, *, validate: bool = True,
                       actor: str = "manual/cli") -> tuple[dict, str, int]:
        # Draft schemas allow missing supplier so a job can pause for clarification.
        # Every formal write, including patches and direct Store calls, crosses here.
        supplier = None
        if item.supplier_id:
            supplier = self._conn.execute("SELECT * FROM pricing_supplier WHERE supplier_id=?",
                                          (item.supplier_id,)).fetchone()
            if supplier is None:
                raise ValueError("supplier_id must reference a confirmed supplier")
            if item.supplier:
                found = self._find_supplier_name(item.supplier)
                if found is None or found["supplier_id"] != item.supplier_id:
                    raise ValueError("supplier name conflicts with supplier_id")
        elif item.supplier:
            name = self._confirmed_supplier_name(item.supplier)
            supplier = self._find_supplier_name(name)
            if supplier is None:
                supplier_id = "supplier-" + uuid4().hex
                self._conn.execute(
                    "INSERT INTO pricing_supplier(supplier_id,name,name_key,provider_type) VALUES (?,?,?,?)",
                    (supplier_id, name, self._supplier_key(name), item.provider_type or "unknown"))
                supplier = self._conn.execute("SELECT * FROM pricing_supplier WHERE supplier_id=?",
                                              (supplier_id,)).fetchone()
        if supplier is None:
            raise ValueError("supplier is required for supplier quotes; complete the draft before confirming")
        self._confirmed_supplier_name(supplier["name"])
        self._ensure_model(item.model_id, actor)
        data = canonical_price_payload(item.model_dump(mode="json"))
        data["pricing_conditions"] = compact_conditions(data.get("pricing_conditions"))
        if data.get("billing_metric") is None:
            data["billing_metric"] = legacy_metric(item.billing_item)
        discount = normalize_discount_rate(item.discount_rate)
        data["discount_rate"] = None if discount is None else format(discount, "f")
        issue = pricing_basis_issue(data, self._official_prices(item.model_id))
        if issue and not getattr(self, "_allow_pricing_conflicts", False):
            raise ValueError(issue["message"])
        supplier_id = supplier["supplier_id"]
        data.update(supplier=supplier["name"], supplier_id=supplier_id)
        channel_key = item.supplier_model_id or ""
        self._conn.execute(
            "INSERT OR IGNORE INTO pricing_channel(supplier_id,model_id,supplier_model_id,channel_key)"
            " VALUES (?,?,?,?)", (supplier_id, item.model_id, item.supplier_model_id, channel_key))
        channel = self._conn.execute(
            "SELECT channel_id FROM pricing_channel WHERE supplier_id=? AND model_id=? AND channel_key=?",
            (supplier_id, item.model_id, channel_key)).fetchone()
        return data, supplier_id, int(channel["channel_id"])

    def _effective_discount_rules(self, row: sqlite3.Row, official: list[dict],
                                  matched: list[dict]) -> list[dict[str, Any]]:
        """Resolve source rule precedence for all pages without deriving any price."""
        if row["supplier_id"] is None:
            rows = [row]
        else:
            rows = self._conn.execute(
                "SELECT * FROM pricing_quote WHERE model_id=? AND supplier_id IS ? AND channel_id IS ? "
                "AND status='active' ORDER BY id",
                (row["model_id"], row["supplier_id"], row["channel_id"])).fetchall()
        eligible: dict[str, list[tuple[int, sqlite3.Row, dict]]] = {}
        source_tags = set(json.loads(row["payload"]).get("tags") or [])
        for candidate in rows:
            value = json.loads(candidate["payload"])
            if set(value.get("tags") or []) != source_tags:
                continue
            if value.get("discount_rate") is None and value.get("amount") is None:
                continue
            applicable, _ = match_official_prices(value, official)
            rank = 0 if value.get("discount_scope") == "model" else 1
            for price in applicable:
                eligible.setdefault(price["official_price_id"], []).append((rank, candidate, value))
        result = []
        for price in matched:
            identifier = price["official_price_id"]
            candidates = eligible.get(identifier, [])
            common = {"official_price_id": identifier, "quote_id": None, "discount_rate": None,
                      "discount_scope": "unknown", "candidate_quote_ids": []}
            if not candidates:
                result.append({**common, "reason": "no_discount_rule"})
                continue
            priority = max(item[0] for item in candidates)
            selected = [item for item in candidates if item[0] == priority]
            quote_ids = sorted(item[1]["id"] for item in selected)
            def signature(value: dict) -> str:
                raw = expand_price_payload(value, allow_legacy=True)
                return _json({key: raw.get(key) for key in (
                    "amount", "pricing_unit_id", "currency", "unit", "unit_quantity",
                    "discount_rate", "pricing_basis")})
            signatures = {signature(item[2]) for item in selected}
            if len(signatures) != 1:
                result.append({**common, "reason": "conflicting_discount_rules",
                               "candidate_quote_ids": quote_ids})
                continue
            _, chosen, value = selected[0]
            discount = normalize_discount_rate(value.get("discount_rate"))
            rate = None if discount is None else format(discount, "f")
            result.append({**common, "quote_id": chosen["id"], "discount_rate": rate,
                "discount_scope": value.get("discount_scope", "unknown"),
                "reason": ("item_amount_override" if value.get("amount") is not None else
                           "item_override" if any(item[0] == 0 for item in candidates) else "explicit_item")
                          if priority else "model_default", "candidate_quote_ids": quote_ids})
        return result

    def _response(self, row: sqlite3.Row) -> QuoteResponse:
        raw = expand_price_payload(json.loads(row["payload"]), allow_legacy=True)
        if row["supplier_id"] is not None:
            supplier = self._conn.execute("SELECT name FROM pricing_supplier WHERE supplier_id=?",
                                          (row["supplier_id"],)).fetchone()
            if supplier:
                raw["supplier"] = supplier["name"]
        official = self._official_prices(row["model_id"])
        data = quote_projection(raw, official, self.pricing_context(), at=self._valuation_at)
        issue = pricing_basis_issue(raw, official)
        if issue:
            data["issues"].append(issue)
        data["effective_discount_rules"] = self._effective_discount_rules(row, official, data["official_prices"])
        model = json.loads(self._require_model(row["model_id"])["payload"])
        data.update(id=row["id"], model_id=row["model_id"], billing_item=row["billing_item"],
                    supplier_id=row["supplier_id"], channel_id=row["channel_id"], vendor=model.get("vendor"),
                    model_name=model.get("model_name"), manufacturer=model.get("vendor"), provider=data.get("supplier"),
                    metric=data.get("billing_metric") or row["billing_item"], model_version=None, status=row["status"],
                    revision=row["revision"], created_at=row["created_at"], updated_at=row["updated_at"],
                    official_price=data["official_price"])
        return QuoteResponse.model_validate(data, context={"allow_legacy_unit": True})

    def get_quote(self, quote_id: int) -> QuoteResponse:
        with self._lock:
            row = self._conn.execute("SELECT * FROM pricing_quote WHERE id=?", (quote_id,)).fetchone()
            if row is None:
                raise QuoteNotFoundError(f"quote {quote_id} was not found")
            return self._response(row)

    def _insert(self, item: QuoteCreate, actor: str) -> int:
        data, supplier_id, channel_id = self._prepare_quote(item, actor=actor)
        now = _utc_text()
        cursor = self._conn.execute(
            "INSERT INTO pricing_quote(model_id,billing_item,supplier_id,channel_id,payload,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?)", (item.model_id, item.billing_item, supplier_id, channel_id,
                                        _json(data), now, now),
        )
        quote_id = int(cursor.lastrowid)
        self._audit("quote", quote_id, "added", actor, None, self.get_quote(quote_id).model_dump(mode="json"))
        return quote_id

    def _update(self, quote_id: int, revision: int, changes: dict[str, Any], actor: str,
                *, replace: bool = False) -> int:
        old = self.get_quote(quote_id)
        if old.revision != revision:
            raise RevisionConflictError(f"quote {quote_id} is at revision {old.revision}")
        if old.status != "active":
            raise ValueError(f"quote {quote_id} is archived")
        unknown = set(changes) - set(EDITABLE_FIELDS)
        if unknown:
            raise ValueError(f"unknown quote fields: {', '.join(sorted(unknown))}")
        raw_row = self._conn.execute("SELECT payload FROM pricing_quote WHERE id=?", (quote_id,)).fetchone()
        original_payload = json.loads(raw_row["payload"])
        data = expand_price_payload(original_payload, allow_legacy=True)
        if old.supplier_id and not data.get("supplier_id"):
            data["supplier_id"] = old.supplier_id
        unit_fields = {"pricing_unit_id", "currency", "unit", "unit_quantity"}
        preserve_legacy = data.get("legacy_unit") is not None and not unit_fields.intersection(changes)
        if preserve_legacy:
            for key in (*unit_fields, "legacy_unit"):
                data.pop(key, None)
        if "pricing_unit_id" in changes:
            # An explicit null means clear, not restore the old ID's projected split fields.
            for key in ("currency", "unit", "unit_quantity", "legacy_unit"):
                data.pop(key, None)
            if changes["pricing_unit_id"] is None:
                data.update(currency=None, unit=None, unit_quantity=None)
        elif any(key in changes for key in ("currency", "unit", "unit_quantity")):
            data.pop("pricing_unit_id", None)
            data.pop("legacy_unit", None)
        if "billing_item" in changes and "billing_metric" not in changes and data.get("billing_metric") == legacy_metric(data.get("billing_item")):
            data["billing_metric"] = None
        if "supplier" in changes and "supplier_id" not in changes:
            data["supplier_id"] = None
        if "supplier_id" in changes and "supplier" not in changes:
            data["supplier"] = None
        data.update(changes)
        # Source amount and source discount are independent facts; editing either never clears the other.
        item = QuoteCreate.model_validate(data)
        payload, supplier_id, channel_id = self._prepare_quote(item, actor=actor)
        if preserve_legacy:
            for key in unit_fields:
                payload.pop(key, None)
                if key in original_payload:
                    payload[key] = original_payload[key]
        self._conn.execute(
            "UPDATE pricing_quote SET model_id=?,billing_item=?,supplier_id=?,channel_id=?,payload=?,"
            "revision=revision+1,updated_at=? WHERE id=? AND revision=?",
            (item.model_id, item.billing_item, supplier_id, channel_id, _json(payload), _utc_text(),
             quote_id, revision),
        )
        self._audit("quote", quote_id, "updated", actor, old.model_dump(mode="json"),
                    self.get_quote(quote_id).model_dump(mode="json"))
        return quote_id

    def _delete(self, quote_id: int, revision: int, actor: str) -> int:
        old = self.get_quote(quote_id)
        if old.revision != revision:
            raise RevisionConflictError(f"quote {quote_id} is at revision {old.revision}")
        self._conn.execute("DELETE FROM pricing_quote WHERE id=? AND revision=?", (quote_id, revision))
        self._audit("quote", quote_id, "deleted", actor, old.model_dump(mode="json"), None)
        return quote_id

    def apply_bundle(self, bundle: PricingBundle | dict, actor: str = "manual/cli",
                     import_id: str | None = None, *,
                     expected_catalog_token: str | None = None) -> dict[str, Any]:
        bundle = PricingBundle.model_validate(bundle)
        serialized = bundle.model_dump(mode="json")
        payload_hash = hashlib.sha256(_json(serialized).encode()).hexdigest()
        legacy = json.loads(_json(serialized))
        compatible = True
        new_fields = {"billing_metric", "pricing_conditions", "discount_scope", "discount_items", "official_price_id"}
        for section in ("official_prices", "create"):
            for item in legacy[section]:
                for key in new_fields:
                    value = item.get(key)
                    if value not in (None, [], "unknown") and (key != "pricing_conditions" or compact_conditions(value)):
                        compatible = False
                    item.pop(key, None)
                if section == "official_prices":
                    if item.get("conditions"):
                        compatible = False
                    item.pop("conditions", None)
                if item.get("billing_item") is not None:
                    item["billing_item"] = item["billing_item"].casefold()
        for operation in legacy["update"]:
            if new_fields.intersection(operation["changes"]):
                compatible = False
        legacy_hash = hashlib.sha256(_json(legacy).encode()).hexdigest() if compatible else None
        if import_id is not None:
            import_id = import_id.strip()
            if not import_id or len(import_id) > 255:
                raise ValueError("import_id must contain 1 to 255 characters")
        ids = [v.id for v in [*bundle.update, *bundle.delete]]
        if len(ids) != len(set(ids)):
            raise ValueError("a quote may only be updated/deleted once per bundle")
        model_ids = [v.model_id for v in bundle.models]
        official_ids = [v.official_price_id or (v.model_id, scenario_key(self._official_payload(v)))
                        for v in bundle.official_prices]
        if len(model_ids) != len(set(model_ids)) or len(official_ids) != len(set(official_ids)):
            raise ValueError("duplicate model or official-price operations in bundle")
        with self._lock, self._transaction(), self._valuation():
            if import_id:
                previous = self._conn.execute("SELECT * FROM pricing_import WHERE import_id=?", (import_id,)).fetchone()
                if previous:
                    if previous["payload_sha256"] not in {payload_hash, legacy_hash}:
                        raise IdempotencyConflictError("import_id was already used for another bundle")
                    result = json.loads(previous["result"])
                    # Historical receipts remain byte-for-byte unchanged on disk; only
                    # the returned current-policy representation uses fixed-2 ratios.
                    returned_quotes = [*result.get("items", []),
                                       *(result.get("catalog") or {}).get("quotes", [])]
                    for item in returned_quotes:
                        for key in ("discount_rate", "raw_discount_rate"):
                            if key in item:
                                value = normalize_discount_rate(item[key])
                                item[key] = None if value is None else format(value, "f")
                    result["reused"] = True
                    return result
            base_catalog_token = self.catalog_token()
            if expected_catalog_token is not None and expected_catalog_token != base_catalog_token:
                raise RevisionConflictError("catalog changed after review; refresh the preview and confirm again")
            audit_start = self._conn.execute("SELECT coalesce(max(id),0) FROM pricing_audit").fetchone()[0]
            for item in bundle.models:
                self._put_model(item, actor)
            for item in bundle.official_prices:
                self._put_official(item, actor, skip_quote_ids=ids)
            result_ids = [self._insert(item, actor) for item in bundle.create]
            for item in bundle.update:
                result_ids.append(self._update(item.id, item.expected_revision, item.changes, actor))
            for item in bundle.delete:
                result_ids.append(self._delete(item.id, item.expected_revision, actor))
            audits = self._conn.execute("SELECT * FROM pricing_audit WHERE id>?", (audit_start,)).fetchall()
            changes = {action: len({r["entity_id"] for r in audits if r["entity_type"] == "quote"
                                    and r["action"] == action}) for action in ("added", "updated", "deleted")}
            changes["total"] = len({r["entity_id"] for r in audits if r["entity_type"] == "quote"})
            changes["models"] = len({r["entity_id"] for r in audits if r["entity_type"] == "model"})
            changes["official_prices"] = len({r["entity_id"] for r in audits if r["entity_type"] == "official_price"})
            affected = list(dict.fromkeys([*result_ids, *[int(r["entity_id"]) for r in audits
                                                         if r["entity_type"] == "quote"]]))
            deleted_ids = [item.id for item in bundle.delete]
            result = {"items": [self.get_quote(i).model_dump(mode="json") for i in affected if i not in deleted_ids],
                      "deleted_ids": deleted_ids, "changes": changes, "catalog": self.catalog_snapshot(), "issues": [], "reused": False,
                      "base_catalog_token": base_catalog_token}
            result["issues"] = [{"quote_id": item["id"], **problem}
                                for item in result["items"] for problem in item.get("issues", [])]
            if import_id:
                self._conn.execute("INSERT INTO pricing_import VALUES (?,?,?,?)",
                                   (import_id, payload_hash, _json(result), _utc_text()))
            return result

    def preview_bundle(self, bundle: PricingBundle | dict) -> dict[str, Any]:
        """Clone under lock and evaluate only in RAM; no formal writes or audit effects."""
        clone = WorkspaceStore(":memory:")
        try:
            with self._lock:
                self._conn.backup(clone._conn)
                base_catalog_token = getattr(self, "_snapshot_base_catalog_token", None) or clone.catalog_token()
            clone._allow_pricing_conflicts = True
            result = clone.apply_bundle(bundle, actor="preview")
            result["base_catalog_token"] = base_catalog_token
            return result
        finally:
            clone.close()

    def preview_reconciliation(self, request: SupplierReconciliationRequest | dict) -> dict[str, Any]:
        """Prepare one reviewable bundle without changing business data or receipts."""
        request = SupplierReconciliationRequest.model_validate(request)
        with self._lock:
            snapshot = self.catalog_snapshot()
            base = getattr(self, "_snapshot_base_catalog_token", None) or self.catalog_token()
            review = build_reconciliation(request, snapshot)
            if review["ready"]:
                review["preview"] = self.preview_bundle(review["bundle"])
                pricing_issues = [value for value in review["preview"]["issues"]
                                  if value.get("code") == "pricing_basis_required"]
                if pricing_issues:
                    # An unresolved price basis makes a full parse incomplete for
                    # absence-based deletion. Preserve only explicit deletions.
                    missing_ids = {value["before"]["id"] for value in review["groups"]["delete"]
                                   if value["reason"] == "missing_from_confirmed_complete_source"}
                    if missing_ids:
                        review["groups"]["delete"] = [
                            value for value in review["groups"]["delete"]
                            if value["before"]["id"] not in missing_ids]
                        review["bundle"]["delete"] = [
                            value for value in review["bundle"]["delete"] if value["id"] not in missing_ids]
                        review["preview"] = self.preview_bundle(review["bundle"])
                    review["ready"] = False
                    review["groups"]["needs_confirmation"].extend(
                        {"entry_key": "pricing:" + str(value["quote_id"]), "reason": "pricing_basis_required",
                         "before": None, "after": None, "candidate_ids": [value["quote_id"]], "evidence": value}
                        for value in pricing_issues)
                    review["review_digest"] = digest({"semantic_review": review["review_digest"],
                                                       "pricing_issues": pricing_issues})
            else:
                review["preview"] = None
            review["base_catalog_token"] = base
            return review

    def apply_reconciliation(self, request: SupplierReconciliationRequest | dict,
                             actor: str = "manual/cli", import_id: str | None = None, *,
                             expected_catalog_token: str, expected_review_digest: str) -> dict[str, Any]:
        """Reconcile again under the same write transaction as bundle, mappings and receipt."""
        request = SupplierReconciliationRequest.model_validate(request)
        request_hash = digest(request.model_dump(mode="json"))
        import_id = import_id or "supplier-update:" + uuid4().hex
        if not import_id.strip() or len(import_id) > 255:
            raise ValueError("import_id must contain 1 to 255 characters")
        with self._lock, self._transaction():
            previous = self._conn.execute(
                "SELECT * FROM pricing_supplier_update WHERE import_id=?", (import_id,)).fetchone()
            if previous is not None:
                if previous["request_hash"] != request_hash:
                    raise IdempotencyConflictError("import_id already applied another supplier update")
                receipt = self._conn.execute("SELECT result FROM pricing_import WHERE import_id=?",
                                             (import_id,)).fetchone()
                if receipt is None:
                    raise RuntimeError("supplier update receipt is incomplete")
                result = json.loads(receipt["result"])
                result["reused"] = True
                return result
            if self._conn.execute("SELECT 1 FROM pricing_import WHERE import_id=?", (import_id,)).fetchone():
                raise IdempotencyConflictError("import_id was already used for another operation")
            if self.catalog_token() != expected_catalog_token:
                raise RevisionConflictError("catalog changed after review; reconcile and confirm again")
            review = build_reconciliation(request, self.catalog_snapshot())
            if not review["ready"]:
                reasons = sorted({value["reason"] for value in review["groups"]["needs_confirmation"]})
                raise ValueError("supplier update requires clarification: " + ", ".join(reasons))
            if review["review_digest"] != expected_review_digest:
                raise RevisionConflictError("supplier update changed after review; reconcile and confirm again")
            before_ids = {row["id"] for row in self._conn.execute("SELECT id FROM pricing_quote")}
            result = self.apply_bundle(review["bundle"], actor, import_id,
                                       expected_catalog_token=expected_catalog_token)
            created_ids = [item["id"] for item in result["items"] if item["id"] not in before_ids]
            for mapping in review["mappings"]:
                quote_id = mapping["quote_id"]
                if quote_id is None:
                    quote_id = created_ids[mapping["create_index"]]
                self._conn.execute(
                    "INSERT INTO pricing_supplier_mapping VALUES (?,?,?,?,?) "
                    "ON CONFLICT(supplier_id,source_scope,entry_key) DO UPDATE SET "
                    "quote_id=excluded.quote_id,identity_hash=excluded.identity_hash",
                    (request.supplier_id, request.source_scope, mapping["entry_key"],
                     quote_id, mapping["identity_hash"]))
            metadata = {key: getattr(request, key) for key in (
                "supplier_id", "source_scope", "mode", "file_sha256", "source_version",
                "source_effective_date", "expected_sheets", "covered_sheets", "model_ids", "supplier_model_ids",
                "scope_confirmed", "complete_read", "latest_confirmed", "allow_rollback")}
            metadata["source_effective_date"] = (
                request.source_effective_date.isoformat() if request.source_effective_date else None)
            metadata.update(semantic_digest=review["semantic_digest"], review_digest=review["review_digest"],
                            applied_at=_utc_text(), changes=result["changes"])
            self._conn.execute("INSERT INTO pricing_supplier_update VALUES (?,?,?,?)",
                               (import_id, request_hash, _json(metadata), metadata["applied_at"]))
            result["reconciliation"] = {
                "supplier_id": request.supplier_id, "source_scope": request.source_scope,
                "mode": request.mode, "review_digest": review["review_digest"],
                "already_applied_file": review["already_applied_file"],
                "counts": {key: len(values) for key, values in review["groups"].items()}}
            result["catalog"] = self.catalog_snapshot()
            self._conn.execute("UPDATE pricing_import SET result=? WHERE import_id=?",
                               (_json(result), import_id))
            return result

    @contextmanager
    def _read_transaction(self) -> Iterator[None]:
        if self._conn.in_transaction:
            yield
        else:
            self._conn.execute("BEGIN")
            try:
                yield
            finally:
                self._conn.rollback()

    def catalog_token(self) -> str:
        """Hash raw catalog/reconciliation state, independent of display time/FX."""
        with self._lock, self._read_transaction():
            tables = ("pricing_vendor", "pricing_model", "pricing_alias", "pricing_official",
                      "pricing_supplier", "pricing_supplier_alias", "pricing_channel", "pricing_quote",
                      "pricing_supplier_mapping", "pricing_supplier_update")
            state = {table: sorted([dict(row) for row in self._conn.execute("SELECT * FROM " + table)],
                                   key=_json) for table in tables}
            return hashlib.sha256(_json(state).encode()).hexdigest()

    def catalog_snapshot(self) -> dict[str, Any]:
        with self._lock, self._read_transaction(), self._valuation():
            models = [self._catalog_row(r) for r in self._conn.execute("SELECT * FROM pricing_model ORDER BY model_id")]
            official = [expand_price_payload(self._catalog_row(r), allow_legacy=True)
                        for r in self._conn.execute(
                "SELECT * FROM pricing_official ORDER BY model_id,billing_item,official_price_id")]
            suppliers = self.list_suppliers()
            channels = [dict(r) for r in self._conn.execute("SELECT * FROM pricing_channel ORDER BY channel_id")]
            quotes = [self._response(r).model_dump(mode="json") for r in self._conn.execute(
                "SELECT * FROM pricing_quote ORDER BY id")]
            legacy = self._conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_quote'").fetchone()
            legacy_count = self._conn.execute("SELECT count(*) FROM workspace_quote").fetchone()[0] if legacy else 0
            for channel in channels:
                channel["tags"] = sorted({tag for q in quotes if q["channel_id"] == channel["channel_id"]
                                           and q["status"] == "active" for tag in q["tags"]})
            mappings = [dict(row) for row in self._conn.execute(
                "SELECT * FROM pricing_supplier_mapping ORDER BY supplier_id,source_scope,entry_key")]
            receipts = [json.loads(row["metadata"]) for row in self._conn.execute(
                "SELECT metadata FROM pricing_supplier_update ORDER BY created_at,import_id")]
            return {"catalog_token": getattr(self, "_snapshot_base_catalog_token", None) or self.catalog_token(), "supplier_mappings": mappings, "supplier_update_receipts": receipts,
                    "models": models, "official_prices": official, "suppliers": suppliers,
                    "pricing_context": self.pricing_context(),
                    "channels": channels, "quotes": quotes, "legacy_quote_count": legacy_count,
                    "legacy_notice": "Legacy rows are preserved and require explicit identity review"
                    if legacy_count else None}

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> "WorkspaceStore":
        """Reconstruct a trusted catalog in RAM, preserving nullable and legacy source values."""
        result = cls(":memory:")
        try:
            for model in snapshot.get("models", []):
                fields = {key: value for key, value in model.items()
                          if key in ModelCreate.model_fields and key != "expected_revision"}
                result._put_model(ModelCreate.model_validate(fields), "snapshot")
                result._conn.execute(
                    "UPDATE pricing_model SET revision=?,created_at=?,updated_at=? WHERE model_id=?",
                    (model.get("revision", 1), model.get("created_at", _utc_text()),
                     model.get("updated_at", _utc_text()), model["model_id"]))
            def raw_price(value: dict, allowed: Any) -> dict:
                raw = {key: item for key, item in value.items()
                       if key in allowed and key != "expected_revision"}
                if value.get("pricing_unit_id") is not None:
                    for key in ("currency", "unit", "unit_quantity"):
                        raw.pop(key, None)
                elif (value.get("legacy_unit") or {}).get("pricing_unit_id"):
                    raw["pricing_unit_id"] = value["legacy_unit"]["pricing_unit_id"]
                    for key in ("currency", "unit", "unit_quantity"):
                        if raw.get(key) is None:
                            raw.pop(key, None)
                return raw
            for price in snapshot.get("official_prices", []):
                result._ensure_model(price["model_id"], "snapshot")
                result._conn.execute(
                    "INSERT INTO pricing_official VALUES (?,?,?,?,?,?,?)",
                    (price.get("official_price_id") or "op-" + hashlib.sha256(_json(price).encode()).hexdigest()[:32],
                     price["model_id"], price.get("billing_item"),
                     _json({**raw_price(price, OfficialPriceCreate.model_fields), "official_price_id":
                         price.get("official_price_id") or "op-" + hashlib.sha256(_json(price).encode()).hexdigest()[:32],
                         "billing_metric": price.get("billing_metric") or legacy_metric(price.get("billing_item"))}), price.get("revision", 1),
                     price.get("created_at", _utc_text()), price.get("updated_at", _utc_text())))
            for supplier in snapshot.get("suppliers", []):
                result._conn.execute(
                    "INSERT INTO pricing_supplier(supplier_id,name,name_key,provider_type,revision) VALUES (?,?,?,?,?)",
                    (supplier["supplier_id"], supplier["name"],
                     result._supplier_key(supplier["name"]),
                     supplier.get("provider_type") or "unknown", supplier.get("revision", 1)))
                for alias in supplier.get("aliases", []):
                    result._conn.execute("INSERT INTO pricing_supplier_alias VALUES (?,?,?)",
                        (result._supplier_key(alias), alias, supplier["supplier_id"]))
            for channel in snapshot.get("channels", []):
                result._conn.execute(
                    "INSERT INTO pricing_channel(channel_id,supplier_id,model_id,supplier_model_id,channel_key) VALUES (?,?,?,?,?)",
                    (channel["channel_id"], channel["supplier_id"], channel["model_id"],
                     channel.get("supplier_model_id"), channel.get("channel_key", channel.get("supplier_model_id") or "")))
            context = snapshot.get("pricing_context")
            if context:
                payload = {key: context.get(key, []) for key in ("fx_rates", "token_equivalences")}
                result._conn.execute("UPDATE pricing_context SET payload=?,revision=?,updated_at=? WHERE id=1",
                    (_json(payload), context.get("revision", 1), context.get("updated_at", _utc_text())))
            for quote in snapshot.get("quotes", []):
                result._ensure_model(quote["model_id"], "snapshot")
                raw = raw_price(quote, EDITABLE_FIELDS)
                if "raw_discount_rate" in quote:
                    raw["discount_rate"] = quote["raw_discount_rate"]
                if "discount_rate" in raw:
                    normalized = normalize_discount_rate(raw["discount_rate"])
                    raw["discount_rate"] = None if normalized is None else format(normalized, "f")
                supplier_id, channel_id = quote.get("supplier_id"), quote.get("channel_id")
                if supplier_id is not None:
                    name = quote.get("supplier")
                    if name is None:
                        raise ValueError("trusted snapshot has supplier_id without supplier metadata")
                    result._conn.execute(
                        "INSERT OR IGNORE INTO pricing_supplier(supplier_id,name,name_key,provider_type) VALUES (?,?,?,?)",
                        (supplier_id, name, result._supplier_key(name), quote.get("provider_type") or "unknown"))
                if channel_id is not None:
                    if supplier_id is None:
                        raise ValueError("trusted snapshot channel requires supplier_id")
                    result._conn.execute(
                        "INSERT OR IGNORE INTO pricing_channel(channel_id,supplier_id,model_id,supplier_model_id,channel_key)"
                        " VALUES (?,?,?,?,?)",
                        (channel_id, supplier_id, quote["model_id"], quote.get("supplier_model_id"),
                         quote.get("supplier_model_id") or ""))
                result._conn.execute(
                    "INSERT INTO pricing_quote(id,model_id,billing_item,supplier_id,channel_id,payload,status,"
                    "revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (quote["id"], quote["model_id"], quote.get("billing_item"), supplier_id, channel_id, _json(raw),
                     quote.get("status", "active"), quote.get("revision", 1),
                     quote.get("created_at", _utc_text()), quote.get("updated_at", _utc_text())))
            for mapping in snapshot.get("supplier_mappings", []):
                result._conn.execute("INSERT INTO pricing_supplier_mapping VALUES (?,?,?,?,?)",
                    tuple(mapping[key] for key in ("supplier_id", "source_scope", "entry_key", "quote_id", "identity_hash")))
            for index, receipt in enumerate(snapshot.get("supplier_update_receipts", [])):
                result._conn.execute("INSERT INTO pricing_supplier_update VALUES (?,?,?,?)",
                    ("snapshot:" + str(index), "", _json(receipt), receipt.get("applied_at", _utc_text())))
            if result._conn.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("trusted snapshot has dangling entity references")
            result._conn.execute("DELETE FROM pricing_audit")
            result._snapshot_base_catalog_token = snapshot.get("catalog_token")
            return result
        except Exception:
            result.close()
            raise

    def list_quotes(self, *, q: str | None = None, limit: int = 100, offset: int = 0,
                    status: str | None = "active", ids: Sequence[int] | None = None,
                    **filters: Any) -> tuple[list[QuoteResponse], int]:
        supported = {"model_id", "model_name", "manufacturer", "vendor", "provider", "supplier",
                     "currency", "pricing_unit_id", "metric", "billing_item", "dimension", "tag", "supplier_model_id"}
        unknown = set(filters) - supported
        if unknown:
            raise ValueError(f"unknown quote filters: {', '.join(sorted(unknown))}")
        with self._lock:
            items = [self._response(r) for r in self._conn.execute(
                "SELECT * FROM pricing_quote ORDER BY updated_at DESC,id DESC")]
        selected = []
        for item in items:
            value = item.model_dump(mode="json")
            if status and item.status != status:
                continue
            if ids is not None and item.id not in ids:
                continue
            if q and q.casefold() not in _json(value).casefold():
                continue
            if any(v is not None and v != "" and
                   (v not in item.tags if key == "tag" else value.get(key) != v)
                   for key, v in filters.items()):
                continue
            selected.append(item)
        return selected[max(0, offset):max(0, offset) + max(0, limit)], len(selected)

    def filter_options(self) -> dict[str, list[str]]:
        items, _ = self.list_quotes(limit=1000000)
        columns = {"model_names": "model_name", "manufacturers": "manufacturer", "providers": "provider",
                   "currencies": "currency", "pricing_unit_ids": "pricing_unit_id",
                   "metrics": "metric", "dimensions": "dimension",
                   "model_ids": "model_id", "vendors": "vendor", "suppliers": "supplier",
                   "billing_items": "billing_item"}
        values = {key: sorted({str(getattr(v, column)) for v in items if getattr(v, column)})
                  for key, column in columns.items()}
        values["tags"] = sorted({tag for v in items for tag in v.tags})
        return values

    def create_many(self, items: Sequence[QuoteCreate], *, actor: str = "manual/cli",
                    idempotency_key: str | None = None) -> tuple[list[QuoteResponse], bool]:
        if not items:
            raise ValueError("at least one quote is required")
        result = self.apply_bundle({"create": list(items)}, actor, idempotency_key)
        return [QuoteResponse.model_validate(v) for v in result["items"]], result["reused"]

    def update_quote(self, quote_id: int, changes: dict, *, expected_revision: int,
                     actor: str = "workspace-ui") -> QuoteResponse:
        self.apply_bundle({"update": [{"id": quote_id, "expected_revision": expected_revision,
                                        "changes": changes}]}, actor)
        return self.get_quote(quote_id)

    def delete_quote(self, quote_id: int, *, expected_revision: int,
                     actor: str = "workspace-ui") -> QuoteResponse:
        """Physically remove one quote; return its before snapshot for API compatibility."""
        with self._lock:
            old = self.get_quote(quote_id)
            self.apply_bundle({"delete": [{"id": quote_id, "expected_revision": expected_revision}]}, actor)
            return old

    def archive_quote(self, quote_id: int, *, expected_revision: int,
                      actor: str = "workspace-ui") -> QuoteResponse:
        """Compatibility method: deletion is now physical, never an archive flag."""
        return self.delete_quote(quote_id, expected_revision=expected_revision, actor=actor)

    def delete_many(self, items: Sequence[dict], *, actor: str = "workspace-ui") -> dict[str, Any]:
        request = BulkQuoteDeleteRequest.model_validate({"items": list(items), "actor": actor})
        result = self.apply_bundle({"delete": request.items}, actor)
        return {"deleted": len(result["deleted_ids"]), "deleted_ids": result["deleted_ids"]}

    def apply_changes(self, *, creates: Sequence[QuoteCreate] = (),
                      updates: Sequence[tuple[int, int, dict]] = (),
                      deletes: Sequence[tuple[int, int]] = (), actor: str = "manual/cli") -> list[QuoteResponse]:
        result = self.apply_bundle({"create": list(creates),
                                   "update": [{"id": i, "expected_revision": r, "changes": c} for i, r, c in updates],
                                   "delete": [{"id": i, "expected_revision": r} for i, r in deletes]}, actor)
        return [QuoteResponse.model_validate(v) for v in result["items"]]

    def change_summary(self, actor: str) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT entity_id,action FROM pricing_audit WHERE actor=? AND entity_type='quote'",
                (actor.strip() or "unknown",),
            ).fetchall()
        values = {action: len({r["entity_id"] for r in rows if r["action"] == action})
                  for action in ("added", "updated", "deleted")}
        values["total"] = len({r["entity_id"] for r in rows})
        return values


def prepare_bundle(bundle: PricingBundle | dict, snapshot: dict[str, Any]) -> dict[str, Any]:
    store = WorkspaceStore.from_snapshot(snapshot)
    try:
        return store.preview_bundle(bundle)
    finally:
        store.close()
