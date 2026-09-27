"""Reader shortcuts: open a Tankarr series in whatever reads the library.

Tankarr writes a generic library (one folder per work, ComicInfo.xml,
cover sidecars) and stays independent of the reader, like Sonarr with
Plex/Jellyfin. The reader is only a *shortcut target*:

* ``komga`` — exact match by managed file path through Komga's API (also
  the optional managed-sync integration);
* ``kavita`` — series lookup through Kavita's API key;
* ``url`` — a template such as ``https://reader.local/search?q={title}``;
* ``none`` — no shortcut.

``auto`` picks Komga when its link is enabled, else the template, else none.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from tankarr.config import Settings
from tankarr.naming import series_directory_name

READER_KINDS = ("tankarr", "auto", "komga", "kavita", "stump", "url", "none")
READER_LABELS = {
    "tankarr": "Tankarr",
    "komga": "Komga",
    "kavita": "Kavita",
    "stump": "Stump",
    "url": "Reader",
    "none": "Reader",
}


def effective_reader_kind(settings: Settings) -> str:
    kind = str(getattr(settings, "reader_kind", "auto") or "auto").casefold()
    if kind != "auto":
        return kind
    if settings.komga_link_enabled:
        return "komga"
    if getattr(settings, "reader_series_url_template", None):
        return "url"
    return "tankarr"


def komga_series_url(
    settings: Settings, series_id: str, *, oneshot: bool = False
) -> str:
    """Build a browser-facing Komga URL without leaking its API credentials."""

    configured = settings.komga_url
    if not configured:
        raise ValueError("Komga URL is not configured")
    route = "oneshot" if oneshot else "series"
    return _reader_route_url(configured, route, series_id)


def komga_book_url(settings: Settings, book_id: str) -> str:
    """Build Komga's browser route that opens one book in the web reader."""

    configured = settings.komga_url
    if not configured:
        raise ValueError("Komga URL is not configured")
    return _reader_route_url(configured, "book", book_id, "read")


def _reader_route_url(base_url: str, *segments: object) -> str:
    """Append an encoded UI route while preserving a configured base path."""

    target = urlsplit(base_url)
    base_path = target.path.rstrip("/")
    suffix = "/".join(quote(str(segment), safe="") for segment in segments)
    path = f"{base_path}/{suffix}" if base_path else f"/{suffix}"
    return urlunsplit((target.scheme, target.netloc, path, "", ""))


def _managed_library_books(
    settings: Settings, database: Any, manga_id: str
) -> list[dict[str, str]]:
    """Return existing managed files with their stable Tankarr chapter ids."""

    root = settings.library_dir.resolve()
    books: list[dict[str, str]] = []
    for chapter in database.list_all_chapters(manga_id):
        raw_path = chapter.get("library_path")
        chapter_id = str(chapter.get("id") or "")
        if not chapter.get("downloaded") or not raw_path or not chapter_id:
            continue
        source_path = Path(str(raw_path))
        path = source_path.resolve()
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if not path.is_file() or source_path.is_symlink():
            continue
        books.append(
            {
                "chapter_id": chapter_id,
                "relative_path": relative.as_posix(),
            }
        )
    return books


