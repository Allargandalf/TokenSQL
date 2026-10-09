import json
import socket

import pytest

from tokensql.workspace.config import WorkspaceSettings
from tokensql.workspace.runtime import available_port, isolated_config


def test_provider_reuse_does_not_copy_shared_runtime_configuration(tmp_path):
    source = tmp_path / "provider.json"
    provider = {"npm": "@ai-sdk/openai-compatible", "options": {"baseURL": "http://127.0.0.1:8080/v1"}}
    source.write_text(json.dumps({
        "provider": {"local": provider}, "plugin": ["shared-plugin"],
        "mcp": {"shared": {}}, "permission": "allow", "model": "other/other",
    }))
    settings = WorkspaceSettings(workspace_dir=tmp_path / "workspace", opencode_provider="local", opencode_model="qwen")
    result = isolated_config(settings, source)
    assert result["provider"] == {"local": provider}
    assert result["model"] == "local/qwen"
    assert "plugin" not in result and "mcp" not in result
    assert result["permission"]["edit"]["*"] == "deny"
    assert result["permission"]["question"] == "deny"


def test_missing_provider_fails_without_shared_fallback(tmp_path):
    source = tmp_path / "provider.json"
    source.write_text('{"provider": {}}')
    with pytest.raises(ValueError, match="missing"):
        isolated_config(WorkspaceSettings(workspace_dir=tmp_path), source)


def test_occupied_port_is_not_reused_or_stopped():
    with socket.socket() as owner:
        owner.bind(("127.0.0.1", 0))
        owner.listen()
        port = owner.getsockname()[1]
        with pytest.raises(OSError):
            available_port("127.0.0.1", port)
        assert owner.getsockname()[1] == port
