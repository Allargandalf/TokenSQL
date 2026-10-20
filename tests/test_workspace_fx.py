import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal, localcontext

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from tokensql.workspace import fx_routes
from tokensql.workspace.store import WorkspaceStore, RevisionConflictError


def feed(day=None, usd="1.2", cny="8.4", hkd="9.6"):
    day = day or datetime.now(timezone.utc).date().isoformat()
    return (f'<Envelope><Cube><Cube time="{day}"><Cube currency="USD" rate="{usd}"/>'
            f'<Cube currency="CNY" rate="{cny}"/><Cube currency="HKD" rate="{hkd}"/>'
            '</Cube></Cube></Envelope>').encode()


def test_ecb_decimal_and_dates():
    now = datetime.now(timezone.utc)
    rates = fx_routes.parse_ecb(feed(), now)
    assert {r["currency"]: r["cny_per_unit"] for r in rates} == {"USD": "7", "HKD": "0.875"}
    for delta in (-11, 3):
        day = (now + timedelta(days=delta)).date().isoformat()
        with pytest.raises(ValueError):
            fx_routes.parse_ecb(feed(day=day), now)
    with pytest.raises(ValueError):
        fx_routes.parse_ecb(feed(usd="0"), now)
    with pytest.raises(ValueError):
        fx_routes.parse_ecb(b"<Envelope/>", now)


def test_refresh_version_and_unchanged_reference(tmp_path):
    store = WorkspaceStore(tmp_path / "test.sqlite3")
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=feed()))
    original = store.catalog_snapshot()["quotes"]
    first = asyncio.run(fx_routes.refresh_context(store, 1, transport=transport))
    second = asyncio.run(fx_routes.refresh_context(store, first["revision"], transport=transport))
    assert second["revision"] == first["revision"] == 2
    assert store.catalog_snapshot()["quotes"] == original
    assert len(store.pricing_context_history()) == 2
    with pytest.raises(RevisionConflictError):
        asyncio.run(fx_routes.refresh_context(store, 1, transport=transport))
    store.close()


def test_failed_feed_preserves_context(tmp_path):
    store = WorkspaceStore(tmp_path / "test.sqlite3")
    before = store.pricing_context()
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=b"<bad/>"))
    with pytest.raises(ValueError):
        asyncio.run(fx_routes.refresh_context(store, 1, transport=transport))
    assert store.pricing_context() == before
    store.close()


def test_refresh_endpoint_error_contract(tmp_path, monkeypatch):
    store = WorkspaceStore(tmp_path / "test.sqlite3")
    app = FastAPI()
    app.include_router(fx_routes.create_fx_router(store))
    async def conflict(*args):
        raise RevisionConflictError("changed")
    monkeypatch.setattr(fx_routes, "refresh_context", conflict)
    with TestClient(app) as client:
        path = "/api/workspace/pricing-context/refresh-fx"
        assert client.post(path, json={"expected_revision": 1}).status_code == 409
        assert client.post(path, json={"expected_revision": 0}).status_code == 422
        async def unavailable(*args):
            raise ValueError("broken feed")
        monkeypatch.setattr(fx_routes, "refresh_context", unavailable)
        assert client.post(path, json={"expected_revision": 1}).status_code == 502
    store.close()


def test_context_change_during_feed_fetch_does_not_erase_concurrent_edit(tmp_path):
    store = WorkspaceStore(tmp_path / "test.sqlite3")
    def response(request):
        previous = store.pricing_context()
        store.set_pricing_context(fx_rates=[], token_equivalences=[],
                                  expected_revision=previous["revision"], actor="concurrent-editor")
        return httpx.Response(200, content=feed())
    with pytest.raises(RevisionConflictError):
        asyncio.run(fx_routes.refresh_context(store, 1,
            transport=httpx.MockTransport(response)))
    assert store.pricing_context()["revision"] == 2
    assert {rate["currency"] for rate in store.pricing_context()["fx_rates"]} == {"CNY"}
    store.close()
