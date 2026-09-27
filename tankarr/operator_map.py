"""Preview and commit operator book boundaries without changing other sources."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from tankarr.chapter_map import (
    MapEntry,
    canonical_label,
    edition_entries,
    effective_entries,
    entries_from_boundaries,
)
from tankarr.chapter_mapping import (
    build_chapter_index,
    canonical_decimal_labels,
    counted_chapter_total,
    suspect_volume_reasons,
)
from tankarr.database import Database
from tankarr.models import BookReadingRequest, ChapterMapRequest
from tankarr.numbering_reconciliation import reconcile_numbering
from tankarr.service import RecoveryBlocked, TankarrService


class StaleChapterMap(ValueError):
    pass


def boundaries_of(entries: list[MapEntry]) -> list[dict[str, str]]:
    first: dict[str, Decimal] = {}
    for entry in entries:
        if len(entry.volumes) != 1:
            continue
        volume = canonical_label(entry.volumes[0])
        chapters = [
            Decimal(label)
            for value in entry.chapters
            if (label := canonical_label(value)) is not None
        ]
        if volume is not None and chapters:
            first[volume] = min(first.get(volume, min(chapters)), *chapters)
    return [
        {"volume": volume, "first_chapter": format(number, "f")}
        for volume, number in sorted(first.items(), key=lambda item: Decimal(item[0]))
    ]


class OperatorChapterMap:
    def __init__(self, database: Database, service: TankarrService):
        self.database = database
        self.service = service
        self._confirmation_key = secrets.token_bytes(32)

    def _inputs(self, manga_id: str) -> dict[str, Any]:
        manga = self.database.get_manga(manga_id)
        releases = self.database.list_chapters(manga_id, manga["preferred_language"])
        entries = self.database.chapter_map(manga_id, raw=True)
        metadata = (self.database.get_series_metadata(manga_id) or {}).get("data") or {}
        # Replacing a map reconciles every language, using edition roles and
        # publisher evidence. Read the same inputs in this snapshot so preview
        # can project those decisions without changing any persisted state.
        with self.database.connect() as connection:
            numbering_releases = [
                self.database._decode_chapter(row)
                for row in connection.execute(
                    "SELECT * FROM chapter_release WHERE manga_id=?", (manga_id,)
                ).fetchall()
            ]
            source_roles = self.database._numbering_source_roles(connection, manga_id)
            secondary_evidence = self.database._secondary_official_evidence_connection(
                connection, manga_id, source_roles
            )
        # An edition can end in books with no chapter releases. Its effective
        # expected total and catalogue endpoint still bound new operator rows.
        known = {
            label
            for release in releases
            if str(release.get("release_unit") or "chapter") != "volume"
            and str(release.get("numbering_status") or "mapped") == "mapped"
            if (
                label := canonical_label(
                    release.get("canonical_chapter")
                    if release.get("canonical_chapter") is not None
                    else release.get("chapter")
                )
            )
            is not None
        }
        known.update(
            label
            for entry in entries
            if entry.source != "operator"
            for value in entry.chapters
            if (label := canonical_label(value)) is not None
        )
        index = build_chapter_index(
            manga,
            metadata,
            releases,
            chapter_map=effective_entries(
                entry for entry in entries if entry.source != "operator"
            ),
        )
        # One stray release numbered many times past the end of the numbering
        # must not stretch the map: every integer up to it is laid into the
        # last book, which then asks for chapters nobody ever wrote. Adekan
        # ends at 73 and a single source listed a "Chapter 487", so volume 20
        # demanded 74-487. Only a wild outlier is dropped, never a chapter
        # just past a stale catalogue: a work at 302 that publishes 303 is
        # still publishing, and the map must follow it. The catalogue values
        # added below are left alone, because a count may legitimately sit
        # above the numbering (a prologue, canonical decimals).
        if (sequence_end := index.get("sequence_end")) is not None:
            outlier = Decimal(str(sequence_end)) * 2
            if outlier > 0:
                known = {label for label in known if Decimal(label) <= outlier}
        elif len(known) >= 10:
            # A running work has no settled end to measure against, so the
            # numbering itself is the reference: a number more than twice the
            # one below it is a gap no publication makes. Adekan runs 1-73 and
            # a source listed "Chapter 487".
            # The count must be a real run, not a handful of scattered hints:
            # a lone release at 20 beside catalogue chapters 1 and 2 reads as
            # "20 is four times 2" and would drop the only release there is.
            # Below ten numbers there is no sequence to be an outlier of.
            ordered = sorted(known, key=Decimal, reverse=True)
            top, below = Decimal(ordered[0]), Decimal(ordered[1])
            if below > 0 and top > below * 2:
                known.discard(ordered[0])
        available_reference = index.get("reference_kind") == "available"
        known.update(
            label
            for value in (
                index.get("expected_count")
                if index["unit"] == "chapter" and not available_reference
                else None,
                manga.get("expected_count_override")
                if manga.get("expected_count_unit_override") != "volume"
                else None,
                manga.get("last_chapter") if not available_reference else None,
                metadata.get("last_chapter") if not available_reference else None,
                metadata.get("chapter_count") if not available_reference else None,
            )
            if (label := canonical_label(value)) is not None
        )
        return {
            "manga": manga,
            "releases": releases,
            "numbering_releases": numbering_releases,
            "source_roles": source_roles,
            "secondary_evidence": secondary_evidence,
            "metadata": metadata,
            "entries": entries,
            "known": sorted(known, key=Decimal),
            "overrides": {
                row["volume_key"]: row["state"]
                for row in self.database.list_volume_monitor_overrides(manga_id)
            },
            "suspects": sorted(self.service.suspect_covering_volumes(manga_id)),
            "suspect_page_limit": self.service.SUSPECT_BOOK_PAGES,
        }

    @staticmethod
    def _project_releases(
        inputs: dict[str, Any], entries: list[MapEntry]
    ) -> list[dict[str, Any]]:
        decisions = reconcile_numbering(
            inputs["numbering_releases"],
            source_roles=inputs["source_roles"],
            secondary_evidence=inputs["secondary_evidence"],
            canonical_labels=canonical_decimal_labels(
                entries,
                inputs["numbering_releases"],
                chapter_total=counted_chapter_total(
                    inputs["manga"], inputs["metadata"]
                ),
            ),
            canonical_end=counted_chapter_total(inputs["manga"], inputs["metadata"]),
        )
        projected = []
        for release in inputs["releases"]:
            decision = decisions.get(str(release["id"]))
            if decision is None:
                projected.append(dict(release))
                continue
            # Match the compatibility column written by reconciliation and
            # exposed by Database._decode_chapter; unresolved labels are not
            # canonical slots, even when their provider number is available.
            projected.append(
                {
                    **release,
                    **decision,
                    "chapter": decision.get("canonical_chapter")
                    if decision["numbering_status"] == "mapped"
                    else None,
                }
            )
        return projected

    @staticmethod
    def _revision(inputs: dict[str, Any]) -> str:
        payload = {**inputs, "entries": [asdict(entry) for entry in inputs["entries"]]}
        return hashlib.sha256(
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), default=str
            ).encode()
        ).hexdigest()

    @staticmethod
    def _warnings(inputs: dict[str, Any], operator: list[MapEntry]) -> list[str]:
        labels = [Decimal(label) for entry in operator for label in entry.chapters]
        known = [Decimal(label) for label in inputs["known"]]
        if not labels:
            return []
        if not known:
            return [
                "The current sources no longer provide canonical chapter numbers. Saved book boundaries have been kept."
            ]
        warnings = []
        if min(labels) < min(known) or max(labels) > max(known):
            warnings.append(
                "Some saved book boundaries are outside the current known chapter range. Review them; they have not been shifted."
            )
        if max(known) > max(labels):
            warnings.append(
                "New chapters extend beyond the saved book boundaries. The last book has not been extended automatically."
            )
        return warnings

    def _response(self, inputs: dict[str, Any]) -> dict[str, Any]:
        operator = [entry for entry in inputs["entries"] if entry.source == "operator"]
        catalogue = [entry for entry in inputs["entries"] if entry.source != "operator"]
        return {
            "boundaries": boundaries_of(operator),
            "suggestions": boundaries_of(catalogue),
            "last_known_chapter": inputs["known"][-1] if inputs["known"] else None,
            "warnings": self._warnings(inputs, operator),
            "revision": self._revision(inputs),
        }

    def read(self, manga_id: str) -> dict[str, Any]:
        with self.database.read_snapshot():
            return self._response(self._inputs(manga_id))

    def _preview(
        self,
        inputs: dict[str, Any],
        boundaries: list[dict[str, str]] | None,
        mode: str = "merge",
        last_chapter: str | None = None,
    ) -> tuple[dict[str, Any], list[MapEntry]]:
        saved = [entry for entry in inputs["entries"] if entry.source == "operator"]
        old_rows = {row["volume"]: row for row in boundaries_of(saved)}
        operator = []
        if boundaries is not None:
            known_end = inputs["known"][-1] if inputs["known"] else None
            # Validate submitted ordering/precision before merging. Unchanged
            # saved rows remain admissible when current source coverage shrinks.
            submitted = []
            for row in boundaries:
                normalized = {
                    key: canonical_label(row.get(key))
                    for key in ("volume", "first_chapter")
                }
                if normalized != old_rows.get(normalized["volume"]):
                    first = normalized["first_chapter"]
                    if (
                        first is None
                        or known_end is None
                        or Decimal(first) > Decimal(known_end)
                    ):
                        raise ValueError(
                            "A book boundary is beyond the last known chapter"
                        )
                submitted.append(normalized)
            saved_labels = [label for entry in saved for label in entry.chapters]
            last = max(
                [*(inputs["known"] or []), *saved_labels], key=Decimal, default=None
            )
            entries_from_boundaries(boundaries, last_chapter=last)
            merged = dict(old_rows) if mode == "merge" else {}
            merged.update((row["volume"], row) for row in submitted)
            rows = sorted(merged.values(), key=lambda row: Decimal(row["volume"]))
            old_by_volume = {
                entry.volumes[0]: entry for entry in saved if len(entry.volumes) == 1
            }
            final_saved = old_by_volume.get(rows[-1]["volume"])
            allowed_end = max(
                [*inputs["known"], *(final_saved.chapters if final_saved else ())],
                key=Decimal,
                default=None,
            )
            last = (
                max(final_saved.chapters, key=Decimal)
                if final_saved and rows[-1]["volume"] == next(reversed(old_rows))
                else allowed_end
            )
            if last_chapter is not None:
                end = canonical_label(last_chapter)
                if (
                    end is None
                    or allowed_end is None
                    or Decimal(end) > Decimal(allowed_end)
                ):
                    raise ValueError("The final chapter is beyond the known edition")
                last = end
            operator = entries_from_boundaries(
                rows,
                last_chapter=last,
                known_chapters=[*inputs["known"], *saved_labels],
            )
            # Keep untouched intervals byte-for-byte, including an old final
            # chapter beyond today's known range. Only a changed next boundary
            # can require adjusting an existing book's end.
            old_order = list(old_rows)
            new_order = [entry.volumes[0] for entry in operator]
            for index, entry in enumerate(operator):
                volume = entry.volumes[0]
                old = old_by_volume.get(volume)
                if old and entry.chapters == old.chapters and entry.exact == old.exact:
                    operator[index] = old
                    continue
                if volume not in old_by_volume or volume in {
                    row["volume"]
                    for row in submitted
                    if row != old_rows.get(row["volume"])
                }:
                    continue
                if index == len(operator) - 1 and last_chapter is not None:
                    continue
                old_next = old_order[old_order.index(volume) + 1 :][:1]
                new_next = new_order[index + 1 :][:1]
                if old_next == new_next and (
                    not new_next or old_rows[new_next[0]] == merged[new_next[0]]
                ):
                    operator[index] = old_by_volume[volume]
        remaining = [entry for entry in inputs["entries"] if entry.source != "operator"]
        proposed = {**inputs, "entries": [*remaining, *operator]}
        response = self._response(proposed)
        # The revision describes the inputs reviewed, not an unsaved map.
        response["revision"] = self._revision(inputs)
        response["intervals"] = [
            {
                "volume": entry.volumes[0],
                "first_chapter": entry.chapters[0],
                "last_chapter": entry.chapters[-1],
                "chapters": list(entry.chapters),
            }
            for entry in operator
        ]
        map_entries = effective_entries(
            edition_entries(proposed["entries"], inputs["metadata"])
        )
        projected_releases = self._project_releases(inputs, map_entries)
        response["chapter_index"] = build_chapter_index(
            inputs["manga"],
            inputs["metadata"],
            projected_releases,
            inputs["overrides"],
            chapter_map=map_entries,
            suspect_covering_volumes=set(
                suspect_volume_reasons(
                    projected_releases,
                    page_limit=inputs["suspect_page_limit"],
                    chapter_map=map_entries,
                )
            ),
        )
        signed = json.dumps(
            {
                "revision": response["revision"],
                "action": "delete" if boundaries is None else mode,
                "operator": [asdict(entry) for entry in operator],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        response["confirmation_snapshot"] = hmac.new(
            self._confirmation_key, signed.encode(), hashlib.sha256
        ).hexdigest()
        return response, operator

    def preview(
        self,
        manga_id: str,
        boundaries: list[dict[str, str]] | None,
        mode: str = "merge",
        last_chapter: str | None = None,
    ) -> dict[str, Any]:
        with self.database.read_snapshot():
            return self._preview(
                self._inputs(manga_id), boundaries, mode, last_chapter
            )[0]

    def commit(
        self,
        manga_id: str,
        boundaries: list[dict[str, str]] | None,
        confirmation: str | None,
        mode: str = "merge",
        last_chapter: str | None = None,
    ) -> dict[str, Any]:
        with self.database.write_snapshot():
            response, entries = self._preview(
                self._inputs(manga_id), boundaries, mode, last_chapter
            )
            if confirmation is None or not hmac.compare_digest(
                confirmation, response["confirmation_snapshot"]
            ):
                raise StaleChapterMap(
                    "Book boundaries or series data changed. Preview and confirm again."
                )
            self.database.replace_chapter_map(manga_id, "operator", entries)
            current = self._inputs(manga_id)
            response.update(self._response(current))
            response["chapter_index"] = build_chapter_index(
                current["manga"],
                current["metadata"],
                current["releases"],
                current["overrides"],
                chapter_map=effective_entries(current["entries"]),
                suspect_covering_volumes=set(current["suspects"]),
            )
            return response


def register_chapter_map_routes(
    app: FastAPI, database: Database, service: TankarrService
) -> None:
    editor = OperatorChapterMap(database, service)

    @app.get("/api/manga/{manga_id}/chapter-map")
    async def get_chapter_map(manga_id: str):
        try:
            return await asyncio.to_thread(editor.read, manga_id)
        except KeyError as exc:
            raise HTTPException(404, "Series not found") from exc

    async def change(
        manga_id: str,
        boundaries,
        dry_run: bool,
        confirmation: str | None,
        mode: str = "merge",
        last_chapter: str | None = None,
    ):
        try:
            if dry_run:
                return await asyncio.to_thread(
                    editor.preview, manga_id, boundaries, mode, last_chapter
                )
            async with service._mutation_lock:
                service.assert_mutations_allowed(db_only=True)
                return await asyncio.to_thread(
                    editor.commit,
                    manga_id,
                    boundaries,
                    confirmation,
                    mode,
                    last_chapter,
                )
        except KeyError as exc:
            raise HTTPException(404, "Series not found") from exc
        except StaleChapterMap as exc:
            raise HTTPException(409, str(exc)) from exc
        except RecoveryBlocked as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.put("/api/manga/{manga_id}/chapter-map")
    async def put_chapter_map(
        manga_id: str,
        request: ChapterMapRequest,
        dry_run: bool = False,
        confirmation_snapshot: str | None = Query(
            default=None, min_length=64, max_length=64, pattern="^[0-9a-f]{64}$"
        ),
    ):
        return await change(
            manga_id,
            [row.model_dump() for row in request.boundaries],
            dry_run,
            confirmation_snapshot,
            request.mode,
            request.last_chapter,
        )

    @app.put("/api/manga/{manga_id}/chapter-map/ocr")
    async def put_chapter_map_reading(manga_id: str, request: BookReadingRequest):
        """What a reader found printed in the books: one chapter range per
        book, accepted only when the ranges are consistent with each other.
        Stored as the ``ocr`` source: it owns the books it read, the operator
        still overrides it, and the page marks those books as read, not
        estimated."""

        from tankarr.chapter_map import MapEntry

        books = sorted(request.books, key=lambda book: Decimal(book.volume))
        entries: list[MapEntry] = []
        previous_last: Decimal | None = None
        for book in books:
            first, last = Decimal(book.first_chapter), Decimal(book.last_chapter)
            if last < first:
                raise HTTPException(
                    status_code=422, detail=f"Book {book.volume}: last before first"
                )
            if previous_last is not None and first <= previous_last:
                raise HTTPException(
                    status_code=422,
                    detail=f"Book {book.volume} overlaps the book before it",
                )
            labels = []
            number = first
            while number <= last:
                labels.append(
                    str(number.normalize())
                    if number != number.to_integral_value()
                    else str(int(number))
                )
                number += 1
            entries.append(
                MapEntry(
                    volumes=(book.volume,),
                    chapters=tuple(labels),
                    exact=True,
                    source="ocr",
                )
            )
            previous_last = last

        def write() -> int:
            manga = database.get_manga(manga_id)
            if manga is None:
                raise HTTPException(status_code=404, detail="Unknown series")
            return database.replace_chapter_map(manga_id, "ocr", entries)

        written = await asyncio.to_thread(write)
        return {"manga_id": manga_id, "books": len(entries), "entries": written}

    @app.delete("/api/manga/{manga_id}/chapter-map")
    async def delete_chapter_map(
        manga_id: str,
        dry_run: bool = False,
        confirmation_snapshot: str | None = Query(
            default=None, min_length=64, max_length=64, pattern="^[0-9a-f]{64}$"
        ),
    ):
        return await change(manga_id, None, dry_run, confirmation_snapshot)
