"""The form of a series: a list of books, or a list of chapters, never both.

A finished (or paused) work with a known number of books is read as books:
every chapter belongs to one book, the page lists the books and says how
each one gets completed. A running work, a webtoon, or a work whose book
count nobody knows is read as chapters.

Book membership comes from source evidence. Unknown boundaries stay unknown;
chapter and volume totals alone cannot establish which book contains a chapter.
Statistical layouts must never change file metadata or acquisition coverage.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

from tankarr.series_summary import PAUSED_STATUSES

# The two sides of a pause are deliberately different, and the difference is
# the point. For acquisition a paused work is still continuing (hiatus sits in
# series_summary's CONTINUING_STATUSES): chapters may resume, so the frontier
# and the counts keep moving. The shelf reads it the other way round: the
# author has stopped, so the books that exist are the edition worth showing,
# and that is why hiatus is absent here. Only the spelling of "paused" is
# shared, and it now comes from one place: the two sets had drifted, and this
# one was missing "on_hiatus", which the official sources can return - such a
# series would have been announced as "finished" while its author is on a
# break.
RUNNING_STATUSES = {"ongoing", "releasing", "publishing", "continuing"}
WEBTOON_URL = re.compile(
    r"(?i)(?:webtoons\.com|comic\.naver\.com/webtoon|tapas\.io/series/|"
    r"manta\.net/(?:en/)?series/|tappytoon\.com|toomics\.com|dongmanmanhua)"
)

PLAN_BOOK = "book"
PLAN_ASSEMBLE = "assemble"
PLAN_PARTIAL = "partial"
PLAN_MISSING = "missing"


def _status(manga: dict[str, Any]) -> str:
    override = str(manga.get("status_override") or "").casefold()
    if override and override != "automatic":
        return override
    return str(manga.get("status") or "").casefold()


def _is_webtoon(metadata: dict[str, Any] | None) -> bool:
    """Whether this work is a webtoon, by whatever the catalogue says.

    The catalogue states it outright in ``content_kind`` and in
    ``classification.kind``; the reading direction only says so on the sources
    that bother to set it, and neither Tower of God nor Omniscient Reader do -
    both arrive as LEFT_TO_RIGHT, so every webtoon rule was silently off.
    """

    if not metadata:
        return False
    classification = metadata.get("classification")
    classification = classification if isinstance(classification, dict) else {}
    subtype = str(classification.get("subtype") or "").casefold()
    stated = [subtype, metadata.get("type"), metadata.get("format")]
    if any("webtoon" in str(value or "").casefold() for value in stated):
        return True
    if str(metadata.get("reading_direction") or "").upper() == "WEBTOON":
        return True
    if any(
        WEBTOON_URL.search(str(link.get("url") or ""))
        for link in metadata.get("official_links") or []
        if isinstance(link, dict)
    ):
        return True
    # Manga catalogues use Manhwa/Manhua/OEL for both printed works and
    # scrolling comics. That family alone is not evidence of page shape.
    broad_subtypes = {
        "manhwa",
        "manhua",
        "oel",
        "original english language",
        "original_english_language",
    }
    classified_kind = str(classification.get("kind") or "").casefold()
    content_kind = str(metadata.get("content_kind") or "").casefold()
    return subtype not in broad_subtypes and (
        classified_kind == "webtoon" or content_kind == "webtoon"
    )


def series_form(
    manga: dict[str, Any],
    metadata: dict[str, Any] | None,
    expected_books: int | None,
) -> tuple[str, str]:
    """Return ``("volumes" | "chapters", reason)``."""

    status = _status(manga)
    # No shelf is pinned to a unit by hand. The form is read from the work and
    # from what the reader owns, every time: a hand-set unit was how Adekan
    # came to be a list of books while it was still running, and how the same
    # pin then had to be undone to bound its numbering. What suits a shelf is
    # decided here, from evidence, not stored as a preference beside it.
    if status in RUNNING_STATUSES:
        return "chapters", "running series: chapters until it ends"
    if _is_webtoon(metadata):
        return "chapters", "webtoon: no certain division into books"
    if not expected_books:
        if status in PAUSED_STATUSES:
            return "chapters", "paused series without a known book count"
        return "chapters", "no known book count"
    if status in PAUSED_STATUSES:
        return "volumes", f"paused series with {expected_books} known books"
    return "volumes", f"finished series with {expected_books} books"


def _number(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except Exception:  # noqa: BLE001 - labels are free text
        return None


def _whole(label: object) -> int | None:
    number = _number(label)
    if number is None or number != number.to_integral_value():
        return None
    return int(number)


def _expected_counts(book: dict[str, Any]) -> tuple[int, int]:
    """Chapters the work expects in this book, and how many are on disk.

    A decimal extra a source lists inside the book ("74.22") is not one of
    its chapters: a book whose every numbered chapter is on disk is complete
    from chapters, extras or not (Iryū 10 and 18, Touch 23).
    """

    slots = book.get("chapters") or []
    if not slots:
        count = int(book.get("chapter_count") or 0)
        return count, min(count, int(book.get("downloaded_chapter_count") or 0))
    expected = [
        slot for slot in slots if slot.get("expected", True) and not slot.get("special")
    ]
    if not expected:
        expected = slots
    return len(expected), sum(1 for slot in expected if slot.get("downloaded"))


def plan_state(book: dict[str, Any]) -> str:
    if book.get("owned"):
        return PLAN_BOOK
    count, downloaded = _expected_counts(book)
    if count and downloaded >= count:
        return PLAN_ASSEMBLE
    if downloaded:
        return PLAN_PARTIAL
    return PLAN_MISSING


def uniform_kind(books: list[dict[str, Any]]) -> str:
    """``books`` when every book is on disk, ``chapters`` when every book's
    chapters are complete, otherwise ``mixed``."""

    expected = [book for book in books if book.get("expected")]
    if not expected:
        return "mixed"
    if all(book.get("owned") for book in expected):
        return "books"
    if all(
        (counts := _expected_counts(book))[0] and counts[1] >= counts[0]
        for book in expected
    ):
        return "chapters"
    return "mixed"


def plan_text(book: dict[str, Any], preference: str, kind: str) -> str:
    state = book["plan_state"]
    rng = book.get("chapter_range") or {}
    span = f"{rng.get('first')}–{rng.get('last')}" if rng else "?"
    downloaded = int(book.get("downloaded_chapter_count") or 0)
    count = int(book.get("chapter_count") or 0)
    if state == PLAN_BOOK:
        if kind == "chapters":
            return "Book on disk; the series completes from chapters, this book is redundant"
        pages = book.get("pages")
        return f"{pages} pages" if pages else "On disk"
    if state == PLAN_ASSEMBLE:
        if kind == "chapters":
            return f"Chapters {span} complete: the whole series comes from chapters"
        return f"Chapters {span} complete"
    if state == PLAN_PARTIAL:
        if preference == "chapters":
            return f"{downloaded} of {count} chapters · search chapters, the book only if they fall short"
        return f"{downloaded} of {count} chapters · search the book, then the missing chapters"
    if preference == "chapters":
        return "Search chapters, then the book"
    return "Search the book, then chapters"


def map_confidence(
    books: list[dict[str, Any]],
    hint_sources: list[str],
    exact_sources: list[str],
    *,
    unassigned_chapters: bool = False,
) -> dict[str, Any]:
    # An owned file outside the managed edition is kept as an extra row so it
    # can be inspected or retired.  It says nothing about the quality of the
    # expected edition's chapter map and must not turn an otherwise exact map
    # into a warning (for example an 18-book edition with an old file labelled
    # volume 21).
    expected = [book for book in books if book.get("expected", True)]
    estimated = sum(1 for book in expected if book.get("estimated"))
    if not expected:
        level = "none"
    elif not unassigned_chapters and all(
        book.get("exact") and book.get("chapter_range") and not book.get("estimated")
        for book in expected
    ):
        level = "exact"
    elif not any(book.get("chapter_range") for book in expected):
        level = "none"
    elif estimated == len(expected):
        level = "estimated"
    else:
        level = "partial"
    return {
        "level": level,
        "estimated_books": estimated,
        "unknown_books": sum(not book.get("chapter_range") for book in expected),
        "sources": sorted(set(exact_sources)),
        "hint_sources": sorted(set(hint_sources)),
    }


def _book_span(item: dict[str, Any]) -> tuple[Decimal, Decimal] | None:
    rng = item.get("chapter_range") or {}
    first, last = _number(rng.get("first")), _number(rng.get("last"))
    if first is None or last is None:
        numbers = [_number(s.get("chapter")) for s in item.get("chapters") or []]
        numbers = [n for n in numbers if n is not None]
        if not numbers:
            return None
        first, last = min(numbers), max(numbers)
    return first, last


def reset_estimated_layout(
    books: list[dict[str, Any]], unassigned: list[dict[str, Any]]
) -> None:
    """Recompute statistical ranges from current releases, never old estimates.

    Explicit boundaries stay fixed. Estimated membership is presentation
    state, so it must not become an anchor when the payload is refreshed.
    """
    seen = {id(slot) for slot in unassigned}
    for book in books:
        sources = set(book.get("map_sources") or ())
        if "estimate" not in sources or sources & {"operator", "ocr"}:
            continue
        for slot in book.get("chapters") or []:
            if id(slot) not in seen:
                unassigned.append(slot)
                seen.add(id(slot))
            if slot.get("volume_inferred"):
                slot["volume"] = None
                slot["volume_inferred"] = False
                slot["covered_by_volume"] = None
                slot["missing"] = bool(
                    slot.get("expected", True)
                    and not slot.get("downloaded")
                    and not slot.get("ignored")
                    and not slot.get("covered_unmapped")
                )
        book.update(
            chapters=[],
            chapter_range=None,
            chapter_count=0,
            downloaded_chapter_count=0,
            missing_chapter_count=0,
            exact=False,
            estimated=False,
            covered_by_chapters=False,
            can_assemble=False,
            map_sources=sorted(sources - {"estimate"}),
            status="owned" if book.get("owned") else "missing",
        )


def estimate_ranges_from_count(
    books: list[dict[str, Any]], expected_chapters: object
) -> int:
    """Give the books still without a range an equal share of the counted
    chapters.

    A series owned as books only, with no chapter file and no source that
    describes a book, has nothing but the catalogue's count to lay out. The
    numbers 1..N not already inside a described book are divided in equal
    parts between known neighbours, exactly as loose chapters are. This is a
    range on the shelf and nothing more: no slot is invented, so nothing can
    be searched, covered or assembled from it.
    """
    total = _whole(expected_chapters)
    if not total or total <= 0:
        return 0

    def ordinal(item: dict[str, Any]) -> Decimal | None:
        return _number(item.get("volume"))

    def undescribed(item: dict[str, Any]) -> bool:
        return not item.get("chapters") and not (item.get("chapter_range") or {})

    numbered = sorted(
        (item for item in books if ordinal(item) is not None), key=ordinal
    )
    # A total no larger than the number of books is a placeholder, not a
    # chapter count - unless there is a single book, where "1 chapter" is
    # simply what a one-shot holds.
    if (len(numbered) > 1 and total <= len(numbered)) or not any(
        undescribed(item) for item in numbered
    ):
        return 0
    taken: set[int] = set()
    for item in numbered:
        span = _book_span(item)
        if span:
            taken.update(
                number for number in range(1, total + 1) if span[0] <= number <= span[1]
            )
    free = [number for number in range(1, total + 1) if number not in taken]
    if not free:
        return 0
    gaps: list[tuple[list[dict[str, Any]], Decimal | None, Decimal | None]] = []
    if any(_book_span(item) for item in numbered):
        index = 0
        while index < len(numbered):
            if not undescribed(numbered[index]):
                index += 1
                continue
            start = index
            while index < len(numbered) and undescribed(numbered[index]):
                index += 1
            lower = next(
                (
                    _book_span(item)[1]
                    for item in reversed(numbered[:start])
                    if _book_span(item)
                ),
                None,
            )
            upper = next(
                (_book_span(item)[0] for item in numbered[index:] if _book_span(item)),
                None,
            )
            gaps.append((numbered[start:index], lower, upper))
    else:
        gaps.append(([item for item in numbered if undescribed(item)], None, None))
    used: set[int] = set()
    filled = 0
    for gap, lower, upper in gaps:
        pool = [
            number
            for number in free
            if number not in used
            and (lower is None or number > lower)
            and (upper is None or number < upper)
        ]
        if not pool:
            continue
        share, extra = divmod(len(pool), len(gap))
        cursor = 0
        for position, item in enumerate(gap):
            take = share + (1 if position < extra else 0)
            labels = pool[cursor : cursor + take]
            cursor += take
            if not labels:
                continue
            used.update(labels)
            item.update(
                chapter_range={"first": str(labels[0]), "last": str(labels[-1])},
                chapter_count=len(labels),
                exact=False,
                estimated=True,
                covered_by_chapters=False,
                can_assemble=False,
                map_sources=sorted(set(item.get("map_sources") or ()) | {"estimate"}),
            )
            item.setdefault("downloaded_chapter_count", 0)
            item.setdefault("missing_chapter_count", 0)
            if not item.get("owned"):
                item["status"] = "missing"
            filled += 1
    return filled


def spread_unmapped_chapters(
    books: list[dict[str, Any]], unassigned: list[dict[str, Any]]
) -> int:
    """Lay the chapters no source placed into the books no source describes.

    A shelf is easier to read when every book shows what it holds, and an
    official map is often unavailable for old or long series. Between two
    described books the loose chapters are divided in equal parts among the
    empty books that separate them; with no described book at all the
    whole run is divided equally. This is a layout, not evidence: the book is
    marked ``estimated``, stays ``exact=False``, never covers, assembles or
    retires anything, and ``chapter_volume_assignments`` skips it, so nothing
    is written back. A special stays outside; a chapter numbered inside a
    described book's range but absent from its map stays unassigned.
    """

    def ordinal(item: dict[str, Any]) -> Decimal | None:
        return _number(item.get("volume"))

    def is_empty(item: dict[str, Any]) -> bool:
        # A book on the shelf with no known contents is as undescribed as a
        # missing one: it takes its share too, and the owned-book pass then
        # says those chapters are in it rather than absent.
        return not item.get("chapters") and not (item.get("chapter_range") or {})

    span = _book_span

    numbered = sorted(
        (item for item in books if ordinal(item) is not None), key=ordinal
    )
    candidates = sorted(
        (
            slot
            for slot in unassigned
            if slot.get("expected")
            and not slot.get("special")
            and _number(slot.get("chapter")) is not None
        ),
        key=lambda slot: _number(slot.get("chapter")),
    )
    if not candidates or not any(is_empty(item) for item in numbered):
        return 0
    anchored = any(span(item) for item in numbered)
    gaps: list[tuple[list[dict[str, Any]], Decimal | None, Decimal | None]] = []
    if anchored:
        index = 0
        while index < len(numbered):
            if not is_empty(numbered[index]):
                index += 1
                continue
            start = index
            while index < len(numbered) and is_empty(numbered[index]):
                index += 1
            lower = next(
                (span(item)[1] for item in reversed(numbered[:start]) if span(item)),
                None,
            )
            upper = next(
                (span(item)[0] for item in numbered[index:] if span(item)), None
            )
            gaps.append((numbered[start:index], lower, upper))
    else:
        gaps.append(([item for item in numbered if is_empty(item)], None, None))
    used: set[int] = set()
    filled = 0
    for gap, lower, upper in gaps:
        pool = [
            slot
            for slot in candidates
            if id(slot) not in used
            and (lower is None or _number(slot["chapter"]) > lower)
            and (upper is None or _number(slot["chapter"]) < upper)
        ]
        if not pool:
            continue
        share, extra = divmod(len(pool), len(gap))
        cursor = 0
        for position, item in enumerate(gap):
            take = share + (1 if position < extra else 0)
            slots = pool[cursor : cursor + take]
            cursor += take
            if not slots:
                continue
            for slot in slots:
                used.add(id(slot))
                slot["volume"] = item.get("volume")
                slot["volume_inferred"] = True
            downloaded = sum(1 for slot in slots if slot.get("downloaded"))
            item.update(
                chapters=slots,
                chapter_range={
                    "first": str(slots[0]["chapter"]),
                    "last": str(slots[-1]["chapter"]),
                },
                chapter_count=len(slots),
                downloaded_chapter_count=downloaded,
                missing_chapter_count=sum(1 for slot in slots if slot.get("missing")),
                exact=False,
                estimated=True,
                covered_by_chapters=False,
                can_assemble=False,
                map_sources=sorted(set(item.get("map_sources") or ()) | {"estimate"}),
                status=(
                    "owned"
                    if item.get("owned")
                    else "covered_by_chapters"
                    if downloaded == len(slots)
                    else "missing"
                ),
            )
            filled += 1
    if used:
        unassigned[:] = [slot for slot in unassigned if id(slot) not in used]
    return filled


def _recount(book: dict[str, Any]) -> None:
    """Counts and range of a book after its chapter rows changed."""

    slots = sorted(
        book.get("chapters") or [],
        key=lambda slot: _number(slot.get("chapter")) or Decimal(0),
    )
    book["chapters"] = slots
    book["chapter_count"] = len(slots)
    book["downloaded_chapter_count"] = sum(1 for s in slots if s.get("downloaded"))
    book["missing_chapter_count"] = sum(1 for s in slots if s.get("missing"))
    book["chapter_range"] = (
        {"first": str(slots[0]["chapter"]), "last": str(slots[-1]["chapter"])}
        if slots
        else None
    )


def _slot_tag(slot: dict[str, Any]) -> int | None:
    """The book the slot's releases file it under, when they agree."""

    from tankarr.source_numbering import title_volume

    tags = [
        tag
        for release in slot.get("releases") or []
        if (tag := title_volume(release.get("title"))) is not None
    ]
    if not tags:
        return None
    best = max(set(tags), key=tags.count)
    return best if tags.count(best) * 2 > len(tags) else None


