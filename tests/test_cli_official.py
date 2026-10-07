"""CLI tests for the explicit, one-shot official refresh entry point."""
from __future__ import annotations

import json
import sys
from contextlib import contextmanager
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from tokensql.cli import (
    CliRuntimeError,
    _load_official_adapter,
    _run_official_refresh,
    app,
)


runner = CliRunner()


class ValidAdapter:
    source_name = "official-test"

    def supports(self, offering):
        return True

    def url_for(self, offering):
        return "https://official.example/pricing"

    def parse(self, result):
        return ()


def _arguments() -> list[str]:
    return [
        "official-refresh",
        "--data-source-id",
        "7",
        "--allowed-domain",
        "official.example",
        "--allowed-domain",
        "api.official.example",
        "--adapter",
        "test_adapter:create",
        "--database-url",
        "sqlite+pysqlite:///:memory:",
        "--limit",
        "3",
        "--timeout",
        "4.5",
    ]


@pytest.mark.parametrize(
    "option, value",
    [
        ("--data-source-id", "7"),
        ("--allowed-domain", "official.example"),
        ("--adapter", "test_adapter:create"),
        ("--database-url", "sqlite+pysqlite:///:memory:"),
    ],
)
def test_official_refresh_requires_all_security_inputs(option, value):
    arguments = _arguments()
    while option in arguments:
        index = arguments.index(option)
        del arguments[index : index + 2]
    result = runner.invoke(app, arguments)

    assert result.exit_code != 0
    assert option in result.output


def test_adapter_path_is_safe_and_protocol_is_strict(monkeypatch):
    module = ModuleType("test_adapter_module")
    module.adapter = ValidAdapter()
    module.create = lambda: ValidAdapter()
    module.bad = object()
    monkeypatch.setitem(sys.modules, module.__name__, module)

    assert _load_official_adapter("test_adapter_module:adapter") is module.adapter
    assert isinstance(_load_official_adapter("test_adapter_module:create"), ValidAdapter)
    with pytest.raises(CliRuntimeError, match="adapter nor a factory"):
        _load_official_adapter("test_adapter_module:bad")
    with pytest.raises(CliRuntimeError, match="safe Python import path"):
        _load_official_adapter("../../adapter.py:create")


def test_refresh_runner_wires_mock_client_adapter_session_and_commits():
    events = []
    adapter = ValidAdapter()

    class FakeEngine:
        def dispose(self):
            events.append("disposed")

    @contextmanager
    def session_scope(factory):
        events.append("transaction-entered")
        yield "session"
        events.append("committed")

    session_module = SimpleNamespace(
        create_db_engine=lambda url: events.append(("database", url)) or FakeEngine(),
        create_session_factory=lambda engine: "factory",
        session_scope=session_scope,
    )

    class FakeClient:
        def __init__(self, *, allowed_domains, timeout):
            events.append(("client", allowed_domains, timeout))

        def close(self):
            events.append("client-closed")

    class FakeService:
        def __init__(self, session, *, data_source_id, client, adapters):
            assert session == "session"
            assert data_source_id == 7
            assert adapters == (adapter,)

        def run_once(self, *, limit):
            assert limit == 3
            return (
                SimpleNamespace(
                    offering_id=9,
                    status="partial",
                    source_artifact_id=4,
                    import_batch_id=5,
                    candidate_count=2,
                    reused_existing=False,
                ),
            )

    modules = {
        "tokensql.db.session": session_module,
        "tokensql.services.official_service": SimpleNamespace(
            OfficialPriceService=FakeService
        ),
        "tokensql.official.http": SimpleNamespace(OfficialHttpClient=FakeClient),
    }
    with (
        patch("tokensql.cli._load_official_adapter", return_value=adapter),
        patch("tokensql.cli._load_module", side_effect=lambda name, purpose: modules[name]),
    ):
        payload, failed = _run_official_refresh(
            data_source_id=7,
            allowed_domains=["Official.Example", "official.example."],
            adapter_path="test_adapter:create",
            database_url="sqlite+pysqlite:///:memory:",
            limit=3,
            timeout=4.5,
        )

    assert not failed
    assert payload["status"] == "completed"
    assert payload["results"][0]["candidate_count"] == 2
    assert ("client", ("official.example",), 4.5) in events
    assert events.index("transaction-entered") < events.index("committed")
    assert events[-2:] == ["client-closed", "disposed"]


def test_refresh_runner_redacts_driver_exception_details():
    adapter = ValidAdapter()
    secret = "postgresql://admin:do-not-print@db/private"
    session_module = SimpleNamespace(
        create_db_engine=lambda url: (_ for _ in ()).throw(RuntimeError(secret))
    )
    modules = {
        "tokensql.db.session": session_module,
        "tokensql.services.official_service": SimpleNamespace(),
        "tokensql.official.http": SimpleNamespace(),
    }
    with (
        patch("tokensql.cli._load_official_adapter", return_value=adapter),
        patch("tokensql.cli._load_module", side_effect=lambda name, purpose: modules[name]),
        pytest.raises(CliRuntimeError) as raised,
    ):
        _run_official_refresh(
            data_source_id=7,
            allowed_domains=["official.example"],
            adapter_path="test_adapter:create",
            database_url=secret,
            limit=None,
            timeout=10.0,
        )

    assert "RuntimeError" in str(raised.value)
    assert "do-not-print" not in str(raised.value)


def test_official_refresh_success_outputs_redacted_json_and_forwards_options():
    payload = {
        "status": "completed",
        "offering_count": 1,
        "failed_count": 0,
        "results": [
            {
                "offering_id": 9,
                "status": "partial",
                "source_artifact_id": 4,
                "import_batch_id": 5,
                "candidate_count": 2,
                "reused_existing": False,
            }
        ],
    }
    with patch(
        "tokensql.cli._run_official_refresh", return_value=(payload, False)
    ) as refresh:
        result = runner.invoke(app, _arguments())

    assert result.exit_code == 0
    assert json.loads(result.stdout) == payload
    refresh.assert_called_once_with(
        data_source_id=7,
        allowed_domains=["official.example", "api.official.example"],
        adapter_path="test_adapter:create",
        database_url="sqlite+pysqlite:///:memory:",
        limit=3,
        timeout=4.5,
    )


def test_official_refresh_partial_failure_is_json_and_nonzero():
    payload = {
        "status": "partial_failure",
        "offering_count": 1,
        "failed_count": 1,
        "results": [{"offering_id": 9, "status": "failed"}],
    }
    with patch(
        "tokensql.cli._run_official_refresh", return_value=(payload, True)
    ):
        result = runner.invoke(app, _arguments())

    assert result.exit_code == 1
    assert json.loads(result.stdout) == payload
    assert "Traceback" not in result.output


def test_official_refresh_rejects_file_paths_before_any_network_call():
    arguments = _arguments()
    index = arguments.index("test_adapter:create")
    arguments[index] = "../../adapter.py:create"

    result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    assert "safe Python import path" in result.output
    assert "Traceback" not in result.output


def test_official_refresh_failure_does_not_echo_database_secret():
    arguments = _arguments()
    database_index = arguments.index("sqlite+pysqlite:///:memory:")
    arguments[database_index] = "postgresql+psycopg://admin:super-secret@db/private"
    with patch(
        "tokensql.cli._run_official_refresh",
        side_effect=CliRuntimeError(
            "Official refresh failed safely (OperationalError); inspect restricted logs."
        ),
    ):
        result = runner.invoke(app, arguments)

    assert result.exit_code == 2
    assert "OperationalError" in result.output
    assert "super-secret" not in result.output
    assert "Traceback" not in result.output
