"""Recorded-fixture tests for the built-in deterministic official JSON adapter."""
from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tokensql.db.base import Base
from tokensql.db.models import (
    AuditEvent,
    BillingMetric,
    DataSource,
    PriceSnapshot,
    ReviewItem,
    SourceArtifact,
    StagedQuote,
)
from tokensql.db.session import create_db_engine, create_session_factory
from tokensql.official.base import FetchResult
from tokensql.official.configured import (
    ConfiguredAdapterError,
    ConfiguredOfficialAdapterConfig,
    ConfiguredOfficialJsonAdapter,
    create_adapter,
    load_config,
)
from tokensql.services.catalog_service import CatalogService
from tokensql.services.official_service import OfficialPriceService


NOW = datetime(2026, 9, 24, 4, 0, tzinfo=timezone.utc)
URL = "https://official.example/pricing/model-0.json"
FIXTURE = Path(__file__).parent / "fixtures" / "official" / "pricing.json"


def _config_payload():
    return {
        "schema_version": 1,
        "source_name": "synthetic-official-fixture",
        "routes": [
            {
                "provider_slug": "official-provider",
                "api_model_id": "model-0",
                "region": "global",
                "url": URL,
                "records_pointer": "/prices",
                "plan_name": {"literal": "standard"},
                "currency_code": {"pointer": "/currency"},
                "effective_from_pointer": "/effective_from",
                "components": [
                    {
                        "metric_code": "official-input",
                        "amount_pointer": "/input_per_million",
                        "unit_name": "token",
                        "unit_quantity": "1000000",
                        "conditions": {"cache": "miss"},
                    },
                    {
                        "metric_code": "official-output",
                        "amount_pointer": "/output_per_million",
                        "unit_name": "token",
                        "unit_quantity": "1000000",
                        "conditions": {"cache": "miss"},
                    },
                ],
            }
        ],
    }


def _fetch(body: bytes | None = None) -> FetchResult:
    body = FIXTURE.read_bytes() if body is None else body
    return FetchResult(
        requested_url=URL,
        source_url=URL,
        fetched_at=NOW,
        status="fetched",
        raw_sha256=hashlib.sha256(body).hexdigest(),
        raw_body=body,
        content_type="application/json",
        etag='"fixture-v1"',
        last_modified="Thu, 24 Sep 2026 04:00:00 GMT",
        attempts=1,
    )


class FakeClient:
    def __init__(self, result: FetchResult):
        self.result = result
        self.urls = []

    def fetch(self, url: str) -> FetchResult:
        self.urls.append(url)
        return self.result


@pytest.fixture
def session() -> Session:
    engine = create_db_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = create_session_factory(engine)
    with factory() as value:
        yield value
    engine.dispose()


def _catalog(session: Session):
    catalog = CatalogService(session)
    manufacturer = catalog.create_manufacturer("Example AI")
    model = catalog.create_model(manufacturer.id, "model-0", display_name="Model 0")
    provider = catalog.create_provider(
        "Official Provider", provider_type="official", slug="official-provider"
    )
    offering = catalog.create_offering(
        provider.id, model.id, "model-0", api_model_id="model-0"
    )
    catalog.create_plan(
        offering.id,
        "standard",
        billing_mode="pay_as_you_go",
        currency_code="USD",
        effective_from=NOW,
    )
    for code, direction in (
        ("official-input", "input"),
        ("official-output", "output"),
    ):
        session.add(
            BillingMetric(
                code=code,
                display_name=code,
                dimension="token",
                direction=direction,
                unit_name="token",
                unit_quantity=1_000_000,
                comparable_group=f"text_{direction}",
            )
        )
    source = DataSource(
        name="official-json-fixture",
        source_type="official",
        authority_level="primary",
        base_url="https://official.example/",
    )
    session.add(source)
    session.flush()
    return source, offering


def test_adapter_deterministically_parses_recorded_fixture_without_ids_or_qwen():
    adapter = ConfiguredOfficialJsonAdapter(
        ConfiguredOfficialAdapterConfig.model_validate(_config_payload())
    )
    offering = SimpleNamespace(
        provider=SimpleNamespace(slug="official-provider"),
        region="global",
        api_model_id="model-0",
        provider_model_name="model-0",
    )

    assert adapter.supports(offering)
    assert adapter.url_for(offering) == URL
    (candidate,) = adapter.parse(_fetch())

    assert candidate["currency_code"] == "USD"
    assert candidate["effective_from"].startswith("2026-09-24T00:00:00")
    assert [item["amount"] for item in candidate["components"]] == ["1.25", "5.00"]
    assert [item["metric_code"] for item in candidate["components"]] == [
        "official-input",
        "official-output",
    ]
    assert "billing_metric_id" not in json.dumps(candidate)
    assert "verified" not in json.dumps(candidate)


