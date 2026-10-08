"""Tests for the side-effect-free Streamlit HTTP client."""
from __future__ import annotations

import importlib
import json
import sys
from typing import Any

import httpx
import pytest

import tokensql.ui.client as client_module
from tokensql.ui.client import (
    APIError,
    RequestValidationError,
    TokenSQLClient,
)


def test_client_covers_all_management_endpoints_with_mock_transport() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok", "qwen": {"available": False}})
        if path == "/imports/profile":
            return httpx.Response(
                200,
                json={
                    "workbook": {
                        "sha256": "a" * 64,
                        "workbook_name": "prices.xlsx",
                        "sheets": [],
                    },
                    "staged_quotes": [],
                },
            )
        if path == "/imports":
            return httpx.Response(200, json={"batch_id": 7, "status": "completed"})
        if path == "/reviews":
            return httpx.Response(200, json={"items": [{"id": 3}]})
        if path.endswith("/approve") or path.endswith("/reject"):
            return httpx.Response(200, json={"id": 3, "status": "resolved"})
        if path == "/aliases":
            return httpx.Response(201, json={"id": 9, "alias": "qwen plus"})
        if path in {"/models", "/providers", "/pricing-plans"}:
            return httpx.Response(200, json=[{"id": 1}])
        if path == "/prices/history":
            return httpx.Response(200, json={"results": [{"id": 11}]})
        if path == "/prices/compare":
            return httpx.Response(
                200, json={"comparable": False, "reasons": ["condition differs"]}
            )
        if path == "/prices/export.csv":
            return httpx.Response(200, content=b"id,amount\n1,2\n")
        return httpx.Response(404, json={"detail": "unexpected endpoint"})

    with TokenSQLClient(
        "http://api.invalid/",
        timeout=2.5,
        transport=httpx.MockTransport(handler),
    ) as client:
        assert client.base_url == "http://api.invalid"
        assert client.health()["status"] == "ok"
        profile = client.profile_import(
            "../../prices.XLSX",
            b"PK workbook bytes",
            context={"profile": {"header_row": 2, "price_headers": ["input"]}},
        )
        assert profile["workbook"]["workbook_name"] == "prices.xlsx"
        summary = client.import_workbook(
            "prices.xlsx",
            b"PK workbook bytes",
            request={
                "data_source_id": 1,
                "parser_version": "test-v1",
                "provider_id": 2,
                "vendor_context": {"region": "cn"},
            },
        )
        assert summary["batch_id"] == 7
        assert client.list_reviews(status="open", limit=10) == [{"id": 3}]
        client.approve_review(
            3,
            reviewer=" alice ",
            resolution={"standard_model_id": 5},
            create_alias_rule=True,
        )
        client.reject_review(3, reviewer="bob", resolution={"reason": "incorrect"})
        client.create_alias(
            {
                "alias": "qwen plus",
                "standard_model_id": 5,
                "provider_id": 2,
            }
        )
        assert client.list_models() == [{"id": 1}]
        assert client.list_providers() == [{"id": 1}]
        assert client.list_pricing_plans(provider_id=2, unused=None) == [{"id": 1}]
        assert client.price_history(pricing_plan_id=8) == [{"id": 11}]
        comparison = client.compare_prices(
            left_component_id=1,
            right_component_id=2,
            left_normalization_id=10,
            algorithm_version="v1",
        )
        assert comparison["comparable"] is False
        assert client.export_prices_csv(pricing_plan_id=8).startswith(b"id,amount")

    assert [(item.method, item.url.path) for item in seen] == [
        ("GET", "/health"),
        ("POST", "/imports/profile"),
        ("POST", "/imports"),
        ("GET", "/reviews"),
        ("POST", "/reviews/3/approve"),
        ("POST", "/reviews/3/reject"),
        ("POST", "/aliases"),
        ("GET", "/models"),
        ("GET", "/providers"),
        ("GET", "/pricing-plans"),
        ("GET", "/prices/history"),
        ("POST", "/prices/compare"),
        ("GET", "/prices/export.csv"),
    ]
    assert dict(seen[3].url.params) == {"status": "open", "limit": "10"}
    assert dict(seen[9].url.params) == {"provider_id": "2"}
    assert dict(seen[10].url.params) == {"pricing_plan_id": "8"}
    assert json.loads(seen[11].content) == {
        "left_component_id": 1,
        "right_component_id": 2,
        "left_normalization_id": 10,
        "algorithm_version": "v1",
    }
    assert seen[12].headers["accept"] == "text/csv"

    profile_body = seen[1].content
    assert b'filename="prices.XLSX"' in profile_body
    assert b'name="context"' in profile_body
    assert b'"header_row":2' in profile_body
    assert b"PK workbook bytes" in profile_body

    import_body = seen[2].content
    assert b'name="request"' in import_body
    assert b'"data_source_id":1' in import_body
    assert b'"parser_version":"test-v1"' in import_body

    approval = json.loads(seen[4].content)
    assert approval == {
        "reviewer": "alice",
        "resolution": {"standard_model_id": 5},
        "create_alias_rule": True,
    }
    rejection = json.loads(seen[5].content)
    assert rejection == {
        "reviewer": "bob",
        "resolution": {"reason": "incorrect"},
    }


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("prices.xls", b"x", "only .xlsx"),
        ("prices.csv", b"x", "only .xlsx"),
        ("prices.xlsx", b"", "empty"),
    ],
)
def test_workbook_validation_happens_before_network(
    filename: str, content: bytes, message: str
) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    with TokenSQLClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RequestValidationError, match=message):
            client.profile_import(filename, content)
    assert calls == 0


