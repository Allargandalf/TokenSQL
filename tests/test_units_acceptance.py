"""Cross-module acceptance guards for the formal 12-unit quote contract."""
from __future__ import annotations

from decimal import Decimal
import json

from tokensql.workspace.draft_cli import main
from tokensql.workspace.memory import BASE_RULES
from tokensql.workspace.store import WorkspaceStore
from tests.test_workspace_data import model, official, quote


def test_agent_rules_name_formal_units_and_forbid_persisting_display_state():
    expected = {
        f"{currency}_{scale}"
        for currency in ("usd", "hkd", "cny")
        for scale in ("ktoken", "mtoken", "second", "image")
    }
    assert all(unit_id in BASE_RULES for unit_id in expected)
    assert "amount and its pricing_unit_id as separate raw business fields" in BASE_RULES
    assert "temporary conversion factors" in BASE_RULES
    assert "never be proposed for database storage or remembered" in BASE_RULES


def test_draft_cli_help_exposes_formal_unit_contract(capsys):
    try:
        main(["--help"])
    except SystemExit as error:
        assert error.code == 0
    help_text = capsys.readouterr().out
    assert "pricing_unit_id" in help_text
    assert "frontend-only display/export" in help_text


def test_extreme_decimal_prices_preserve_raw_values_and_compute_without_float(tmp_path):
    store = WorkspaceStore(tmp_path / "extreme-decimals.sqlite")
    official_amount = "1" + "0" * 200
    supplier_amount = "5" + "0" * 199
    store.apply_bundle({
        "models": [model()],
        "official_prices": [official(
            amount=official_amount, pricing_unit_id="cny_mtoken",
        )],
    })
    huge = store.apply_bundle({"create": [quote(
        amount=supplier_amount, pricing_unit_id="cny_mtoken",
        unit_quantity="1000000",
    )]})["items"][0]
    assert huge["amount"] == supplier_amount
    assert huge["discount_rate"] is None and huge["derived_amount"] is None
    tiny = store.apply_bundle({"create": [quote(
        amount="1E-90", supplier="tiny", pricing_unit_id="cny_mtoken",
        unit_quantity="1000000",
    )]})["items"][0]
    raw = json.loads(store._conn.execute(
        "SELECT payload FROM pricing_quote WHERE id=?", (tiny["id"],),
    ).fetchone()[0])
    assert raw["amount"] == "1E-90"
    assert tiny["discount_rate"] is None and tiny["derived_amount"] is None
