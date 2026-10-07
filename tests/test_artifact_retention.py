"""Uploaded workbook evidence survives request temporary-file cleanup."""
import hashlib
import json

from tokensql.api.dependencies import get_session
from tokensql.db.models import SourceArtifact
from tests.test_api import api, _xlsx_bytes


def test_uploaded_original_is_recoverable_after_request(api, tmp_path, monkeypatch):
    import importlib
    api_module = importlib.import_module("tokensql.api.app")
    monkeypatch.setattr(api_module.tempfile, "tempdir", str(tmp_path))
    client, ids = api
    payload = _xlsx_bytes()
    response = client.post(
        "/imports",
        files={"file": ("original.xlsx", payload)},
        data={"request": json.dumps({
            "data_source_id": ids["source"],
            "provider_id": ids["provider"],
            "parser_version": "evidence-v1",
            "profile": {"discount_scale": "10"},
        })},
    )
    assert response.status_code == 200, response.text
    assert not list(tmp_path.glob("tokensql-upload-*"))
    scope = client.app.dependency_overrides[get_session]()
    session = next(scope)
    try:
        artifact = session.get(SourceArtifact, response.json()["source_artifact_id"])
        assert artifact.raw_content == payload
        assert artifact.sha256 == hashlib.sha256(payload).hexdigest()
        assert artifact.original_filename == "original.xlsx"
        assert artifact.uri == f"urn:sha256:{artifact.sha256}"
        assert artifact.storage_uri.startswith("db://source-artifact/")
    finally:
        scope.close()
