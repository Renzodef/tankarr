from __future__ import annotations

import ssl

import httpx

from tankarr import http


def test_every_client_shares_one_tls_context(monkeypatch):
    monkeypatch.setattr(http, "_context", None)
    built: list[ssl.SSLContext] = []
    original = httpx.create_ssl_context

    def counting(*args, **kwargs):
        context = original(*args, **kwargs)
        built.append(context)
        return context

    monkeypatch.setattr(httpx, "create_ssl_context", counting)
    first = http.async_client(timeout=5.0)
    second = http.async_client(timeout=5.0, follow_redirects=True)
    assert len(built) == 1
    assert http.ssl_context() is built[0]
    assert isinstance(first, httpx.AsyncClient)
    assert second.follow_redirects is True


def test_a_caller_may_still_choose_its_own_verification(monkeypatch):
    monkeypatch.setattr(http, "_context", None)
    calls: list[dict] = []
    original = httpx.AsyncClient.__init__

    def recording(self, *args, **kwargs):
        calls.append(dict(kwargs))
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", recording)
    http.async_client(verify=False)
    assert calls[-1]["verify"] is False
