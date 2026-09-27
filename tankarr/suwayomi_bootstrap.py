from __future__ import annotations

import argparse
import base64
import json
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from http.cookiejar import CookieJar
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import (
    HTTPCookieProcessor,
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from tankarr import USER_AGENT

# Tankarr ships no extension repository and no list of extensions: the
# operator configures a Mihon/Tachiyomi-compatible repository
# (TANKARR_SUWAYOMI_EXTENSION_STORE) and the managed runtime installs every
# safe extension it offers for the enabled languages, except the ones below.

# Sources that cannot answer without FlareSolverr (Cloudflare challenge on
# every request). Weeb Central sits behind Cloudflare too but works without
# the bypass, so it is not here: the criterion is "needs the bypass".
FLARESOLVERR_EXTENSIONS = frozenset(
    {
        "eu.kanade.tachiyomi.extension.en.manganelo",
    }
)

# Sources that need Suwayomi's own WebView (CEF, an embedded Chromium) on top
# of FlareSolverr: MangaFire adds a shape-selecting captcha that only the
# WebView can solve. The managed runtime keeps CEF off - there is no arm64
# build for it and it would need an X11 stack in the container - so these
# sources cannot work here at all and are neither installed nor recommended.
WEBVIEW_EXTENSIONS = frozenset({"eu.kanade.tachiyomi.extension.all.mangafire"})

FETCH_EXTENSIONS = """
mutation TankarrRefreshExtensions($input: FetchExtensionsInput!) {
  fetchExtensions(input: $input) {
    extensions { pkgName }
  }
}
"""

LIST_EXTENSIONS = """
query TankarrExtensions {
  extensions(first: 5000) {
    nodes {
      pkgName
      name
      lang
      contentWarning
      storeIndexUrl
      isInstalled
      hasUpdate
      isObsolete
    }
  }
}
"""

INSTALL_EXTENSIONS = """
mutation TankarrInstallExtensions($input: UpdateExtensionsInput!) {
  updateExtensions(input: $input) {
    extensions { pkgName isInstalled hasUpdate }
  }
}
"""

LIST_SOURCES = """
query TankarrInstalledSources($language: String!) {
  sources(condition: {lang: $language}, first: 1000) {
    nodes {
      id
      name
      displayName
      lang
      contentWarning
      extension { pkgName }
    }
  }
}
"""


class GraphQL(Protocol):
    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]: ...


class BootstrapError(RuntimeError):
    """The requested Suwayomi source set could not be proven safe and ready."""


@dataclass(frozen=True)
class BootstrapResult:
    extensions: tuple[str, ...]
    sources: tuple[dict[str, Any], ...]


class GraphQLClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout_seconds: float = 60,
    ) -> None:
        parsed = urlsplit(base_url.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise BootstrapError("Invalid Suwayomi service URL")
        if not username or not password:
            raise BootstrapError("Suwayomi bootstrap credentials are required")
        normalized_base_url = base_url.rstrip("/") + "/"
        self.url = urljoin(normalized_base_url, "api/graphql")
        self.login_url = urljoin(normalized_base_url, "login.html?redirect=%2F")
        self.username = username
        self.password = password
        token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
        self.authorization = f"Basic {token}"
        self.timeout_seconds = timeout_seconds
        self.opener = build_opener(
            HTTPCookieProcessor(CookieJar()), _NoRedirectHandler()
        )

    def execute(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        try:
            payload = self._execute_once(query, variables)
        except HTTPError as exc:
            if exc.code != 401:
                raise BootstrapError(
                    f"Suwayomi GraphQL returned HTTP {exc.code}"
                ) from exc
            self._login()
            payload = self._execute_after_login(query, variables)

        if self._unauthorized(payload):
            self._login()
            payload = self._execute_after_login(query, variables)

        errors = payload.get("errors") or []
        data = payload.get("data")
        if errors or not isinstance(data, dict):
            messages = "; ".join(
                str(item.get("message") or item)
                if isinstance(item, dict)
                else str(item)
                for item in errors[:3]
            )
            raise BootstrapError(
                f"Suwayomi GraphQL failed: {messages or 'missing data'}"
            )
        return data

    def _execute_once(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        request = Request(
            self.url,
            data=json.dumps({"query": query, "variables": variables}).encode(),
            headers={
                "Accept": "application/json",
                "Authorization": self.authorization,
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout_seconds) as response:
                return json.load(response)
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise BootstrapError(
                f"Unable to call Suwayomi GraphQL: {type(exc).__name__}"
            ) from exc

    def _execute_after_login(
        self, query: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            return self._execute_once(query, variables)
        except HTTPError as exc:
            raise BootstrapError(
                f"Suwayomi GraphQL returned HTTP {exc.code} after login"
            ) from exc

    def _login(self) -> None:
        request = Request(
            self.login_url,
            data=urlencode({"user": self.username, "pass": self.password}).encode(),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with self.opener.open(request, timeout=self.timeout_seconds):
                # Invalid credentials render the login page with HTTP 200.
                raise BootstrapError(
                    "Suwayomi rejected the bootstrap username or password"
                )
        except HTTPError as exc:
            if exc.code not in {302, 303}:
                raise BootstrapError(
                    f"Suwayomi login returned HTTP {exc.code}"
                ) from exc
        except (URLError, TimeoutError) as exc:
            raise BootstrapError(
                f"Unable to log in to Suwayomi: {type(exc).__name__}"
            ) from exc

    @staticmethod
    def _unauthorized(payload: dict[str, Any]) -> bool:
        return any(
            "unauthorized" in str(item.get("message") or item).casefold()
            if isinstance(item, dict)
            else "unauthorized" in str(item).casefold()
            for item in payload.get("errors") or []
        )


class _NoRedirectHandler(HTTPRedirectHandler):
    """Never forward Suwayomi credentials or cookies across a redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def bootstrap(
    client: GraphQL,
    package_names: Sequence[str],
    *,
    extension_store: str | None = None,
    language: str = "en",
    attempts: int = 30,
    sleep: Callable[[float], None] = time.sleep,
) -> BootstrapResult:
    """Install the requested extensions on a Suwayomi server and prove that
    they expose sources for the language. With ``extension_store`` every
    extension must come from that repository index."""

    requested = tuple(
        dict.fromkeys(name.strip() for name in package_names if name.strip())
    )
    if not requested:
        raise BootstrapError("At least one Suwayomi extension must be requested")

    client.execute(FETCH_EXTENSIONS, {"input": {}})
    extension_data = client.execute(LIST_EXTENSIONS, {})
    nodes = list((extension_data.get("extensions") or {}).get("nodes") or [])
    extensions = {str(item.get("pkgName") or ""): item for item in nodes}

    missing = [name for name in requested if name not in extensions]
    if missing:
        raise BootstrapError(
            "Requested extensions are absent from the configured store: "
            + ", ".join(missing)
        )
    for name in requested:
        extension = extensions[name]
        if extension_store and extension.get("storeIndexUrl") != extension_store:
            raise BootstrapError(f"Extension {name} came from an untrusted store")
        if extension.get("contentWarning") == "NSFW":
            raise BootstrapError(f"Refusing NSFW bootstrap extension: {name}")
        if extension.get("isObsolete"):
            raise BootstrapError(f"Refusing obsolete bootstrap extension: {name}")

    client.execute(
        INSTALL_EXTENSIONS,
        {"input": {"ids": list(requested), "patch": {"install": True, "update": True}}},
    )

    latest_sources: list[dict[str, Any]] = []
    for attempt in range(max(1, attempts)):
        source_data = client.execute(LIST_SOURCES, {"language": language})
        latest_sources = list((source_data.get("sources") or {}).get("nodes") or [])
        by_extension = {
            str((source.get("extension") or {}).get("pkgName") or "")
            for source in latest_sources
        }
        if set(requested) <= by_extension:
            selected = tuple(
                source
                for source in latest_sources
                if str((source.get("extension") or {}).get("pkgName") or "")
                in requested
                and source.get("contentWarning") != "NSFW"
            )
            if selected:
                return BootstrapResult(requested, selected)
        if attempt + 1 < attempts:
            sleep(2)

    raise BootstrapError(
        "Installed extensions did not expose every requested language source"
    )


def _csv(raw: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install Suwayomi extensions and verify that they expose sources"
    )
    parser.add_argument(
        "--url",
        default=os.getenv("SUWAYOMI_BOOTSTRAP_URL", "http://127.0.0.1:4567"),
    )
    parser.add_argument(
        "--language", default=os.getenv("TANKARR_SUWAYOMI_LANGUAGE", "en")
    )
    parser.add_argument(
        "--extensions",
        default=os.getenv("TANKARR_SUWAYOMI_BOOTSTRAP_EXTENSIONS", ""),
        help="comma-separated extension package names to install",
    )
    parser.add_argument(
        "--store",
        default=os.getenv("TANKARR_SUWAYOMI_EXTENSION_STORE", ""),
        help="repository index URL every extension must come from (optional)",
    )
    args = parser.parse_args()
    if not args.extensions.strip():
        parser.error(
            "--extensions (or TANKARR_SUWAYOMI_BOOTSTRAP_EXTENSIONS) is required"
        )
    username = os.getenv("SUWAYOMI_USERNAME", "")
    password = os.getenv("SUWAYOMI_PASSWORD", "")
    try:
        result = bootstrap(
            GraphQLClient(args.url, username, password),
            _csv(args.extensions),
            extension_store=args.store.strip() or None,
            language=args.language,
        )
    except BootstrapError as exc:
        parser.exit(1, f"Suwayomi bootstrap failed: {exc}\n")
    source_labels = ", ".join(
        str(item.get("displayName") or item.get("name") or item.get("id"))
        for item in result.sources
    )
    print(
        f"Suwayomi ready: {len(result.extensions)} extensions, "
        f"{len(result.sources)} {args.language} sources ({source_labels})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
