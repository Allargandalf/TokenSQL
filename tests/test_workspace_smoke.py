from copy import deepcopy

from tools.workspace_smoke import _validation_errors


def valid_result():
    rows = []
    for model, direction, amount, cell in (
        ("Alpha-1", "input", "1.25", "B2"),
        ("Alpha-1", "output", "3.5", "C2"),
        ("Beta 2", "input", "0.8", "B3"),
        ("Beta 2", "output", "2.4", "C3"),
    ):
        rows.append({
            "model_name": model,
            "direction": direction,
            "amount": amount,
            "currency": "CNY",
            "unit": "token",
            "unit_quantity": "1000000",
            "provider": "烟雾测试供应商",
            "provider_type": "reseller",
            "source": "两行 报价.xlsx",
            "source_ref": f"报价!A2,{cell}",
        })
    return {
        "status": "completed",
        "database_total": 4,
        "changes": {"added": 4, "updated": 0, "deleted": 0, "total": 4},
        "quotes": rows,
    }


def test_workspace_smoke_validation_checks_generated_values():
    assert _validation_errors(valid_result()) == []


def test_workspace_smoke_validation_rejects_wrong_amount_and_source():
    result = deepcopy(valid_result())
    result["quotes"][0]["amount"] = "999"
    result["quotes"][1]["source_ref"] = "wrong"
    errors = _validation_errors(result)
    assert any("amount differs" in error for error in errors)
    assert any("source reference" in error for error in errors)
