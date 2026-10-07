from datetime import datetime, timezone

import httpx

from tokensql.official import OfficialHttpClient


NOW = datetime(2026, 9, 23, 8, 0, tzinfo=timezone.utc)
PUBLIC_IP = "93.184.216.34"


def client_for(handler, **changes):
    values = {
        "allowed_domains": {"official.example"},
        "client": httpx.Client(transport=httpx.MockTransport(handler)),
        "resolver": lambda host, port: [PUBLIC_IP],
        "clock": lambda: NOW,
        "retries": 0,
    }
    values.update(changes)
    return OfficialHttpClient(**values)


def test_success_preserves_provenance_and_raw_evidence():
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(
            200,
            headers={
                "content-type": "application/json; charset=utf-8",
                "etag": '"abc"',
                "last-modified": "Wed, 23 Sep 2026 08:00:00 GMT",
            },
            content=b'{"price":"1.20"}',
        )

    result = client_for(handler).fetch("https://official.example/pricing")

    assert result.status == "fetched"
    assert result.source_url == "https://official.example/pricing"
    assert result.fetched_at == NOW
    assert result.raw_body == b'{"price":"1.20"}'
    assert len(result.raw_sha256 or "") == 64
    assert result.etag == '"abc"'
    assert result.last_modified is not None


def test_only_allowlisted_https_urls_are_accepted_without_request():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="x")

    service = client_for(handler)
    http_result = service.fetch("http://official.example/pricing")
    other_result = service.fetch("https://search.example/summary")

    assert http_result.needs_review
    assert other_result.needs_review
    assert not calls


def test_literal_private_and_loopback_destinations_are_rejected():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="x")

    service = client_for(
        handler,
        allowed_domains={"127.0.0.1", "10.0.0.2"},
    )

    assert service.fetch("https://127.0.0.1/").needs_review
    assert service.fetch("https://10.0.0.2/").needs_review
    assert not calls


def test_dns_resolution_to_private_address_is_rejected():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, headers={"content-type": "text/plain"}, text="x")

    service = client_for(handler, resolver=lambda host, port: ["192.168.1.5"])
    result = service.fetch("https://official.example/pricing")

    assert result.needs_review
    assert "non-public" in result.review_reasons[0]
    assert not calls


def test_redirect_out_of_original_domain_is_rejected():
    def handler(request):
        return httpx.Response(
            302,
            headers={"location": "https://cdn.official.example/pricing"},
        )

    service = client_for(
        handler,
        allowed_domains={"official.example", "cdn.official.example"},
    )
    result = service.fetch("https://official.example/pricing")

    assert result.needs_review
    assert "different host" in result.review_reasons[0]


def test_content_type_is_checked():
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/octet-stream"},
            content=b"binary",
        )

    result = client_for(handler).fetch("https://official.example/pricing")

    assert result.needs_review
    assert "Content-Type" in result.review_reasons[0]


def test_oversize_content_length_and_stream_are_rejected():
    def declared_handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/html", "content-length": "99"},
            content=b"short",
        )

    declared = client_for(declared_handler, max_bytes=10).fetch(
        "https://official.example/pricing"
    )
    assert declared.needs_review
    assert "exceeds" in declared.review_reasons[0]

    def streamed_handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"01234567890",
        )

    streamed = client_for(streamed_handler, max_bytes=10).fetch(
        "https://official.example/pricing"
    )
    assert streamed.needs_review
    assert "exceeds" in streamed.review_reasons[0]


def test_retry_is_get_only_and_bounded():
    calls = []

    def handler(request):
        calls.append(request.method)
        if len(calls) == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            headers={"content-type": "text/plain"},
            content=b"ok",
        )

    result = client_for(handler, retries=1).fetch(
        "https://official.example/pricing"
    )

    assert result.status == "fetched"
    assert result.attempts == 2
    assert calls == ["GET", "GET"]


def test_default_client_ignores_environment_proxies():
    service = OfficialHttpClient(
        allowed_domains={"official.example"},
        resolver=lambda host, port: [PUBLIC_IP],
    )
    try:
        assert service._client.trust_env is False
    finally:
        service.close()


def test_injected_client_configuration_is_preserved():
    custom = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text="ok")
        ),
        trust_env=True,
    )
    service = OfficialHttpClient(
        allowed_domains={"official.example"},
        client=custom,
        resolver=lambda host, port: [PUBLIC_IP],
    )
    try:
        assert service._client is custom
        assert custom.trust_env is True
    finally:
        custom.close()
