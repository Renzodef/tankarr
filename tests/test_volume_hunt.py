from __future__ import annotations

import pytest

from tankarr.internet_archive import PROVIDER_NAME as DIRECT_PROVIDER
from tankarr.volume_hunt import (
    hunt_volumes,
    pick_release,
    plan_releases,
    probe_offers,
    volume_queries,
)


def rel(title, volume, protocol="usenet", seeders=0, ident=None):
    return {
        "id": ident or title,
        "provider": "prowlarr",
        "title": title,
        "volume": str(volume),
        "protocol": protocol,
        "seeders": seeders,
        "download": None,
    }


def test_pick_release_requires_exact_title_words_and_volume_and_prefers_official_digital_usenet():
    results = [
        rel("BECK v03 (2019) (Kodansha Comics USA) (Digital) (1r0n)", 3),
        rel("BECK v03 raw scan", 3, protocol="torrent", seeders=5),
        rel("BECK v04 (2019) (Kodansha Comics USA) (Digital)", 4),
        rel(
            "Beck Mongolian Chop Squad v03", 3, protocol="torrent", seeders=0
        ),  # dead torrent
        rel("Something Else v03 (Digital)", 3),
    ]
    chosen = pick_release(results, title="BECK", volume=3, publisher="Kodansha")
    assert chosen["title"].startswith("BECK v03 (2019) (Kodansha")
    assert pick_release(results, title="BECK", volume=9, publisher=None) is None
    only_torrent = [rel("BECK v03 raw scan", 3, protocol="torrent", seeders=5)]
    assert (
        pick_release(only_torrent, title="BECK", volume=3, publisher=None)["seeders"]
        == 5
    )
    assert (
        pick_release(
            [rel("BECK v03", 3, protocol="torrent", seeders=0)],
            title="BECK",
            volume=3,
            publisher=None,
        )
        is None
    )
    assert volume_queries("BECK", 3, "Kodansha") == ["BECK Kodansha", "BECK"]
    assert volume_queries("Grass", 1, ["Drawn & Quarterly", "Bori Publishing"]) == [
        "Grass Drawn Quarterly",
        "Grass Bori Publishing",
        "Grass",
    ]
    # A one-book work: the release carries no volume number.
    book = [
        rel(
            "Grass (2019) (Drawn&Quarterly) (Digital) (morrol4n)",
            "",
            protocol="torrent",
            seeders=1,
        )
    ]
    assert (
        pick_release(
            book,
            title="Grass",
            volume=1,
            publisher=["Drawn & Quarterly"],
            single_volume=True,
        )
        is not None
    )
    assert pick_release(book, title="Grass", volume=1, publisher=None) is None


def test_multiword_work_title_is_not_a_subtitle_of_another_book():
    from tankarr.volume_hunt import offered_volumes

    foreign = rel(
        "The.Witcher.v08-Wild.Animals.2024.digital.Son.of.Ultron-Empire",
        8,
        protocol="usenet",
    )
    assert (
        pick_release([foreign], title="Wild Animals", volume=8, publisher=None) is None
    )
    assert offered_volumes([foreign], title="Wild Animals") == []

    own = rel("Wild Animals v01 (Digital)", 1, protocol="usenet")
    assert offered_volumes([own], title="Wild Animals")


def test_same_title_different_publisher_is_not_imported_or_offered():
    from tankarr.volume_hunt import offered_volumes

    foreign = rel("River Birds v01 [2026] [Silver Pine Studios] [Digital]", 1)
    own = rel("River Birds v01 [North Coast Press] [Digital]", 1)
    publishers = ["North Coast Press", "East Harbor Books"]

    assert (
        pick_release([foreign], title="River Birds", volume=1, publisher=publishers)
        is None
    )
    assert "unverified publisher" in foreign["_review"]
    assert (
        plan_releases(
            [foreign],
            title="River Birds",
            missing_volumes=[1],
            publisher=publishers,
        )
        == []
    )
    assert offered_volumes([foreign], title="River Birds", publisher=publishers) == []
    assert (
        pick_release([own], title="River Birds", volume=1, publisher=publishers) is own
    )
    assert [
        row["volume"]
        for row in offered_volumes([own], title="River Birds", publisher=publishers)
    ] == [1]
    # "Manga" can be a release-group label, not an explicit publisher claim.
    group = rel("River Birds v02 [KG Manga] [Digital]", 2)
    assert (
        pick_release([group], title="River Birds", volume=2, publisher=publishers)
        is group
    )


