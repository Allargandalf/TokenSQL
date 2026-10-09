from __future__ import annotations

import json
import threading
import time

import httpx
import pytest
from pydantic import ValidationError

from tokensql.llm.client import QwenClientConfig
from tokensql.llm.extraction_schemas import (
    ComponentExtractionModel,
    RowExtractionBatchModel,
    TableUnderstandingModel,
)
from tokensql.llm.semantic import (
    ROW_PROMPT_VERSION,
    TABLE_PROMPT_VERSION,
    SemanticQwenClient,
    SemanticQwenError,
)


def _config(**overrides) -> QwenClientConfig:
    defaults = dict(
        base_url="http://mock.local/v1",
        api_key="secret",
        model="qwen-test",
        timeout_seconds=0.5,
        allow_remote=True,
    )
    defaults.update(overrides)
    return QwenClientConfig(**defaults)


def _client(handler, **overrides) -> SemanticQwenClient:
    return SemanticQwenClient(_config(**overrides), transport=httpx.MockTransport(handler))


def _completion(content: object, **envelope) -> dict[str, object]:
    message = content if isinstance(content, str) else json.dumps(content)
    payload: dict[str, object] = {"choices": [{"message": {"content": message}}]}
    payload.update(envelope)
    return payload


_TABLE_DATA = {
    "schema_version": 1,
    "sheets": [
        {
            "sheet": "Sheet1",
            "provider": "OpenRouter",
            "provider_type": "aggregator",
            "currency": "USD",
            "column_roles": [
                {
                    "column_letter": "A",
                    "role": "model_name",
                    "confidence": 0.95,
                    "evidence": ["Sheet1!A1"],
                }
            ],
            "confidence": 0.9,
            "evidence": ["Sheet1!A1"],
        }
    ],
    "notes": None,
}


def _row_batch_data() -> dict[str, object]:
    return {
        "schema_version": 1,
        "rows": [
            {
                "row_number": 2,
                "model_name": "GPT-4.1",
                "components": [
                    {
                        "component_type": "monetary",
                        "direction": "input",
                        "dimension": "token",
                        "raw_amount": "2.00",
                        "currency": "USD",
                        "unit_name": "tokens",
                        "unit_quantity": "1000000",
                        "evidence": ["Sheet1!C2"],
                    }
                ],
                "confidence": 0.93,
            }
        ],
    }


def test_envelope_extension_fields_are_accepted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["reasoning_effort"] == "low"
        assert payload["max_tokens"] == 8192
        assert payload["response_format"]["json_schema"]["strict"] is True
        assert payload["response_format"]["json_schema"]["name"] == (
            "TableUnderstandingModel"
        )
        assert "user" not in payload
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(_TABLE_DATA),
                            "reasoning_content": "thinking in Chinese: 逐列判断",
                        }
                    }
                ],
                "model": "qwen-test",
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 240,
                    "total_tokens": 360,
                    "prompt_tokens_details": {"cached_tokens": 96},
                },
                "timings": {"total_ms": 421},
            },
        )

    with _client(handler) as client:
        result = client.understand_table(
            user_context={"provider": "OpenRouter"},
            workbook_summary={"sheets": [{"name": "Sheet1"}]},
        )

    assert result.model == "qwen-test"
    assert result.usage == {
        "prompt_tokens": 120,
        "completion_tokens": 240,
        "total_tokens": 360,
        "cached_tokens": 96,
    }
    assert result.reasoning_content is not None
    assert result.data["sheets"][0]["provider"] == "OpenRouter"
    assert len(result.request_id) == 32


def test_extract_rows_sends_canonical_payload() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json=_completion(_row_batch_data()))

    with _client(handler) as client:
        result = client.extract_rows(
            user_context={"currency": "USD"},
            table_understanding=_TABLE_DATA,
            rows=[{"row_number": 2, "cells": {"A": "GPT-4.1", "C": "$2.00 / 1M tokens"}}],
        )

    user_payload = json.loads(captured["messages"][1]["content"])
    assert set(user_payload) == {"context", "understanding", "rows"}
    assert captured["messages"][0]["content"].startswith("row-extraction-v3")
    assert result.data["rows"][0]["components"][0]["raw_amount"] == "2.00"


def test_prompt_versions_are_exported_and_marked() -> None:
    assert TABLE_PROMPT_VERSION == "qwen-table-v2"
    assert ROW_PROMPT_VERSION == "qwen-extract-v3"


def test_non_json_content_is_invalid_json() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("这不是 JSON"))

    with _client(handler) as client:
        with pytest.raises(SemanticQwenError) as excinfo:
            client.understand_table(user_context={}, workbook_summary={})
    assert excinfo.value.kind == "invalid_json"


def test_business_schema_violation_is_schema_invalid() -> None:
    broken = json.loads(json.dumps(_row_batch_data()))
    del broken["rows"][0]["components"][0]["evidence"]

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(broken))

    with _client(handler) as client:
        with pytest.raises(SemanticQwenError) as excinfo:
            client.extract_rows(
                user_context={}, table_understanding=_TABLE_DATA, rows=[]
            )
    assert excinfo.value.kind == "schema_invalid"
    assert "evidence" in excinfo.value.detail


