"""Build a fictional library for screenshots and demos.

Writes ``<output>/tankarr.sqlite3`` (a database snapshot for
``tests/browser_server.py --snapshot``) and ``<output>/artwork`` (its
``--artwork-root``: one drawn cover per series). Every title, author and
cover is invented, so nothing here is anyone's work; the shapes of the data
are real: running and finished works, chapters and books, a backlog, a
queue, releases dated around today for the Calendar.

    .venv/bin/python tests/demo_snapshot.py /tmp/tankarr-demo
    .venv/bin/python tests/browser_server.py --snapshot /tmp/tankarr-demo/tankarr.sqlite3 \\
        --artwork-root /tmp/tankarr-demo/artwork --port 18880
    npm run screenshots --prefix frontend   # writes docs/assets/screenshots/*.png
    .venv/bin/python tests/demo_snapshot.py --shrink docs/assets/screenshots  # -> .webp
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from tankarr.database import Database  # noqa: E402

FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")

# (id, title, authors, status, original language, year, unit, total, owned,
#  palette, description)
SERIES = [
    (
        "cartographers-daughter",
        "The Cartographer's Daughter",
        ["Mina Ferrand"],
        "ongoing",
        "ja",
        2021,
        "chapters",
        148,
        141,
        ("#0f2027", "#2c5364"),
        "Iris inherits a half-finished atlas and the enemies that come with it.",
    ),
    (
        "paper-lanterns",
        "Paper Lanterns Over Kagura",
        ["Sho Takemura"],
        "completed",
        "ja",
        2016,
        "chapters",
        96,
        96,
        ("#3a1c71", "#d76d77"),
        "A festival town, a missing lantern-maker and one long summer.",
    ),
    (
        "signal-lost",
        "Signal Lost",
        ["Hae-won Ryu"],
        "ongoing",
        "ko",
        2023,
        "chapters",
        87,
        80,
        ("#000428", "#004e92"),
        "The last radio operator on a drowned coast keeps answering.",
    ),
    (
        "hollow-season",
        "Hollow Season",
        ["Tomasz Wierzba", "Agata Nowak"],
        "ongoing",
        "en",
        2022,
        "chapters",
        61,
        55,
        ("#134e5e", "#71b280"),
        "Four seasons in a town where autumn refuses to end.",
    ),
    (
        "bakery-end-of-line",
        "Bakery at the End of the Line",
        ["Yumi Ohara"],
        "ongoing",
        "ja",
        2024,
        "chapters",
        34,
        34,
        ("#42275a", "#734b6d"),
        "Rye bread, tram schedules and the slow repair of a family.",
    ),
    (
        "iron-petal",
        "Iron Petal",
        ["Ren Kagami"],
        "completed",
        "ja",
        2011,
        "volumes",
        12,
        11,
        ("#232526", "#414345"),
        "A mecha epic told through the eyes of its mechanics.",
    ),
    (
        "midnight-tramline",
        "Midnight Tramline",
        ["Lucía Ortega"],
        "ongoing",
        "es",
        2020,
        "chapters",
        112,
        97,
        ("#141e30", "#243b55"),
        "Every night the 23 tram makes one stop that is not on the map.",
    ),
    (
        "salt-and-ember",
        "Salt & Ember",
        ["Nadia Kessler"],
        "hiatus",
        "de",
        2019,
        "chapters",
        73,
        73,
        ("#870000", "#190a05"),
        "Two rival lighthouse keepers, one storm season.",
    ),
    (
        "ninth-archive",
        "The Ninth Archive",
        ["Pyotr Malinin"],
        "ongoing",
        "ru",
        2022,
        "chapters",
        58,
        44,
        ("#1f4037", "#99f2c8"),
        "Librarians who catalogue things that should not exist.",
    ),
    (
        "kestrel-days",
        "Kestrel Days",
        ["Ji-ho Baek"],
        "completed",
        "ko",
        2018,
        "volumes",
        8,
        8,
        ("#8e2de2", "#4a00e0"),
        "A falconry school, three friends and one impossible bird.",
    ),
    (
        "moth-kingdom",
        "Moth Kingdom",
        ["Elin Sørby"],
        "ongoing",
        "en",
        2023,
        "chapters",
        29,
        21,
        ("#373b44", "#4286f4"),
        "The insects of an abandoned greenhouse build a court.",
    ),
    (
        "rooftop-astronomy-club",
        "Rooftop Astronomy Club",
        ["Kaito Miyashiro"],
        "ongoing",
        "ja",
        2025,
        "chapters",
        18,
        18,
        ("#0f0c29", "#302b63"),
        "Five students, a broken telescope and a city that never gets dark.",
    ),
    (
        "clockwork-orchard",
        "Clockwork Orchard",
        ["Beatriz Lemos"],
        "completed",
        "pt",
        2014,
        "volumes",
        6,
        6,
        ("#5a3f37", "#2c7744"),
        "The gardener who keeps time for a valley that forgot how.",
    ),
    (
        "harbour-of-glass",
        "Harbour of Glass",
        ["Aoi Nishikawa"],
        "ongoing",
        "ja",
        2021,
        "chapters",
        203,
        188,
        ("#1e3c72", "#2a5298"),
        "A port city rebuilt from the sea's own wreckage.",
    ),
]

TAGS = {
    "cartographers-daughter": ["Adventure", "Mystery"],
    "paper-lanterns": ["Slice of life", "Drama"],
    "signal-lost": ["Science fiction", "Thriller"],
    "hollow-season": ["Fantasy", "Mystery"],
    "bakery-end-of-line": ["Slice of life", "Comedy"],
    "iron-petal": ["Mecha", "Action"],
    "midnight-tramline": ["Supernatural", "Drama"],
    "salt-and-ember": ["Drama", "Romance"],
    "ninth-archive": ["Horror", "Mystery"],
    "kestrel-days": ["Sports", "Coming of age"],
    "moth-kingdom": ["Fantasy", "Comedy"],
    "rooftop-astronomy-club": ["Slice of life", "School"],
    "clockwork-orchard": ["Fantasy", "Historical"],
    "harbour-of-glass": ["Adventure", "Fantasy"],
}


def _hex(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return tuple(int(color[index : index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]


def draw_cover(title: str, authors: list[str], palette: tuple[str, str]) -> Image.Image:
    width, height = 600, 900
    top, bottom = _hex(palette[0]), _hex(palette[1])
    image = Image.new("RGB", (width, height), top)
    draw = ImageDraw.Draw(image)
    for y in range(height):
        blend = y / (height - 1)
        draw.line(
            [(0, y), (width, y)],
            fill=tuple(
                round(top[i] * (1 - blend) + bottom[i] * blend) for i in range(3)
            ),
        )
    # A few translucent shapes so the covers do not all look alike.
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    shapes = ImageDraw.Draw(overlay)
    seed = int(hashlib.sha256(title.encode()).hexdigest()[:8], 16)
    for index in range(3):
        radius = 140 + (seed >> (index * 5)) % 160
        x = (seed >> (index * 7)) % width
        y = 220 + (seed >> (index * 3)) % 500
        shapes.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=(255, 255, 255, 18 + index * 8),
        )
    image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.truetype(str(FONT_DIR / "DejaVuSans-Bold.ttf"), 58)
    author_font = ImageFont.truetype(str(FONT_DIR / "DejaVuSans.ttf"), 30)
    words = title.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=title_font) > width - 90 and current:
            lines.append(current)
            current = word
        else:
            current = candidate
    lines.append(current)
    y = 96
    for line in lines:
        draw.text((46, y), line, font=title_font, fill=(255, 255, 255))
        y += 70
    draw.line([(46, y + 16), (200, y + 16)], fill=(255, 255, 255), width=3)
    draw.text(
        (46, height - 96), ", ".join(authors), font=author_font, fill=(235, 235, 235)
    )
    return image


def build(output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    artwork_root = output / "artwork" / "metadata" / "artwork"
    database_path = output / "tankarr.sqlite3"
    if database_path.exists():
        database_path.unlink()
    database = Database(database_path)
    database.initialize()
    today = datetime.now(UTC).replace(hour=9, minute=0, second=0, microsecond=0)
    for index, (
        identifier,
        title,
        authors,
        status,
        language,
        year,
        unit,
        total,
        owned,
        palette,
        description,
    ) in enumerate(SERIES):
        database.upsert_manga(
            {
                "id": identifier,
                "provider": "local",
                "title": title,
                "description": description,
                "authors": authors,
                "status": status,
                "original_language": language,
                "available_languages": ["en"],
                "year": year,
            },
            "en",
            "all" if status != "completed" or owned < total else "existing",
        )
        # A weekly cadence per series, the newest chapter within the last
        # week and, for a running work, the next one already announced by
        # its publisher: that is what fills the Calendar's current week.
        newest = today - timedelta(days=index % 7)
        announced = total + 1 if status == "ongoing" else total
        releases = []
        for number in range(1, announced + 1):
            published = newest - timedelta(weeks=total - number)
            if unit == "volumes":
                releases.append(
                    {
                        "id": f"{identifier}-v{number:02}",
                        "chapter": None,
                        "volume": str(number),
                        "title": f"Volume {number}",
                        "language": "en",
                        "provider": "local",
                        "groups": [],
                        "pages": 190,
                        "publish_at": published.isoformat(),
                        "source_url": f"https://reader.example.com/{identifier}/v{number}",
                    }
                )
            else:
                releases.append(
                    {
                        "id": f"{identifier}-c{number:03}",
                        "chapter": str(number),
                        "volume": str((number - 1) // 9 + 1)
                        if status == "completed"
                        else None,
                        "title": f"Chapter {number}",
                        "language": "en",
                        "provider": "local",
                        "groups": ["Demo Scans"],
                        "pages": 18 + (number * 7) % 15,
                        "publish_at": published.isoformat(),
                        "source_url": f"https://reader.example.com/{identifier}/{number}",
                    }
                )
        database.upsert_chapters(identifier, releases)
        cover = draw_cover(title, authors, palette)
        cover_dir = artwork_root / identifier
        cover_dir.mkdir(parents=True, exist_ok=True)
        cover_path = cover_dir / "series.png"
        cover.save(cover_path, optimize=True)
        digest = hashlib.sha256(cover_path.read_bytes()).hexdigest()
        database.save_series_metadata(
            identifier,
            {
                "title": title,
                "authors": authors,
                "year": year,
                "status": status,
                "description": description,
                "genres": TAGS[identifier],
                # The publisher's own platform, on a documentation domain:
                # its dates are what the Calendar shows as upcoming.
                "official_links": [
                    {
                        "url": f"https://reader.example.com/{identifier}",
                        "language": "en",
                    }
                ],
                "chapter_count": total if unit == "chapters" else None,
                "volume_count": total if unit == "volumes" else None,
            },
            artwork_path=str(cover_path.relative_to(output / "artwork")),
            artwork_sha256=digest,
            artwork_media_type="image/png",
            source_status=[{"source": "demo", "ok": True}],
        )
        folder = f"{title} ({', '.join(authors)})"
        for release in releases[:owned]:
            label = (
                f"v{int(release['volume']):02}"
                if unit == "volumes"
                else f"c{int(release['chapter']):03}"
            )
            database.mark_chapter_downloaded(
                release["id"], Path("/library") / folder / f"{title} - {label} [en].cbz"
            )
    # A short queue for the Activity page.
    for identifier, release_id in (
        ("harbour-of-glass", "harbour-of-glass-c189"),
        ("midnight-tramline", "midnight-tramline-c098"),
        ("ninth-archive", "ninth-archive-c045"),
    ):
        database.create_job(identifier, release_id, "en", origin="automatic")
    database.close()
    print(f"demo snapshot: {database_path}\nartwork root: {output / 'artwork'}")


def shrink(directory: Path) -> None:
    """Convert the captured PNG files to WebP: a fifth of the bytes, no banding.

    A 256-colour palette would band the cover gradients; lossy WebP at this
    quality keeps text crisp and gradients smooth.
    """

    for path in sorted(directory.glob("*.png")):
        before = path.stat().st_size
        target = path.with_suffix(".webp")
        Image.open(path).convert("RGB").save(target, quality=88, method=6)
        path.unlink()
        print(
            f"{target.name}: {before // 1024} KiB -> {target.stat().st_size // 1024} KiB"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "output", type=Path, help="snapshot directory, or the screenshots with --shrink"
    )
    parser.add_argument(
        "--shrink", action="store_true", help="quantise the PNG files in OUTPUT instead"
    )
    arguments = parser.parse_args()
    if arguments.shrink:
        shrink(arguments.output)
    else:
        build(arguments.output)
