from tankarr.release_kind import classify_release


def test_page_count_decides_books_over_chapter_labels():
    grass = classify_release(title="Ch. 1 - volume 1", pages=454)
    assert (
        grass.kind == "volume" and grass.volume == "1" and "454 pages" in grass.evidence
    )
    zero = classify_release(title="Ch. 0", pages=451)
    assert (
        zero.kind == "volume" and zero.volume is None
    )  # a book without a usable number
    chapter = classify_release(title="Ch. 12", pages=19)
    assert chapter.kind == "chapter" and chapter.chapter == "12"


def test_indexer_names_are_books_packs_or_rejected():
    beck = classify_release(
        title="BECK v03 (2019) (Kodansha Comics USA) (Digital) (1r0n)",
        size_bytes=295_000_000,
    )
    assert beck.kind == "volume" and beck.volume == "3"
    kamui = classify_release(
        title="The Legend of Kamui v01-02 (2025) (c2c) (Trite)",
        size_bytes=1_933_000_000,
    )
    assert kamui.kind == "pack" and kamui.volumes == (1, 2)
    french = classify_release(
        title="Kamui-Den.T04.2012.FRENCH.HYBRiD.COMiC.CBZ.eBook-TONER",
        size_bytes=3_300_000_000,
    )
    assert french.kind == "rejected"
    grass = classify_release(
        title="Grass (2019) (Drawn&Quarterly) (Digital) (morrol4n)",
        size_bytes=186_000_000,
    )
    assert grass.kind == "volume" and grass.volume is None
    chapter = classify_release(
        title="One Piece c1191 (2026) (Digital)", size_bytes=12_000_000
    )
    assert chapter.kind == "chapter" and chapter.chapter == "1191"


def test_title_must_be_a_contiguous_phrase_followed_by_a_marker():
    from tankarr.release_kind import title_matches_release

    assert title_matches_release(
        "Kingdom", "VIZ.Media-Kingdom.Vol.10.2026.HYBRID.MANGA"
    )
    assert title_matches_release("Akira", "Akira v03 (2000) (Digital)")
    assert not title_matches_release("Akira", "Momose Akira no Hatsukoi Hatan-chuu")
    assert not title_matches_release("Monster", "Monster War v01 [2006] [Digital]")
    assert title_matches_release("Uzumaki", "Uzumaki ( Complete) ( VIZ )")
    assert not title_matches_release(
        "20th Century Boys", "20th & 21st Century Boys Complete v2"
    )
    assert title_matches_release("Grass", "Grass (2019) (Drawn&Quarterly) (Digital)")


def test_dotted_usenet_names_yield_the_volume_not_a_decimal():
    for name, volume in (
        ("VIZ.Media-Kingdom.Vol.10.2026.HYBRID.MANGA", "10"),
        ("Dark.Horse-Berserk.Vol.41.2022.Hybrid.Comic.eBook-BitBook", "41"),
        ("Yen.Press-Solo.Leveling.Vol.14.Comic.Side.Stories.1.2025.Ret", "14"),
    ):
        kind = classify_release(title=name, size_bytes=300_000_000)
        assert (kind.kind, kind.volume) == ("volume", volume), name


def test_a_japanese_volume_marker_is_a_raw_pack_whatever_the_bracketed_english_says():
    from tankarr.release_kind import classify_release

    raw = classify_release(
        title="ヨコハマ買い出し紀行 第01-14巻 [Yokohama Kaidashi Kikou vol 01-14]"
    )
    assert raw.kind == "rejected"
    assert classify_release(title="Yotsuba&! 全15巻 (Digital)").kind == "rejected"
    assert classify_release(title="Some Work v03 [raw]").kind == "rejected"
    english = classify_release(title="Yokohama Kaidashi Kikou Volume 07 [Yūgen-ykk]")
    assert english.kind != "rejected"


def test_an_omnibus_or_deluxe_edition_is_another_edition_not_a_book_to_grab():
    from tankarr.release_kind import classify_release

    for name in (
        "Yokohama.Kaidashi.Kikou-Deluxe.Edition.v05.2023.Digital",
        "[0v3r] Yokohama Kaidashi Kikou - Deluxe Edition v01-04",
        "Berserk Deluxe Edition v01 (2019) (Digital)",
        "Vagabond VIZBIG 3-in-1 v01",
        "Fullmetal Alchemist Omnibus v03",
    ):
        assert classify_release(title=name).kind == "rejected", name
    assert (
        classify_release(title="Yokohama Kaidashi Kikou Volume 07 [Yūgen-ykk]").kind
        != "rejected"
    )


def test_the_creators_name_may_follow_the_title_and_the_leading_the_is_optional():
    from tankarr.release_kind import title_matches_release

    assert title_matches_release("Black Magic", "[0v3r] Black Magic by Masamune Shirow")
    assert title_matches_release(
        "The Ghost in the Shell",
        "GHOST IN THE SHELL masamune shirow [TPB] [Eng] zip",
        ["Masamune Shirow"],
    )
    assert not title_matches_release(
        "The Ghost in the Shell", "GHOST IN THE SHELL masamune shirow [TPB]"
    )
    assert not title_matches_release("Monster", "Monster War v01", ["Naoki Urasawa"])