def test_numbered_family_does_not_override_explicit_publisher_conflict():
    from tankarr.volume_hunt import offered_volumes

    foreign = [
        rel(f"Mars v{number:02d} [Silver Pine Studios]", number)
        for number in range(1, 16)
    ]
    assert (
        pick_release(
            foreign,
            title="Mars",
            volume=1,
            publisher="North Coast Press",
            volume_count=15,
        )
        is None
    )
    assert (
        offered_volumes(
            foreign, title="Mars", publisher="North Coast Press", volume_count=15
        )
        == []
    )


@pytest.mark.asyncio
async def test_book_probe_prunes_explicit_publisher_conflict_but_keeps_other_offers(
    tmp_path,
):
    from types import SimpleNamespace

    from tankarr.database import Database

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga({"id": "book", "title": "River Birds"}, "en")
    database.record_indexer_offers(
        "book",
        [
            {
                "volume": "1",
                "protocol": "usenet",
                "title": "River Birds v01 [Silver Pine Studios] [Digital]",
            },
            {
                "volume": "2",
                "protocol": "usenet",
                "title": "River Birds v02 [North Coast Press] [Digital]",
            },
            {
                "volume": "3",
                "protocol": "usenet",
                "title": "Far Birds v03 [North Coast Press] [Digital]",
            },
        ],
    )

    class EmptyIndexer:
        prowlarr = SimpleNamespace(enabled=True, configured=True)
        direct_available = False

        def __init__(self):
            self.database = database

        async def search(self, *_args, **_kwargs):
            return {"results": []}

    await probe_offers(
        EmptyIndexer(),
        manga={"id": "book", "title": "River Birds"},
        publisher="North Coast Press",
        volume_count=3,
    )

    assert sorted(
        (row["volume"], row["title"]) for row in database.list_indexer_offers("book")
    ) == [
        ("2", "River Birds v02 [North Coast Press] [Digital]"),
        ("3", "Far Birds v03 [North Coast Press] [Digital]"),
    ]


@pytest.mark.asyncio
async def test_rule_refused_book_is_not_reported_as_an_open_review(tmp_path):
    from types import SimpleNamespace

    from tankarr.database import Database
    from tankarr.wanted_recovery import NOT_OFFERED, book_hunt_outcome

    database = Database(tmp_path / "tankarr.sqlite3")
    database.initialize()
    database.upsert_manga({"id": "book", "title": "River Birds"}, "en")

    class ForeignBookSource:
        prowlarr = SimpleNamespace(enabled=True, configured=True)
        direct_available = False

        def __init__(self):
            self.database = database

        async def search(self, *_args, **_kwargs):
            return {
                "results": [rel("River Birds v01 [Silver Pine Studios] [Digital]", 1)]
            }

        async def grab(self, *_args):
            pytest.fail("A conflicting edition must not be downloaded")

    result = await hunt_volumes(
        ForeignBookSource(),
        manga={"id": "book", "title": "River Birds"},
        missing_volumes=[1],
        publisher="North Coast Press",
        volume_count=1,
    )
    assert result["grabbed"] == []
    assert result["needs_review"] is False
    assert book_hunt_outcome(result, volumes=[1])[0] == NOT_OFFERED
    assert database.list_match_reviews() == []
    history = database.list_match_reviews(open_only=False)
    assert len(history) == 1
    assert history[0]["resolution"]


class FakeTorrents:
    class prowlarr:
        enabled = True
        configured = True

    def __init__(self):
        self.queries: list[str] = []
        self.grabbed: list[str] = []

    async def search(self, manga_id, query, *, limit=50, _include_download_ref=False):
        self.queries.append(query)
        if query.endswith("Kodansha"):
            return {
                "results": [
                    rel(
                        "BECK v02 (2019) (Kodansha Comics USA) (Digital)", 2, ident="k2"
                    ),
                    rel(
                        "BECK v07 (2019) (Kodansha Comics USA) (Digital)", 7, ident="k7"
                    ),
                ]
            }
        return {"results": []}

    async def grab(self, manga_id, provider, release_id):
        self.grabbed.append(release_id)
        return {"id": len(self.grabbed)}


class RecordingBookSources:
    direct_available = True

    class prowlarr:
        enabled = True
        configured = True

    def __init__(self):
        self.calls = []

    async def search(self, manga_id, query, *, sources, **kwargs):
        self.calls.append((query, sources))
        return {"results": []}


