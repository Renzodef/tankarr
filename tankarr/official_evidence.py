"""Read-only official-edition evidence used to align translated releases.

These collectors never expose pages as download candidates.  They only retain
the publisher's item index, title and publication date, which lets the
numbering resolver prove an edition bridge without trusting mirrors.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from tankarr import USER_AGENT

NAVER_WEBTOON_HOST = "comic.naver.com"


def naver_webtoon_id(url: object) -> str | None:
    parsed = urlsplit(str(url or ""))
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    if host != NAVER_WEBTOON_HOST:
        return None
    title_id = (parse_qs(parsed.query).get("titleId") or [""])[0].strip()
    return title_id if title_id.isdecimal() else None


def _published_at(value: object) -> str | None:
    text = str(value or "").strip()
    try:
        parsed = datetime.strptime(text, "%y.%m.%d").replace(tzinfo=UTC)
    except ValueError:
        return None
    return parsed.isoformat()


def parse_naver_webtoon_items(
    payload: dict[str, Any], title_id: str
) -> list[dict[str, Any]]:
    """Normalize the official article-list response without interpreting content."""

    items: list[dict[str, Any]] = []
    for article in payload.get("articleList") or []:
        if not isinstance(article, dict):
            continue
        number = article.get("no")
        if not isinstance(number, int) or number <= 0:
            continue
        items.append(
            {
                "provider_index": str(number),
                "edition_chapter": str(article.get("volumeNo") or number),
                "title": str(article.get("subtitle") or ""),
                "publish_at": _published_at(article.get("serviceDateDescription")),
                "source_url": (
                    "https://comic.naver.com/webtoon/detail?titleId="
                    f"{title_id}&no={number}"
                ),
                "evidence": {
                    "official": True,
                    "locked": bool(article.get("thumbnailLock")),
                    "source": "naver_webtoon_article_list",
                },
            }
        )
    return items


async def fetch_naver_webtoon_items(
    client: httpx.AsyncClient, url: str
) -> tuple[str, list[dict[str, Any]]]:
    """Fetch the newest official Naver items for one MangaBaka platform link."""

    title_id = naver_webtoon_id(url)
    if title_id is None:
        return "", []
    response = await client.get(
        "https://comic.naver.com/api/article/list",
        params={"titleId": title_id, "page": 1},
        headers={"Referer": "https://comic.naver.com/", "User-Agent": USER_AGENT},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("Naver returned an invalid article list")
    return NAVER_WEBTOON_HOST, parse_naver_webtoon_items(payload, title_id)


__all__ = [
    "NAVER_WEBTOON_HOST",
    "fetch_naver_webtoon_items",
    "naver_webtoon_id",
    "parse_naver_webtoon_items",
]
