"""Direct tests for the pure UI import-request merge helper."""

from __future__ import annotations

from tokensql.ui.app import build_import_request


def test_prompt_injected_when_request_omits_it() -> None:
    base = {"data_source_id": 1, "parser_version": "streamlit-v1"}
    request = build_import_request(base, "  厂商 AlphaLabs；币种 CNY  ", {})
    assert request["prompt_text"] == "  厂商 AlphaLabs；币种 CNY  "
    assert "context_defaults" not in request


def test_blank_prompt_is_not_injected() -> None:
    base = {"data_source_id": 1}
    request = build_import_request(base, "   \n ", {})
    assert "prompt_text" not in request


def test_structured_defaults_are_merged_and_stripped() -> None:
    base = {"data_source_id": 1}
    request = build_import_request(
        base,
        "",
        {
            "manufacturer": "AlphaLabs",
            "provider_name": " ResellerOne ",
            "currency": "CNY",
            "unit_basis": "   ",
        },
    )
    assert request["context_defaults"] == {
        "manufacturer": "AlphaLabs",
        "provider_name": "ResellerOne",
        "currency": "CNY",
    }


def test_blank_defaults_do_not_create_context_defaults_key() -> None:
    request = build_import_request(
        {"data_source_id": 1}, "", {"manufacturer": "", "currency": "  "}
    )
    assert "context_defaults" not in request


def test_explicit_json_values_win_over_sidebar_widgets() -> None:
    base = {
        "data_source_id": 1,
        "prompt_text": "JSON 中的显式说明",
        "context_defaults": {"currency": "USD"},
    }
    request = build_import_request(
        base,
        "侧栏 Prompt 不应覆盖",
        {"manufacturer": "AlphaLabs", "currency": "CNY"},
    )
    assert request["prompt_text"] == "JSON 中的显式说明"
    assert request["context_defaults"] == {"currency": "USD"}


def test_base_request_is_not_mutated() -> None:
    base = {"data_source_id": 1}
    request = build_import_request(base, "厂商 X", {"currency": "CNY"})
    assert base == {"data_source_id": 1}
    assert request is not base