@pytest.mark.asyncio
async def test_volume_hunt_asks_archive_once_and_indexers_for_each_book_query():
    torrents = RecordingBookSources()
    await hunt_volumes(
        torrents,
        manga={"id": "work", "title": "A Work"},
        missing_volumes=[1, 2],
        publisher=None,
    )
    assert torrents.calls[0] == ("A Work", (DIRECT_PROVIDER,))
    assert sum(sources == (DIRECT_PROVIDER,) for _, sources in torrents.calls) == 1
    assert all(sources == ("prowlarr",) for _, sources in torrents.calls[1:])
    assert len(torrents.calls) > 2

    torrents.calls.clear()
    await probe_offers(
        torrents,
        manga={"id": "work", "title": "A Work"},
        publisher="Publisher",
    )
    assert torrents.calls == [
        ("A Work", (DIRECT_PROVIDER,)),
        ("A Work Publisher", ("prowlarr",)),
        ("A Work", ("prowlarr",)),
    ]


@pytest.mark.asyncio
async def test_archive_only_book_probe_keeps_its_offer_without_indexer_queries():
    class ArchiveOnly(RecordingBookSources):
        class prowlarr:
            enabled = False
            configured = False

        async def search(self, manga_id, query, *, sources, **kwargs):
            self.calls.append((query, sources))
            return {
                "results": [
                    {
                        **rel("A Work v01 (Digital)", 1),
                        "provider": DIRECT_PROVIDER,
                        "protocol": "http",
                    }
                ]
            }

        async def grab(self, manga_id, provider, release_id):
            assert provider == DIRECT_PROVIDER
            return {"id": 1}

    torrents = ArchiveOnly()
    offers = await probe_offers(
        torrents,
        manga={"id": "work", "title": "A Work"},
        publisher=None,
        volume_count=1,
    )
    assert offers == 1
    assert torrents.calls == [("A Work", (DIRECT_PROVIDER,))]

    torrents.calls.clear()
    result = await hunt_volumes(
        torrents,
        manga={"id": "work", "title": "A Work"},
        missing_volumes=[1],
        publisher=None,
        volume_count=1,
    )
    assert [item["volume"] for item in result["grabbed"]] == [1]
    assert torrents.calls == [("A Work", (DIRECT_PROVIDER,))]


@pytest.mark.asyncio
async def test_hunt_grabs_one_release_per_missing_volume_only_when_unambiguous():
    torrents = FakeTorrents()
    result = await hunt_volumes(
        torrents,
        manga={"id": "beck", "title": "BECK"},
        missing_volumes=[2, 5, 7],
        publisher="Kodansha",
    )
    assert [item["volume"] for item in result["grabbed"]] == [2, 7]
    assert torrents.grabbed == ["k2", "k7"]
    # One publisher query, then the plain title, then v05 asked by number
    # because the series queries never surfaced it.
    assert torrents.queries == [
        "BECK Kodansha",
        "BECK",
        "BECK v05",
        "BECK Vol 5",
        "BECK volume 5",
    ]


def test_offered_volumes_expand_packs_and_reject_foreign_or_dead_releases():
    from tankarr.volume_hunt import offered_volumes

    results = [
        rel(
            "The Legend of Kamui v01-02 (2025) (c2c) (Trite)",
            "",
            protocol="torrent",
            seeders=15,
        ),
        rel(
            "Kamui-Den.T04.2012.FRENCH.HYBRiD.COMiC.CBZ.eBook-TONER",
            "",
            protocol="usenet",
        ),
        rel(
            "The Legend of Kamui v03 (2025) (Digital)",
            "3",
            protocol="torrent",
            seeders=0,
        ),
    ]
    rows = offered_volumes(results, title="The Legend of Kamui")
    assert sorted(row["volume"] for row in rows) == [1, 2]


def pack(volumes: str, *, seeders: int = 20, identifier: str = "pack") -> dict:
    return {
        "id": identifier,
        "provider": "prowlarr",
        "title": f"Example Work v{volumes} (Digital)",
        "seeders": seeders,
        "size_bytes": 2_000_000_000,
        "protocol": "torrent",
    }


