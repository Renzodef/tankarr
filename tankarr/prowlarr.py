from __future__ import annotations

import re
from collections import defaultdict
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from tankarr import USER_AGENT
from tankarr.config import Settings
from tankarr.http import async_client
from tankarr.sabnzbd import nzb_pseudo_hash
from tankarr.torrent_utils import (
    INFO_HASH_PATTERN,
    MAX_TORRENT_BYTES,
    magnet_info_hash,
    release_match_score,
    release_number_hints,
    torrent_info_hash,
)

MAX_RESPONSE_BYTES = 8 * 1024 * 1024
STANDARD_BOOK_CATEGORIES = {
    7000: "Books",
    7010: "Magazines",
    7020: "EBooks",
    7030: "Comics",
    7040: "Technical",
    7050: "Other books",
    7060: "Foreign books",
}
BOOK_CATEGORY_PATTERN = re.compile(
    r"(?:^|[\s/_-])(?:books?|e-?books?|comics?|manga|literature|mags?|"
    r"magazines?|novels?|fumetti)(?:$|[\s/_-])",
    re.IGNORECASE,
)
NON_ENGLISH_RELEASE_TAG_PATTERN = re.compile(
    r"(?:\[|\()(?:ja|jp|jpn|japanese|it|ita|italian|fr|fre|french|es|spa|"
    r"spanish|de|ger|german|ru|rus|pt(?:-br)?|por|cn|chi|zh|kr|kor)"
    r"(?:\]|\))",
    re.IGNORECASE,
)
RAW_RELEASE_MARKER_PATTERN = re.compile(
    r"(?:\[|\()[^\]\)]{0,12}raw[^\]\)]{0,12}(?:\]|\))",
    re.IGNORECASE,
)


class ProwlarrError(RuntimeError):
    pass


def _positive_integer(value: object) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if 0 < parsed <= 2_147_483_647 else None


def _nonnegative_integer(value: object) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return 0
    return max(0, min(parsed, 9_223_372_036_854_775_807))


