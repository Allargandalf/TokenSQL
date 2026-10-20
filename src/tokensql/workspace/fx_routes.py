"""Fetch authoritative reference FX; never overwrite source quote amounts."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext
import logging
import xml.etree.ElementTree as ET

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from .store import RevisionConflictError

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
logger = logging.getLogger(__name__)


def parse_ecb(xml: bytes, fetched_at: datetime | None = None) -> list[dict]:
    fetched = fetched_at or datetime.now(timezone.utc)
    root = ET.fromstring(xml)
    dated = next((node for node in root.iter() if "time" in node.attrib), None)
    if dated is None:
        raise ValueError("ECB feed has no observation date")
    observed = datetime.fromisoformat(dated.attrib["time"]).replace(tzinfo=timezone.utc)
    rates = {node.attrib["currency"]: Decimal(node.attrib["rate"])
             for node in dated.iter() if "currency" in node.attrib}
    if not {"USD", "HKD", "CNY"} <= rates.keys():
        raise ValueError("ECB feed lacks required currencies")
    if any(not value.is_finite() or value <= 0 for value in rates.values()):
        raise ValueError("ECB feed contains invalid rates")
    if observed > fetched + timedelta(days=1) or fetched - observed > timedelta(days=10):
        raise ValueError("ECB feed observation is future-dated or stale")
    with localcontext() as context:
        context.prec = 60
        return [{"currency": currency, "cny_per_unit": str(rates["CNY"] / rates[currency]),
                 "source": "ECB daily reference (not a settlement/real-time tick rate)",
                 "source_url": ECB_URL, "observed_at": observed.isoformat(),
                 "fetched_at": fetched.isoformat(),
                 "expires_at": (observed + timedelta(days=7)).isoformat()}
                for currency in ("USD", "HKD")]


async def refresh_context(store, expected_revision: int | None = None,
                          *, transport=None) -> dict:
    before = store.pricing_context()
    expected = before["revision"] if expected_revision is None else expected_revision
    if expected != before["revision"]:
        raise RevisionConflictError("pricing context revision changed; reload before refresh")
    async with httpx.AsyncClient(timeout=20, follow_redirects=True, trust_env=False,
                                 transport=transport) as client:
        response = await client.get(ECB_URL)
        response.raise_for_status()
        if len(response.content) > 1000000:
            raise ValueError("FX feed exceeds expected size")
        rates = parse_ecb(response.content)
    # An unchanged daily reference does not invalidate pending human reviews.
    def signature(items):
        def instant(value):
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")) if value else None
        return sorted((item["currency"], Decimal(str(item["cny_per_unit"])),
                       instant(item.get("observed_at")), instant(item.get("expires_at")),
                       str(item.get("source_url")))
                      for item in items if item["currency"] != "CNY")
    if signature(before["fx_rates"]) == signature(rates):
        latest = store.pricing_context()
        if latest["revision"] != expected:
            raise RevisionConflictError("pricing context changed during FX refresh")
        return latest
    return store.set_pricing_context(fx_rates=rates,
        token_equivalences=before["token_equivalences"],
        expected_revision=expected, actor="fx-reference:ecb")


class FXRefreshRequest(BaseModel):
    expected_revision: int = Field(ge=1)


def create_fx_router(store) -> APIRouter:
    router = APIRouter(prefix="/api/workspace", tags=["reference-fx"])

    @router.post("/pricing-context/refresh-fx")
    async def refresh(request: FXRefreshRequest):
        try:
            return await refresh_context(store, request.expected_revision)
        except RevisionConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (httpx.HTTPError, ValueError, ET.ParseError) as exc:
            logger.warning("Reference FX refresh failed: %s", type(exc).__name__)
            raise HTTPException(502, "Reference FX unavailable; previous rates preserved") from exc
    return router


async def refresh_loop(store, interval: float) -> None:
    while True:
        try:
            await refresh_context(store)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Automatic reference FX unchanged: %s", type(exc).__name__)
        await asyncio.sleep(interval)
