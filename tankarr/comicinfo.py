from __future__ import annotations

from xml.etree import ElementTree as ET

from tankarr.assembly_provenance import assembly_provenance


def build_comic_info(manga: dict, chapter: dict, page_count: int) -> bytes:
    root = ET.Element("ComicInfo")
    series_metadata = manga.get("metadata") or {}
    volume_metadata = chapter.get("metadata") or {}

    def add(name: str, value: object | None) -> None:
        if value is None or value == "":
            return
        ET.SubElement(root, name).text = str(value)

    chapter_number = chapter.get("chapter")
    volume_number = chapter.get("volume")
    is_chapter = chapter_number not in (None, "")
    is_comic = str(series_metadata.get("content_kind") or "").casefold() == "comic"
    book_metadata = volume_metadata if not is_chapter or is_comic else {}
    if is_chapter:
        book_title = (
            f"Issue {chapter_number}" if is_comic else f"Chapter {chapter_number}"
        )
    elif volume_number not in (None, ""):
        book_title = f"Volume {volume_number}"
    else:
        book_title = "Special"
    add(
        "Title",
        book_metadata.get("title") or book_title,
    )
    add("Series", manga["title"])
    add("Number", chapter_number if chapter_number not in (None, "") else volume_number)
    add("Volume", volume_number)
    add(
        "Count",
        series_metadata.get("chapter_count")
        if is_chapter
        else series_metadata.get("book_count") or series_metadata.get("volume_count"),
    )
    add(
        "Summary",
        book_metadata.get("description")
        or series_metadata.get("description")
        or manga.get("description"),
    )
    creators = book_metadata.get("creators") or series_metadata.get("creators") or []
    writers = [
        item.get("name")
        for item in creators
        if item.get("name")
        and str(item.get("role") or "").casefold() in {"writer", "author", "story"}
    ]
    artists = [
        item.get("name")
        for item in creators
        if item.get("name")
        and str(item.get("role") or "").casefold() in {"artist", "art", "penciller"}
    ]
    add(
        "Writer",
        ", ".join(
            writers or series_metadata.get("authors") or manga.get("authors", [])
        ),
    )
    add("Penciller", ", ".join(artists))
    add(
        "Publisher",
        book_metadata.get("publisher") or series_metadata.get("publisher"),
    )
    add("Genre", ", ".join(series_metadata.get("genres") or []))
    add("Tags", ", ".join(series_metadata.get("tags") or []))
    release_date = str(book_metadata.get("release_date") or "")
    add(
        "Year",
        release_date[:4] if len(release_date) >= 4 else series_metadata.get("year"),
    )
    add("LanguageISO", chapter["language"])
    links = book_metadata.get("links") or series_metadata.get("links") or []
    add(
        "Web",
        chapter.get("source_url")
        or next((item.get("url") for item in links if item.get("url")), None),
    )
    add("GTIN", book_metadata.get("isbn"))
    reading_direction = series_metadata.get("reading_direction")
    if reading_direction == "RIGHT_TO_LEFT":
        add("Manga", "YesAndRightToLeft")
    elif str(series_metadata.get("work_type") or "").casefold() == "manga":
        add("Manga", "Yes")
    add("PageCount", page_count)
    provenance = assembly_provenance(chapter.get("assembled_from"))
    add("Notes", chapter.get("notes") or (provenance or {}).get("notes"))
    add(
        "ScanInformation",
        "Downloaded via Tankarr from "
        f"{chapter.get('source_provider') or chapter['provider']}",
    )
    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
