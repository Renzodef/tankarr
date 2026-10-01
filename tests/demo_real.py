"""Build the demo library from real catalogue records.

The online demo (docs site, /demo/) shows Tankarr on works people know. This
script adds well-known manga, manhwa and manhua by their MangaBaka identity
through Tankarr's own code path, "Add New" followed by the metadata
enrichment that fetches the covers and the catalogue data, so what the demo
shows is what an installation shows. Only the release history is invented:
a weekly cadence, a few recent chapters missing for the Wanted list, a short
queue. The one series with real pages is Pepper&Carrot by David Revoy,
published under CC BY 4.0 (https://www.peppercarrot.com), so the built-in
reader has books to open; no page of a licensed work is distributed.

Output layout is tests/demo_snapshot.py's: ``<output>/tankarr.sqlite3``,
``<output>/artwork`` (``--artwork-root``) and ``<output>/library``
(``--library-root``). Needs network access to MangaBaka and peppercarrot.com.

    .venv/bin/python tests/demo_real.py /tmp/tankarr-demo
"""

from __future__ import annotations

import argparse
import asyncio
import io
import re
import shutil
import sqlite3
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from PIL import Image  # noqa: E402

from tankarr.app import create_app  # noqa: E402
from tankarr.config import Settings  # noqa: E402

USER_AGENT = "Tankarr demo builder (+https://github.com/Renzodef/tankarr)"
PEPPER_SOURCES = "https://www.peppercarrot.com/0_sources/"
# The newest owned episodes carry their real pages; the two newest of all
# stay missing, for the Wanted list.
PEPPER_EPISODES_WITH_PAGES = 6
PEPPER_EPISODES_MISSING = 2
PEPPER_PAGE_WIDTH = 1000
MINIMUM_SERIES = 20

# (MangaBaka id, unit, recent releases missing). Manga, manhwa, manhua and an
# OEL webtoon, running and finished, so every state of the interface shows.
REAL_SERIES: list[tuple[str, str, int]] = [
    ("377", "chapters", 2),  # One Piece
    ("1692", "chapters", 3),  # Berserk
    ("1566", "chapters", 0),  # Vagabond
    ("3248", "chapters", 0),  # Vinland Saga
    ("10748", "volumes", 0),  # Monster
    ("1677", "chapters", 6),  # Chainsaw Man
    ("6199", "chapters", 1),  # Jujutsu Kaisen
    ("1995", "chapters", 4),  # Frieren
    ("4627", "chapters", 7),  # Dandadan
    ("1288", "chapters", 2),  # Spy x Family
    ("593", "chapters", 0),  # Oshi no Ko
    ("83", "chapters", 9),  # Blue Lock
    ("708", "chapters", 3),  # Kagurabachi
    ("13074", "volumes", 1),  # Slam Dunk
    ("1797", "chapters", 12),  # Kingdom
    ("8312", "volumes", 0),  # Fullmetal Alchemist
    ("677", "chapters", 5),  # Hunter x Hunter
    ("1331", "chapters", 0),  # Dorohedoro
    ("11139", "volumes", 0),  # Pluto
    ("6766", "chapters", 2),  # Yotsuba&!
    ("2553", "chapters", 1),  # Made in Abyss
    ("3397", "chapters", 0),  # Solo Leveling
    ("9999", "chapters", 8),  # Tower of God
    ("2060", "chapters", 4),  # Omniscient Reader
    ("1238", "chapters", 6),  # The Beginning After the End
    ("33900", "chapters", 0),  # Lore Olympus
    ("57", "chapters", 11),  # Eleceed
    ("1536", "chapters", 15),  # Lookism
    ("964", "chapters", 3),  # WIND BREAKER
    ("145", "chapters", 5),  # Nano Machine
    ("1746", "chapters", 4),  # Return of the Blossoming Blade
    ("2509", "chapters", 2),  # Tales of Demons and Gods
    ("1372", "chapters", 0),  # Soul Land
]


def safe_name(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|]+', "-", value).strip(" .")
    return cleaned or "Untitled"