def test_workbook_size_limit_happens_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(client_module, "MAX_EXCEL_BYTES", 4)
    transport = httpx.MockTransport(
        lambda _: pytest.fail("oversized workbook must not be sent")
    )
    with TokenSQLClient(transport=transport) as client:
        with pytest.raises(RequestValidationError, match="exceeds"):
            client.import_workbook("prices.xlsx", b"12345", request={})


def test_transport_and_http_errors_are_safe_and_strict() -> None:
    secret = "do-not-leak"

    def rejected(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "detail": [
                    {
                        "loc": ["body", "file"],
                        "msg": "invalid",
                        "input": secret,
                        "api_key": secret,
                    }
                ]
            },
        )

    with TokenSQLClient(transport=httpx.MockTransport(rejected)) as client:
        with pytest.raises(APIError) as caught:
            client.health()
    error = caught.value
    assert error.status_code == 422
    assert secret not in str(error)
    assert secret not in repr(error.detail)
    assert error.detail[0]["input"] == "[redacted]"
    assert error.detail[0]["api_key"] == "[redacted]"

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("contains sensitive transport detail", request=request)

    with TokenSQLClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(APIError, match="timed out") as caught_timeout:
            client.health()
    assert "sensitive" not in str(caught_timeout.value)


def test_invalid_json_and_shapes_are_rejected() -> None:
    responses: list[httpx.Response] = [
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json={"items": "not-a-list"}),
    ]

    def handler(_: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    with TokenSQLClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(APIError, match="malformed JSON"):
            client.health()
        with pytest.raises(APIError, match="unexpected response shape"):
            client.list_models()


@pytest.mark.parametrize("review_id", ["", "../1", "1?admin=true", "1#fragment"])
def test_review_id_and_reviewer_are_validated(review_id: str) -> None:
    transport = httpx.MockTransport(lambda _: pytest.fail("must not send"))
    with TokenSQLClient(transport=transport) as client:
        with pytest.raises(RequestValidationError):
            client.approve_review(review_id, reviewer="alice")
        with pytest.raises(RequestValidationError, match="reviewer"):
            client.reject_review(1, reviewer="   ")


def test_importing_app_has_no_ui_or_network_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_: Any, **__: Any) -> Any:
        pytest.fail("module import must not create a client or perform a request")

    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.delitem(sys.modules, "streamlit", raising=False)
    sys.modules.pop("tokensql.ui.app", None)

    module = importlib.import_module("tokensql.ui.app")

    assert callable(module.main)
    assert "streamlit" not in sys.modules