def test_a_pack_inside_the_work_is_grabbed_without_asking():
    """Most of the pack is what the library is missing, and it stays inside
    the work: nothing for a human to decide."""

    chosen = pick_release(
        [pack("01-10")],
        title="Example Work",
        volume=2,
        publisher=None,
        volume_count=10,
        wanted_volumes=set(range(2, 11)),
    )

    assert chosen is not None
    assert "_review" not in chosen


def test_planner_grabs_one_pack_instead_of_one_release_per_volume():
    releases = [
        pack("01-10", identifier="complete-pack"),
        rel("Example Work v02 (Digital)", 2, ident="single-2"),
        rel("Example Work v03 (Digital)", 3, ident="single-3"),
    ]

    plan = plan_releases(
        releases,
        title="Example Work",
        missing_volumes=list(range(2, 11)),
        publisher=None,
        volume_count=10,
    )

    assert [(release["id"], coverage) for release, coverage in plan] == [
        ("complete-pack", tuple(range(2, 11)))
    ]


def test_single_word_title_requires_publisher_identity_before_auto_grab():
    ambiguous = rel("Kingdom v01 (Digital)", 1)
    corroborated = rel("VIZ Media Kingdom v01 (Digital)", 1)

    assert (
        pick_release([ambiguous], title="Kingdom", volume=1, publisher="VIZ Media")
        is None
    )
    assert "publisher corroboration" in ambiguous["_review"]
    assert (
        pick_release([corroborated], title="Kingdom", volume=1, publisher="VIZ Media")
        is corroborated
    )


def test_a_pack_that_runs_past_the_work_is_reviewed():
    """A 17-volume pack for a 10-volume work is a different edition."""

    release = pack("01-17")
    chosen = pick_release(
        [release],
        title="Example Work",
        volume=2,
        publisher=None,
        volume_count=10,
        wanted_volumes=set(range(2, 11)),
    )

    assert chosen is None
    assert "beyond the work's 10" in release["_review"]


def test_a_pack_of_books_already_owned_is_reviewed():
    """Grabbing it would re-download and overwrite what is on disk."""

    release = pack("01-10")
    chosen = pick_release(
        [release],
        title="Example Work",
        volume=9,
        publisher=None,
        volume_count=10,
        wanted_volumes={9, 10},
    )

    assert chosen is None
    assert release["_review"].startswith("pack of 10 volumes")


def test_a_pack_past_the_works_last_volume_is_answered_not_asked():
    """A one-volume work offered a three-volume pack is not a doubtful match:
    the extra books belong to something else, so the operator is not asked."""

    candidate = pack("01-03")
    assert (
        pick_release(
            [candidate],
            title="Example Work",
            volume=1,
            publisher=None,
            volume_count=1,
            wanted_volumes={1},
        )
        is None
    )
    assert candidate["_review_resolution"] == "rejected"
    assert "beyond the work's 1" in candidate["_review"]


def test_a_pack_offering_nothing_missing_is_answered_not_asked():
    candidate = pack("01-10")
    assert (
        pick_release(
            [candidate],
            title="Example Work",
            volume=2,
            publisher=None,
            volume_count=10,
            wanted_volumes=set(),
        )
        is None
    )
    assert candidate["_review_resolution"] == "rejected"
    assert "none of them missing" in candidate["_review"]


def test_a_pack_that_is_mostly_owned_is_still_the_operators_call():
    """Inside the work and genuinely useful in part: that trade-off is a
    judgement, so it keeps reaching the confirmation list."""

    candidate = pack("01-10")
    assert (
        pick_release(
            [candidate],
            title="Example Work",
            volume=2,
            publisher=None,
            volume_count=10,
            wanted_volumes={2, 3},
        )
        is None
    )
    assert candidate.get("_review_resolution") is None
    assert "2 of them missing" in candidate["_review"]


def test_a_release_nobody_seeds_is_never_offered_as_a_decision():
    """Accepting it could not download anything, so it is not a question."""

    candidate = pack("01-10", seeders=0)
    assert (
        pick_release(
            [candidate],
            title="Example Work",
            volume=2,
            publisher=None,
            volume_count=10,
            wanted_volumes={2, 3},
        )
        is None
    )
    assert candidate["_review_resolution"] == "rejected"
    assert "no seeders" in candidate["_review"]


