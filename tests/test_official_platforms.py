from tankarr.official_platforms import extensions_to_install, official_platforms_for


def test_official_platforms_come_from_the_catalogue_and_map_to_extensions():
    manga = {"preferred_language": "en"}
    metadata = {
        "official_links": [
            {
                "name": "WEBTOON",
                "language": "en",
                "url": "https://www.webtoons.com/-/-/-/list?title_no=88",
            },
            {
                "name": "WEBTOON",
                "language": "fr",
                "url": "https://www.webtoons.com/fr/-/list?title_no=1883",
            },
            {
                "name": "VIZ Media",
                "language": "en",
                "url": "https://www.viz.com/kingdom",
            },
            {
                "name": "NAVER",
                "language": "ko",
                "url": "https://comic.naver.com/webtoon/list?titleId=552960",
            },
            {
                "name": "Publisher site",
                "language": "en",
                "url": "https://one-piece.com/",
            },
        ]
    }
    installed = [
        {
            "name": "Webtoons.com (EN)",
            "pkg_name": "eu.kanade.tachiyomi.extension.all.webtoons",
            "home_url": "https://www.webtoons.com",
        }
    ]
    mapped = [{"source_name": "Webtoons.com (EN)"}]
    platforms = official_platforms_for(
        manga, metadata, installed_sources=installed, release_sources=mapped
    )
    by_host = {item["host"]: item for item in platforms}
    assert set(by_host) == {
        "webtoons.com",
        "viz.com",
    }  # ko-only and unknown sites are skipped
    assert (
        by_host["webtoons.com"]["free"]
        and by_host["webtoons.com"]["installed"]
        and by_host["webtoons.com"]["mapped"]
    )
    assert (
        by_host["viz.com"]["free"] is False and by_host["viz.com"]["installed"] is False
    )
    assert (
        extensions_to_install(platforms) == []
    )  # VIZ is paid, WEBTOON already installed
    fresh = official_platforms_for(
        manga,
        {
            "official_links": [
                {"language": "en", "url": "https://tapas.io/series/tbate-comic/info"}
            ]
        },
        installed_sources=[],
    )
    assert extensions_to_install(fresh) == ["eu.kanade.tachiyomi.extension.en.tapastic"]


def test_installed_source_home_url_maps_platforms_without_a_seed_entry():
    metadata = {
        "official_links": [
            {"language": "en", "url": "https://www.example-official.io/series/1"}
        ]
    }
    installed = [
        {
            "name": "Example Official (EN)",
            "pkg_name": "eu.kanade.tachiyomi.extension.en.example",
            "home_url": "https://example-official.io",
        }
    ]
    platforms = official_platforms_for(
        {"preferred_language": "en"}, metadata, installed_sources=installed
    )
    assert (
        platforms
        and platforms[0]["pkg_name"].endswith("en.example")
        and platforms[0]["installed"]
    )


def test_mapped_is_detected_by_the_download_source_url_host_too():
    metadata = {
        "official_links": [
            {"language": "en", "url": "https://tapas.io/series/tbate-comic/info"}
        ]
    }
    mapped = [
        {"source_name": "Tapas (EN)", "source_url": "https://tapas.io/series/111423"}
    ]
    platforms = official_platforms_for(
        {"preferred_language": "en"},
        metadata,
        installed_sources=[],
        release_sources=mapped,
    )
    assert platforms[0]["mapped"] is True and platforms[0]["installed"] is False


def test_the_platform_link_is_the_one_in_the_series_language():
    """WEBTOON publishes Lookism in es, fr, th and en; the English series must
    show and map the English page, not whichever came first."""

    metadata = {
        "official_links": [
            {
                "name": "WEBTOON",
                "language": "es",
                "url": "https://www.webtoons.com/es/x/list?title_no=1930",
                "type": "webplatform",
            },
            {
                "name": "WEBTOON",
                "language": "en",
                "url": "https://www.webtoons.com/en/x/list?title_no=1049",
                "type": "webplatform",
            },
        ]
    }
    manga = {"id": "m1", "preferred_language": "en"}
    platforms = official_platforms_for(manga, metadata, installed_sources=[])

    assert [item["url"] for item in platforms] == [
        "https://www.webtoons.com/en/x/list?title_no=1049"
    ]
