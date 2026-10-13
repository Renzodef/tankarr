"""Operator verification of imported books: what a book holds, proved once.

The listing gives a person everything needed to check a book and to write the
proof: the book's content digest, its reason for being suspect and the chapters
the map lists for its volume. The proof is refused unless it matches all three
of the book as it is now, and it stops applying by itself when the content or
the map changes (see ``book_verification``). Nothing here retires a chapter file.
"""

from __future__ import annotations

import asyncio
import hmac
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from tankarr.book_verification import book_verification, verification_problem
from tankarr.chapter_map import canonical_label, chapters_by_volume
from tankarr.chapter_mapping import canonical_number
from tankarr.database import Database, utc_now
from tankarr.models import BookVerificationRequest
from tankarr.series_unit import is_volume_release
from tankarr.service import RecoveryBlocked, TankarrService


class VerificationConflict(ValueError):
    """The proof does not match the book or the map as they are now."""


def _sorted_labels(labels: set[str]) -> list[str]:
    return sorted(labels, key=lambda label: (len(label.split(".")[0]), label))


class VerifiedBooks:
    def __init__(self, database: Database, service: TankarrService):
        self.database = database
        self.service = service

    def _state(self, manga_id: str) -> tuple[list[dict[str, Any]], dict[str, set[str]]]:
        releases = self.database.list_all_chapters(manga_id)
        return releases, chapters_by_volume(self.database.chapter_map(manga_id))

    @staticmethod
    def _books(releases: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            release
            for release in releases
            if release.get("downloaded") and is_volume_release(release)
        ]

    def _entry(
        self,
        release: dict[str, Any],
        mapped: dict[str, set[str]],
        reasons: dict[str, str],
    ) -> dict[str, Any]:
        volume = canonical_number(release.get("volume")) or ""
        proof = book_verification(release.get("verified_book"))
        verification = None
        if proof is not None:
            problem = verification_problem(release, mapped)
            verification = {
                "chapters": proof["chapters"],
                "source": proof["source"],
                "note": proof["note"],
                "verified_at": proof["verified_at"],
                "content_sha256": proof["content_sha256"],
                "valid": problem is None,
                "problem": problem,
            }
        return {
            "release_id": str(release["id"]),
            "volume": volume,
            "provider": release.get("provider"),
            "language": release.get("language"),
            "pages": release.get("pages"),
            "content_sha256": release.get("local_import_sha256"),
            "mapped_chapters": _sorted_labels(mapped.get(volume, set())),
            "suspect_reason": reasons.get(volume),
            "verification": verification,
        }

    def read(self, manga_id: str) -> dict[str, Any]:
        self.database.get_manga(manga_id)
        releases, mapped = self._state(manga_id)
        reasons = self.service.suspect_covering_volumes(manga_id)
        return {
            "manga_id": manga_id,
            "books": [
                self._entry(release, mapped, reasons)
                for release in self._books(releases)
                if release.get("provider") != "assembled"
            ],
        }

    def verify(
        self, manga_id: str, volume: str, request: BookVerificationRequest
    ) -> dict[str, Any]:
        self.database.get_manga(manga_id)
        wanted = canonical_number(volume)
        releases, mapped = self._state(manga_id)
        books = [
            release
            for release in self._books(releases)
            if canonical_number(release.get("volume")) == wanted
            and release.get("provider") != "assembled"
            and (request.release_id is None or str(release["id"]) == request.release_id)
        ]
        if not books:
            raise KeyError(volume)
        if len(books) > 1:
            raise VerificationConflict(
                f"Volume {wanted} has several books; name the release_id: "
                + ", ".join(str(book["id"]) for book in books)
            )
        release = books[0]
        digest = release.get("local_import_sha256")
        if not isinstance(digest, str) or not digest:
            raise VerificationConflict(
                "This book has no content digest, so nothing can be bound to it"
            )
        if not hmac.compare_digest(digest, request.content_sha256):
            raise VerificationConflict(
                "The book's content is not the one that was checked; "
                f"its current digest is {digest}"
            )
        labels = [canonical_label(item) for item in request.chapters]
        if any(label is None for label in labels):
            raise ValueError("Every chapter must be a plain number such as 12 or 12.5")
        if len(set(labels)) != len(labels):
            raise ValueError("A chapter is listed twice")
        listed = mapped.get(wanted or "", set())
        if listed != set(labels):
            raise VerificationConflict(
                f"The chapter map lists {', '.join(_sorted_labels(listed)) or 'nothing'} "
                f"for volume {wanted}, not the proven chapters; "
                "register the exact chapters for the book first"
            )
        note = request.note.strip()
        if not note:
            raise ValueError("Say how the book was checked")
        proof = {
            "version": 1,
            "release_id": str(release["id"]),
            "volume": wanted,
            "content_sha256": digest,
            "chapters": _sorted_labels(set(labels)),
            "source": request.source.strip(),
            "note": note,
            "verified_at": utc_now(),
        }
        stored = self.database.set_book_verification(
            manga_id, str(release["id"]), proof
        )
        reasons = self.service.suspect_covering_volumes(manga_id)
        return self._entry(stored, mapped, reasons)

    def remove(self, manga_id: str, volume: str, release_id: str | None) -> int:
        self.database.get_manga(manga_id)
        wanted = canonical_number(volume)
        removed = 0
        for release in self.database.list_book_verifications(manga_id):
            if canonical_number(release.get("volume")) != wanted:
                continue
            if release_id is not None and str(release["id"]) != release_id:
                continue
            self.database.set_book_verification(manga_id, str(release["id"]), None)
            removed += 1
        if not removed:
            raise KeyError(volume)
        return removed


def register_verified_book_routes(
    app: FastAPI, database: Database, service: TankarrService
) -> None:
    books = VerifiedBooks(database, service)

    async def change(work):
        try:
            async with service._mutation_lock:
                service.assert_mutations_allowed(db_only=True)
                return await asyncio.to_thread(work)
        except KeyError as exc:
            raise HTTPException(404, "Series or book not found") from exc
        except VerificationConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except RecoveryBlocked as exc:
            raise HTTPException(503, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/manga/{manga_id}/verified-books")
    async def list_verified_books(manga_id: str):
        try:
            return await asyncio.to_thread(books.read, manga_id)
        except KeyError as exc:
            raise HTTPException(404, "Series not found") from exc

    @app.put("/api/manga/{manga_id}/volumes/{volume}/verification")
    async def verify_book(manga_id: str, volume: str, request: BookVerificationRequest):
        """Record that a person checked what this imported book holds.

        A verified book is no longer taken for another edition or a wrong
        split, and may satisfy Wanted for the chapters the map lists for it.
        It never makes a chapter file disposable."""

        return await change(lambda: books.verify(manga_id, volume, request))

    @app.delete("/api/manga/{manga_id}/volumes/{volume}/verification")
    async def unverify_book(
        manga_id: str,
        volume: str,
        release_id: str | None = Query(default=None, min_length=1, max_length=512),
    ):
        removed = await change(lambda: books.remove(manga_id, volume, release_id))
        return {"manga_id": manga_id, "volume": volume, "removed": removed}