def test_a_one_word_title_never_matches_a_release_that_merely_contains_the_word():
    # "And" (Mari Okazaki), live: the pack search for the work's eight
    # volumes offered five packs of other works, all with "and" in their
    # names, as questions. A release must start with the title to count.
    from tankarr.volume_hunt import _title_leads

    assert _title_leads("And", "And v01-v08 (Digital) (danke)")
    assert _title_leads("And", "[Group] And (Mari Okazaki) v01")
    assert not _title_leads(
        "And", "The Condemned Villainess Goes Back in Time and Aims v01-v08"
    )
    assert not _title_leads("Kingdom", "The Peaceable Kingdom v01")
    assert _title_leads("Kingdom", "Kingdom v01-v70 (Digital)")


def test_a_numbered_family_of_books_vouches_for_a_one_word_title():
    """Fifteen 'Mars vNN' releases for a fifteen-volume work are the work."""

    family = [
        rel(f"Mars v{index:02d} [2019] [Digital] [XRA-Empire]", index)
        for index in range(1, 16)
    ]

    chosen = pick_release(
        family, title="Mars", volume=3, publisher="Kodansha Manga", volume_count=15
    )

    assert chosen is family[2]
    assert "_review" not in family[2]

    # Two stray releases are not a family: the question stays open.
    strays = family[:2]
    assert (
        pick_release(
            strays, title="Mars", volume=1, publisher="Kodansha Manga", volume_count=15
        )
        is None
    )
    assert "publisher corroboration" in strays[0]["_review"]


def test_a_family_never_vouches_for_a_release_that_merely_contains_the_word():
    from tankarr.volume_hunt import offered_volumes

    family = [
        rel(f"Mars v{index:02d} [2019] [Digital] [XRA-Empire]", index)
        for index in range(1, 16)
    ]
    strays = [
        rel("Queen Of Mars Vol 1 [2024] [Digital] [ASO]", 1, ident="queen"),
        rel("Biker Mice From Mars v02 [2026] [Oni Press]", 2, ident="mice"),
    ]

    chosen = pick_release(
        family + strays,
        title="Mars",
        volume=1,
        publisher="Kodansha Manga",
        volume_count=15,
    )
    assert chosen is family[0]

    offers = offered_volumes(
        family + strays, title="Mars", publisher="Kodansha Manga", volume_count=15
    )
    assert {row["title"] for row in offers} == {release["title"] for release in family}


def test_the_planner_judges_the_family_on_the_whole_pool():
    family = [
        rel(f"Mars v{index:02d} [2019] [Digital] [XRA-Empire]", index)
        for index in (1, 2, 3, 4, 7, 8, 9, 11, 12, 13, 15)
    ]

    planned = plan_releases(
        family,
        title="Mars",
        missing_volumes=[11, 12, 13, 14, 15],
        publisher="Kodansha Manga",
        volume_count=15,
    )

    assert sorted(volume for _release, coverage in planned for volume in coverage) == [
        11,
        12,
        13,
        15,
    ]
    assert not any(release.get("_review") for release in family)


class BuriedTorrents(FakeTorrents):
    """An indexer that lists the wanted book only when asked by number."""

    async def search(self, manga_id, query, limit=75, **_kwargs):
        self.queries.append(query)
        if query == "Master Keaton v11":
            return {
                "results": [
                    rel(
                        "Master Keaton v11 (2016) (Digital) (VIZ Media)",
                        11,
                        ident="k11",
                    )
                ]
            }
        return {"results": []}


@pytest.mark.asyncio
async def test_a_book_the_series_queries_bury_is_found_by_number():
    torrents = BuriedTorrents()
    result = await hunt_volumes(
        torrents,
        manga={"id": "mk", "title": "Master Keaton"},
        missing_volumes=[11],
        publisher="VIZ Media",
    )
    assert torrents.grabbed == ["k11"]
    assert [item["volume"] for item in result["grabbed"]] == [11]
    assert torrents.queries[-1] == "Master Keaton v11"


def test_the_authors_name_in_the_release_corroborates_a_one_word_title():
    from tankarr.volume_hunt import _identity_review_reason

    assert (
        _identity_review_reason(
            "Dominion", "Dominion masamune shirow TPB", ["Dark Horse Manga"]
        )
        is not None
    )
    assert (
        _identity_review_reason(
            "Dominion",
            "Dominion masamune shirow TPB",
            ["Dark Horse Manga"],
            ["Masamune Shirow"],
        )
        is None
    )
    assert (
        _identity_review_reason(
            "Dominion",
            "Dominion v01 (Digital)",
            ["Dark Horse Manga"],
            ["Masamune Shirow"],
        )
        is not None
    )