def test_config_and_factory_are_strict_and_file_backed(tmp_path, monkeypatch):
    config_path = tmp_path / "official.json"
    config_path.write_text(json.dumps(_config_payload()), encoding="utf-8")
    monkeypatch.setenv("TOKENSQL_OFFICIAL_ADAPTER_CONFIG", str(config_path))

    loaded = load_config(config_path)
    adapter = create_adapter()
    assert loaded.source_name == "synthetic-official-fixture"
    assert adapter.source_name == loaded.source_name

    invalid = _config_payload()
    invalid["routes"][0]["url"] = "http://official.example/pricing.json"
    with pytest.raises(ValidationError, match="credential-free HTTPS"):
        ConfiguredOfficialAdapterConfig.model_validate(invalid)

    config_path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
    with pytest.raises(ConfiguredAdapterError, match="duplicate key"):
        load_config(config_path)


def test_mock_fetch_to_stage_review_preserves_evidence_and_never_publishes(session):
    source, offering = _catalog(session)
    adapter = ConfiguredOfficialJsonAdapter(
        ConfiguredOfficialAdapterConfig.model_validate(_config_payload())
    )
    client = FakeClient(_fetch())
    service = OfficialPriceService(
        session,
        data_source_id=source.id,
        client=client,
        adapters=(adapter,),
        clock=lambda: NOW,
    )

    (result,) = service.run_once()

    assert client.urls == [URL]
    assert result.status == "partial"
    assert result.candidate_count == 1
    assert session.scalar(select(func.count()).select_from(PriceSnapshot)) == 0
    staged = session.get(StagedQuote, result.staged_quote_ids[0])
    assert staged.review_status == "needs_review"
    assert staged.validation_errors == [
        "official_candidate_requires_explicit_verification"
    ]
    assert {item["metric_code"] for item in staged.extracted_quote["components"]} == {
        "official-input",
        "official-output",
    }
    assert session.scalar(select(ReviewItem)).status == "open"
    artifact = session.get(SourceArtifact, result.source_artifact_id)
    assert artifact.uri == URL
    assert artifact.raw_metadata["etag"] == '"fixture-v1"'
    assert base64.b64decode(artifact.raw_metadata["raw_body_base64"]) == FIXTURE.read_bytes()


def test_malformed_or_changed_official_json_becomes_review_and_failure_audit(session):
    source, offering = _catalog(session)
    adapter = ConfiguredOfficialJsonAdapter(
        ConfiguredOfficialAdapterConfig.model_validate(_config_payload())
    )
    changed = b'{"prices":[{"currency":"USD"}]}'
    service = OfficialPriceService(
        session,
        data_source_id=source.id,
        client=FakeClient(_fetch(changed)),
        adapters=(adapter,),
        clock=lambda: NOW,
    )

    result = service.collect_offering(offering.id)

    assert result.status == "failed"
    assert "possible page change" in result.review_reasons[0]
    staged = session.get(StagedQuote, result.staged_quote_ids[0])
    assert staged.review_status == "needs_review"
    assert staged.import_row.error_code == "page_changed"
    audit = session.scalar(select(AuditEvent))
    assert audit.action == "official_collection_failed"
    assert audit.after_data["source_url"] == URL


def test_parser_rejects_wrong_content_type_and_nonfinite_or_duplicate_json():
    adapter = ConfiguredOfficialJsonAdapter(
        ConfiguredOfficialAdapterConfig.model_validate(_config_payload())
    )
    wrong_type = _fetch()
    wrong_type = FetchResult(**{**wrong_type.__dict__, "content_type": "text/html"})
    with pytest.raises(ConfiguredAdapterError, match="application/json"):
        adapter.parse(wrong_type)

    with pytest.raises(ConfiguredAdapterError, match="duplicate key"):
        adapter.parse(_fetch(b'{"prices":[],"prices":[]}'))
    with pytest.raises(ConfiguredAdapterError, match="non-finite"):
        adapter.parse(_fetch(b'{"prices":[{"amount":NaN}]}'))