class ProwlarrClient:
    """Indexer discovery and verified torrent hand-off through Prowlarr."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return bool(self.settings.prowlarr_url and self.settings.prowlarr_api_key)

    @property
    def enabled(self) -> bool:
        return bool(self.settings.prowlarr_enabled)

    def _client(self) -> httpx.AsyncClient:
        if not self.enabled:
            raise ProwlarrError("Prowlarr is disabled")
        if not self.configured:
            raise ProwlarrError("Prowlarr URL and API key are required")
        return async_client(
            base_url=self.settings.prowlarr_url,
            timeout=httpx.Timeout(self.settings.request_timeout_seconds),
            follow_redirects=False,
            headers={
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
                "X-Api-Key": str(self.settings.prowlarr_api_key),
            },
        )

    async def probe(self) -> dict[str, Any]:
        async with self._client() as client:
            status = await self._get_json(client, "/api/v1/system/status")
            raw_indexers = await self._get_json(client, "/api/v1/indexer")

        if not isinstance(status, dict):
            raise ProwlarrError("Prowlarr returned an invalid system status")
        if not isinstance(raw_indexers, list):
            raise ProwlarrError("Prowlarr returned an invalid indexer list")

        indexers, categories = self._catalog(raw_indexers)
        return {
            "ok": True,
            "version": str(status.get("version") or "unknown")[:80],
            "instance_name": str(status.get("instanceName") or "Prowlarr")[:120],
            "indexers": indexers,
            "categories": categories,
            "enabled_indexers": sum(1 for item in indexers if item["enabled"]),
            "compatible_indexers": sum(
                1 for item in indexers if item["enabled"] and item["compatible"]
            ),
        }

    async def search(
        self,
        query: str,
        language: str = "en",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        normalized = " ".join(str(query or "").split()).strip()
        if len(normalized) < 2 or len(normalized) > 200:
            raise ValueError("Prowlarr search query must contain 2 to 200 characters")
        bounded_limit = max(1, min(int(limit), 75))
        parameters: list[tuple[str, object]] = [
            ("query", normalized),
            ("type", "search"),
            ("limit", bounded_limit),
        ]
        parameters.extend(
            ("categories", category_id)
            for category_id in sorted(self.settings.prowlarr_category_id_set)
        )
        parameters.extend(
            ("indexerIds", indexer_id)
            for indexer_id in sorted(self.settings.prowlarr_indexer_id_set)
        )
        async with self._client() as client:
            payload = await self._get_json(client, "/api/v1/search", params=parameters)
        if not isinstance(payload, list):
            raise ProwlarrError("Prowlarr returned an invalid search result")

        output: list[dict[str, Any]] = []
        seen_hashes: set[str] = set()
        for raw in payload:
            release = self._normalize_release(raw, normalized, language)
            if release is None or release["info_hash"] in seen_hashes:
                continue
            seen_hashes.add(release["info_hash"])
            output.append(release)
        output.sort(
            key=lambda item: (
                int(item["match_score"]),
                -int(item["indexer_priority"]),
                int(item["seeders"]),
                int(item["downloads"]),
            ),
            reverse=True,
        )
        return output[:bounded_limit]

    async def resolve_download(
        self, download_ref: str, expected_hash: str
    ) -> tuple[str, bytes | str]:
        """Resolve one cached result without exposing Prowlarr credentials."""

        normalized_hash = str(expected_hash or "").casefold()
        if INFO_HASH_PATTERN.fullmatch(normalized_hash) is None:
            raise ValueError("Invalid Prowlarr info hash")
        if str(download_ref).startswith("magnet:"):
            if magnet_info_hash(download_ref) != normalized_hash:
                raise ValueError("Prowlarr magnet hash differs from its search result")
            return "magnet", download_ref

        current = self._safe_download_ref(download_ref, normalized_hash)
        async with self._client() as client:
            for _ in range(2):
                try:
                    response = await client.get(
                        current,
                        headers={"Accept": "application/x-bittorrent"},
                    )
                except httpx.HTTPError as exc:
                    raise ProwlarrError(
                        f"Prowlarr download failed: {type(exc).__name__}"
                    ) from exc
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = str(response.headers.get("location") or "")
                    if location.startswith("magnet:"):
                        if magnet_info_hash(location) != normalized_hash:
                            raise ValueError(
                                "Prowlarr redirect hash differs from its search result"
                            )
                        return "magnet", location
                    current = self._safe_download_ref(location, normalized_hash)
                    continue
                if response.status_code in {401, 403}:
                    raise ProwlarrError("Prowlarr rejected the API key")
                try:
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise ProwlarrError(
                        f"Prowlarr download failed with HTTP {response.status_code}"
                    ) from exc
                if len(response.content) > MAX_TORRENT_BYTES:
                    raise ValueError(
                        "Prowlarr torrent exceeds the metadata safety limit"
                    )
                if torrent_info_hash(response.content) != normalized_hash:
                    raise ValueError(
                        "Prowlarr torrent hash differs from its search result"
                    )
                return "torrent", response.content
        raise ProwlarrError("Prowlarr returned too many download redirects")

    @staticmethod
    async def _get_json(
        client: httpx.AsyncClient,
        path: str,
        *,
        params: list[tuple[str, object]] | None = None,
    ) -> Any:
        try:
            response = await client.get(path, params=params)
            if response.status_code in {401, 403}:
                raise ProwlarrError("Prowlarr rejected the API key")
            response.raise_for_status()
        except ProwlarrError:
            raise
        except httpx.HTTPError as exc:
            raise ProwlarrError(
                f"Prowlarr request failed: {type(exc).__name__}"
            ) from exc
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ProwlarrError("Prowlarr response exceeds the safety limit")
        try:
            return response.json()
        except ValueError as exc:
            raise ProwlarrError("Prowlarr returned invalid JSON") from exc

    def _normalize_release(
        self,
        raw: object,
        query: str,
        language: str,
    ) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        protocol = str(raw.get("protocol") or "").casefold()
        if protocol not in {"torrent", "usenet"}:
            return None
        title = " ".join(str(raw.get("title") or "").split()).strip()
        indexer_id = _positive_integer(raw.get("indexerId"))
        indexer = " ".join(str(raw.get("indexer") or "").split()).strip()
        if not title or indexer_id is None or not indexer:
            return None
        if protocol == "usenet":
            # NZBs have no info hash: a stable digest of the release guid
            # plays that role so the job table and the UI treat both alike.
            guid = str(raw.get("guid") or raw.get("downloadUrl") or "").strip()
            if not guid:
                return None
            info_hash = nzb_pseudo_hash(guid)
            download_ref = str(raw.get("downloadUrl") or "").strip()
            if not download_ref.startswith(("http://", "https://")):
                return None
        else:
            info_hash = str(raw.get("infoHash") or "").strip().casefold()
            if INFO_HASH_PATTERN.fullmatch(info_hash) is None:
                return None
            download_ref = self._release_download_ref(raw, info_hash)
            if download_ref is None:
                return None
        category_ids, category_names = self._release_categories(raw.get("categories"))
        if language.casefold() == "en" and self._clearly_non_english_release(
            title, category_names
        ):
            return None
        selected = self.settings.prowlarr_category_id_set
        if selected and not category_ids.intersection(selected):
            return None
        configured_indexers = self.settings.prowlarr_indexer_id_set
        if configured_indexers and indexer_id not in configured_indexers:
            return None
        volume, chapter = release_number_hints(title)
        size_bytes = _nonnegative_integer(raw.get("size"))
        flags = {
            str(item).casefold()
            for item in raw.get("indexerFlags") or []
            if isinstance(item, str)
        }
        source_url = self._public_http_url(raw.get("infoUrl"))
        indexer_priority = (
            _positive_integer(raw.get("indexerPriority") or raw.get("priority")) or 50
        )
        return {
            "id": f"{indexer_id}-{info_hash}",
            "provider": "prowlarr",
            "protocol": protocol,
            "provider_label": f"Prowlarr · {indexer}",
            "indexer": indexer[:200],
            "indexer_id": indexer_id,
            "indexer_priority": indexer_priority,
            "title": title[:500],
            "language": language,
            "category_id": str(min(category_ids)) if category_ids else "",
            "category_ids": sorted(category_ids),
            "category": ", ".join(category_names)[:500]
            or ("Prowlarr usenet" if protocol == "usenet" else "Prowlarr torrent"),
            "size": self._format_size(size_bytes),
            "size_bytes": size_bytes,
            "seeders": _nonnegative_integer(raw.get("seeders")),
            "leechers": _nonnegative_integer(raw.get("leechers")),
            "downloads": _nonnegative_integer(raw.get("grabs")),
            "comments": 0,
            "trusted": "trusted" in flags,
            "remake": False,
            "info_hash": info_hash,
            "publish_at": str(raw.get("publishDate") or "")[:100] or None,
            "source_url": source_url,
            "download_ref": download_ref,
            "volume": volume,
            "chapter": chapter,
            "match_score": release_match_score(query, title),
        }

    @staticmethod
    def _clearly_non_english_release(title: str, categories: list[str]) -> bool:
        """Reject only explicit non-English evidence before costly downloading.

        Ambiguous releases remain eligible and are still required to pass the
        archive and OCR audit after qBittorrent finishes.
        """

        if NON_ENGLISH_RELEASE_TAG_PATTERN.search(title):
            return True
        if RAW_RELEASE_MARKER_PATTERN.search(title):
            return True
        return any(
            re.search(r"(?:^|[\s/_-])raws?(?:$|[\s/_-])", category, re.IGNORECASE)
            for category in categories
        )

    def _release_download_ref(
        self, raw: dict[str, Any], expected_hash: str
    ) -> str | None:
        candidates = [
            raw.get("guid"),
            raw.get("downloadUrl"),
            raw.get("magnetUrl"),
        ]
        for candidate in candidates:
            value = str(candidate or "").strip()
            if not value:
                continue
            try:
                if value.startswith("magnet:"):
                    if magnet_info_hash(value) == expected_hash:
                        return value
                    continue
                return self._safe_download_ref(value, expected_hash)
            except ValueError:
                continue
        return None

    def _safe_download_ref(self, raw: object, expected_hash: str) -> str:
        value = str(raw or "").strip()
        if not value or len(value) > 65_535:
            raise ValueError("Invalid Prowlarr download reference")
        if value.startswith("magnet:"):
            if magnet_info_hash(value) != expected_hash:
                raise ValueError("Prowlarr magnet hash differs from its search result")
            return value
        parsed = urlsplit(value)
        base = urlsplit(str(self.settings.prowlarr_url or ""))
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Unsafe Prowlarr download reference")
        if parsed.scheme or parsed.netloc:
            if (
                parsed.scheme.casefold() != base.scheme.casefold()
                or parsed.hostname != base.hostname
                or parsed.port != base.port
            ):
                raise ValueError("Prowlarr download reference changed origin")
        if not re.fullmatch(r"/[1-9][0-9]*/download", parsed.path):
            raise ValueError("Unsupported Prowlarr download path")
        query = [
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() != "apikey"
        ]
        if not query:
            raise ValueError("Prowlarr download reference is incomplete")
        return urlunsplit(("", "", parsed.path, urlencode(query, doseq=True), ""))

    @staticmethod
    def _release_categories(raw: object) -> tuple[set[int], list[str]]:
        identifiers: set[int] = set()
        names: list[str] = []

        def collect(values: object) -> None:
            if not isinstance(values, list):
                return
            for item in values:
                if not isinstance(item, dict):
                    continue
                identifier = _positive_integer(item.get("id"))
                if identifier is not None:
                    identifiers.add(identifier)
                name = " ".join(str(item.get("name") or "").split()).strip()
                if name and name not in names:
                    names.append(name[:200])
                collect(item.get("subCategories"))

        collect(raw)
        return identifiers, names

    @staticmethod
    def _public_http_url(raw: object) -> str:
        value = str(raw or "").strip()
        if not value or len(value) > 2_000:
            return ""
        parsed = urlsplit(value)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.username or parsed.password:
            return ""
        return value

    @staticmethod
    def _format_size(value: int) -> str:
        amount = float(max(0, value))
        units = ("B", "KiB", "MiB", "GiB", "TiB")
        for unit in units:
            if amount < 1024 or unit == units[-1]:
                return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
            amount /= 1024
        return f"{int(value)} B"

    def _catalog(
        self, raw_indexers: list[object]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        category_names: dict[int, set[str]] = defaultdict(set)
        category_indexers: dict[int, set[int]] = defaultdict(set)
        indexer_categories: dict[int, set[int]] = defaultdict(set)
        parsed_indexers: list[dict[str, Any]] = []

        for raw in raw_indexers:
            if not isinstance(raw, dict):
                continue
            indexer_id = _positive_integer(raw.get("id"))
            name = str(raw.get("name") or "").strip()
            if indexer_id is None or not name:
                continue
            capabilities = raw.get("capabilities")
            roots = (
                capabilities.get("categories") if isinstance(capabilities, dict) else []
            )
            self._collect_categories(
                roots if isinstance(roots, list) else [],
                indexer_id=indexer_id,
                names=category_names,
                indexers=category_indexers,
                selected=indexer_categories[indexer_id],
            )
            parsed_indexers.append(
                {
                    "id": indexer_id,
                    "name": name[:200],
                    "enabled": bool(raw.get("enable")),
                    "protocol": str(raw.get("protocol") or "unknown")[:30],
                    "priority": int(raw.get("priority") or 0),
                }
            )

        configured_indexers = self.settings.prowlarr_indexer_id_set
        indexer_output: list[dict[str, Any]] = []
        for item in parsed_indexers:
            indexer_id = int(item["id"])
            compatible = bool(indexer_categories[indexer_id])
            item["compatible"] = compatible
            item["selected"] = (
                indexer_id in configured_indexers
                if configured_indexers
                else bool(item["enabled"] and compatible)
            )
            item["category_ids"] = sorted(indexer_categories[indexer_id])
            indexer_output.append(item)
        indexer_output.sort(
            key=lambda item: (
                not item["enabled"],
                not item["compatible"],
                int(item["priority"]),
                str(item["name"]).casefold(),
            )
        )

        configured_categories = self.settings.prowlarr_category_id_set
        category_output = [
            {
                "id": category_id,
                "name": STANDARD_BOOK_CATEGORIES.get(
                    category_id,
                    min(
                        category_names[category_id],
                        key=lambda value: (len(value), value),
                    ),
                ),
                "indexer_ids": sorted(category_indexers[category_id]),
                "selected": category_id in configured_categories,
            }
            for category_id in category_names
        ]
        category_output.sort(
            key=lambda item: (
                0 if int(item["id"]) in STANDARD_BOOK_CATEGORIES else 1,
                int(item["id"]),
                str(item["name"]).casefold(),
            )
        )
        return indexer_output, category_output

    @classmethod
    def _collect_categories(
        cls,
        values: list[object],
        *,
        indexer_id: int,
        names: dict[int, set[str]],
        indexers: dict[int, set[int]],
        selected: set[int],
        book_parent: bool = False,
    ) -> None:
        for raw in values:
            if not isinstance(raw, dict):
                continue
            category_id = _positive_integer(raw.get("id"))
            name = str(raw.get("name") or "").strip()
            is_book = bool(
                book_parent
                or (category_id is not None and category_id in STANDARD_BOOK_CATEGORIES)
                or BOOK_CATEGORY_PATTERN.search(name)
            )
            if is_book and category_id is not None and name:
                names[category_id].add(name[:200])
                indexers[category_id].add(indexer_id)
                selected.add(category_id)
            children = raw.get("subCategories")
            cls._collect_categories(
                children if isinstance(children, list) else [],
                indexer_id=indexer_id,
                names=names,
                indexers=indexers,
                selected=selected,
                book_parent=is_book,
            )