def test_client_verified_management_requests_are_explicit_and_strict() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": 1, "status": "ok"})

    with TokenSQLClient(transport=httpx.MockTransport(handler)) as client:
        client.publish_reviewed_quote(
            staged_quote_id=1,
            pricing_plan_id=2,
            source_artifact_id=3,
            actor=" alice ",
            idempotency_key="publish-1",
        )
        client.publish_official_quote(
            staged_quote_id=4,
            pricing_plan_id=5,
            source_artifact_id=6,
            data_source_id=7,
            actor="bob",
            idempotency_key="official-1",
        )
        client.create_fx_rate(
            base_currency="USD",
            quote_currency="CNY",
            rate="7.1",
            rate_date="2026-09-24",
            data_source_id=8,
            actor="carol",
        )
        client.persist_normalization(
            price_component_id=9,
            source_artifact_id=10,
            fx_rate_id=11,
            actor="dave",
        )

    assert [request.url.path for request in seen] == [
        "/prices/publish",
        "/official-prices/publish",
        "/fx-rates",
        "/price-normalizations",
    ]
    bodies = [json.loads(request.content) for request in seen]
    assert all(body["verified"] is True for body in bodies)
    assert bodies[0]["actor"] == "alice"
    assert bodies[0]["source_artifact_id"] == 3
    assert bodies[1]["data_source_id"] == 7
    assert bodies[2]["rate"] == "7.1"
    assert bodies[3]["fx_rate_id"] == 11

    transport = httpx.MockTransport(lambda _: pytest.fail("must not send"))
    with TokenSQLClient(transport=transport) as client:
        with pytest.raises(RequestValidationError, match="actor"):
            client.publish_reviewed_quote(
                staged_quote_id=1,
                pricing_plan_id=2,
                source_artifact_id=3,
                actor="   ",
                idempotency_key="publish-1",
            )


def test_client_staged_quote_inspection_and_confirmation() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET" and request.url.path == "/staged-quotes":
            return httpx.Response(200, json=[{"id": 12}])
        return httpx.Response(200, json={"id": 12, "review_status": "needs_review"})

    confirmation = {
        "actor": "alice",
        "source_artifact_id": 3,
        "provider_id": 4,
        "standard_model_id": 5,
        "offering_id": 6,
        "pricing_plan_id": 7,
        "currency_code": "CNY",
        "observed_at": "2026-09-24T00:00:00+00:00",
        "effective_from": "2026-09-24T00:00:00+00:00",
        "components": [
            {
                "candidate_index": 0,
                "billing_metric_id": 8,
                "component_type": "monetary",
                "raw_amount": "1",
                "raw_currency_code": "CNY",
                "raw_unit_name": "token",
                "raw_unit_quantity": "1000000",
            }
        ],
        "note": "checked source cell",
    }
    with TokenSQLClient(transport=httpx.MockTransport(handler)) as client:
        assert client.list_staged_quotes(
            review_status="needs_review", import_batch_id=9, limit=10
        ) == [{"id": 12}]
        assert client.get_staged_quote(12)["id"] == 12
        assert client.confirm_staged_quote(
            12, confirmation=confirmation
        )["review_status"] == "needs_review"

    assert [request.url.path for request in seen] == [
        "/staged-quotes",
        "/staged-quotes/12",
        "/staged-quotes/12/confirm",
    ]
    assert dict(seen[0].url.params) == {
        "review_status": "needs_review",
        "import_batch_id": "9",
        "limit": "10",
    }
    assert json.loads(seen[2].content) == confirmation


def test_get_import_context_uses_dedicated_endpoint() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/imports/7/context":
            return httpx.Response(
                200,
                json={
                    "batch_id": 7,
                    "import_context": {
                        "prompt_text": "厂商为 Example AI",
                        "prompt_sha256": "a" * 64,
                        "context_defaults": {"currency": "CNY"},
                        "context_scope": "batch",
                        "context_version": 1,
                    },
                },
            )
        return httpx.Response(404, json={"detail": "import batch not found"})

    with TokenSQLClient(transport=httpx.MockTransport(handler)) as client:
        payload = client.get_import_context(7)
        with pytest.raises(APIError) as caught:
            client.get_import_context(999)

    assert seen[0].method == "GET"
    assert seen[0].url.path == "/imports/7/context"
    assert payload["batch_id"] == 7
    assert payload["import_context"]["prompt_text"] == "厂商为 Example AI"
    assert payload["import_context"]["context_defaults"] == {"currency": "CNY"}
    assert caught.value.status_code == 404

    transport = httpx.MockTransport(lambda _: pytest.fail("must not send"))
    with TokenSQLClient(transport=transport) as client:
        with pytest.raises(RequestValidationError):
            client.get_import_context("../1")
