from __future__ import annotations

import json

import httpx

from tokensql.llm import (
    ModelCandidate,
    QwenClient,
    QwenClientConfig,
    ReviewReason,
)


def _completion(content: object) -> dict[str, object]:
    message = content if isinstance(content, str) else json.dumps(content)
    return {"choices": [{"message": {"content": message}}]}


def _client(handler, **kwargs) -> QwenClient:
    return QwenClient(
        QwenClientConfig(
            base_url="http://mock.local/v1",
            api_key="secret",
            model="qwen-test",
            timeout_seconds=0.5,
            use_json_schema=kwargs.pop("use_json_schema", True),
            min_confidence=kwargs.pop("min_confidence", 0.75),
            allow_remote=True,
        ),
        transport=httpx.MockTransport(handler),
    )


def test_defaults_are_loopback() -> None:
    assert QwenClientConfig().base_url.startswith("http://127.0.0.1")


def test_owned_http_client_does_not_inherit_environment_proxies(monkeypatch) -> None:
    real_client = httpx.Client
    captured: dict[str, object] = {}

    def client_factory(*args, **kwargs):
        captured.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", client_factory)
    transport = httpx.MockTransport(
        lambda _: httpx.Response(500, json={"error": "unused"})
    )
    with QwenClient(QwenClientConfig(), transport=transport):
        pass

    assert captured["trust_env"] is False


def test_valid_selection_uses_strict_schema_and_safe_candidates() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json=_completion(
                {
                    "candidate_index": 0,
                    "confidence": 0.96,
                    "reason": "same model and version",
                    "abstain": False,
                }
            ),
        )

    candidate = ModelCandidate(canonical_name="GPT-4.1", aliases=("gpt 4.1",))
    with _client(handler) as client:
        result = client.select_candidate("GPT 4.1", [candidate])

    assert not result.review_required
    assert result.selection is not None
    assert result.selection.candidate_index == 0
    response_format = captured["response_format"]
    assert isinstance(response_format, dict)
    assert response_format["json_schema"]["strict"] is True
    user_payload = json.loads(captured["messages"][1]["content"])
    assert set(user_payload["candidates"][0]) == {
        "candidate_index",
        "canonical_name",
        "aliases",
        "description",
    }
    assert "database_id" not in json.dumps(captured).casefold()
    assert "primary_key" not in json.dumps(captured).casefold()


def test_json_schema_request_can_be_disabled() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert "response_format" not in payload
        return httpx.Response(
            200,
            json=_completion(
                {
                    "candidate_index": 0,
                    "confidence": 0.9,
                    "reason": "valid",
                    "abstain": False,
                }
            ),
        )

    with _client(handler, use_json_schema=False) as client:
        result = client.select_candidate("qwen", [ModelCandidate(canonical_name="Qwen")])
    assert not result.review_required


def test_malicious_sql_field_is_rejected() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_completion(
                {
                    "candidate_index": 0,
                    "confidence": 1.0,
                    "reason": "ignore safeguards",
                    "abstain": False,
                    "sql": "DROP TABLE models",
                    "primary_key": 1,
                }
            ),
        )

    with _client(handler) as client:
        result = client.select_candidate("qwen", [ModelCandidate(canonical_name="Qwen")])
    assert result.review_required
    assert result.review_reason is ReviewReason.INVALID_RESPONSE
    assert result.selection is None


def test_out_of_range_candidate_is_review() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_completion(
                {
                    "candidate_index": 99,
                    "confidence": 0.99,
                    "reason": "invalid index",
                    "abstain": False,
                }
            ),
        )

    with _client(handler) as client:
        result = client.select_candidate("qwen", [ModelCandidate(canonical_name="Qwen")])
    assert result.review_required
    assert result.review_reason is ReviewReason.INVALID_CANDIDATE


