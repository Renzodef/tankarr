from __future__ import annotations

import io
import json
from typing import Any
from urllib.error import HTTPError
from urllib.parse import parse_qs

import pytest

from tankarr.suwayomi_bootstrap import BootstrapError, GraphQLClient, bootstrap

STORE = "https://example.com/extensions/index.min.json"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class SimpleLoginOpener:
    def __init__(self, *, accept_login: bool = True):
        self.accept_login = accept_login
        self.graphql_calls = 0
        self.login_calls = 0

    def open(self, request, timeout):  # noqa: ANN001, ARG002
        if request.full_url.endswith("login.html?redirect=%2F"):
            self.login_calls += 1
            assert parse_qs(request.data.decode()) == {
                "user": ["tankarr"],
                "pass": ["secret"],
            }
            if self.accept_login:
                raise HTTPError(request.full_url, 303, "See Other", {}, None)
            return FakeResponse(b"Invalid username or password")

        self.graphql_calls += 1
        if self.graphql_calls == 1:
            payload = {"data": None, "errors": [{"message": "Unauthorized"}]}
        else:
            payload = {"data": {"extensions": {"nodes": []}}}
        return FakeResponse(json.dumps(payload).encode())


class FakeGraphQL:
    def __init__(self, extensions: list[dict[str, Any]], sources: list[dict[str, Any]]):
        self.extensions = extensions
        self.sources = sources
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((query, variables))
        if "TankarrRefreshExtensions" in query:
            return {"fetchExtensions": {"extensions": []}}
        if "TankarrExtensions" in query:
            return {"extensions": {"nodes": self.extensions}}
        if "TankarrInstallExtensions" in query:
            return {"updateExtensions": {"extensions": self.extensions}}
        if "TankarrInstalledSources" in query:
            return {"sources": {"nodes": self.sources}}
        raise AssertionError("Unexpected GraphQL operation")


def extension(package: str, **changes: Any) -> dict[str, Any]:
    result = {
        "pkgName": package,
        "name": package.rsplit(".", 1)[-1],
        "lang": "en",
        "contentWarning": "MIXED",
        "storeIndexUrl": STORE,
        "isInstalled": False,
        "hasUpdate": False,
        "isObsolete": False,
    }
    result.update(changes)
    return result


def source(package: str) -> dict[str, Any]:
    return {
        "id": "123",
        "name": "Example",
        "displayName": "Example (EN)",
        "lang": "en",
        "contentWarning": "MIXED",
        "extension": {"pkgName": package},
    }


def test_bootstrap_refreshes_installs_updates_and_proves_sources():
    package = "eu.kanade.tachiyomi.extension.all.mangadex"
    client = FakeGraphQL([extension(package)], [source(package)])

    result = bootstrap(client, [package], sleep=lambda _: None)

    assert result.extensions == (package,)
    assert result.sources[0]["displayName"] == "Example (EN)"
    install_call = next(
        variables
        for query, variables in client.calls
        if "TankarrInstallExtensions" in query
    )
    assert install_call == {
        "input": {
            "ids": [package],
            "patch": {"install": True, "update": True},
        }
    }


def test_graphql_client_establishes_simple_login_session_after_unauthorized():
    client = GraphQLClient("http://suwayomi:4567", "tankarr", "secret")
    opener = SimpleLoginOpener()
    client.opener = opener

    result = client.execute("query TankarrExtensions { extensions { nodes } }", {})

    assert result == {"extensions": {"nodes": []}}
    assert opener.graphql_calls == 2
    assert opener.login_calls == 1


def test_graphql_client_reports_rejected_simple_login_credentials():
    client = GraphQLClient("http://suwayomi:4567", "tankarr", "secret")
    opener = SimpleLoginOpener(accept_login=False)
    client.opener = opener

    with pytest.raises(BootstrapError, match="rejected.*username or password"):
        client.execute("query TankarrExtensions { extensions { nodes } }", {})


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"storeIndexUrl": "https://attacker.test/index.pb"}, "untrusted"),
        ({"contentWarning": "NSFW"}, "NSFW"),
        ({"isObsolete": True}, "obsolete"),
    ],
)
def test_bootstrap_rejects_untrusted_or_unsafe_extensions(changes, message):
    package = "eu.kanade.tachiyomi.extension.en.example"
    client = FakeGraphQL([extension(package, **changes)], [source(package)])

    with pytest.raises(BootstrapError, match=message):
        bootstrap(client, [package], extension_store=STORE, sleep=lambda _: None)


def test_bootstrap_accepts_any_repository_when_none_is_required():
    package = "eu.kanade.tachiyomi.extension.en.example"
    client = FakeGraphQL(
        [extension(package, storeIndexUrl="https://other.example/index.min.json")],
        [source(package)],
    )
    assert bootstrap(client, [package], sleep=lambda _: None).extensions == (package,)


def test_bootstrap_fails_closed_when_requested_extension_or_source_is_missing():
    package = "eu.kanade.tachiyomi.extension.en.example"
    with pytest.raises(BootstrapError, match="absent"):
        bootstrap(FakeGraphQL([], []), [package], sleep=lambda _: None)

    with pytest.raises(BootstrapError, match="did not expose"):
        bootstrap(
            FakeGraphQL([extension(package)], []),
            [package],
            attempts=1,
            sleep=lambda _: None,
        )
