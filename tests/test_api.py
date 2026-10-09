"""Acceptance tests for the FastAPI boundary."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import hashlib
from io import BytesIO
from pathlib import Path
import asyncio
import importlib
import json
import zipfile
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from tokensql.api.app import (
    MAX_UPLOAD_BYTES,
    _save_xlsx,
    create_app,
)
from tokensql.api.dependencies import get_session
from tokensql.db.base import Base, utc_now
from tokensql.db.models import (
    AuditEvent,
    BillingMetric,
    DataSource,
    ImportBatch,
    ImportRow,
    Manufacturer,
    Offering,
    PriceComponent,
    PriceSnapshot,
    PricingPlan,
    Provider,
    ReviewItem,
    SourceArtifact,
    StagedQuote,
    StandardModel,
)


def _xlsx_bytes() -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "报价"
    sheet.append(["模型", "报价或折扣（最低）"])
    sheet.append(["alpha", "8折"])
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def _archive_bytes(
    *,
    extra: tuple[tuple[str, bytes], ...] = (),
    dimension: str = "A1:B2",
) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in extra:
            archive.writestr(name, payload)
        archive.writestr("[Content_Types].xml", b"<Types/>")
        archive.writestr("xl/workbook.xml", b"<workbook/>")
        worksheet = (
            '<worksheet xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main"><dimension ref="'
            + dimension
            + '"/></worksheet>'
        )
        archive.writestr("xl/worksheets/sheet1.xml", worksheet.encode())
    return output.getvalue()


def _seed(factory: sessionmaker) -> dict[str, int]:
    now = utc_now()
    with factory.begin() as session:
        manufacturer = Manufacturer(name="Acme", slug="acme")
        provider = Provider(
            name="Vendor", slug="vendor", provider_type="reseller"
        )
        source = DataSource(
            name="API fixture source",
            source_type="provider_excel",
            authority_level="secondary",
        )
        session.add_all([manufacturer, provider, source])
        session.flush()

        first_model = StandardModel(
            manufacturer=manufacturer,
            canonical_name="alpha",
            display_name="Alpha",
            modality="text",
        )
        second_model = StandardModel(
            manufacturer=manufacturer,
            canonical_name="beta",
            display_name="Beta",
            modality="text",
        )
        session.add_all([first_model, second_model])
        session.flush()

        offering = Offering(
            provider=provider,
            standard_model=first_model,
            provider_model_name="alpha",
            region="global",
        )
        session.add(offering)
        session.flush()
        plan = PricingPlan(
            offering=offering,
            name="standard",
            billing_mode="pay_as_you_go",
            currency_code="CNY",
            effective_from=now - timedelta(days=2),
        )
        input_metric = BillingMetric(
            code="input_tokens",
            display_name="Input tokens",
            dimension="token",
            direction="input",
            unit_name="token",
            unit_quantity=1_000_000,
            comparable_group="tokens",
        )
        output_metric = BillingMetric(
            code="output_tokens",
            display_name="Output tokens",
            dimension="token",
            direction="output",
            unit_name="token",
            unit_quantity=1_000_000,
            comparable_group="tokens",
        )
        artifact = SourceArtifact(
            data_source=source,
            uri="memory://fixture.xlsx",
            original_filename="fixture.xlsx",
            media_type=(
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ),
            sha256="a" * 64,
            fetched_at=now,
        )
        session.add_all([plan, input_metric, output_metric, artifact])
        session.flush()

        snapshot = PriceSnapshot(
            pricing_plan=plan,
            data_source=source,
            source_artifact=artifact,
            observed_at=now - timedelta(hours=1),
            effective_from=now - timedelta(hours=1),
            currency_code="CNY",
            verification_status="verified",
        )
        session.add(snapshot)
        session.flush()
        left = PriceComponent(
            price_snapshot_id=snapshot.id,
            billing_metric=input_metric,
            component_type="monetary",
            tier_start=0,
            condition_key="default",
            raw_amount=Decimal("1.25"),
            raw_currency_code="CNY",
            raw_unit_name="token",
            raw_unit_quantity=1_000_000,
        )
        right = PriceComponent(
            price_snapshot_id=snapshot.id,
            billing_metric=output_metric,
            component_type="monetary",
            tier_start=0,
            condition_key="default",
            raw_amount=Decimal("2.50"),
            raw_currency_code="CNY",
            raw_unit_name="token",
            raw_unit_quantity=1_000_000,
        )

        batch = ImportBatch(
            source_artifact=artifact,
            provider=provider,
            idempotency_key="fixture-review",
            parser_version="test",
            status="partial",
            total_rows=1,
            review_rows=1,
        )
        row = ImportRow(
            import_batch=batch,
            sheet_name="报价",
            row_number=2,
            row_hash="b" * 64,
            raw_data={},
            status="staged",
        )
        staged = StagedQuote(
            import_row=row,
            provider=provider,
            extracted_model_name="unknown",
            review_status="needs_review",
            extracted_quote={},
            validation_errors=[],
        )
        approve_item = ReviewItem(
            staged_quote=staged,
            review_type="model_match",
            severity="warning",
            reason="model unresolved",
        )
        reject_item = ReviewItem(
            staged_quote=staged,
            review_type="price",
            severity="warning",
            reason="price unclear",
        )
        session.add_all([left, right, batch, row, staged, approve_item, reject_item])
        session.flush()
        return {
            "source": source.id,
            "provider": provider.id,
            "model": first_model.id,
            "offering": offering.id,
            "artifact": artifact.id,
            "input_metric": input_metric.id,
            "unreviewed_staged": staged.id,
            "other_model": second_model.id,
            "plan": plan.id,
            "left": left.id,
            "right": right.id,
            "approve_review": approve_item.id,
            "reject_review": reject_item.id,
        }


def _seed_management(client: TestClient, ids: dict[str, int]) -> dict[str, int]:
    factory = client.app.state.test_session_factory
    now = utc_now()
    with factory.begin() as session:
        source = session.get(DataSource, ids["source"])
        provider = session.get(Provider, ids["provider"])
        offering = session.get(Offering, ids["offering"])
        artifact = session.get(SourceArtifact, ids["artifact"])
        metric = session.get(BillingMetric, ids["input_metric"])
        batch = ImportBatch(
            source_artifact=artifact,
            provider=provider,
            idempotency_key="management-reviewed",
            parser_version="test",
            status="completed",
            total_rows=1,
            accepted_rows=1,
        )
        row = ImportRow(
            import_batch=batch,
            sheet_name="reviewed",
            row_number=1,
            row_hash="d" * 64,
            raw_data={},
            status="accepted",
        )
        reviewed = StagedQuote(
            import_row=row,
            provider=provider,
            matched_standard_model_id=ids["model"],
            matched_offering=offering,
            extracted_model_name="alpha",
            extracted_plan_name="standard",
            match_method="manual",
            match_confidence=Decimal("1"),
            review_status="approved",
            extracted_quote={
                "origin": "third_party",
                "observed_at": now.isoformat(),
                "effective_from": now.isoformat(),
                "currency_code": "CNY",
                "components": [
                    {
                        "billing_metric_id": metric.id,
                        "component_type": "monetary",
                        "raw_amount": "1.10",
                        "raw_currency_code": "CNY",
                        "raw_unit_name": "token",
                        "raw_unit_quantity": "1000000",
                    }
                ],
            },
            validation_errors=[],
        )
        reviewed.review_items.append(
            ReviewItem(
                review_type="price",
                status="resolved",
                severity="warning",
                reason="confirm staged price",
                resolution={"approved": True},
                resolved_by="reviewer",
                resolved_at=now,
            )
        )
        session.add(reviewed)

        official_source = DataSource(
            name="Official API management fixture",
            source_type="official",
            authority_level="primary",
            base_url="https://official.example/pricing",
        )
        official_provider = Provider(
            name="Official Vendor",
            slug="official-vendor",
            provider_type="official",
        )
        session.add_all([official_source, official_provider])
        session.flush()
        official_offering = Offering(
            provider=official_provider,
            standard_model_id=ids["model"],
            provider_model_name="alpha-official",
            region="global",
        )
        session.add(official_offering)
        session.flush()
        official_plan = PricingPlan(
            offering=official_offering,
            name="official-standard",
            billing_mode="pay_as_you_go",
            currency_code="CNY",
            effective_from=now - timedelta(days=1),
        )
        official_artifact = SourceArtifact(
            data_source=official_source,
            uri="https://official.example/pricing/alpha",
            media_type="text/html",
            sha256="e" * 64,
            fetched_at=now,
        )
        session.add_all([official_plan, official_artifact])
        session.flush()
        official_batch = ImportBatch(
            source_artifact=official_artifact,
            provider=official_provider,
            idempotency_key="management-official",
            parser_version="official-v1",
            status="partial",
            total_rows=1,
            review_rows=1,
        )
        official_row = ImportRow(
            import_batch=official_batch,
            sheet_name="official",
            row_number=1,
            row_hash="f" * 64,
            raw_data={},
            status="staged",
        )
        official_staged = StagedQuote(
            import_row=official_row,
            provider=official_provider,
            matched_standard_model_id=ids["model"],
            matched_offering=official_offering,
            extracted_model_name="alpha-official",
            extracted_plan_name="official-standard",
            match_method="exact",
            match_confidence=Decimal("1"),
            review_status="needs_review",
            extracted_quote={
                "origin": "official",
                "observed_at": now.isoformat(),
                "effective_from": now.isoformat(),
                "currency_code": "CNY",
                "components": [
                    {
                        "billing_metric_id": metric.id,
                        "component_type": "monetary",
                        "raw_amount": "0.90",
                        "raw_currency_code": "CNY",
                        "raw_unit_name": "token",
                        "raw_unit_quantity": "1000000",
                    }
                ],
            },
            validation_errors=["official_candidate_requires_explicit_verification"],
        )
        official_staged.review_items.append(
            ReviewItem(
                review_type="price",
                status="open",
                severity="warning",
                reason="official candidate requires explicit verification",
            )
        )
        session.add(official_staged)
        session.flush()
        return {
            "reviewed_staged": reviewed.id,
            "official_staged": official_staged.id,
            "official_source": official_source.id,
            "official_artifact": official_artifact.id,
            "official_plan": official_plan.id,
        }


@pytest.fixture
def api() -> tuple[TestClient, dict[str, int]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    ids = _seed(factory)
    application = create_app()
    application.state.test_session_factory = factory

    def override_session():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    application.dependency_overrides[get_session] = override_session
    with TestClient(application) as client:
        yield client, ids
    application.dependency_overrides.clear()
    engine.dispose()


def test_health_and_catalog_lists(api) -> None:
    client, ids = api
    assert client.get("/health").json() == {
        "status": "ok",
        "database": "available",
        "qwen": {"enabled": False, "configured": False},
    }
    assert client.get("/models").json()[0]["canonical_name"] == "alpha"
    assert client.get("/providers").json()[0]["id"] == ids["provider"]
    assert client.get("/pricing-plans").json()[0]["id"] == ids["plan"]


def test_health_reports_qwen_configuration_without_probe(api, monkeypatch) -> None:
    client, _ = api
    api_module = importlib.import_module("tokensql.api.app")
    monkeypatch.setattr(
        api_module,
        "get_settings",
        lambda: SimpleNamespace(qwen_enabled=True, qwen_configured=True),
    )

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["qwen"] == {"enabled": True, "configured": True}


def test_profile_rejects_bad_extension_and_oversized_upload(api) -> None:
    client, _ = api
    payload = _xlsx_bytes()
    response = client.post(
        "/imports/profile",
        files={"file": ("quote.xlsx", payload)},
        data={"context": "{}"},
    )
    assert response.status_code == 200
    assert response.json()["source_name"].endswith(".xlsx")
    assert response.json()["sheets"][0]["title"] == "报价"

    assert client.post(
        "/imports/profile",
        files={"file": ("quote.xls", payload)},
        data={"context": "{}"},
    ).status_code == 415
    assert client.post(
        "/imports/profile",
        files={"file": ("large.xlsx", b"x" * (MAX_UPLOAD_BYTES + 1))},
        data={"context": "{}"},
    ).status_code == 413


def test_profile_rejects_malicious_archives_without_temp_residue(
    api,
    tmp_path,
    monkeypatch,
) -> None:
    client, _ = api
    api_module = importlib.import_module("tokensql.api.app")
    monkeypatch.setattr(api_module.tempfile, "tempdir", str(tmp_path))

    def reject(payload: bytes, expected: str) -> None:
        response = client.post(
            "/imports/profile",
            files={"file": ("attack.xlsx", payload)},
            data={"context": "{}"},
        )
        assert response.status_code == 422
        assert expected in response.json()["detail"]
        assert list(tmp_path.glob("tokensql-upload-*")) == []

    reject(
        _archive_bytes(extra=(("../escape", b"x"),)),
        "unsafe ZIP member path",
    )
    reject(
        _archive_bytes(dimension="A1:XFD1048576"),
        "declared dimension",
    )

    monkeypatch.setattr(api_module, "MAX_ZIP_MEMBERS", 3)
    reject(_archive_bytes(extra=(("extra", b"x"),)), "too many ZIP members")
    monkeypatch.setattr(api_module, "MAX_ZIP_MEMBERS", 1_024)

    monkeypatch.setattr(api_module, "MAX_ZIP_MEMBER_BYTES", 5)
    reject(_archive_bytes(extra=(("large", b"123456"),)), "member is too large")
    monkeypatch.setattr(api_module, "MAX_ZIP_MEMBER_BYTES", 25 * 1024 * 1024)

    monkeypatch.setattr(api_module, "MAX_ZIP_EXPANDED_BYTES", 12)
    reject(
        _archive_bytes(extra=(("expanded", b"1234567"),)),
        "expands beyond",
    )
    monkeypatch.setattr(api_module, "MAX_ZIP_EXPANDED_BYTES", 100 * 1024 * 1024)

    monkeypatch.setattr(api_module, "MAX_ZIP_COMPRESSION_RATIO", 2)
    reject(
        _archive_bytes(extra=(("ratio", b"x" * 1_000),)),
        "compression ratio",
    )


def test_import_context_failure_removes_temp_file(
    api,
    tmp_path,
    monkeypatch,
) -> None:
    client, ids = api
    api_module = importlib.import_module("tokensql.api.app")
    monkeypatch.setattr(api_module.tempfile, "tempdir", str(tmp_path))

    def reject_context(**_: object) -> object:
        raise ValueError("context rejected")

    monkeypatch.setattr(api_module, "ImportContext", reject_context)
    response = client.post(
        "/imports",
        files={"file": ("quote.xlsx", _xlsx_bytes())},
        data={
            "request": json.dumps(
                {
                    "data_source_id": ids["source"],
                    "provider_id": ids["provider"],
                    "parser_version": "cleanup-test",
                    "profile": {},
                }
            )
        },
    )

    assert response.status_code == 422
    assert list(tmp_path.glob("tokensql-upload-*")) == []


def test_cancelled_upload_removes_temp_file(tmp_path, monkeypatch) -> None:
    from starlette.datastructures import UploadFile

    api_module = importlib.import_module("tokensql.api.app")
    monkeypatch.setattr(api_module.tempfile, "tempdir", str(tmp_path))
    upload = UploadFile(file=BytesIO(b""), filename="cancel.xlsx")

    async def cancelled_read(_: int) -> bytes:
        raise asyncio.CancelledError

    upload.read = cancelled_read
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_save_xlsx(upload))
    assert list(tmp_path.glob("tokensql-upload-*")) == []


def test_import_is_idempotent(api) -> None:
    client, ids = api
    request = json.dumps(
        {
            "data_source_id": ids["source"],
            "provider_id": ids["provider"],
            "parser_version": "api-test-v1",
            "profile": {"discount_scale": "10"},
        }
    )
    payload = _xlsx_bytes()
    first = client.post(
        "/imports",
        files={"file": ("quote.xlsx", payload)},
        data={"request": request},
    )
    second = client.post(
        "/imports",
        files={"file": ("quote.xlsx", payload)},
        data={"request": request},
    )
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json()["batch_id"] == second.json()["batch_id"]
    assert first.json()["reused_existing"] is False
    assert second.json()["reused_existing"] is True


def test_import_prompt_roundtrip_and_context_endpoint(api) -> None:
    client, ids = api
    prompt = "该表模型厂商为 Acme；缺省币种 CNY；价格按每百万 Token。"
    request = json.dumps(
        {
            "data_source_id": ids["source"],
            "provider_id": ids["provider"],
            "parser_version": "api-prompt-v1",
            "profile": {"discount_scale": "10"},
            "prompt_text": prompt,
            "context_defaults": {"currency": "CNY", "unit_basis": "每百万 Token"},
        }
    )
    imported = client.post(
        "/imports",
        files={"file": ("quote.xlsx", _xlsx_bytes())},
        data={"request": request},
    )
    assert imported.status_code == 200, imported.text
    batch_id = imported.json()["batch_id"]

    response = client.get(f"/imports/{batch_id}/context")
    assert response.status_code == 200
    payload = response.json()
    assert payload["batch_id"] == batch_id
    context = payload["import_context"]
    assert context["prompt_text"] == prompt
    assert context["context_defaults"] == {
        "currency": "CNY",
        "unit_basis": "每百万 Token",
    }
    assert context["prompt_sha256"]
    assert context["interpretation_version"] == "deterministic-v1"
    assert context["context_scope"] == "batch"

    missing = client.get("/imports/999999/context")
    assert missing.status_code == 404


def test_import_prompt_without_provider_id_is_accepted(api) -> None:
    client, ids = api
    request = json.dumps(
        {
            "data_source_id": ids["source"],
            "parser_version": "api-prompt-noprovider-v1",
            "profile": {"discount_scale": "10"},
            "prompt_text": "报价供应商为 Vendor；缺省币种 CNY。",
        }
    )
    imported = client.post(
        "/imports",
        files={"file": ("quote.xlsx", _xlsx_bytes())},
        data={"request": request},
    )
    assert imported.status_code == 200, imported.text
    batch_id = imported.json()["batch_id"]
    context = client.get(f"/imports/{batch_id}/context").json()["import_context"]
    assert context["provider_id"] is None
    assert context["prompt_text"] == "报价供应商为 Vendor；缺省币种 CNY。"


def test_review_approve_reject_status_and_conflict(api) -> None:
    client, ids = api
    assert len(client.get("/reviews").json()) == 2

    approved = client.post(
        f"/reviews/{ids['approve_review']}/approve",
        json={
            "reviewer": "tester",
            "resolution": {"standard_model_id": ids["model"]},
        },
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "resolved"
    assert client.post(
        f"/reviews/{ids['approve_review']}/approve",
        json={"reviewer": "tester", "resolution": {}},
    ).status_code == 409
    resolved = client.get("/reviews", params={"status": "resolved"})
    assert [item["id"] for item in resolved.json()] == [ids["approve_review"]]

    rejected = client.post(
        f"/reviews/{ids['reject_review']}/reject",
        json={"reviewer": "tester", "resolution": {"reason": "unsupported"}},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "dismissed"


def test_alias_validation_and_conflict(api) -> None:
    client, ids = api
    body = {
        "alias": "Alpha API",
        "standard_model_id": ids["model"],
    }
    assert client.post(
        "/aliases", json={**body, "verified": True}
    ).status_code == 422
    assert client.post(
        "/aliases", json={**body, "human_confirmed": True}
    ).status_code == 422

    created = client.post("/aliases", json=body)
    assert created.status_code == 200
    assert created.json()["normalized_alias"] == "alpha api"
    assert created.json()["is_verified"] is False

    conflict = client.post(
        "/aliases", json={**body, "standard_model_id": ids["other_model"]}
    )
    assert conflict.status_code == 409
    assert client.post("/aliases", json={**body, "unexpected": True}).status_code == 422


def test_history_incomparable_comparison_csv_and_not_found(api) -> None:
    client, ids = api
    history = client.get("/prices/history", params={"pricing_plan_id": ids["plan"]})
    assert history.status_code == 200
    assert history.json()[0]["components"][0]["raw_amount"] in {"1.250000000000", "1.25"}
    assert history.json()[0]["observed_at"].endswith("+00:00")

    normalizations = []
    for component_id in (ids["left"], ids["right"]):
        response = client.post(
            "/price-normalizations",
            json={
                "price_component_id": component_id,
                "source_artifact_id": ids["artifact"],
                "actor": "comparison-reviewer",
                "verified": True,
                "algorithm_version": "tokensql-normalization-v1",
            },
        )
        assert response.status_code == 200, response.text
        normalizations.append(response.json()["id"])

    comparison = client.post(
        "/prices/compare",
        json={
            "left_component_id": ids["left"],
            "right_component_id": ids["right"],
            "left_normalization_id": normalizations[0],
            "right_normalization_id": normalizations[1],
            "algorithm_version": "tokensql-normalization-v1",
        },
    )
    assert comparison.status_code == 200
    assert comparison.json()["comparable"] is False
    assert "comparison_key differs" in comparison.json()["reasons"]

    exported = client.get(
        "/prices/export.csv", params={"pricing_plan_id": ids["plan"]}
    )
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert "snapshot_id,pricing_plan_id" in exported.text
    assert "input_tokens" in exported.text

    assert client.get(
        "/prices/history", params={"pricing_plan_id": 999999}
    ).status_code == 404
    assert client.post(
        "/prices/compare",
        json={"left_component_id": 999999, "right_component_id": ids["right"]},
    ).status_code == 404
    assert client.get("/reviews", params={"status": "invalid"}).status_code == 422



def test_verified_management_workflow_and_trust_boundaries(api) -> None:
    client, ids = api
    management = _seed_management(client, ids)
    reviewed_body = {
        "staged_quote_id": management["reviewed_staged"],
        "pricing_plan_id": ids["plan"],
        "source_artifact_id": ids["artifact"],
        "actor": "publisher",
        "verified": True,
        "idempotency_key": "reviewed-publication-v1",
    }

    assert client.post(
        "/prices/publish", json={**reviewed_body, "verified": False}
    ).status_code == 422
    assert client.post(
        "/prices/publish",
        json={
            **reviewed_body,
            "staged_quote_id": ids["unreviewed_staged"],
        },
    ).status_code == 422
    assert client.post(
        "/prices/publish",
        json={
            **reviewed_body,
            "source_artifact_id": management["official_artifact"],
        },
    ).status_code == 422

    published = client.post("/prices/publish", json=reviewed_body)
    assert published.status_code == 200, published.text
    assert published.json()["reused_existing"] is False
    assert published.json()["snapshot"]["verification_status"] == "verified"
    replay = client.post("/prices/publish", json=reviewed_body)
    assert replay.status_code == 200
    assert replay.json()["reused_existing"] is True
    assert replay.json()["snapshot"]["id"] == published.json()["snapshot"]["id"]

    official_body = {
        "staged_quote_id": management["official_staged"],
        "pricing_plan_id": management["official_plan"],
        "source_artifact_id": management["official_artifact"],
        "data_source_id": management["official_source"],
        "actor": "official-reviewer",
        "verified": True,
        "idempotency_key": "official-publication-v1",
    }
    assert client.post(
        "/prices/publish",
        json={key: value for key, value in official_body.items() if key != "data_source_id"},
    ).status_code == 422
    wrong_source = client.post(
        "/official-prices/publish",
        json={**official_body, "data_source_id": ids["source"]},
    )
    assert wrong_source.status_code == 422
    official = client.post("/official-prices/publish", json=official_body)
    assert official.status_code == 200, official.text
    assert official.json()["snapshot"]["data_source_id"] == management["official_source"]
    assert client.post(
        "/official-prices/publish", json=official_body
    ).json()["reused_existing"] is True

    fx_body = {
        "base_currency": "USD",
        "quote_currency": "CNY",
        "rate": "7.100000000000",
        "rate_date": utc_now().date().isoformat(),
        "data_source_id": ids["source"],
        "actor": "fx-reviewer",
        "verified": True,
    }
    assert client.post(
        "/fx-rates", json={**fx_body, "verified": False}
    ).status_code == 422
    fx = client.post("/fx-rates", json=fx_body)
    assert fx.status_code == 200, fx.text
    assert fx.json()["verification_status"] == "verified"
    assert fx.json()["reused_existing"] is False
    fx_replay = client.post("/fx-rates", json=fx_body)
    assert fx_replay.json()["id"] == fx.json()["id"]
    assert fx_replay.json()["reused_existing"] is True

    component_id = published.json()["snapshot"]["components"][0]["id"]
    normalization_body = {
        "price_component_id": component_id,
        "source_artifact_id": ids["artifact"],
        "actor": "normalization-reviewer",
        "verified": True,
        "algorithm_version": "tokensql-normalization-v1",
    }
    assert client.post(
        "/price-normalizations",
        json={**normalization_body, "algorithm_version": "invented-v2"},
    ).status_code == 422
    assert client.post(
        "/price-normalizations",
        json={
            **normalization_body,
            "source_artifact_id": management["official_artifact"],
        },
    ).status_code == 422
    normalization = client.post(
        "/price-normalizations", json=normalization_body
    )
    assert normalization.status_code == 200, normalization.text
    assert normalization.json()["verification_status"] == "verified"
    assert normalization.json()["reused_existing"] is False
    normalization_replay = client.post(
        "/price-normalizations", json=normalization_body
    )
    assert normalization_replay.json()["id"] == normalization.json()["id"]
    assert normalization_replay.json()["reused_existing"] is True

    factory = client.app.state.test_session_factory
    with factory() as session:
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.actor.in_(
                        [
                            "publisher",
                            "official-reviewer",
                            "fx-reviewer",
                            "normalization-reviewer",
                        ]
                    )
                )
            )
        )
    assert {event.action for event in events} == {
        "publish_reviewed_price",
        "verify_official_price",
        "verify_fx_rate",
        "persist_price_normalization",
    }


def test_real_excel_api_confirmation_review_publish_normalize_compare_csv(api) -> None:
    client, ids = api
    workbook_path = Path(__file__).resolve().parents[1] / "test" / "token报价 (1)_B公司.xlsx"
    workbook_bytes = workbook_path.read_bytes()
    imported = client.post(
        "/imports",
        files={"file": (workbook_path.name, workbook_bytes)},
        data={
            "request": json.dumps(
                {
                    "data_source_id": ids["source"],
                    "provider_id": ids["provider"],
                    "parser_version": "real-api-e2e-v1",
                    "profile": {},
                    "initiated_by": "e2e-importer",
                }
            )
        },
    )
    assert imported.status_code == 200, imported.text
    summary = imported.json()
    assert summary["total_rows"] == 121
    assert summary["staged_quote_count"] == 121

    staged_response = client.get(
        "/staged-quotes",
        params={"import_batch_id": summary["batch_id"], "limit": 1000},
    )
    assert staged_response.status_code == 200, staged_response.text
    staged_quotes = staged_response.json()
    assert len(staged_quotes) == 121
    selected = None
    selected_index = None
    for staged in staged_quotes:
        for index, candidate in enumerate(staged["extracted_quote"]["candidates"]):
            parsed = candidate["parsed_payload"]
            if parsed["currency"] == "CNY" and parsed["unit"] == "token":
                selected = staged
                selected_index = index
                break
        if selected is not None:
            break
    assert selected is not None and selected_index is not None
    candidate = selected["extracted_quote"]["candidates"][selected_index]
    parsed = candidate["parsed_payload"]
    assert selected["source"]["sha256"] == hashlib.sha256(workbook_bytes).hexdigest()
    assert "components" not in selected["extracted_quote"]

    now = utc_now().isoformat()
    confirmation_body = {
        "actor": "quote-confirmer",
        "source_artifact_id": summary["source_artifact_id"],
        "provider_id": ids["provider"],
        "standard_model_id": ids["model"],
        "model_version_id": None,
        "offering_id": ids["offering"],
        "pricing_plan_id": ids["plan"],
        "currency_code": "CNY",
        "observed_at": now,
        "effective_from": now,
        "components": [
            {
                "candidate_index": selected_index,
                "billing_metric_id": ids["input_metric"],
                "component_type": "monetary",
                "raw_amount": parsed["amount"],
                "raw_currency_code": "CNY",
                "raw_unit_name": "token",
                "raw_unit_quantity": "1",
                "conditions": {},
            }
        ],
        "note": "confirmed against the uploaded workbook cell",
    }
    confirmed = client.post(
        f"/staged-quotes/{selected['id']}/confirm",
        json=confirmation_body,
    )
    assert confirmed.status_code == 200, confirmed.text
    confirmed_body = confirmed.json()
    assert confirmed_body["review_status"] == "needs_review"
    assert confirmed_body["extracted_quote"]["candidates"] == selected["extracted_quote"]["candidates"]
    assert confirmed_body["extracted_quote"]["components"][0]["raw_amount"] == parsed["amount"]
    assert confirmed_body["extracted_quote"]["_human_confirmation"]["actor"] == "quote-confirmer"
    assert any(
        item["reason"] == "human_quote_confirmation_requires_approval"
        for item in confirmed_body["reviews"]
    )
    assert client.post(
        "/prices/publish",
        json={
            "staged_quote_id": selected["id"],
            "pricing_plan_id": ids["plan"],
            "source_artifact_id": summary["source_artifact_id"],
            "actor": "publisher",
            "verified": True,
            "idempotency_key": "real-api-e2e-publication",
        },
    ).status_code == 422
    assert client.post(
        f"/staged-quotes/{selected['id']}/confirm",
        json=confirmation_body,
    ).status_code == 422

    for review in confirmed_body["reviews"]:
        response = client.post(
            f"/reviews/{review['id']}/approve",
            json={"reviewer": "quote-reviewer", "resolution": {}},
        )
        assert response.status_code == 200, response.text
    approved = client.get(f"/staged-quotes/{selected['id']}")
    assert approved.status_code == 200
    assert approved.json()["review_status"] == "approved"

    published = client.post(
        "/prices/publish",
        json={
            "staged_quote_id": selected["id"],
            "pricing_plan_id": ids["plan"],
            "source_artifact_id": summary["source_artifact_id"],
            "actor": "publisher",
            "verified": True,
            "idempotency_key": "real-api-e2e-publication",
        },
    )
    assert published.status_code == 200, published.text
    snapshot = published.json()["snapshot"]
    published_component = snapshot["components"][0]["id"]

    normalization_ids = []
    for component_id, artifact_id in (
        (ids["left"], ids["artifact"]),
        (published_component, summary["source_artifact_id"]),
    ):
        normalized = client.post(
            "/price-normalizations",
            json={
                "price_component_id": component_id,
                "source_artifact_id": artifact_id,
                "actor": "normalization-reviewer",
                "verified": True,
                "algorithm_version": "tokensql-normalization-v1",
            },
        )
        assert normalized.status_code == 200, normalized.text
        normalization_ids.append(normalized.json()["id"])

    compared = client.post(
        "/prices/compare",
        json={
            "left_component_id": ids["left"],
            "right_component_id": published_component,
            "left_normalization_id": normalization_ids[0],
            "right_normalization_id": normalization_ids[1],
            "algorithm_version": "tokensql-normalization-v1",
        },
    )
    assert compared.status_code == 200, compared.text
    assert compared.json()["comparable"] is True

    exported = client.get(
        "/prices/export.csv", params={"pricing_plan_id": ids["plan"]}
    )
    assert exported.status_code == 200
    assert str(snapshot["id"]) in exported.text
    assert parsed["amount"] in exported.text
