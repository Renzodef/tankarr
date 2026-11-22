import io
import zipfile

from PIL import Image, ImageDraw

from tankarr.translation_strip_audit import (
    StripScanStore,
    next_unscanned,
    scan_archive,
    strip_findings_by_series,
    strip_row,
)


def _artwork(width=900, height=1300):
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    for y in range(0, height, 40):
        draw.line((0, y, width, y + 200), fill=40, width=6)
    return image


def _with_strip(page):
    width, height = page.size
    image = Image.new("L", (width, height + 700), 255)
    image.paste(page, (0, 0))
    draw = ImageDraw.Draw(image)
    rule = height + 30
    draw.line((0, rule, width, rule), fill=200, width=1)
    for line in range(8):
        y = rule + 60 + line * 60
        draw.rectangle((40, y, 40 + 500 - line * 30, y + 14), fill=20)
    return image


def _bordered_panel():
    # A grey panel border with artwork below it is not a strip.
    image = Image.new("L", (900, 1300), 255)
    draw = ImageDraw.Draw(image)
    draw.line((0, 700, 900, 700), fill=200, width=1)
    for y in range(706, 1300, 18):
        draw.line((0, y, 900, y + 40), fill=30, width=7)
    return image


def test_strip_row_finds_translation_band_and_ignores_artwork():
    assert strip_row(_with_strip(_artwork())) is not None
    assert strip_row(_artwork()) is None
    assert strip_row(_bordered_panel()) is None
    assert strip_row(Image.new("L", (900, 1300), 255)) is None


def _cbz(path, pages):
    with zipfile.ZipFile(path, "w") as archive:
        for number, page in enumerate(pages, 1):
            data = io.BytesIO()
            page.save(data, "PNG")
            archive.writestr(f"{number:04d}.png", data.getvalue())


def test_scan_archive_reports_page_numbers(tmp_path):
    path = tmp_path / "volume.cbz"
    _cbz(
        path, [_artwork(), _with_strip(_artwork()), _artwork(), _with_strip(_artwork())]
    )
    assert scan_archive(path) == [2, 4]


def test_store_and_findings_group_by_series(tmp_path):
    store = StripScanStore(tmp_path / "strips.json")
    files = [
        {
            "id": "a",
            "manga_id": "s1",
            "volume": "1",
            "chapter": None,
            "release_unit": "volume",
            "library_sha256": "h1",
        },
        {
            "id": "b",
            "manga_id": "s1",
            "volume": "2",
            "chapter": None,
            "release_unit": "volume",
            "library_sha256": "h2",
        },
        {
            "id": "c",
            "manga_id": "s2",
            "volume": None,
            "chapter": "7",
            "release_unit": "chapter",
            "library_sha256": "h3",
        },
    ]
    assert next_unscanned(files, store)["id"] == "a"
    store.put("h1", [3, 9])
    store.put("h2", [])
    assert next_unscanned(files, store)["id"] == "c"
    reloaded = StripScanStore(tmp_path / "strips.json")
    assert strip_findings_by_series(files, reloaded) == {
        "s1": [{"chapter_id": "a", "unit": "volume", "number": "1", "pages": [3, 9]}]
    }


def test_series_findings_report_strip_volumes():
    from tankarr.metadata_audit import series_metadata_findings

    volumes = [{"chapter_id": "a", "unit": "volume", "number": "1", "pages": [2, 212]}]
    findings = series_metadata_findings(
        {"title": "Billy the Kid", "authors": [], "preferred_language": "en"},
        [],
        has_external_metadata=True,
        strip_volumes=volumes,
    )
    assert findings == [
        {
            "code": "translation_strip",
            "label": "Some pages still carry a translation strip at the bottom",
            "volumes": volumes,
        }
    ]
    assert (
        series_metadata_findings(
            {"title": "Billy the Kid", "authors": [], "preferred_language": "en"},
            [],
            has_external_metadata=True,
        )
        == []
    )


def _page_with_bottom_margin():
    # A normal page: artwork, a full-width panel edge, then a white margin
    # holding only a page number. It is not taller than a page.
    image = Image.new("L", (900, 1300), 255)
    draw = ImageDraw.Draw(image)
    for y in range(0, 1080, 30):
        draw.line((0, y, 900, y + 120), fill=40, width=6)
    draw.rectangle((0, 1083, 900, 1100), fill=255)
    draw.line((0, 1105, 900, 1105), fill=200, width=1)
    draw.rectangle((430, 1200, 470, 1215), fill=20)
    return image


def _spread_with_strip():
    spread = Image.new("L", (1800, 1300), 255)
    draw = ImageDraw.Draw(spread)
    for y in range(0, 1300, 40):
        draw.line((0, y, 1800, y + 200), fill=40, width=6)
    return _with_strip(spread)


def test_page_shape_rejects_margins_and_keeps_spreads():
    assert strip_row(_page_with_bottom_margin()) is None
    assert strip_row(_spread_with_strip()) is not None


def test_volume_where_every_page_has_a_strip(tmp_path):
    from tankarr.translation_strip_audit import volume_page_aspect

    # Every page lengthened by a strip of a different height: the rule height
    # repeats, so it still gives the original page shape.
    assert abs(volume_page_aspect([], [1.44] * 6 + [1.45]) - 1.44) < 0.02
    # A few ruled pages do not define the shape; plain pages do.
    assert volume_page_aspect([1.5] * 6, [0.8]) == 1.5
    pages = []
    for extra in (300, 500, 700, 400, 600, 350):
        page = _with_strip(_artwork())
        pages.append(page.crop((0, 0, page.width, 1300 + extra)))
    path = tmp_path / "all.cbz"
    _cbz(path, pages)
    assert scan_archive(path) == [1, 2, 3, 4, 5, 6]
