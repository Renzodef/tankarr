from pathlib import Path

from tankarr.naming import (
    chapter_filename,
    final_library_path,
    safe_component,
    series_directory_name,
)


def test_safe_component_removes_path_and_windows_characters():
    assert safe_component(' One/Two: "Three"? ') == "One Two Three"
    assert safe_component("CON") == "_CON"


def test_library_path_keeps_language_on_release():
    manga = {"title": "My/Manga", "authors": ["First Author", "Second Author"]}
    chapter = {
        "id": "chapter-id",
        "chapter": "12.5",
        "volume": "2",
        "title": "A: title",
        "language": "en",
    }
    assert final_library_path(Path("/library"), manga, chapter) == Path(
        "/library/My Manga (First Author, Second Author)/"
        "My Manga - v002 c012.5 [en].cbz"
    )
    assert chapter_filename(manga, chapter).endswith("[en].cbz")
    assert "A title" not in chapter_filename(manga, chapter)


def test_naming_always_includes_explicit_missing_metadata():
    manga = {"title": "", "authors": []}
    chapter = {
        "id": "release-123",
        "chapter": None,
        "volume": None,
        "language": "",
    }

    assert series_directory_name(manga) == "Unknown Series (Unknown Author)"
    assert chapter_filename(manga, chapter) == (
        "Unknown Series - cUnknown-release-123 [und].cbz"
    )


def test_numeric_tokens_are_padded_for_lexical_sorting():
    manga = {"title": "Series", "authors": ["Author"]}

    names = [
        chapter_filename(
            manga,
            {
                "id": f"chapter-{number}",
                "chapter": number,
                "volume": "14",
                "language": "en",
            },
        )
        for number in ("1", "9", "10", "53.5", "100")
    ]

    assert names == sorted(names)
    assert names[0].endswith("v014 c001 [en].cbz")
    assert names[3].endswith("v014 c053.5 [en].cbz")


def test_filename_stays_under_common_filesystem_component_limit():
    manga = {"title": "漫" * 200, "authors": ["作" * 200]}
    chapter = {
        "id": "chapter-" + "a" * 80,
        "chapter": None,
        "volume": None,
        "language": "zh-hant",
    }

    filename = chapter_filename(manga, chapter)

    assert len(filename.encode("utf-8")) <= 255
    assert "cUnknown-" in filename
    assert "cUnknown-" in filename
    assert filename.endswith("[zh-hant].cbz")


def test_local_volume_archives_get_clean_volume_only_names():
    manga = {"title": "Basara", "authors": ["Yumi Tamura"]}
    local_volume = {
        "id": "local-basara-0027",
        "chapter": None,
        "volume": "27",
        "language": "en",
        "provider": "local",
    }
    assert chapter_filename(manga, local_volume) == "Basara - v027 [en].cbz"

    provider_release = {**local_volume, "provider": "mangadex"}
    assert "cUnknown-local-basara-0027" in chapter_filename(manga, provider_release)


def test_nyaa_volume_packs_get_padded_range_without_unknown_chapter():
    manga = {"title": "Kamui", "authors": ["Sanpei Shirato"]}
    volume_pack = {
        "id": "nyaa-pack",
        "chapter": None,
        "volume": "1-2",
        "language": "en",
        "provider": "nyaa",
    }

    assert chapter_filename(manga, volume_pack) == "Kamui - v001-002 [en].cbz"


def test_numbered_prologue_has_a_distinct_path_before_ordinary_chapters():
    manga = {"title": "Example", "authors": []}
    normal = {"id": "regular", "chapter": "2", "language": "en", "title": "Chapter 2"}
    prologue = {**normal, "id": "prelude", "title": "Prologue 2", "chapter": None}
    assert chapter_filename(manga, prologue) == "Example - c000-prologue-002 [en].cbz"
    assert chapter_filename(manga, prologue) < chapter_filename(manga, normal)
