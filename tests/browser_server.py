"""Isolated real-API server for browser tests; never starts download workers.

Run with the repository's Python environment. Optional --snapshot is copied
using SQLite backup in read-only mode; all subsequent writes stay in a fresh
temporary directory. No credentials/settings or library files are inherited.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18878)
    parser.add_argument(
        "--repo", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument(
        "--url-base", default="", help="Serve under a sub-path, like TANKARR_URL_BASE"
    )
    parser.add_argument("--artwork-root", type=Path)
    parser.add_argument(
        "--temp-root",
        type=Path,
        help="Existing scratch directory; prefer disk-backed storage for large artwork copies",
    )
    options = parser.parse_args()
    sys.path.insert(0, str(options.repo))

    import uvicorn
    from fastapi import Request
    from fastapi.responses import JSONResponse

    from tankarr.app import create_app
    from tankarr.config import Settings
    from tankarr.database import Database

    with tempfile.TemporaryDirectory(
        prefix="tankarr-browser-server-", dir=options.temp_root
    ) as temporary:
        root = Path(temporary)
        data = root / "data"
        library = root / "library"
        data.mkdir()
        library.mkdir()
        identity = "0123456789abcdef0123456789abcdef\n"
        (data / ".tankarr-library-id").write_text(identity)
        (library / ".tankarr-library-id").write_text(identity)
        database_path = data / "tankarr.sqlite3"
        if options.snapshot:
            source = sqlite3.connect(
                f"{options.snapshot.resolve().as_uri()}?mode=ro", uri=True
            )
            destination = sqlite3.connect(database_path)
            try:
                source.backup(destination)
                # Stored settings may otherwise enable external services and
                # carry credentials. This is exclusively the disposable copy.
                destination.execute("DELETE FROM setting")
                destination.commit()
            finally:
                destination.close()
                source.close()
        database = Database(database_path)
        database.initialize()
        if not options.snapshot:
            for number in range(60):
                manga_id = f"browser-{number:03}"
                database.upsert_manga(
                    {
                        "id": manga_id,
                        "provider": "local",
                        "title": f"Browser Series {number:03}",
                        "description": "A deterministic browser-test work.",
                        "authors": ["Test Author"],
                        "status": "ongoing",
                        "original_language": "ja",
                        "available_languages": ["en"],
                    },
                    "en",
                    "all",
                )
                database.upsert_chapters(
                    manga_id,
                    [
                        {
                            "id": f"{manga_id}-chapter-{chapter}",
                            "chapter": str(chapter),
                            "volume": None,
                            "title": f"Chapter {chapter}",
                            "language": "en",
                            "provider": "local",
                            "groups": [],
                            "publish_at": "2025-01-01T00:00:00+00:00",
                            "source_url": "",
                        }
                        for chapter in range(1, 101)
                    ],
                )
        # Artwork can be copied independently; never give the server access to
        # production books. Covers are optional in CI's synthetic dataset.
        if options.artwork_root:
            import shutil

            for relative in ("metadata/artwork", "metadata/thumbnails", "covers"):
                origin = options.artwork_root / relative
                if origin.is_dir():
                    shutil.copytree(origin, data / relative, dirs_exist_ok=True)

        settings = Settings(
            _env_file=None,
            data_dir=data,
            library_dir=library,
            frontend_dir=options.repo / "frontend" / "dist",
            monitor_enabled=False,
            wanted_search_enabled=False,
            metadata_enabled=False,
            suwayomi_enabled=False,
            internet_archive_enabled=False,
            prowlarr_enabled=False,
            komga_link_enabled=False,
            reader_kind="none",
            auth_username=None,
            auth_password=None,
            auth_required=False,
            preferred_unit="chapters",
            url_base=options.url_base,
        )
        app = create_app(settings)
        # Lifespan is disabled below: exercise the real rendering/API routes
        # without organization, downloading, reconciliation or remote probes.
        service = app.state.service
        service.last_deletion_recovery = {"recovery_blocked": False, "warnings": []}
        service.last_library_organization = service._organization_report()
        service.last_komga_library_alignment = {"ready": True, "configured": False}

        @app.middleware("http")
        async def disposable_api(request: Request, call_next):
            # Only metadata-only PATCH is needed for the search invalidation
            # test. Everything else remains a display/read-only benchmark.
            path = request.url.path
            if settings.url_base and path.startswith(f"{settings.url_base}/"):
                path = path[len(settings.url_base) :]
            metadata_patch = (
                request.method == "PATCH"
                and path.startswith("/api/manga/")
                and path.count("/") == 3
            )
            fixture_rename = (
                options.snapshot is None
                and request.method == "POST"
                and path.startswith("/api/manga/browser-")
                and path.endswith("/rename")
            )
            # Actual operator previews against disposable SQLite; no provider
            # is enabled and no acquisition/import/apply endpoint is allowed.
            operator_preview = request.method == "POST" and path in {
                "/api/acquisition/preview",
                "/api/library/list/preview",
                "/api/library/repair/preview",
                "/api/system/preflight",
            }
            if request.method not in {"GET", "HEAD"} and not (
                metadata_patch or fixture_rename or operator_preview
            ):
                return JSONResponse({"detail": "Disabled in browser fixture"}, 403)
            return await call_next(request)

        uvicorn.run(
            app,
            host="127.0.0.1",
            port=options.port,
            lifespan="off",
            access_log=False,
        )


if __name__ == "__main__":
    main()
