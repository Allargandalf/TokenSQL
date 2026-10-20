"""Scope and evidence contracts for explicitly requested, review-only official lookups.

These checks validate provenance metadata, not the truth of a remote web page. Human
review must still verify that the linked page is the model vendor's actual price list.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import ipaddress
import json
from typing import Any
from urllib.parse import urlparse


def validate_lookup_bundle(job: dict[str, Any], bundle: dict[str, Any],
                           snapshot: dict[str, Any]) -> None:
    if job.get("intent") != "official_price_lookup":
        return
    target = job["lookup_model_id"]
    billing = job.get("lookup_billing_item")
    if any(bundle.get(key) for key in ("create", "update", "delete")):
        raise ValueError("official lookup cannot create, edit or delete supplier quotes")
    for item in bundle.get("models", []):
        if not isinstance(item, dict) or item.get("model_id") != target:
            raise ValueError("official lookup must retain the requested model_id")
    for item in bundle.get("official_prices", []):
        if not isinstance(item, dict) or item.get("model_id") != target:
            raise ValueError("official lookup price must match the requested model_id")
        if billing and item.get("billing_item") != billing:
            raise ValueError("official lookup price is outside the requested billing item")
        if item.get("amount") is None:
            continue
        if item.get("explicit_official") is not True:
            raise ValueError("a retrieved official price requires explicit_official=true")
        source = item.get("source")
        parsed = urlparse(source if isinstance(source, str) else "")
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("a retrieved official price requires a public HTTPS source URL")
        host = parsed.hostname.casefold()
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
            raise ValueError("official source URL must not point to a local service")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError("official source URL must not point to a private address")
        try:
            evidence = json.loads(item.get("source_ref") or "")
        except (TypeError, ValueError):
            raise ValueError("official source_ref must contain JSON retrieval evidence") from None
        if not isinstance(evidence, dict) or any(
                not isinstance(evidence.get(key), str) or not evidence[key].strip()
                for key in ("retrieved_at", "region", "tier", "evidence")):
            raise ValueError("official evidence needs retrieved_at, region, tier and exact price evidence")
        try:
            retrieved = date.fromisoformat(evidence["retrieved_at"])
        except ValueError:
            raise ValueError("official retrieved_at must be an ISO calendar date") from None
        if retrieved > datetime.now(timezone.utc).date() + timedelta(days=1):
            raise ValueError("official retrieval date must not be in the future")