def _reader_book_links(
    managed_books: list[dict[str, str]],
    matches: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    links: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for managed in managed_books:
        match = matches.get(managed["relative_path"])
        if not match or not match.get("url"):
            continue
        key = (managed["chapter_id"], match["url"])
        if key in seen:
            continue
        seen.add(key)
        links.append(
            {
                "chapter_id": managed["chapter_id"],
                "book_id": str(match.get("book_id") or ""),
                "url": match["url"],
            }
        )
    return links


def _managed_path_index(managed_paths: set[str]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for relative in managed_paths:
        name = PurePosixPath(relative).name.casefold()
        index.setdefault(name, []).append(relative)
    return index


def _managed_relative_path(
    reader_path: object, managed_paths: dict[str, list[str]]
) -> str | None:
    """Match a reader path by the complete Tankarr-relative suffix."""

    normalized = PurePosixPath(str(reader_path or "").replace("\\", "/")).as_posix()
    folded = normalized.casefold()
    candidates = [
        relative
        for relative in managed_paths.get(PurePosixPath(normalized).name.casefold(), [])
        if folded == relative.casefold() or folded.endswith("/" + relative.casefold())
    ]
    return candidates[0] if len(candidates) == 1 else None


def _normalized(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()


def _kavita_binding(database: Any, key: str, result: dict | None = None) -> dict | None:
    """Disposable persistent identity cache; every hit is re-proven by files."""
    with database.connect() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS reader_binding (key TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        if result is not None:
            data = {
                "seriesId": result["series_id"],
                "libraryId": result["library_id"],
                "name": result["title"],
            }
            connection.execute(
                "INSERT INTO reader_binding(key, data) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET data=excluded.data, updated_at=CURRENT_TIMESTAMP",
                (key, json.dumps(data)),
            )
            connection.execute(
                "DELETE FROM reader_binding WHERE key NOT IN (SELECT key FROM reader_binding ORDER BY updated_at DESC LIMIT 2000)"
            )
            return data
        row = connection.execute(
            "SELECT data FROM reader_binding WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        except ValueError:
            return None


def template_url(template: str, manga: dict[str, Any]) -> str:
    """Fill ``{title}``, ``{folder}``, ``{id}``, ``{title_raw}`` placeholders."""

    title = str(manga.get("title") or "")
    try:
        folder = series_directory_name(manga)
    except Exception:  # noqa: BLE001 - naming needs authors; fall back to title
        folder = title
    values = {
        "title": quote(title, safe=""),
        "title_raw": title,
        "folder": quote(folder, safe=""),
        "id": quote(str(manga.get("id") or ""), safe=""),
    }
    result = template
    for key, value in values.items():
        result = result.replace("{" + key + "}", value)
    return result


class KavitaReader:
    """Minimal Kavita client: API-key login and series search."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        api_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        binding: dict[str, Any] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_url = (api_url or base_url).rstrip("/")
        self.api_key = api_key
        self._client = client
        self.binding = binding

    async def _path_matches(self, client, headers, item, managed_books):
        series_id = str(item.get("seriesId") or item.get("id") or "")
        library_id = str(item.get("libraryId") or "")
        response = await client.get(
            f"{self.api_url}/api/Series/volumes",
            params={"seriesId": series_id},
            headers=headers,
        )
        if response.status_code == 404:
            return {}
        response.raise_for_status()
        managed_paths = _managed_path_index(
            {book["relative_path"] for book in managed_books}
        )
        matches: dict[str, dict[str, str]] = {}
        ambiguous: set[str] = set()
        for volume in response.json() or []:
            for chapter in volume.get("chapters") or []:
                chapter_id = str(chapter.get("id") or "")
                if not chapter_id:
                    continue
                for file_data in chapter.get("files") or []:
                    relative = _managed_relative_path(
                        file_data.get("filePath"), managed_paths
                    )
                    if not relative:
                        continue
                    file_format = int(file_data.get("format") or 0)
                    route = (
                        "book"
                        if file_format == 3
                        else "pdf"
                        if file_format == 4
                        else "manga"
                    )
                    match = {
                        "book_id": chapter_id,
                        "url": _reader_route_url(
                            self.base_url,
                            "library",
                            library_id,
                            "series",
                            series_id,
                            route,
                            chapter_id,
                        ),
                    }
                    if relative in matches and matches[relative] != match:
                        ambiguous.add(relative)
                    matches[relative] = match
        if ambiguous:
            raise ValueError(
                "Kavita has multiple records for the same managed file; repair the reader index first"
            )
        return matches

    def _result(self, item, manga, managed_books, matches, method):
        series_id = str(item.get("seriesId") or item.get("id") or "")
        library_id = str(item.get("libraryId") or "")
        books = _reader_book_links(managed_books, matches)
        return {
            "available": True,
            "series_id": series_id,
            "library_id": library_id,
            "title": str(item.get("name") or manga.get("title") or ""),
            "url": _reader_route_url(
                self.base_url, "library", library_id, "series", series_id
            ),
            "matched_books": len(books),
            "books": books,
            "match_method": method,
        }

    async def series_url(
        self,
        manga: dict[str, Any],
        managed_books: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        managed_books = managed_books or []
        owned = self._client is None
        client = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            auth = await client.post(
                f"{self.api_url}/api/Plugin/authenticate",
                params={"apiKey": self.api_key, "pluginName": "Tankarr"},
            )
            if auth.is_error:
                # raise_for_status() would quote the URL with the API key in it.
                raise ValueError(f"Kavita answered HTTP {auth.status_code}")
            token = str((auth.json() or {}).get("token") or "")
            if not token:
                raise ValueError("Kavita did not return a token for this API key")
            headers = {"Authorization": f"Bearer {token}"}
            if self.binding and managed_books:
                matches = await self._path_matches(
                    client, headers, self.binding, managed_books
                )
                if matches:
                    return self._result(
                        self.binding, manga, managed_books, matches, "persistent_path"
                    )
            title = str(manga.get("title") or "")
            response = await client.get(
                f"{self.api_url}/api/Search/search",
                params={"queryString": title},
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json() or {}
            wanted = {_normalized(title), _normalized(manga.get("source_title"))}
            wanted.discard("")
            candidates = []
            for item in payload.get("series") or []:
                names = {
                    _normalized(item.get("name")),
                    _normalized(item.get("localizedName")),
                    _normalized(item.get("originalName")),
                }
                if names & wanted:
                    candidates.append(item)
            if managed_books:
                # Exact titles narrow the request, but only a managed file
                # proves identity. Homonyms are checked, never picked by order.
                pool = candidates or list(payload.get("series") or [])
                if len(pool) > 20:
                    # Do not claim uniqueness from a truncated title search.
                    pool = []
                verified = []
                seen = set()
                for item in pool[:20]:
                    identity = str(item.get("seriesId") or item.get("id") or "")
                    if not identity or identity in seen:
                        continue
                    seen.add(identity)
                    matches = await self._path_matches(
                        client, headers, item, managed_books
                    )
                    if matches:
                        verified.append((item, matches))
                if len(verified) > 1:
                    return {
                        "available": False,
                        "reason": "Managed files map to multiple Kavita series",
                        "match_method": "ambiguous_path",
                    }
                if verified:
                    return self._result(
                        verified[0][0],
                        manga,
                        managed_books,
                        verified[0][1],
                        "managed_path",
                    )

                # A translated/renamed title may not appear in search at all.
                # Page the lightweight series catalogue; query volumes only
                # when a full managed folder suffix matches (not basename).
                folders = {
                    PurePosixPath(book["relative_path"]).parent.as_posix().casefold()
                    for book in managed_books
                }
                complete = False
                for page in range(1, 21):
                    response = await client.post(
                        f"{self.api_url}/api/Series/all-v2",
                        params={"pageNumber": page, "pageSize": 100},
                        json={"statements": [], "combination": 0},
                        headers=headers,
                    )
                    response.raise_for_status()
                    rows = response.json() or []
                    if not isinstance(rows, list) or len(rows) > 100:
                        raise ValueError("Kavita returned an invalid series catalogue")
                    for item in rows:
                        folder = (
                            str(
                                item.get("folderPath")
                                or item.get("lowestFolderPath")
                                or ""
                            )
                            .replace("\\", "/")
                            .rstrip("/")
                            .casefold()
                        )
                        if not any(
                            folder == owned or folder.endswith("/" + owned)
                            for owned in folders
                        ):
                            continue
                        matches = await self._path_matches(
                            client, headers, item, managed_books
                        )
                        if matches:
                            verified.append((item, matches))
                    pagination = response.headers.get("pagination")
                    if pagination:
                        page_info = json.loads(pagination)
                        last_page = int(page_info["currentPage"]) >= int(
                            page_info["totalPages"]
                        )
                    else:
                        last_page = len(rows) < 100
                    if last_page:
                        complete = True
                        break
                if complete and len(verified) == 1:
                    return self._result(
                        verified[0][0],
                        manga,
                        managed_books,
                        verified[0][1],
                        "catalogue_path",
                    )
                return {
                    "available": False,
                    "match_method": "ambiguous_path" if verified else "unmatched_path",
                    "reason": "Reader catalogue exceeds the 2,000-series safety limit; use matching titles to narrow the lookup"
                    if not complete
                    else "Managed files map to multiple Kavita series"
                    if verified
                    else "Kavita has not indexed the managed files; check its scan and library mount",
                }
            if not candidates:
                return {
                    "available": False,
                    "reason": "Kavita has no series with this title",
                }
            if len(candidates) > 1:
                return {
                    "available": False,
                    "reason": "Kavita has several series with this title",
                }
            return self._result(candidates[0], manga, [], {}, "title_unverified")
        finally:
            if owned:
                await client.aclose()


class StumpReader:
    """Stump: password login (REST v2) then a GraphQL series lookup by the
    library folder path — deterministic, no title matching."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        library_path: str,
        api_url: str | None = None,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_url = (api_url or base_url).rstrip("/")
        self.username = username
        self.password = password
        self.library_path = library_path.rstrip("/")
        self._client = client

    async def series_url(
        self,
        manga: dict[str, Any],
        managed_books: list[dict[str, str]] | None = None,
    ) -> dict[str, Any]:
        managed_books = managed_books or []
        owned = self._client is None
        client = self._client or httpx.AsyncClient(timeout=15.0)
        try:
            auth = await client.post(
                f"{self.api_url}/api/v2/auth/login",
                params={"generate_token": "true", "create_session": "false"},
                json={"username": self.username, "password": self.password},
            )
            auth.raise_for_status()
            token = str((auth.json() or {}).get("accessToken") or "")
            if not token:
                raise ValueError("Stump did not return an access token")
            try:
                folder = series_directory_name(manga)
            except Exception:  # noqa: BLE001
                folder = str(manga.get("title") or "")
            path = f"{self.library_path}/{folder}"
            query = "query($p: String!) { series(filter:{path:{eq:$p}}, pagination:{offset:{page:1,pageSize:2}}) { nodes { id name path libraryId } } }"
            response = await client.post(
                f"{self.api_url}/api/graphql",
                json={"query": query, "variables": {"p": path}},
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload = response.json() or {}
            if payload.get("errors"):
                raise ValueError(
                    str(payload["errors"][0].get("message") or "GraphQL error")
                )
            nodes = ((payload.get("data") or {}).get("series") or {}).get("nodes") or []
            if not nodes:
                return {
                    "available": False,
                    "reason": f"Stump has not indexed {path} yet",
                }
            item = nodes[0]
            series_id = str(item.get("id") or "")
            matches: dict[str, dict[str, str]] = {}
            if managed_books and series_id:
                managed_paths = _managed_path_index(
                    {book["relative_path"] for book in managed_books}
                )
                page = 1
                page_size = 500
                media_query = (
                    "query($id: String!, $page: Int!, $size: Int!) { "
                    "media(filter:{seriesId:{eq:$id}}, pagination:{offset:{"
                    "page:$page,pageSize:$size}}) { nodes { id path } } }"
                )
                while True:
                    media_response = await client.post(
                        f"{self.api_url}/api/graphql",
                        json={
                            "query": media_query,
                            "variables": {
                                "id": series_id,
                                "page": page,
                                "size": page_size,
                            },
                        },
                        headers={"Authorization": f"Bearer {token}"},
                    )
                    media_response.raise_for_status()
                    media_payload = media_response.json() or {}
                    if media_payload.get("errors"):
                        raise ValueError(
                            str(
                                media_payload["errors"][0].get("message")
                                or "GraphQL error"
                            )
                        )
                    media_nodes = (
                        (media_payload.get("data") or {}).get("media") or {}
                    ).get("nodes") or []
                    for media in media_nodes:
                        relative = _managed_relative_path(
                            media.get("path"), managed_paths
                        )
                        media_id = str(media.get("id") or "")
                        if not relative or not media_id:
                            continue
                        # The book's own page, not the reader: from there
                        # Stump resumes at the right page and shows the
                        # book's metadata.
                        matches[relative] = {
                            "book_id": media_id,
                            "url": _reader_route_url(self.base_url, "books", media_id),
                        }
                    if len(media_nodes) < page_size:
                        break
                    page += 1
            books = _reader_book_links(managed_books, matches)
            return {
                "available": True,
                "series_id": series_id,
                "title": str(item.get("name") or manga.get("title") or ""),
                "url": _reader_route_url(self.base_url, "series", series_id),
                "matched_books": len(books),
                "books": books,
            }
        finally:
            if owned:
                await client.aclose()


async def resolve_reader_link(
    settings: Settings,
    database: Any,
    komga_links: Any,
    manga_id: str,
    *,
    kavita_client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    kind = effective_reader_kind(settings)
    label = READER_LABELS.get(kind, "Reader")
    base = {"reader": kind, "label": label, "configured": False, "available": False}
    if kind == "tankarr":
        from tankarr.native_reader import native_reader_link

        return await asyncio.to_thread(native_reader_link, database, manga_id)
    manga = await asyncio.to_thread(database.get_manga, manga_id)
    if kind == "none":
        return {**base, "reason": "No reader shortcut is configured"}
    if kind == "url":
        template = str(getattr(settings, "reader_series_url_template", "") or "")
        if not template:
            return {
                **base,
                "reason": "Set a series URL template under Settings → Reader",
            }
        return {
            **base,
            "configured": True,
            "available": True,
            "url": template_url(template, manga),
        }
    managed_books = await asyncio.to_thread(
        _managed_library_books, settings, database, manga_id
    )
    base["managed_books"] = len(managed_books)
    if kind == "stump":
        url = str(getattr(settings, "reader_url", "") or "")
        username = str(getattr(settings, "reader_username", "") or "")
        password = str(getattr(settings, "reader_password", "") or "")
        if not url or not username or not password:
            return {
                **base,
                "reason": "Stump needs a URL, username and password under Settings → Reader",
            }
        try:
            result = await StumpReader(
                url,
                username,
                password,
                library_path=str(
                    getattr(settings, "reader_library_path", "/data/comics")
                    or "/data/comics"
                ),
                api_url=getattr(settings, "reader_internal_url", None) or None,
                client=kavita_client,
            ).series_url(manga, managed_books)
        except Exception as exc:  # noqa: BLE001 - the reader is optional
            return {
                **base,
                "configured": True,
                "reason": f"Stump lookup failed: {exc}"[:300],
            }
        return {**base, "configured": True, **result}
    if kind == "kavita":
        url = str(getattr(settings, "reader_url", "") or "")
        key = str(getattr(settings, "reader_api_key", "") or "")
        if not url or not key:
            return {
                **base,
                "reason": "Kavita needs a URL and an API key under Settings → Reader",
            }
        instance_key = hashlib.sha256(
            json.dumps(
                [
                    url,
                    settings.reader_internal_url,
                    key,
                    str(settings.library_dir.resolve()),
                    manga_id,
                ]
            ).encode()
        ).hexdigest()
        try:
            binding = (
                await asyncio.to_thread(_kavita_binding, database, instance_key)
                if managed_books
                else None
            )
            result = await asyncio.wait_for(
                KavitaReader(
                    url,
                    key,
                    api_url=getattr(settings, "reader_internal_url", None) or None,
                    client=kavita_client,
                    binding=binding,
                ).series_url(manga, managed_books),
                timeout=30,
            )
            if result.get("matched_books"):
                await asyncio.to_thread(_kavita_binding, database, instance_key, result)
        except Exception as exc:  # noqa: BLE001 - the reader is optional
            return {
                **base,
                "configured": True,
                "reason": f"Kavita lookup failed ({type(exc).__name__}); verify connection, scan and library paths",
            }
        return {**base, "configured": True, **result}
    # komga
    if not settings.komga_link_enabled:
        return {**base, "reason": "Komga shortcut is disabled"}
    if not komga_links.configured:
        return {**base, "reason": "Komga shortcut credentials are incomplete"}
    relative_paths = {book["relative_path"] for book in managed_books}
    if not relative_paths:
        return {
            **base,
            "configured": True,
            "reason": "No downloaded Tankarr books are available to match",
        }
    catalogue = await komga_links.catalogue_for_paths(relative_paths)
    matched_catalogue_books = {
        relative: book
        for relative, book in catalogue.get("books", {}).items()
        if relative in relative_paths and book.get("series_id")
    }
    series_ids = {
        str(book["series_id"])
        for book in matched_catalogue_books.values()
        if book.get("series_id")
    }
    if not series_ids:
        return {
            **base,
            "configured": True,
            "reason": "Komga has not indexed this Tankarr series yet",
        }
    if len(series_ids) != 1:
        return {
            **base,
            "configured": True,
            "matched_books": len(matched_catalogue_books),
            "reason": "The managed books map to multiple Komga series",
        }
    series_id = next(iter(series_ids))
    series = catalogue.get("series", {}).get(series_id) or {}
    raw_metadata = series.get("metadata")
    metadata_payload = raw_metadata if isinstance(raw_metadata, dict) else {}
    title = metadata_payload.get("title") or series.get("name")
    matches = {
        relative: {
            "book_id": str(book.get("id") or ""),
            "url": komga_book_url(settings, str(book.get("id") or "")),
        }
        for relative, book in matched_catalogue_books.items()
        if book.get("id")
    }
    books = _reader_book_links(managed_books, matches)
    return {
        **base,
        "configured": True,
        "available": True,
        "url": komga_series_url(
            settings, series_id, oneshot=bool(series.get("oneshot"))
        ),
        "series_id": series_id,
        "title": title,
        "matched_books": len(books),
        "books": books,
    }


__all__ = [
    "READER_KINDS",
    "KavitaReader",
    "effective_reader_kind",
    "komga_book_url",
    "komga_series_url",
    "resolve_reader_link",
    "template_url",
]