def release_rows(
    manga_id: str,
    *,
    unit: str,
    total: int,
    status: str | None,
    published_end: str | None,
    index: int,
    today: datetime,
) -> list[dict]:
    """A plausible release history: weekly chapters, quarterly volumes."""

    ongoing = status == "ongoing"
    if ongoing:
        newest = today - timedelta(days=index % 7)
    elif published_end:
        try:
            newest = datetime.fromisoformat(published_end).replace(hour=9, tzinfo=UTC)
        except ValueError:
            newest = today - timedelta(days=400)
    else:
        newest = today - timedelta(days=400)
    announced = total + 1 if ongoing else total
    step = timedelta(weeks=1) if unit == "chapters" else timedelta(days=90)
    rows = []
    for number in range(1, announced + 1):
        published = newest - step * (total - number)
        if unit == "volumes":
            rows.append(
                {
                    "id": f"{manga_id}-v{number:03}",
                    "chapter": None,
                    "volume": str(number),
                    "title": f"Volume {number}",
                    "language": "en",
                    "provider": "local",
                    "groups": [],
                    "pages": 190 + (number * 13) % 40,
                    "publish_at": published.isoformat(),
                    "source_url": "",
                }
            )
        else:
            rows.append(
                {
                    "id": f"{manga_id}-c{number:04}",
                    "chapter": str(number),
                    "volume": str((number - 1) // 9 + 1) if not ongoing else None,
                    "title": f"Chapter {number}",
                    "language": "en",
                    "provider": "local",
                    "groups": ["Demo Scans"],
                    "pages": 18 + (number * 7) % 15,
                    "publish_at": published.isoformat(),
                    "source_url": "",
                }
            )
    return rows


async def pepper_episodes(http: httpx.AsyncClient) -> list[tuple[int, str, str]]:
    """(number, folder, title) of every published episode, from the sources index."""

    response = await http.get(PEPPER_SOURCES)
    response.raise_for_status()
    episodes = []
    for match in re.finditer(r'href="(ep(\d{2})_([^"/]+))/"', response.text):
        folder, number, slug = match.group(1), int(match.group(2)), match.group(3)
        episodes.append((number, folder, slug.replace("-", " ").replace("_", " ")))
    episodes.sort()
    return episodes


async def pepper_pages(
    http: httpx.AsyncClient, folder: str, number: int
) -> list[bytes]:
    """The English pages of one episode, downscaled for the demo."""

    pages: list[bytes] = []
    for page in range(0, 24):
        url = (
            f"{PEPPER_SOURCES}{folder}/low-res/"
            f"en_Pepper-and-Carrot_by-David-Revoy_E{number:02}P{page:02}.jpg"
        )
        response = await http.get(url)
        if response.status_code == 404:
            break
        response.raise_for_status()
        with Image.open(io.BytesIO(response.content)) as opened:
            image = opened.convert("RGB")
            if image.width > PEPPER_PAGE_WIDTH:
                image = image.resize(
                    (
                        PEPPER_PAGE_WIDTH,
                        round(image.height * PEPPER_PAGE_WIDTH / image.width),
                    )
                )
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=74, optimize=True)
        pages.append(buffer.getvalue())
    return pages