MIN_TAGGED = 5
TAG_AGREEMENT = 0.8


def relocate_by_source_tags(
    books: list[dict[str, Any]], unassigned: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Let the sources' own volume tags fill a book the map left phantom.

    A map written in a numbering the releases do not use leaves a book
    holding only chapters no source ever released, while the releases
    tagged for that book sit in the book next door (Nana: the map said
    vol 1 = 0.1–0.3, every source says "Vol.1 Chapter 1–2"). When the tags
    agree with the map everywhere else, they are the better witness for
    that one book: its rows move there, as a layout only, and the shelf
    says so. A source whose numbering disagrees with the map throughout is
    not listened to: nothing moves, the map stands.
    """

    tagged: dict[int, list[tuple[dict[str, Any], dict[str, Any] | None]]] = {}
    agree = total = 0
    for book in books:
        number = _whole(book.get("volume"))
        for slot in book.get("chapters") or []:
            tag = _slot_tag(slot)
            if tag is None:
                continue
            tagged.setdefault(tag, []).append((slot, book))
            if number is not None:
                total += 1
                agree += tag == number
    for slot in unassigned:
        tag = _slot_tag(slot)
        if tag is not None:
            tagged.setdefault(tag, []).append((slot, None))
    if total < MIN_TAGGED or agree < TAG_AGREEMENT * total:
        return []
    moved_hints: list[dict[str, Any]] = []
    for book in books:
        number = _whole(book.get("volume"))
        slots = book.get("chapters") or []
        if (
            number is None
            or not book.get("expected", True)
            or not slots
            or any(s.get("releases") or s.get("downloaded") for s in slots)
            or not tagged.get(number)
        ):
            continue
        movers = [(slot, donor) for slot, donor in tagged[number] if donor is not book]
        if not movers:
            continue
        for slot, donor in movers:
            if donor is None:
                unassigned.remove(slot)
            else:
                donor["chapters"] = [s for s in donor["chapters"] if s is not slot]
                _recount(donor)
                donor.update(
                    exact=False,
                    estimated=True,
                    covered_by_chapters=False,
                    can_assemble=False,
                    map_sources=sorted(
                        set(donor.get("map_sources") or ()) | {"source_tags"}
                    ),
                )
            slot["volume"] = book.get("volume")
            slot["volume_inferred"] = True
        # The phantom rows described nothing anyone released: they go.
        book["chapters"] = [slot for slot, _donor in movers]
        _recount(book)
        downloaded = book["downloaded_chapter_count"]
        book.update(
            exact=False,
            estimated=True,
            covered_by_chapters=False,
            can_assemble=False,
            map_sources=sorted(set(book.get("map_sources") or ()) | {"source_tags"}),
        )
        if not book.get("owned"):
            book["status"] = (
                "covered_by_chapters"
                if downloaded == len(book["chapters"])
                else "missing"
            )
        moved_hints.append(
            {
                "volumes": [str(book.get("volume"))],
                "chapters": [str(slot["chapter"]) for slot in book["chapters"]],
                "source": "source_tags",
            }
        )
    return moved_hints


def apply_content_verdicts(
    books: list[dict[str, Any]],
    unassigned: list[dict[str, Any]],
    content: dict[str, dict[str, Any]],
) -> list[str]:
    """Where the pages were read, they place the chapter file, not the map.

    A chapter file whose pages were found inside a book is shown in that
    book; one whose pages no book holds is shown loose even if the map
    files it under a book on the shelf. Layout only: the verdicts are what
    the retirement rules read, this just keeps the page honest about them.
    """

    if not content:
        return []
    by_volume = {str(book.get("volume")): book for book in books if book.get("volume")}
    notes: list[str] = []

    def verdict_of(slot: dict[str, Any]) -> dict[str, Any] | None:
        for file in slot.get("files") or []:
            row = content.get(str(file.get("id")))
            if row and row.get("verdict") in {"inside", "outside", "ambiguous"}:
                return row
        return None

    moved_out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    moved_in: list[tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]] = []
    for book in books:
        for slot in list(book.get("chapters") or []):
            row = verdict_of(slot)
            if row is None:
                continue
            slot["content_verdict"] = row["verdict"]
            target = by_volume.get(str(row.get("volume") or ""))
            if row["verdict"] == "inside" and target is not None and target is not book:
                moved_in.append((slot, book, target))
            elif row["verdict"] in {"outside", "ambiguous"} and book.get("owned"):
                moved_out.append((slot, book))
    for slot in list(unassigned):
        row = verdict_of(slot)
        if row is None:
            continue
        slot["content_verdict"] = row["verdict"]
        target = by_volume.get(str(row.get("volume") or ""))
        if row["verdict"] == "inside" and target is not None:
            moved_in.append((slot, None, target))
    touched: set[int] = set()
    for slot, book in moved_out:
        book["chapters"] = [s for s in book["chapters"] if s is not slot]
        slot["volume"] = None
        slot["volume_inferred"] = True
        unassigned.append(slot)
        touched.add(id(book))
        notes.append(
            f"Chapter {slot.get('chapter')}: its pages are not in book "
            f"{book.get('volume')}; the file is kept."
        )
    for slot, donor, target in moved_in:
        if donor is not None:
            donor["chapters"] = [s for s in donor["chapters"] if s is not slot]
            touched.add(id(donor))
        elif slot in unassigned:
            unassigned.remove(slot)
        slot["volume"] = target.get("volume")
        slot["volume_inferred"] = True
        target["chapters"] = [*(target.get("chapters") or []), slot]
        touched.add(id(target))
        notes.append(
            f"Chapter {slot.get('chapter')}: its pages were found in book "
            f"{target.get('volume')}."
        )
    for book in books:
        if id(book) in touched:
            _recount(book)
            book["map_sources"] = sorted(
                set(book.get("map_sources") or ()) | {"content"}
            )
    return notes


def drop_split_parents(
    books: list[dict[str, Any]], unassigned: list[dict[str, Any]]
) -> int:
    """A whole chapter whose parts the shelf already files is not missing.

    Sources cut the same chapter differently: one numbers the story 2, the
    other 2.1 and 2.2, and the map, written from the second, files the
    parts in their books. The whole "2" then hangs loose as a chapter to
    hunt, although every page of it is on the shelf (Violence Jack: 17 such
    ghosts). A loose whole number with no file, whose parts sit in books, is
    the same chapter under another count and leaves the list.
    """

    parts_in_books: set[int] = set()
    for book in books:
        for slot in book.get("chapters") or []:
            number = _number(slot.get("chapter"))
            if number is not None and number != number.to_integral_value():
                parts_in_books.add(int(number))
    if not parts_in_books:
        return 0
    kept: list[dict[str, Any]] = []
    dropped = 0
    for slot in unassigned:
        number = _number(slot.get("chapter"))
        if (
            number is not None
            and number == number.to_integral_value()
            and int(number) in parts_in_books
            and not slot.get("downloaded")
            and not (slot.get("files") or [])
        ):
            dropped += 1
            continue
        kept.append(slot)
    unassigned[:] = kept
    return dropped


def absorb_loose_on_complete_shelf(
    books: list[dict[str, Any]], unassigned: list[dict[str, Any]], manga: dict[str, Any]
) -> int:
    """On a finished shelf with every book on it, a chapter is in a book.

    BECK's 34 books are all on the shelf and the work is over; the map ends
    at 102 and chapter 103 hangs loose, "missing", and would be fetched only
    to be retired again as a duplicate of the last book. A finished work
    prints every chapter in some book, so the loose chapter is laid into the
    book its number falls in (the last book for a tail), as an estimate:
    not missing, not hunted, never written to the map. A paused work is left
    alone: its tail may truly be uncollected (Nana, 81–84).
    """

    status = _status(manga)
    if status in RUNNING_STATUSES or status in PAUSED_STATUSES or not status:
        return 0
    expected = [book for book in books if book.get("expected", True)]
    if not expected or not all(book.get("owned") for book in expected):
        return 0
    numbered = sorted(
        (book for book in expected if _whole(book.get("volume")) is not None),
        key=lambda book: _whole(book.get("volume")),
    )
    if not numbered:
        return 0
    absorbed = 0
    kept: list[dict[str, Any]] = []
    for slot in unassigned:
        number = _number(slot.get("chapter"))
        if (
            number is None
            or slot.get("downloaded")
            or slot.get("special")
            or not slot.get("expected", True)
            or slot.get("ignored")
        ):
            kept.append(slot)
            continue
        target = None
        for book in numbered:
            span = _book_span(book)
            if span and span[0] <= number <= span[1]:
                target = book
                break
        if target is None:
            before = [
                book
                for book in numbered
                if (span := _book_span(book)) and span[0] <= number
            ]
            target = before[-1] if before else numbered[0]
        slot["volume"] = target.get("volume")
        slot["volume_inferred"] = True
        slot["missing"] = False
        slot["covered_by_volume"] = target.get("volume")
        target["chapters"] = [*(target.get("chapters") or []), slot]
        _recount(target)
        target.update(
            exact=False,
            estimated=True,
            covered_by_chapters=False,
            can_assemble=False,
            map_sources=sorted(set(target.get("map_sources") or ()) | {"estimate"}),
        )
        absorbed += 1
    unassigned[:] = kept
    return absorbed


def mark_beyond_frontier(
    books: list[dict[str, Any]],
    unassigned: list[dict[str, Any]],
    frontier: Decimal | None,
) -> int:
    """A chapter past the official edition is not one the shelf is missing.

    Acquisition refuses every release past the frontier, so counting those
    chapters as missing asks for what is never fetched. They are shown as
    extras until the edition reaches them; a file already on disk keeps its
    row as it is.
    """

    if frontier is None:
        return 0
    marked = 0
    for slot in [*(s for b in books for s in b.get("chapters") or []), *unassigned]:
        number = _number(slot.get("chapter"))
        if number is None or number <= frontier or slot.get("downloaded"):
            continue
        if not slot.get("expected", True):
            continue
        slot.update(
            expected=False,
            special=True,
            missing=False,
            searchable=False,
            beyond_official_frontier=True,
        )
        marked += 1
    for book in books:
        if any(s.get("beyond_official_frontier") for s in book.get("chapters") or []):
            book["missing_chapter_count"] = sum(
                1 for s in book["chapters"] if s.get("missing")
            )
    return marked


def apply_series_form(
    units: dict[str, Any],
    manga: dict[str, Any],
    metadata: dict[str, Any] | None,
    *,
    preference: str = "volumes",
    exact_sources: list[str] | None = None,
    specials_outside_books: bool = False,
    content: dict[str, dict[str, Any]] | None = None,
    frontier: Decimal | None = None,
) -> dict[str, Any]:
    """Decorate the grouped payload with the form and the completion plan."""

    preference = "chapters" if str(preference).casefold() == "chapters" else "volumes"
    form, reason = series_form(manga, metadata, units.get("expected_book_count"))
    books = list(units.get("books") or [])
    unassigned = list(units.get("unassigned_chapters") or [])
    reset_estimated_layout(books, unassigned)
    units["beyond_frontier"] = mark_beyond_frontier(books, unassigned, frontier)
    expected_books = units.get("expected_book_count")
    # A broad catalogue family (for example Manhwa classified as webtoon)
    # cannot invalidate boundaries read from the actual printed edition.
    # Only a complete, explicit map overrides the chapter-only default;
    # sparse hints must never invent printed volumes for a scrolling work.
    mapped_books = {
        _whole(book.get("volume"))
        for book in books
        if book.get("exact")
        and book.get("chapter_range")
        and book.get("chapters")
        and not book.get("estimated")
        and set(book.get("map_sources") or ()) & {"operator", "ocr"}
    }
    if (
        _status(manga) not in RUNNING_STATUSES
        and expected_books
        and mapped_books == set(range(1, int(expected_books) + 1))
    ):
        form = "volumes"
        reason = f"saved division into {expected_books} books"
    # A shelf made only of books is read as books, even while the work is
    # still running. What the reader owns is the edition: there is no chapter
    # file to list, so the chapters-until-it-ends default shows a page of
    # rows nobody has (Moonlight Mile: 24 tankobon on disk, a chapter list of
    # 24 entries and not one book visible). A webtoon is published by episode
    # and keeps its form; a shelf holding any chapter file is a chapter shelf.
    if (
        form != "volumes"
        and any(book.get("owned") for book in books)
        and not _is_webtoon(metadata)
        and not any(slot.get("downloaded") for slot in unassigned)
        and not any(
            slot.get("downloaded")
            for book in books
            for slot in book.get("chapters") or []
        )
    ):
        form = "volumes"
        reason = "the shelf holds books, not chapters"
    # The mirror of the rule above, on the same principle: what the reader owns
    # decides. A work still running, or merely paused, has no settled division
    # into books, so a complete catalogue map is not evidence enough to turn a
    # shelf of chapter files into a page of books nobody has (Guyver: 200
    # chapters on disk, not one tankobon, shown as 32 empty books). A finished
    # work keeps its books: that is where the division into volumes is made.
    if (
        form == "volumes"
        and _status(manga) in RUNNING_STATUSES | PAUSED_STATUSES
        and not any(book.get("owned") for book in books)
        and (
            any(slot.get("downloaded") for slot in unassigned)
            or any(
                slot.get("downloaded")
                for book in books
                for slot in book.get("chapters") or []
            )
        )
    ):
        form = "chapters"
        reason = "an unfinished shelf of chapters is not divided into books"
    # A special no book contains, a half chapter or a second cut of a story
    # published apart, is a row on a shelf of books only if the reader asked
    # for it. It is never counted as missing either way.
    if not specials_outside_books:
        hidden = [slot for slot in unassigned if slot.get("special")]
        for slot in hidden:
            unassigned.remove(slot)
        units["hidden_specials"] = len(hidden)
    else:
        units["hidden_specials"] = 0
    if form != "volumes" and len(books) > 1 and len(unassigned) >= len(books):
        # A chapter shelf still has books on it, and showing them empty beside
        # a heap of loose chapters is the disorder the even split exists to
        # remove (redEyes: 27 books, 79 chapters, every book blank; Ate Ya:
        # chapters 9-14 orphaned between two described books). Layout only,
        # and nothing here can escape the page: chapter_volume_assignments
        # returns nothing at all unless the form is volumes, so no membership
        # is ever written back. A lone book with a stray chapter is left
        # alone: with that little to go on, an invented range says less than
        # an honest blank.
        spread_unmapped_chapters(books, unassigned)
        estimate_ranges_from_count(books, units.get("expected_chapter_count"))
        if any(
            not (book.get("chapter_range") or {})
            for book in books
            if book.get("expected", True)
        ) and not any(slot.get("downloaded") for slot in unassigned):
            estimate_ranges_from_count(books, units.get("catalogue_chapter_count"))
    if form == "volumes":
        # Keep physical books and expected edition slots. A chapter stays
        # unassigned until a source explicitly establishes its membership.
        books = [
            book
            for book in books
            if book.get("expected")
            or book.get("owned")
            or int(book.get("downloaded_chapter_count") or 0) > 0
        ]
        # Reject legacy catalogue shapes that repeat book numbers as
        # chapter numbers. A rejected range stays unknown, never evenly split.
        chapter_end = (
            _whole(units.get("chapter_sequence_end"))
            or _whole(units.get("expected_chapter_count"))
            or 0
        )
        legacy_end = _whole(manga.get("last_chapter")) or 0
        manual_count = str(
            manga.get("expected_count_unit_override") or ""
        ).casefold() == "chapter" and manga.get("expected_count_override")
        rejected_legacy = False
        for book in books:
            span = book.get("chapter_range") or {}
            number = _whole(book.get("volume"))
            first, last = _whole(span.get("first")), _whole(span.get("last"))
            if set(book.get("map_sources") or []) & {"operator", "ocr"}:
                continue
            if (
                number
                and first is not None
                and (
                    first < number
                    or (
                        not manual_count
                        and chapter_end <= len(books) < legacy_end
                        and first == last == number
                    )
                )
            ):
                unassigned.extend(book.get("chapters") or [])
                book.update(
                    chapter_range=None,
                    chapters=[],
                    chapter_count=0,
                    downloaded_chapter_count=0,
                    missing_chapter_count=0,
                    exact=False,
                    covered_by_chapters=False,
                )
                if not book.get("owned"):
                    book["status"] = "missing"
                rejected_legacy = True
        # Provider tags and content guesses do not override the operator or
        # the even layout. Explicit book readings enter through the map API.
        units["content_notes"] = []
        # A whole chapter whose parts the books hold is not a chapter to hunt;
        # on a finished, complete shelf a loose chapter is in one of the books.
        units["hidden_specials"] = int(
            units.get("hidden_specials") or 0
        ) + drop_split_parents(books, unassigned)
        # What no source described is laid out between its known neighbours,
        # in equal parts, so the shelf never shows an empty book beside a
        # pile of loose chapters. Layout only: see spread_unmapped_chapters.
        spread_unmapped_chapters(books, unassigned)
        # Only after the even split: a chapter the split could not place —
        # a tail past the last book — belongs to the book it falls in. Run
        # first, it swallowed the whole run into book one, because no book
        # had a range yet to fall inside (Captain Harlock: 55 chapters, 3
        # omnibus books, and book 1 read "ch. 1-55").
        if not rejected_legacy and not any(
            set(book.get("map_sources") or ()) & {"operator", "ocr"} for book in books
        ):
            absorb_loose_on_complete_shelf(books, unassigned, manga)
        # A count that was itself the rejected shape (book numbers standing
        # in for chapter numbers) describes nothing: it must not come back
        # as an even split.
        if not rejected_legacy:
            estimate_ranges_from_count(books, units.get("expected_chapter_count"))
            # A running work has no settled total, so the pass above stands
            # down and its books would show nothing at all. On a shelf made
            # only of books the catalogue's running count lays them out
            # anyway: a division on the shelf, never evidence.
            if any(
                not (book.get("chapter_range") or {})
                for book in books
                if book.get("expected", True)
            ) and not any(slot.get("downloaded") for slot in unassigned):
                estimate_ranges_from_count(books, units.get("catalogue_chapter_count"))
        # Once every book shows a range and no counted chapter is left loose,
        # "not known" is no longer what the shelf says: the confidence line
        # carries the estimate instead. A shelf with no chapter at all (books
        # only, nothing counted) has nothing the notice could be about.
        no_chapters = not unassigned and not any(book.get("chapters") for book in books)
        if no_chapters or (
            not any(
                not (book.get("chapter_range") or {})
                for book in books
                if book.get("expected", True)
            )
            and not any(slot.get("expected") for slot in unassigned)
        ):
            units["warning"] = None
        for book in books:
            book.setdefault("chapter_count", len(book.get("chapters") or []))
            book.setdefault("chapter_range", None)
            if "estimate" in (book.get("map_sources") or []):
                book["chapter_range_estimated"] = True
            book["estimated"] = bool(
                book.get("estimated") or book.pop("chapter_range_estimated", False)
            )
            # A chapter printed in a book you own is not missing: it is in the
            # book. Opening a tankobon on the shelf and being told the five
            # chapters inside it are absent is the one thing the page must
            # never say.
            if book.get("owned"):
                for slot in book.get("chapters") or []:
                    if not slot.get("downloaded"):
                        slot["missing"] = False
                        slot["covered_by_volume"] = book["volume"]
                book["missing_chapter_count"] = 0
            book["plan_state"] = plan_state(book)
        kind = uniform_kind(books)
        for book in books:
            book["plan_text"] = plan_text(book, preference, kind)
            book["redundant_book"] = bool(
                kind == "chapters" and book.get("owned") and book.get("expected")
            )
    else:
        kind = "chapters"
        units.setdefault("content_notes", [])
        # A chapter shelf shows no book, so "which chapters each book
        # contains is not known" answers a question the page never asks:
        # sixteen series carried that line above a list of chapters.
        units["warning"] = None
        for book in books:
            book.setdefault("estimated", False)
            book["plan_state"] = plan_state(book)
            book["plan_text"] = ""
            book["redundant_book"] = False
    units["books"] = books
    units["unassigned_chapters"] = unassigned
    units["form"] = form
    units["form_reason"] = reason
    units["uniform"] = kind
    units["preference"] = preference
    units["map_confidence"] = map_confidence(
        books if form == "volumes" else [],
        [str(hint.get("source")) for hint in units.get("hints") or []],
        exact_sources or [],
        unassigned_chapters=any(
            slot.get("downloaded") and not slot.get("special") for slot in unassigned
        ),
    )
    # Keep file ownership, known chapter coverage and layout estimates separate.
    # A virtual book is never evidence that the printed edition is on disk.
    indexed = {
        str(slot.get("key") or slot.get("chapter")): slot
        for slot in [
            *(s for book in books for s in book.get("chapters") or []),
            *unassigned,
        ]
        if not slot.get("special") and (slot.get("releases") or slot.get("files"))
    }
    expected = units.get("edition_book_count") or units.get("expected_book_count")
    owned = sum(bool(book.get("owned")) for book in books if book.get("expected", True))
    units["completeness"] = {
        "indexed_chapters": len(indexed),
        "indexed_chapters_on_disk": sum(
            bool(slot.get("downloaded")) for slot in indexed.values()
        ),
        "owned_books": owned,
        "expected_books": expected,
        "edition_files_complete": bool(expected and owned >= int(expected)),
        "estimated_groups": sum(bool(book.get("estimated")) for book in books),
        "manual_boundaries": "operator" in (exact_sources or []),
    }
    return units


def estimate_entries(units: dict[str, Any]) -> list[Any]:
    """Legacy compatibility: no statistical map is written back."""
    return []


def chapter_volume_assignments(units: dict[str, Any]) -> dict[str, str]:
    """Release IDs with explicit book membership; never infer from a range."""
    if units.get("form") != "volumes":
        return {}
    return {
        str(file["id"]): str(book["volume"])
        for book in units.get("books") or []
        if book.get("volume") and book.get("exact") and not book.get("estimated")
        for slot in book.get("chapters") or []
        if not slot.get("volume_inferred")
        for file in slot.get("files") or []
        if file.get("id")
    }


def sync_books(database: Any, manga_id: str, units: dict[str, Any]) -> dict[str, int]:
    """Remove retired estimates and write only explicit chapter memberships."""
    from tankarr.chapter_map import effective_entries

    stored = database.chapter_map(manga_id, raw=True)
    supported = {
        (str(volume), str(chapter))
        for entry in effective_entries(stored)
        if entry.exact
        for volume in entry.volumes
        for chapter in entry.chapters
    }
    legacy = {
        (str(volume), str(chapter))
        for entry in stored
        if entry.source == "estimate"
        for volume in entry.volumes
        for chapter in entry.chapters
    }
    legacy -= supported
    verified = chapter_volume_assignments(units)
    assignments: dict[str, str | None] = dict(verified)
    slots = list(units.get("unassigned_chapters") or []) + [
        slot for book in units.get("books") or [] for slot in book.get("chapters") or []
    ]
    for slot in slots:
        for release in slot.get("releases") or []:
            identifier = str(release.get("id") or "")
            if (
                identifier
                and identifier not in verified
                and release.get("downloaded")
                and (str(release.get("volume")), str(release.get("chapter"))) in legacy
            ):
                assignments[identifier] = None
    # Clear derived file tags before deleting their provenance, so a retry
    # after interruption can still identify the obsolete assignments.
    changed_files = database.set_chapter_volumes(manga_id, assignments)
    removed = 0
    for source in ("estimate", "releases"):
        rows = [entry for entry in stored if entry.source == source]
        if rows:
            database.replace_chapter_map(manga_id, source, [])
            removed += len(rows)
    return {"map_entries": removed, "chapters_renumbered": int(changed_files or 0)}
