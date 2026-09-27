from tankarr.release_sources import official_link_for, official_platform_candidate


def test_official_link_matches_by_platform_title_id_when_paths_are_placeholders():
    target = {
        "language": "en",
        "official_links": [
            {
                "name": "WEBTOON",
                "language": "en",
                "url": "https://www.webtoons.com/-/-/-/list?title_no=88",
            },
            {
                "name": "WEBTOON",
                "language": "fr",
                "url": "https://www.webtoons.com/-/-/-/list?title_no=1883",
            },
        ],
    }
    candidate = {
        "source_url": "https://www.webtoons.com/en/action/the-gamer/list?title_no=88"
    }
    assert official_link_for(target, candidate)["url"].endswith("title_no=88")
    other = {"source_url": "https://www.webtoons.com/en/action/other/list?title_no=99"}
    assert official_link_for(target, other) is None
    # Path matching still works for platforms without an id parameter.
    tapas = {
        "language": "en",
        "official_links": [
            {"language": "en", "url": "https://tapas.io/series/tbate-comic/info"}
        ],
    }
    assert (
        official_link_for(tapas, {"source_url": "https://tapas.io/series/tbate-comic"})
        is not None
    )


def test_exact_title_on_an_official_platform_counts_as_official():
    target = {
        "title": "The Beginning After the End",
        "alternate_titles": ["TBATE"],
        "language": "en",
        "official_links": [
            {"language": "en", "url": "https://tapas.io/series/tbate-comic/info"}
        ],
    }
    candidates = [
        {"title": "After the End", "source_url": "https://tapas.io/series/304929"},
        {
            "title": "The Beginning After the End",
            "source_url": "https://tapas.io/series/111423",
            "source_name": "Tapas",
        },
        {
            "title": "The Beginning After the End",
            "source_url": "https://mangafire.to/manga/x",
        },
    ]
    assert official_platform_candidate(target, candidates)["source_url"].endswith(
        "111423"
    )
    assert (
        official_platform_candidate({**target, "official_links": []}, candidates)
        is None
    )
