from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class ReaderIndependentLibrary:
    """Runtime boundary between Tankarr and any library reader.

    Tankarr owns files and portable metadata only. Komga, Kavita, or another
    reader observes the shared library on its own schedule and credentials are
    never required by Tankarr.
    """

    standalone = True
    configured = False

    @staticmethod
    def result(*, requested: bool = False) -> dict[str, Any]:
        return {
            "configured": False,
            "requested": requested,
            "triggered": False,
            "reader_independent": True,
            "reason": "Reader applications scan Tankarr's filesystem independently",
        }

    async def scan(self, relative_paths: Iterable[str] | None = None) -> dict[str, Any]:
        return self.result(requested=bool(tuple(relative_paths or ())))

    async def ensure_present(self, relative_paths: Iterable[str]) -> dict[str, Any]:
        paths = tuple(relative_paths)
        return {
            **self.result(),
            "expected_books": len(paths),
            "ready": True,
        }

    async def prepare_for_moves(self) -> dict[str, Any]:
        return self.result()

    async def reconcile_deleted(
        self,
        relative_paths: Iterable[str],
        *,
        safety_check=None,
    ) -> dict[str, Any]:
        if safety_check is not None:
            safety_check()
        paths = tuple(relative_paths)
        return {
            **self.result(requested=bool(paths)),
            "purged": False,
            "paths": len(paths),
        }