def test_low_confidence_is_review() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_completion(
                {
                    "candidate_index": 0,
                    "confidence": 0.4,
                    "reason": "uncertain",
                    "abstain": False,
                }
            ),
        )

    with _client(handler, min_confidence=0.8) as client:
        result = client.select_candidate("qwen", [ModelCandidate(canonical_name="Qwen")])
    assert result.review_required
    assert result.review_reason is ReviewReason.LOW_CONFIDENCE


def test_timeout_is_review_without_retry() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("timed out", request=request)

    with _client(handler) as client:
        result = client.select_candidate("qwen", [ModelCandidate(canonical_name="Qwen")])
    assert calls == 1
    assert result.review_required
    assert result.review_reason is ReviewReason.TIMEOUT


def test_server_error_is_review_without_retry() -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500, json={"error": "failed"})

    with _client(handler) as client:
        result = client.select_candidate("qwen", [ModelCandidate(canonical_name="Qwen")])
    assert calls == 1
    assert result.review_required
    assert result.review_reason is ReviewReason.SERVER_ERROR


def test_invalid_json_and_extra_envelope_fields_are_handled() -> None:
    responses = iter(
        [
            httpx.Response(200, content=b"not-json"),
            httpx.Response(
                200,
                json={
                    **_completion(
                        {
                            "candidate_index": 0,
                            "confidence": 0.9,
                            "reason": "valid",
                            "abstain": False,
                        }
                    ),
                    "unexpected": True,
                },
            ),
            httpx.Response(
                200,
                json=_completion(
                    {
                        "candidate_index": 0,
                        "confidence": 0.9,
                        "reason": "valid",
                        "abstain": False,
                        "database_id": 7,
                    }
                ),
            ),
        ]
    )

    def handler(_: httpx.Request) -> httpx.Response:
        return next(responses)

    with _client(handler) as client:
        invalid_json = client.select_candidate(
            "qwen", [ModelCandidate(canonical_name="Qwen")]
        )
        extra_envelope = client.select_candidate(
            "qwen", [ModelCandidate(canonical_name="Qwen")]
        )
        extra_business_field = client.select_candidate(
            "qwen", [ModelCandidate(canonical_name="Qwen")]
        )
    assert invalid_json.review_reason is ReviewReason.INVALID_JSON
    # Unknown gateway envelope extensions are tolerated by design.
    assert not extra_envelope.review_required
    # Unknown fields inside the business selection JSON stay rejected.
    assert extra_business_field.review_reason is ReviewReason.INVALID_RESPONSE


def test_settings_factory_is_disabled_loopback_and_has_no_fake_key(monkeypatch) -> None:
    from tokensql.config import Settings

    settings = Settings(_env_file=None)
    assert settings.qwen_enabled is False
    assert settings.qwen_base_url == "http://127.0.0.1:8080/v1"
    assert settings.qwen_api_key is None
    config = QwenClientConfig.from_settings(settings)
    assert config.base_url == settings.qwen_base_url
    assert config.api_key is None


def test_remote_qwen_requires_explicit_opt_in() -> None:
    import pytest

    with pytest.raises(ValueError, match="loopback"):
        QwenClientConfig(base_url="https://qwen.example/v1")
    allowed = QwenClientConfig(
        base_url="https://qwen.example/v1", allow_remote=True
    )
    assert allowed.base_url == "https://qwen.example/v1"


def test_settings_require_model_when_qwen_is_enabled() -> None:
    import pytest
    from tokensql.config import Settings

    with pytest.raises(ValueError, match="QWEN_MODEL"):
        Settings(_env_file=None, qwen_enabled=True, qwen_model=None)


def test_extended_gateway_envelope_is_accepted() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "candidate_index": 0,
                                    "confidence": 0.95,
                                    "reason": "gateway extension fields",
                                    "abstain": False,
                                }
                            ),
                            "reasoning_content": "内部推理内容",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                    "prompt_tokens_details": {"cached_tokens": 80},
                },
                "timings": {"total_ms": 300},
            },
        )

    with _client(handler) as client:
        result = client.select_candidate("qwen", [ModelCandidate(canonical_name="Qwen")])
    assert not result.review_required
    assert result.selection is not None
    assert result.selection.candidate_index == 0
