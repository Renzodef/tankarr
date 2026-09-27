from __future__ import annotations

from tankarr.official_evidence import naver_webtoon_id, parse_naver_webtoon_items


def test_naver_evidence_keeps_only_official_index_metadata():
    items = parse_naver_webtoon_items(
        {
            "articleList": [
                {
                    "no": 612,
                    "volumeNo": 612,
                    "subtitle": "612화",
                    "serviceDateDescription": "26.08.06",
                    "thumbnailLock": True,
                }
            ]
        },
        "641253",
    )

    assert (
        naver_webtoon_id("https://comic.naver.com/webtoon/list?titleId=641253")
        == "641253"
    )
    assert items == [
        {
            "provider_index": "612",
            "edition_chapter": "612",
            "title": "612화",
            "publish_at": "2026-08-06T00:00:00+00:00",
            "source_url": "https://comic.naver.com/webtoon/detail?titleId=641253&no=612",
            "evidence": {
                "official": True,
                "locked": True,
                "source": "naver_webtoon_article_list",
            },
        }
    ]
