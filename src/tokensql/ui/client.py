"""HTTP client used by the optional Streamlit management interface.

The module is intentionally UI-agnostic and performs no network I/O at import
time.  File payloads and credentials are never included in exception messages.
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 15.0
MAX_EXCEL_BYTES = 10 * 1024 * 1024
EXCEL_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)


class TokenSQLClientError(RuntimeError):
    """Base class for safe-to-display client failures."""


class RequestValidationError(TokenSQLClientError, ValueError):
    """Raised before a malformed request is sent."""


class APIError(TokenSQLClientError):
    """Raised when the API returns an unsuccessful or invalid response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail


class TokenSQLClient:
    """Small, strict wrapper around the TokenSQL HTTP API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float | httpx.Timeout = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        normalized = base_url.strip().rstrip("/")
        if not normalized:
            raise RequestValidationError("API base URL must not be empty")
        self._client = httpx.Client(
            base_url=normalized,
            timeout=timeout,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    @property
    def base_url(self) -> str:
        return str(self._client.base_url).rstrip("/")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TokenSQLClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise APIError("TokenSQL API request timed out") from exc
        except httpx.RequestError as exc:
            raise APIError("TokenSQL API is unavailable") from exc

        if not response.is_success:
            detail = _safe_error_detail(response)
            message = f"TokenSQL API returned HTTP {response.status_code}"
            if isinstance(detail, str) and detail:
                message = f"{message}: {detail}"
            raise APIError(
                message, status_code=response.status_code, detail=detail
            )
        return response

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._request(method, path, **kwargs)
        try:
            return response.json()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise APIError(
                "TokenSQL API returned malformed JSON",
                status_code=response.status_code,
            ) from exc

    def health(self) -> dict[str, Any]:
        return _expect_object(self._json("GET", "/health"), "/health")

    def profile_import(
        self,
        filename: str,
        content: bytes,
        *,
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        name, data = _validate_workbook(filename, content)
        payload = self._json(
            "POST",
            "/imports/profile",
            files={"file": (name, data, EXCEL_CONTENT_TYPE)},
            data={"context": _json_field(context or {})},
        )
        return _expect_object(payload, "/imports/profile")

    def import_workbook(
        self,
        filename: str,
        content: bytes,
        *,
        request: Mapping[str, Any],
    ) -> dict[str, Any]:
        name, data = _validate_workbook(filename, content)
        if not isinstance(request, Mapping):
            raise RequestValidationError("import request must be a JSON object")
        payload = self._json(
            "POST",
            "/imports",
            files={"file": (name, data, EXCEL_CONTENT_TYPE)},
            data={"request": _json_field(request)},
        )
        return _expect_object(payload, "/imports")

    def get_import_context(self, batch_id: int | str) -> dict[str, Any]:
        identifier = _resource_id(batch_id)
        return _expect_object(
            self._json("GET", f"/imports/{identifier}/context"),
            "import context",
        )

    def list_staged_quotes(
        self,
        *,
        review_status: str | None = None,
        import_batch_id: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 1000:
            raise RequestValidationError("staged quote limit must be between 1 and 1000")
        return _expect_items(
            self._json(
                "GET",
                "/staged-quotes",
                params=_clean(
                    {
                        "review_status": review_status,
                        "import_batch_id": import_batch_id,
                        "limit": limit,
                    }
                ),
            ),
            "/staged-quotes",
        )

    def get_staged_quote(self, staged_quote_id: int | str) -> dict[str, Any]:
        identifier = _resource_id(staged_quote_id)
        return _expect_object(
            self._json("GET", f"/staged-quotes/{identifier}"),
            "staged quote",
        )

    def confirm_staged_quote(
        self,
        staged_quote_id: int | str,
        *,
        confirmation: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(confirmation, Mapping) or not confirmation:
            raise RequestValidationError(
                "staged quote confirmation must be a non-empty object"
            )
        identifier = _resource_id(staged_quote_id)
        return _expect_object(
            self._json(
                "POST",
                f"/staged-quotes/{identifier}/confirm",
                json=dict(confirmation),
            ),
            "staged quote confirmation",
        )

    def list_reviews(
        self,
        *,
        status: str | None = "open",
        severity: str | None = None,
        review_type: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise RequestValidationError("review limit must be between 1 and 500")
        payload = self._json(
            "GET",
            "/reviews",
            params=_clean({"status": status, "severity": severity,
                           "review_type": review_type, "limit": limit}),
        )
        return _expect_items(payload, "/reviews")

    def approve_review(
        self,
        review_id: int | str,
        *,
        reviewer: str,
        resolution: Mapping[str, Any] | None = None,
        create_alias_rule: bool | None = None,
    ) -> dict[str, Any]:
        body = _review_body(reviewer, resolution)
        if create_alias_rule is not None:
            body["create_alias_rule"] = create_alias_rule
        return _expect_object(
            self._json(
                "POST", f"/reviews/{_resource_id(review_id)}/approve", json=body
            ),
            "review approval",
        )

    def reject_review(
        self,
        review_id: int | str,
        *,
        reviewer: str,
        resolution: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = _review_body(reviewer, resolution)
        return _expect_object(
            self._json(
                "POST", f"/reviews/{_resource_id(review_id)}/reject", json=body
            ),
            "review rejection",
        )

    def create_alias(self, alias: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(alias, Mapping) or not alias:
            raise RequestValidationError("alias request must be a non-empty object")
        return _expect_object(
            self._json("POST", "/aliases", json=dict(alias)), "/aliases"
        )

    def list_models(self) -> list[dict[str, Any]]:
        return _expect_items(self._json("GET", "/models"), "/models")

    def list_providers(self) -> list[dict[str, Any]]:
        return _expect_items(self._json("GET", "/providers"), "/providers")

    def list_pricing_plans(
        self, **filters: Any
    ) -> list[dict[str, Any]]:
        return _expect_items(
            self._json("GET", "/pricing-plans", params=_clean(filters)),
            "/pricing-plans",
        )

    def publish_reviewed_quote(
        self,
        *,
        staged_quote_id: int,
        pricing_plan_id: int | None,
        source_artifact_id: int,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body = _verified_body(actor)
        body.update(
            {
                "staged_quote_id": staged_quote_id,
                "source_artifact_id": source_artifact_id,
                "idempotency_key": _required_text(
                    idempotency_key, "idempotency key"
                ),
            }
        )
        if pricing_plan_id is not None:
            body["pricing_plan_id"] = pricing_plan_id
        return _expect_object(
            self._json("POST", "/prices/publish", json=body),
            "/prices/publish",
        )

    def publish_official_quote(
        self,
        *,
        staged_quote_id: int,
        pricing_plan_id: int | None,
        source_artifact_id: int,
        data_source_id: int,
        actor: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        body = _verified_body(actor)
        body.update(
            {
                "staged_quote_id": staged_quote_id,
                "source_artifact_id": source_artifact_id,
                "data_source_id": data_source_id,
                "idempotency_key": _required_text(
                    idempotency_key, "idempotency key"
                ),
            }
        )
        if pricing_plan_id is not None:
            body["pricing_plan_id"] = pricing_plan_id
        return _expect_object(
            self._json("POST", "/official-prices/publish", json=body),
            "/official-prices/publish",
        )

    def create_fx_rate(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        rate: str,
        rate_date: str,
        data_source_id: int,
        actor: str,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        body = _verified_body(actor)
        body.update(
            {
                "base_currency": _required_text(base_currency, "base currency"),
                "quote_currency": _required_text(quote_currency, "quote currency"),
                "rate": _required_text(rate, "FX rate"),
                "rate_date": _required_text(rate_date, "FX rate date"),
                "data_source_id": data_source_id,
            }
        )
        if observed_at:
            body["observed_at"] = observed_at.strip()
        return _expect_object(
            self._json("POST", "/fx-rates", json=body), "/fx-rates"
        )

    def persist_normalization(
        self,
        *,
        price_component_id: int,
        source_artifact_id: int,
        actor: str,
        fx_rate_id: int | None = None,
        algorithm_version: str = "tokensql-normalization-v1",
    ) -> dict[str, Any]:
        body = _verified_body(actor)
        body.update(
            {
                "price_component_id": price_component_id,
                "source_artifact_id": source_artifact_id,
                "algorithm_version": _required_text(
                    algorithm_version, "algorithm version"
                ),
            }
        )
        if fx_rate_id is not None:
            body["fx_rate_id"] = fx_rate_id
        return _expect_object(
            self._json("POST", "/price-normalizations", json=body),
            "/price-normalizations",
        )

    def price_history(self, **filters: Any) -> list[dict[str, Any]]:
        return _expect_items(
            self._json("GET", "/prices/history", params=_clean(filters)),
            "/prices/history",
        )

    def compare_prices(
        self,
        *,
        left_component_id: int,
        right_component_id: int,
        left_normalization_id: int | None = None,
        right_normalization_id: int | None = None,
        algorithm_version: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "left_component_id": left_component_id,
            "right_component_id": right_component_id,
        }
        if left_normalization_id is not None:
            body["left_normalization_id"] = left_normalization_id
        if right_normalization_id is not None:
            body["right_normalization_id"] = right_normalization_id
        if algorithm_version:
            body["algorithm_version"] = algorithm_version
        return _expect_object(
            self._json("POST", "/prices/compare", json=body),
            "/prices/compare",
        )

    def export_prices_csv(self, **filters: Any) -> bytes:
        response = self._request(
            "GET",
            "/prices/export.csv",
            params=_clean(filters),
            headers={"Accept": "text/csv"},
        )
        return response.content


def _validate_workbook(filename: str, content: bytes) -> tuple[str, bytes]:
    name = Path(filename or "").name
    if not name.lower().endswith(".xlsx"):
        raise RequestValidationError("only .xlsx workbooks are accepted")
    if not isinstance(content, bytes):
        raise RequestValidationError("workbook content must be bytes")
    if not content:
        raise RequestValidationError("workbook is empty")
    if len(content) > MAX_EXCEL_BYTES:
        raise RequestValidationError(
            f"workbook exceeds the {MAX_EXCEL_BYTES // (1024 * 1024)} MiB limit"
        )
    return name, content


def _json_field(value: Mapping[str, Any]) -> str:
    try:
        return json.dumps(dict(value), ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise RequestValidationError("multipart metadata must be valid JSON") from exc


def _required_text(value: str, label: str) -> str:
    cleaned = " ".join(value.split()) if isinstance(value, str) else ""
    if not cleaned:
        raise RequestValidationError(f"{label} must not be empty")
    return cleaned


def _verified_body(actor: str) -> dict[str, Any]:
    return {"actor": _required_text(actor, "actor"), "verified": True}


def _review_body(
    reviewer: str, resolution: Mapping[str, Any] | None
) -> dict[str, Any]:
    cleaned = reviewer.strip()
    if not cleaned:
        raise RequestValidationError("reviewer must not be empty")
    if resolution is not None and not isinstance(resolution, Mapping):
        raise RequestValidationError("resolution must be a JSON object")
    return {"reviewer": cleaned, "resolution": dict(resolution or {})}


def _resource_id(value: int | str) -> str:
    text = str(value).strip()
    if not text or "/" in text or "?" in text or "#" in text:
        raise RequestValidationError("invalid resource id")
    return text


def _clean(values: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in (None, "")}


def _expect_object(payload: Any, source: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise APIError(f"{source} returned an unexpected response shape")
    return payload


def _expect_items(payload: Any, source: str) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        payload = payload.get("items", payload.get("results", payload.get("data")))
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise APIError(f"{source} returned an unexpected response shape")
    return payload


def _safe_error_detail(response: httpx.Response) -> Any:
    try:
        payload = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    detail = payload.get("detail")
    if isinstance(detail, str):
        return detail[:500]
    if isinstance(detail, list):
        return [_redact_error_item(item) for item in detail[:20]]
    return None


def _redact_error_item(value: Any) -> Any:
    """Remove request data and likely secrets from validation diagnostics."""
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            lowered = key.lower()
            sensitive = ("password", "secret", "api_key", "token", "content", "file")
            if lowered == "input" or any(marker in lowered for marker in sensitive):
                safe[key] = "[redacted]"
            else:
                safe[key] = _redact_error_item(item)
        return safe