def test_http_502_is_http_status() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": "upstream"})

    with _client(handler) as client:
        with pytest.raises(SemanticQwenError) as excinfo:
            client.understand_table(user_context={}, workbook_summary={})
    assert excinfo.value.kind == "http_status"
    assert excinfo.value.status_code == 502


def test_connect_timeout_is_timeout_kind() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("connect timed out", request=request)

    with _client(handler) as client:
        with pytest.raises(SemanticQwenError) as excinfo:
            client.understand_table(user_context={}, workbook_summary={})
    assert calls == 1
    assert excinfo.value.kind == "timeout"


def test_empty_choices_is_empty_kind() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    with _client(handler) as client:
        with pytest.raises(SemanticQwenError) as excinfo:
            client.understand_table(user_context={}, workbook_summary={})
    assert excinfo.value.kind == "empty"


def test_verify_model_accepts_loaded_model() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "other-model", "status": "loaded"},
                    {"id": "qwen-test", "status": "loaded"},
                ],
            },
        )

    with _client(handler) as client:
        assert client.verify_model() == "qwen-test"


def test_verify_model_accepts_object_status_value() -> None:
    handler = httpx.MockTransport(
        lambda _: httpx.Response(
            200,
            json={"data": [{"id": "qwen-test", "status": {"value": "loaded"}}]},
        )
    )
    with SemanticQwenClient(_config(), transport=handler) as client:
        assert client.verify_model() == "qwen-test"


def test_verify_model_rejects_missing_or_unloaded_model() -> None:
    missing = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"data": [{"id": "other", "status": "loaded"}]})
    )
    with SemanticQwenClient(_config(), transport=missing) as client:
        with pytest.raises(SemanticQwenError) as excinfo:
            client.verify_model()
    assert excinfo.value.kind == "model_mismatch"

    loading = httpx.MockTransport(
        lambda _: httpx.Response(200, json={"data": [{"id": "qwen-test", "status": "loading"}]})
    )
    with SemanticQwenClient(_config(), transport=loading) as client:
        with pytest.raises(SemanticQwenError) as excinfo:
            client.verify_model()
    assert excinfo.value.kind == "model_mismatch"


def test_monetary_component_requires_amount_and_unit_quantity() -> None:
    with pytest.raises(ValidationError, match="raw_amount"):
        ComponentExtractionModel(
            component_type="monetary",
            unit_quantity="1000000",
            evidence=["Sheet1!C2"],
        )
    with pytest.raises(ValidationError, match="unit_quantity"):
        ComponentExtractionModel(
            component_type="monetary",
            raw_amount="2.00",
            evidence=["Sheet1!C2"],
        )


def test_discount_component_rejects_money_fields() -> None:
    with pytest.raises(ValidationError, match="currency"):
        ComponentExtractionModel(
            component_type="discount",
            discount_rate="0.10",
            currency="USD",
            evidence=["user_context"],
        )
    with pytest.raises(ValidationError, match="discount_rate"):
        ComponentExtractionModel(
            component_type="discount",
            evidence=["user_context"],
        )


def test_extraction_schemas_forbid_extra_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        RowExtractionBatchModel.model_validate(
            {
                "schema_version": 1,
                "rows": [],
                "sql": "DROP TABLE models",
            }
        )
    with pytest.raises(ValidationError):
        TableUnderstandingModel.model_validate(
            {**_TABLE_DATA, "primary_key": 7}
        )


def test_column_roles_accept_optional_notes() -> None:
    sheet = dict(_TABLE_DATA["sheets"][0])
    roles = [dict(role) for role in sheet["column_roles"]]
    roles[0]["notes"] = "表头含空格，已去除"
    sheet["column_roles"] = roles
    model = TableUnderstandingModel.model_validate(
        {**_TABLE_DATA, "sheets": [sheet]}
    )
    assert model.sheets[0].column_roles[0].notes == "表头含空格，已去除"


def test_max_in_flight_one_serializes_two_threads() -> None:
    lock = threading.Lock()
    state = {"active": 0, "max_active": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.08)
        with lock:
            state["active"] -= 1
        return httpx.Response(200, json=_completion(_TABLE_DATA))

    with _client(handler, max_in_flight=1) as client:
        threads = [
            threading.Thread(
                target=lambda: client.understand_table(
                    user_context={}, workbook_summary={}
                )
            )
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert state["max_active"] == 1


def test_max_in_flight_two_allows_overlap() -> None:
    lock = threading.Lock()
    state = {"active": 0, "max_active": 0}
    started = threading.Barrier(2, timeout=2)

    def handler(_: httpx.Request) -> httpx.Response:
        started.wait()
        with lock:
            state["active"] += 1
            state["max_active"] = max(state["max_active"], state["active"])
        time.sleep(0.05)
        with lock:
            state["active"] -= 1
        return httpx.Response(200, json=_completion(_TABLE_DATA))

    with _client(handler, max_in_flight=2) as client:
        threads = [
            threading.Thread(
                target=lambda: client.understand_table(
                    user_context={}, workbook_summary={}
                )
            )
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    assert state["max_active"] == 2