async def add_pepper_and_carrot(app, output: Path, today: datetime) -> str:
    database = app.state.database
    metadata = app.state.metadata
    manga_id = "pepper-and-carrot"
    title = "Pepper&Carrot"
    authors = ["David Revoy"]
    description = (
        "Pepper, a young witch, and her cat Carrot live in the world of Hereva, "
        "between potions, rival schools of magic and festivals. A free webcomic "
        "by David Revoy, published under the Creative Commons Attribution 4.0 "
        "licence at www.peppercarrot.com; the pages in this demo are his."
    )
    async with httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, timeout=60, follow_redirects=True
    ) as http:
        episodes = await pepper_episodes(http)
        if not episodes:
            raise RuntimeError("no Pepper&Carrot episodes found")
        cover = await http.get(
            f"{PEPPER_SOURCES}{episodes[0][1]}/low-res/"
            f"en_Pepper-and-Carrot_by-David-Revoy_E{episodes[0][0]:02}.jpg"
        )
        cover.raise_for_status()
        database.upsert_manga(
            {
                "id": manga_id,
                "provider": "local",
                "title": title,
                "description": description,
                "authors": authors,
                "status": "ongoing",
                "original_language": "fr",
                "available_languages": ["en"],
                "year": 2014,
            },
            "en",
            "all",
        )
        stored = metadata.artwork.store_bytes(
            manga_id, "series", cover.content, trusted_local=True
        )
        database.save_series_metadata(
            manga_id,
            {
                "title": title,
                "alternate_titles": ["Pepper & Carrot"],
                "description": description,
                "authors": authors,
                "creators": [
                    {"name": "David Revoy", "role": "writer"},
                    {"name": "David Revoy", "role": "artist"},
                ],
                "genres": ["fantasy", "comedy", "slice of life"],
                "tags": ["Witches", "Cats", "Free culture"],
                "publisher": None,
                "year": 2014,
                "status": "ongoing",
                "work_type": "Webcomic",
                "content_kind": "comic",
                "original_language": "fr",
                "translation_language": "en",
                "reading_direction": "ltr",
                "volume_count": None,
                "chapter_count": None,
                "rating": None,
                "external_ids": {},
                "links": [
                    {
                        "label": "peppercarrot.com",
                        "url": "https://www.peppercarrot.com/",
                    },
                    {
                        "label": "Licence CC BY 4.0",
                        "url": "https://creativecommons.org/licenses/by/4.0/",
                    },
                ],
                "provider_correlations": [],
                "official_links": [
                    {
                        "name": "Pepper&Carrot",
                        "url": "https://www.peppercarrot.com/",
                        "language": "en",
                        "type": "webplatform",
                    }
                ],
            },
            artwork_path=stored["path"],
            artwork_sha256=stored["sha256"],
            artwork_media_type=stored["media_type"],
            source_status=[{"source": "demo", "ok": True}],
        )
        rows = []
        for position, (number, _folder, episode_title) in enumerate(episodes):
            published = today - timedelta(days=20 + 45 * (len(episodes) - 1 - position))
            rows.append(
                {
                    "id": f"{manga_id}-c{number:03}",
                    "chapter": str(number),
                    "volume": None,
                    "title": f"Episode {number}: {episode_title}",
                    "language": "en",
                    "provider": "local",
                    "groups": [],
                    "pages": 8,
                    "publish_at": published.isoformat(),
                    "source_url": f"https://www.peppercarrot.com/en/webcomic/ep{number:02}_{_folder.split('_', 1)[1]}.html",
                }
            )
        database.upsert_chapters(manga_id, rows)
        folder_name = f"{safe_name(title)} ({', '.join(authors)})"
        owned = episodes[:-PEPPER_EPISODES_MISSING]
        with_pages = {
            number for number, _folder, _title in owned[-PEPPER_EPISODES_WITH_PAGES:]
        }
        for number, folder, _episode_title in owned:
            file_name = f"{safe_name(title)} - c{number:03} [en].cbz"
            database.mark_chapter_downloaded(
                f"{manga_id}-c{number:03}", Path("/library") / folder_name / file_name
            )
            if number in with_pages:
                pages = await pepper_pages(http, folder, number)
                if not pages:
                    continue
                destination = output / "library" / folder_name / file_name
                destination.parent.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(destination, "w", zipfile.ZIP_STORED) as archive:
                    for index, page in enumerate(pages, start=1):
                        archive.writestr(f"{index:03}.jpg", page)
                database.set_release_pages(f"{manga_id}-c{number:03}", len(pages))
    return manga_id


