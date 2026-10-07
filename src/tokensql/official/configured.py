"""Strict built-in adapter for reviewed official JSON price documents.

This module is intentionally deterministic.  It does not call an LLM, choose
catalog identifiers, perform network I/O, or mark any candidate verified.
"""
from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from tokensql.db.models import Offering
from tokensql.official.base import FetchResult


_CONFIG_ENV = "TOKENSQL_OFFICIAL_ADAPTER_CONFIG"
_MAX_CONFIG_BYTES = 256_000
_METRIC_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
_CURRENCY = re.compile(r"^[A-Za-z]{3}$")
_INVALID_POINTER_ESCAPE = re.compile(r"~(?:[^01]|$)")


class ConfiguredAdapterError(ValueError):
    """A reviewed adapter config or official JSON document is invalid."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TextValue(_StrictModel):
    """Read text from a record or use an explicitly reviewed literal."""

    pointer: str | None = None
    literal: str | None = Field(default=None, min_length=1, max_length=512)

    @field_validator("pointer")
    @classmethod
    def validate_pointer(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_pointer(value)
        return value

    @field_validator("literal")
    @classmethod
    def clean_literal(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("literal cannot be blank")
        return cleaned

    @model_validator(mode="after")
    def exactly_one_source(self) -> "TextValue":
        if (self.pointer is None) == (self.literal is None):
            raise ValueError("exactly one of pointer or literal is required")
        return self

    def resolve(self, record: Mapping[str, Any], label: str) -> str:
        value: Any = self.literal
        if self.pointer is not None:
            value = _resolve_pointer(record, self.pointer)
        if not isinstance(value, str) or not value.strip():
            raise ConfiguredAdapterError(f"{label} must resolve to non-empty text")
        return value.strip()


class ComponentRule(_StrictModel):
    """Reviewed mapping from one official JSON value to one billing metric."""

    metric_code: str
    amount_pointer: str
    unit_name: str = Field(min_length=1, max_length=64)
    unit_quantity: Decimal = Field(gt=0)
    conditions: dict[str, str | int | bool] = Field(default_factory=dict)
    tier_start: Decimal = Field(default=Decimal("0"), ge=0)
    tier_end: Decimal | None = Field(default=None, gt=0)

    @field_validator("metric_code")
    @classmethod
    def validate_metric_code(cls, value: str) -> str:
        cleaned = value.strip()
        if not _METRIC_CODE.fullmatch(cleaned):
            raise ValueError("metric_code has an invalid format")
        return cleaned

    @field_validator("amount_pointer")
    @classmethod
    def validate_amount_pointer(cls, value: str) -> str:
        _validate_pointer(value)
        return value

    @field_validator("unit_name")
    @classmethod
    def clean_unit(cls, value: str) -> str:
        cleaned = value.strip().casefold()
        if not cleaned:
            raise ValueError("unit_name cannot be blank")
        return cleaned

    @model_validator(mode="after")
    def validate_tier(self) -> "ComponentRule":
        if self.tier_end is not None and self.tier_end <= self.tier_start:
            raise ValueError("tier_end must be greater than tier_start")
        return self


class OfferingRoute(_StrictModel):
    """One reviewed official endpoint mapped to one exact catalog offering."""

    provider_slug: str = Field(min_length=1, max_length=160)
    api_model_id: str | None = Field(default=None, min_length=1, max_length=255)
    provider_model_name: str | None = Field(
        default=None, min_length=1, max_length=255
    )
    region: str = Field(default="global", min_length=1, max_length=64)
    url: str
    records_pointer: str = ""
    plan_name: TextValue
    currency_code: TextValue
    effective_from_pointer: str | None = None
    effective_to_pointer: str | None = None
    components: tuple[ComponentRule, ...] = Field(min_length=1)
    max_records: int = Field(default=100, ge=1, le=1_000)

    @field_validator("provider_slug", "region")
    @classmethod
    def clean_casefolded(cls, value: str) -> str:
        cleaned = value.strip().casefold()
        if not cleaned:
            raise ValueError("selector value cannot be blank")
        return cleaned

    @field_validator("api_model_id", "provider_model_name")
    @classmethod
    def clean_optional_selector(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("selector value cannot be blank")
        return cleaned

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        candidate = value.strip()
        parsed = urlsplit(candidate)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("url contains an invalid port") from exc
        if (
            parsed.scheme.casefold() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port not in (None, 443)
        ):
            raise ValueError(
                "url must be credential-free HTTPS on the default port without "
                "query or fragment"
            )
        return candidate

    @field_validator(
        "records_pointer", "effective_from_pointer", "effective_to_pointer"
    )
    @classmethod
    def validate_json_pointer(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_pointer(value)
        return value

    @model_validator(mode="after")
    def validate_selectors_and_components(self) -> "OfferingRoute":
        if self.api_model_id is None and self.provider_model_name is None:
            raise ValueError(
                "api_model_id or provider_model_name is required for exact mapping"
            )
        metric_codes = [item.metric_code for item in self.components]
        if len(metric_codes) != len(set(metric_codes)):
            raise ValueError("component metric_code values must be unique")
        return self

    def matches(self, offering: Offering) -> bool:
        provider = offering.provider
        return (
            provider.slug.casefold() == self.provider_slug
            and offering.region.casefold() == self.region
            and (
                self.api_model_id is None
                or offering.api_model_id == self.api_model_id
            )
            and (
                self.provider_model_name is None
                or offering.provider_model_name == self.provider_model_name
            )
        )


class ConfiguredOfficialAdapterConfig(_StrictModel):
    schema_version: Literal[1]
    source_name: str = Field(min_length=1, max_length=255)
    routes: tuple[OfferingRoute, ...] = Field(min_length=1, max_length=1_000)

    @field_validator("source_name")
    @classmethod
    def clean_source_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("source_name cannot be blank")
        return cleaned

    @model_validator(mode="after")
    def unique_routes(self) -> "ConfiguredOfficialAdapterConfig":
        selectors = [
            (
                item.provider_slug,
                item.api_model_id,
                item.provider_model_name,
                item.region,
            )
            for item in self.routes
        ]
        if len(selectors) != len(set(selectors)):
            raise ValueError("route offering selectors must be unique")
        urls = [item.url for item in self.routes]
        if len(urls) != len(set(urls)):
            raise ValueError("each route must use a unique official URL")
        return self


class ConfiguredOfficialJsonAdapter:
    """Parse an explicitly configured official JSON schema without inference."""

    def __init__(self, config: ConfiguredOfficialAdapterConfig) -> None:
        self.config = config

    @property
    def source_name(self) -> str:
        return self.config.source_name

    def _routes_for(self, offering: Offering) -> tuple[OfferingRoute, ...]:
        return tuple(route for route in self.config.routes if route.matches(offering))

    def supports(self, offering: Offering) -> bool:
        matches = self._routes_for(offering)
        if len(matches) > 1:
            raise ConfiguredAdapterError("multiple configured routes match offering")
        return len(matches) == 1

    def url_for(self, offering: Offering) -> str:
        matches = self._routes_for(offering)
        if len(matches) != 1:
            raise ConfiguredAdapterError(
                "offering must match exactly one configured official route"
            )
        return matches[0].url

    def parse(self, result: FetchResult) -> Sequence[Mapping[str, Any]]:
        if result.status != "fetched" or result.raw_body is None:
            raise ConfiguredAdapterError("only a fetched response with raw body can parse")
        if result.content_type != "application/json":
            raise ConfiguredAdapterError(
                "configured JSON adapter requires application/json evidence"
            )
        routes = [
            route for route in self.config.routes if route.url == result.requested_url
        ]
        if len(routes) != 1:
            raise ConfiguredAdapterError(
                "requested URL does not identify exactly one configured route"
            )
        route = routes[0]
        try:
            text = result.raw_body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfiguredAdapterError("official JSON is not valid UTF-8") from exc
        document = _loads_strict_json(text, "official JSON")
        records = _resolve_pointer(document, route.records_pointer)
        if not isinstance(records, list):
            raise ConfiguredAdapterError("records_pointer must resolve to an array")
        if not records:
            raise ConfiguredAdapterError("official JSON contains no price records")
        if len(records) > route.max_records:
            raise ConfiguredAdapterError(
                f"official JSON exceeds configured max_records={route.max_records}"
            )
        return tuple(self._candidate(route, record, result) for record in records)

    @staticmethod
    def _candidate(
        route: OfferingRoute,
        record: Any,
        result: FetchResult,
    ) -> Mapping[str, Any]:
        if not isinstance(record, Mapping):
            raise ConfiguredAdapterError("each price record must be an object")
        plan_name = route.plan_name.resolve(record, "plan_name")
        currency = route.currency_code.resolve(record, "currency_code").upper()
        if not _CURRENCY.fullmatch(currency):
            raise ConfiguredAdapterError("currency_code must contain three letters")
        effective_from = result.fetched_at.isoformat()
        if route.effective_from_pointer is not None:
            effective_from = _iso_datetime(
                _resolve_pointer(record, route.effective_from_pointer),
                "effective_from",
            )
        effective_to = None
        if route.effective_to_pointer is not None:
            effective_to = _iso_datetime(
                _resolve_pointer(record, route.effective_to_pointer),
                "effective_to",
            )
        components = []
        for rule in route.components:
            amount = _decimal_text(
                _resolve_pointer(record, rule.amount_pointer),
                f"amount for {rule.metric_code}",
            )
            component: dict[str, Any] = {
                "metric_code": rule.metric_code,
                "component_type": "monetary",
                "amount": amount,
                "currency": currency,
                "unit_name": rule.unit_name,
                "unit_quantity": str(rule.unit_quantity),
                "tier_start": str(rule.tier_start),
                "conditions": dict(rule.conditions),
            }
            if rule.tier_end is not None:
                component["tier_end"] = str(rule.tier_end)
            components.append(component)
        candidate: dict[str, Any] = {
            "schema_version": 1,
            "plan_name": plan_name,
            "currency_code": currency,
            "observed_at": result.fetched_at.isoformat(),
            "effective_from": effective_from,
            "components": components,
        }
        if effective_to is not None:
            candidate["effective_to"] = effective_to
        return candidate


def load_config(path: str | Path) -> ConfiguredOfficialAdapterConfig:
    """Load a small strict JSON config without following unbounded input."""
    try:
        candidate = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ConfiguredAdapterError("official adapter config file was not found") from exc
    if not candidate.is_file():
        raise ConfiguredAdapterError("official adapter config must be a file")
    size = candidate.stat().st_size
    if size <= 0 or size > _MAX_CONFIG_BYTES:
        raise ConfiguredAdapterError(
            f"official adapter config must be 1..{_MAX_CONFIG_BYTES} bytes"
        )
    try:
        text = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfiguredAdapterError("official adapter config is unreadable") from exc
    payload = _loads_strict_json(text, "official adapter config")
    try:
        return ConfiguredOfficialAdapterConfig.model_validate(payload)
    except ValidationError as exc:
        raise ConfiguredAdapterError(f"official adapter config is invalid: {exc}") from exc


def create_adapter() -> ConfiguredOfficialJsonAdapter:
    """CLI factory using TOKENSQL_OFFICIAL_ADAPTER_CONFIG as the reviewed file."""
    path = os.environ.get(_CONFIG_ENV, "").strip()
    if not path:
        raise ConfiguredAdapterError(f"{_CONFIG_ENV} must name a reviewed JSON file")
    return ConfiguredOfficialJsonAdapter(load_config(path))


def _validate_pointer(pointer: str) -> None:
    if pointer and not pointer.startswith("/"):
        raise ValueError("JSON pointer must be empty or start with /")
    if _INVALID_POINTER_ESCAPE.search(pointer):
        raise ValueError("JSON pointer contains an invalid ~ escape")


def _resolve_pointer(document: Any, pointer: str) -> Any:
    _validate_pointer(pointer)
    current = document
    if pointer == "":
        return current
    for raw_token in pointer[1:].split("/"):
        token = raw_token.replace("~1", "/").replace("~0", "~")
        if isinstance(current, Mapping):
            if token not in current:
                raise ConfiguredAdapterError(f"JSON pointer {pointer!r} is missing")
            current = current[token]
        elif isinstance(current, list):
            if not token.isdigit() or (len(token) > 1 and token.startswith("0")):
                raise ConfiguredAdapterError(f"JSON pointer {pointer!r} has bad index")
            index = int(token)
            if index >= len(current):
                raise ConfiguredAdapterError(f"JSON pointer {pointer!r} is missing")
            current = current[index]
        else:
            raise ConfiguredAdapterError(f"JSON pointer {pointer!r} crosses a scalar")
    return current


def _loads_strict_json(text: str, label: str) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ConfiguredAdapterError(f"{label} contains duplicate key {key!r}")
            value[key] = item
        return value

    try:
        return json.loads(
            text,
            parse_float=Decimal,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ConfiguredAdapterError(f"{label} contains non-finite number {value}")
            ),
            object_pairs_hook=object_pairs,
        )
    except ConfiguredAdapterError:
        raise
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ConfiguredAdapterError(f"{label} is not valid JSON") from exc


def _decimal_text(value: Any, label: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ConfiguredAdapterError(f"{label} must be a decimal string or number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ConfiguredAdapterError(f"{label} is not a valid decimal") from exc
    if not result.is_finite() or result < 0:
        raise ConfiguredAdapterError(f"{label} must be finite and non-negative")
    return str(result)


def _iso_datetime(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ConfiguredAdapterError(f"{label} must be an ISO datetime string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfiguredAdapterError(f"{label} must be an ISO datetime string") from exc
    if parsed.tzinfo is None:
        raise ConfiguredAdapterError(f"{label} must include a timezone")
    return parsed.isoformat()
