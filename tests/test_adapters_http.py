"""HU 1.1.2 — Tests del cliente HTTP resiliente (httpx.MockTransport)."""

from __future__ import annotations

import httpx
import pytest

from valuebet.adapters.http import DefinitiveHTTPError, HTTPClient


def _client(handler, **kwargs) -> HTTPClient:
    transport = httpx.MockTransport(handler)
    inner = httpx.Client(transport=transport, base_url="http://test")
    # backoff_base=0 -> sin esperas reales entre reintentos (tests rápidos).
    return HTTPClient(client=inner, max_attempts=3, backoff_base=0.0, **kwargs)


def test_retries_on_5xx_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    client = _client(handler)
    resp = client.get("/x")

    assert resp.status_code == 200
    assert calls["n"] == 2  # reintentó una vez


def test_does_not_retry_on_4xx() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    client = _client(handler)
    with pytest.raises(DefinitiveHTTPError) as exc_info:
        client.get("/missing")

    assert calls["n"] == 1  # NO reintenta en 4xx
    assert exc_info.value.status == 404
    assert "/missing" in exc_info.value.url  # contexto: url


def test_retries_timeout_until_max() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("tardó demasiado", request=request)

    client = _client(handler)
    with pytest.raises(httpx.ReadTimeout):
        client.get("/slow")

    assert calls["n"] == 3  # reintentó hasta max_attempts