async def build(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    data = output / "artwork"
    library = output / "library"
    for path in (data, library):
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True)
    identity = "0123456789abcdef0123456789abcdef\n"
    (data / ".tankarr-library-id").write_text(identity)
    (library / ".tankarr-library-id").write_text(identity)
    settings = Settings(
        _env_file=None,
        data_dir=data,
        library_dir=library,
        frontend_dir=Path(__file__).resolve().parents[1] / "frontend" / "dist",
        metadata_enabled=True,
        monitor_enabled=False,
        wanted_search_enabled=False,
        suwayomi_enabled=False,
        internet_archive_enabled=False,
        prowlarr_enabled=False,
        komga_link_enabled=False,
        reader_kind="none",
        auth_username=None,
        auth_password=None,
        auth_required=False,
        preferred_unit="chapters",
    )
    app = create_app(settings)
    service = app.state.service
    database = app.state.database
    metadata = app.state.metadata
    service.last_deletion_recovery = {"recovery_blocked": False, "warnings": []}
    service.last_library_organization = service._organization_report()
    mangabaka = next(
        source for source in metadata.sources if source.name == "mangabaka"
    )
    today = datetime.now(UTC).replace(hour=9, minute=0, second=0, microsecond=0)

    added: list[tuple[str, str]] = []
    queued: list[tuple[str, str]] = []
    for index, (external_id, unit, missing) in enumerate(REAL_SERIES):
        try:
            record = await mangabaka.get_series(external_id)
            manga = await service.add_catalogue_series(
                record, "en", "all", series_unit=unit
            )
            manga_id = str(manga["id"])
            await asyncio.wait_for(
                metadata.enrich_series(manga_id, force=True), timeout=180
            )
            stored = database.get_series_metadata(manga_id) or {}
            canonical = stored.get("data") or {}
            status = canonical.get("status") or record.get("status")
            if unit == "volumes":
                total = int(
                    canonical.get("volume_count") or record.get("volume_count") or 0
                )
            else:
                total = int(
                    canonical.get("chapter_count")
                    or record.get("chapter_count")
                    or record.get("latest_release_chapter")
                    or 0
                )
            if total <= 0:
                raise RuntimeError("the catalogue has no release count")
            rows = release_rows(
                manga_id,
                unit=unit,
                total=total,
                status=status,
                published_end=record.get("published_end"),
                index=index,
                today=today,
            )
            database.upsert_chapters(manga_id, rows)
            owned = max(0, total - missing)
            title = str(manga.get("title") or record.get("title"))
            folder = f"{safe_name(title)} ({', '.join(record.get('authors') or ['Unknown'])})"
            for row in rows[:owned]:
                label = (
                    f"v{int(row['volume']):02}"
                    if unit == "volumes"
                    else f"c{int(row['chapter']):04}"
                )
                database.mark_chapter_downloaded(
                    row["id"],
                    Path("/library")
                    / safe_name(folder)
                    / f"{safe_name(title)} - {label} [en].cbz",
                )
            if missing and status == "ongoing" and len(queued) < 3:
                queued.append((manga_id, rows[owned]["id"]))
            sources = ", ".join(
                f"{item.get('source')}:{'ok' if item.get('ok') else 'x'}"
                for item in (stored.get("source_status") or [])
            )
            added.append((external_id, title))
            print(f"added {title} [{unit}, {total} total, {missing} missing] {sources}")
        except Exception as exc:  # noqa: BLE001 - one work must not stop the build
            print(f"skipped MangaBaka {external_id}: {type(exc).__name__}: {exc}")
    if len(added) < MINIMUM_SERIES:
        raise SystemExit(
            f"only {len(added)} series could be added; the demo needs {MINIMUM_SERIES}"
        )

    pepper = await add_pepper_and_carrot(app, output, today)
    print(f"added Pepper&Carrot ({pepper}) with pages")
    for manga_id, release_id in queued:
        database.create_job(manga_id, release_id, "en", origin="automatic")

    await metadata.aclose() if hasattr(metadata, "aclose") else None
    database.close()
    source = settings.database_path
    with sqlite3.connect(source) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    destination = output / "tankarr.sqlite3"
    destination.unlink(missing_ok=True)
    shutil.move(str(source), destination)
    for suffix in ("-wal", "-shm"):
        Path(f"{source}{suffix}").unlink(missing_ok=True)
    print(
        f"demo snapshot: {destination}\nartwork root: {data}\nlibrary root: {library}"
        f"\n{len(added) + 1} series"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("output", type=Path)
    asyncio.run(build(parser.parse_args().output))
