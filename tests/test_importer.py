from __future__ import annotations

import asyncio
import io
import json
import shutil
import time
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import tankarr.importer as importer_module
from tankarr.app import create_app
from tankarr.archive import package_cbz, sha256, validate_cbz
from tankarr.config import Settings
from tankarr.database import Database
from tankarr.importer import (
    LibraryImporter,
    infer_numbered_name,
    natural_key,
    split_author_title,
)
from tankarr.naming import final_library_path
from tankarr.service import (
    ExternalImportConflict,
    TankarrService,
    local_page_content_sha256,
)

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
)


def provision_library_identity(settings: Settings) -> None:
    identity = "0123456789abcdef0123456789abcdef"
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.library_dir.mkdir(parents=True, exist_ok=True)
    (settings.data_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )
    (settings.library_dir / ".tankarr-library-id").write_text(
        f"{identity}\n", encoding="utf-8"
    )


def make_volume_zip(path: Path, pages: int = 3, payload: bytes = PNG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        for index in range(1, pages + 1):
            archive.writestr(f"p{index:03d}.png", payload)


def make_pdf(path: Path, pages: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    images = [
        Image.new("RGB", (64, 96), (240 - index * 10, 240, 240))
        for index in range(pages)
    ]
    try:
        images[0].save(path, "PDF", save_all=True, append_images=images[1:])
    finally:
        for image in images:
            image.close()


def wait_for_import(client: TestClient) -> dict:
    state: dict = {}
    for _ in range(100):
        state = client.get("/api/import/status").json()
        if state.get("finished"):
            break
        time.sleep(0.1)
    assert state.get("finished") is True
    # `finished` is set in the task's finally block. Let the task itself leave
    # the event loop before starting the next import in the same test.
    time.sleep(0.02)
    return state


def wait_until_ready(client: TestClient, timeout: float = 30.0) -> None:
    """Startup recovery runs in the background after create_app; an import
    posted before it finishes is refused with 503 (issue #6)."""

    deadline = time.monotonic() + timeout
    while client.get("/api/system/ready").status_code != 200:
        assert time.monotonic() < deadline, "the application never became ready"
        time.sleep(0.05)


def import_group(client: TestClient, title: str, language: str = "en") -> dict:
    wait_until_ready(client)
    scan = client.get("/api/import/scan").json()
    group = next(group for group in scan["groups"] if group["title"] == title)
    response = client.post(
        "/api/import", json={"groups": [group], "language": language}
    )
    assert response.status_code == 202
    return wait_for_import(client)


def build_import_tree(root: Path) -> None:
    series_dir = root / "Manga Series" / "Author Name - Example Saga"
    make_volume_zip(series_dir / "Author Name - Example Saga 1.zip")
    make_volume_zip(series_dir / "Author Name - Example Saga 2.cbz")
    chapter_dir = root / "Manga Series" / "Keiko - Light" / "Chapter 2.5 _ Extras"
    chapter_dir.mkdir(parents=True)
    for index in range(1, 4):
        (chapter_dir / f"{index:02d}.png").write_bytes(PNG)
    make_volume_zip(root / "Singles" / "Osamu - One Shot.zip")
    (root / "Singles" / "Ignored.pdf").write_bytes(b"%PDF-1.4")
    (root / "Singles" / ".DS_Store").write_bytes(b"junk")
    junk = root / "Tool" / "lib"
    junk.mkdir(parents=True)
    (junk / "app.jar").write_bytes(b"PK")


def test_importer_purges_only_stale_private_workspaces(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    settings.ensure_directories()
    stale = [
        settings.staging_dir / name
        for name in (
            "import-interrupted",
            "nyaa-import-interrupted",
            "torrent-import-interrupted",
            "job-12-interrupted",
            "manual-import-interrupted",
            "assemble-interrupted",
        )
    ]
    unrelated = settings.staging_dir / "user-kept"
    for directory in (*stale, unrelated):
        directory.mkdir()
        (directory / "payload").write_bytes(b"temporary")

    LibraryImporter(settings, object(), object())  # type: ignore[arg-type]

    assert [directory.name for directory in stale if directory.exists()] == []
    assert (unrelated / "payload").read_bytes() == b"temporary"


def test_helpers_parse_names_and_numbers():
    assert natural_key("v2") < natural_key("v10")
    assert split_author_title("Osamu Tezuka - Dororo") == (["Osamu Tezuka"], "Dororo")
    assert split_author_title("Minami q-ta - Not All Girls are Stupid") == (
        ["Minami q-ta"],
        "Not All Girls are Stupid",
    )
    assert split_author_title("Secret Comics Japan") == ([], "Secret Comics Japan")


def test_archive_import_keeps_main_chapter_before_decimal_extras(tmp_path: Path):
    source = tmp_path / "chapters.cbz"
    names = [
        "ch4.1/page10.png",
        "ch4.12/page2.png",
        "ch004/page10.png",
        "ch4.2/page2.png",
        "ch004/page2.png",
        "ch4.1/page2.png",
    ]
    expected = [names[index] for index in (4, 2, 5, 0, 1, 3)]
    payloads = {}
    for index, name in enumerate(names):
        with Image.new("RGB", (4, 4), (index * 30, 40, 60)) as image:
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            payloads[name] = buffer.getvalue()
    with zipfile.ZipFile(source, "w") as archive:
        for name in names:
            archive.writestr(name, payloads[name])
    settings = Settings(data_dir=tmp_path / "data", library_dir=tmp_path / "library")
    importer = LibraryImporter(settings, object(), object())
    pages = importer._extract_pages(source, tmp_path / "pages")

    assert [page.read_bytes() for page in pages] == [payloads[n] for n in expected]
    assert importer._fast_source_content_sha256(source) == local_page_content_sha256(
        pages
    )
    managed = tmp_path / "managed.cbz"
    reversed_managed = tmp_path / "wrong-order.cbz"
    for path, order in [
        (managed, expected),
        (reversed_managed, list(reversed(expected))),
    ]:
        with zipfile.ZipFile(path, "w") as archive:
            for index, name in enumerate(order, 1):
                archive.writestr(f"{index:04d}.png", payloads[name])
    assert importer._matching_archive_page_signature(source, managed)
    assert not importer._matching_archive_page_signature(source, reversed_managed)


@pytest.mark.parametrize(
    "names",
    [
        ["page1.10.png", "page1.2.png", "page1.png", "page10.png", "page2.png"],
        ["root/ch2/page10.png", "root/ch2/page2.png", "root/ch10/page1.png"],
    ],
)
def test_archive_page_order_preserves_non_decimal_folder_order(names):
    assert sorted(names, key=importer_module.archive_page_key) == sorted(
        names, key=natural_key
    )


def test_rar_page_signature_keeps_main_chapter_before_decimal_extras(
    tmp_path: Path, monkeypatch
):
    archive = tmp_path / "chapters.cbr"
    archive.write_bytes(b"Rar!\x1a\x07\x00")
    payload = {
        "lsarContents": [
            {"XADFileName": "ch4.1/0001.jpg", "XADFileSize": 10, "RARCRC32": 110},
            {"XADFileName": "ch004/0001.jpg", "XADFileSize": 20, "RARCRC32": 220},
        ]
    }

    class Result:
        returncode = 0
        stdout = json.dumps(payload)

    monkeypatch.setattr(
        importer_module.shutil,
        "which",
        lambda name: "/usr/bin/lsar" if name == "lsar" else None,
    )
    monkeypatch.setattr(importer_module.subprocess, "run", lambda *_a, **_k: Result())

    assert LibraryImporter._rar_page_signature(archive) == ((20, 220), (10, 110))


def test_zip_fast_path_rejects_a_rar_with_a_cbz_extension(tmp_path: Path):
    disguised_rar = tmp_path / "disguised.cbz"
    disguised_rar.write_bytes(b"Rar!\x1a\x07\x00" + b"not-a-zip")
    managed = tmp_path / "managed.cbz"
    make_volume_zip(managed)

    assert (
        LibraryImporter._matching_archive_page_signature(disguised_rar, managed)
        is False
    )


def test_rar_page_signature_uses_sorted_image_crc_index(tmp_path: Path, monkeypatch):
    archive = tmp_path / "nested.cbr"
    archive.write_bytes(b"Rar!\x1a\x07\x00")
    payload = {
        "lsarContents": [
            {"XADFileName": "root/p10.jpg", "XADFileSize": 10, "RARCRC32": 110},
            {"XADFileName": "root/readme.txt", "XADFileSize": 1, "RARCRC32": 1},
            {"XADFileName": "root/p2.png", "XADFileSize": 20, "RARCRC32": 220},
        ]
    }

    class Result:
        returncode = 0
        stdout = json.dumps(payload)

    monkeypatch.setattr(
        importer_module.shutil,
        "which",
        lambda name: "/usr/bin/lsar" if name == "lsar" else None,
    )
    monkeypatch.setattr(importer_module.subprocess, "run", lambda *_a, **_k: Result())

    assert LibraryImporter._rar_page_signature(archive) == ((20, 220), (10, 110))


def test_scan_groups_series_singles_and_chapter_folders(tmp_path: Path):
    import_root = tmp_path / "incoming"
    build_import_tree(import_root)
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        import_dir=import_root,
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    with TestClient(create_app(settings)) as client:
        scan = client.get("/api/import/scan")
        assert scan.status_code == 200
        payload = scan.json()
        titles = {group["title"]: group for group in payload["groups"]}
        assert titles["Example Saga"]["authors"] == ["Author Name"]
        assert [item["volume"] for item in titles["Example Saga"]["items"]] == [
            "1",
            "2",
        ]
        assert titles["One Shot"]["items"][0]["volume"] == "1"
        chapter_group = titles["Light"]
        assert chapter_group["items"][0]["chapter"] == "2.5"
        assert chapter_group["items"][0]["chapter_title"] == "Extras"
        assert any("PDF" in item["reason"] for item in payload["skipped"])
        assert all("jar" not in group["title"].lower() for group in payload["groups"])


def test_manual_import_preserves_source_and_original_language(tmp_path: Path):
    import_root = tmp_path / "incoming"
    make_volume_zip(import_root / "Marine Blue - v001 [en].cbz")
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        import_dir=import_root,
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    with TestClient(create_app(settings)) as client:
        group = client.get("/api/import/scan").json()["groups"][0]
        group.update(
            {
                "title": "Marine Blue",
                "authors": ["Ai Yazawa"],
                "source_provider": "example-scan",
                "source_url": "https://example.test/catalogue/marine-blue/",
                "original_language": "fr",
            }
        )
        group["items"][0].update(
            {
                "source_provider": "example-scan",
                "source_url": "https://example.test/marine-blue-volume-1/",
            }
        )
        response = client.post(
            "/api/import", json={"groups": [group], "language": "en"}
        )
        assert response.status_code == 202
        assert wait_for_import(client)["errors"] == []

        manga = next(
            item
            for item in client.get("/api/manga").json()
            if item["title"] == "Marine Blue"
        )
        detail = client.get(f"/api/manga/{manga['id']}").json()
        chapter = detail["chapters"][0]
        assert detail["source_url"] == ("https://example.test/catalogue/marine-blue/")
        assert detail["original_language"] == "fr"
        assert chapter["provider"] == "local"
        assert chapter["groups"] == ["example-scan"]
        assert chapter["source_url"] == ("https://example.test/marine-blue-volume-1/")
        with zipfile.ZipFile(chapter["library_path"]) as archive:
            comic_info = archive.read("ComicInfo.xml").decode()
        assert "Downloaded via Tankarr from example-scan" in comic_info
        assert "<Web>https://example.test/marine-blue-volume-1/</Web>" in comic_info


def test_manual_archive_parts_attach_to_an_existing_remote_series(
    tmp_path: Path, monkeypatch
):
    import_root = tmp_path / "incoming"
    archive = import_root / "Rebirth volume 22.zip"
    make_volume_zip(archive, pages=6)
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        import_dir=import_root,
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    monkeypatch.setattr(
        importer_module,
        "audit_english_pages",
        lambda _pages, **_kwargs: {
            "verdict": "confirmed",
            "reason": "test pages",
        },
    )
    app = create_app(settings)
    app.state.database.upsert_manga(
        {
            "id": "rebirth",
            "provider": "mangadex",
            "title": "Rebirth",
            "description": "",
            "cover_url": None,
            "authors": ["Lee Kang-Woo"],
            "original_language": "ko",
            "status": "completed",
            "last_volume": "26",
            "last_chapter": "107",
            "available_languages": ["en"],
            "source_url": "https://mangadex.org/title/rebirth",
        },
        "en",
        "existing",
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/manga/rebirth/manual-import",
            json={
                "path": archive.name,
                "language": "en",
                "source_url": "https://www.dropbox.com/rebirth-volume-22",
                "source_name": "Dropbox",
                "confirm_language": False,
                "parts": [
                    {
                        "volume": "22",
                        "chapter": "89",
                        "title": "In Motion",
                        "page_start": 1,
                        "page_end": 3,
                    },
                    {
                        "volume": "22",
                        "chapter": "90",
                        "title": "An End to Preparations",
                        "page_start": 4,
                        "page_end": 6,
                    },
                ],
            },
        )

        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["books"] == 2
        assert payload["reader_independent"] is True
        assert archive.is_file()
        detail = client.get("/api/manga/rebirth").json()
        imported = [item for item in detail["chapters"] if item["provider"] == "manual"]
        assert [(item["volume"], item["chapter"]) for item in imported] == [
            ("22", "89"),
            ("22", "90"),
        ]
        assert all(item["downloaded"] is True for item in imported)
        assert all(item["groups"] == ["Dropbox"] for item in imported)
        assert all(Path(item["library_path"]).is_file() for item in imported)
        assert "v022 c089" in Path(imported[0]["library_path"]).name
        with zipfile.ZipFile(imported[0]["library_path"]) as packaged:
            comic_info = packaged.read("ComicInfo.xml").decode()
        assert "Downloaded via Tankarr from Dropbox" in comic_info
        assert "<Web>https://www.dropbox.com/rebirth-volume-22</Web>" in comic_info


@pytest.mark.parametrize(
    ("filename", "expected_status"),
    [
        ("Example volume 1 [en].zip", 201),
        ("Example volume 1 [ja].zip", 409),
    ],
)
def test_manual_language_confirmation_overrides_ocr_script_false_positive(
    tmp_path: Path, monkeypatch, filename: str, expected_status: int
):
    import_root = tmp_path / "incoming"
    archive = import_root / filename
    make_volume_zip(archive, pages=6)
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        import_dir=import_root,
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    monkeypatch.setattr(
        importer_module,
        "audit_english_pages",
        lambda _pages, **_kwargs: {
            "verdict": "refused",
            "foreign_script_detected": True,
            "reason": "OCR misidentified lettering in artwork",
        },
    )
    app = create_app(settings)
    app.state.database.upsert_manga(
        {
            "id": "example",
            "provider": "mangadex",
            "title": "Example",
            "description": "",
            "cover_url": None,
            "authors": [],
            "original_language": "ja",
            "status": "completed",
            "last_volume": "1",
            "last_chapter": None,
            "available_languages": ["en"],
            "source_url": "https://mangadex.org/title/example",
        },
        "en",
        "existing",
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/manga/example/manual-import",
            json={
                "path": archive.name,
                "language": "en",
                "source_url": "https://mangadex.org/chapter/example",
                "source_name": "MangaDex English scanlation",
                "confirm_language": True,
                "parts": [
                    {
                        "volume": "1",
                        "page_start": 1,
                        "page_end": 6,
                    }
                ],
            },
        )

    assert response.status_code == expected_status, response.text
    if expected_status == 201:
        assert response.json()["language_evidence"]["manual_confirmation"] is True
        assert response.json()["books"] == 1


def test_browser_upload_imports_a_volume_into_the_selected_series(tmp_path: Path):
    selected = tmp_path / "selected.cbz"
    make_volume_zip(selected)
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        import_dir=None,
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    app = create_app(settings)
    app.state.database.upsert_manga(
        {
            "id": "selected-series",
            "provider": "catalogue",
            "source_id": "selected-series",
            "title": "Selected Series",
            "description": "",
            "cover_url": None,
            "authors": ["Example Author"],
            "original_language": "ja",
            "status": "completed",
            "last_volume": "3",
            "last_chapter": "12",
            "available_languages": ["en"],
            "source_url": "https://example.test/selected-series",
        },
        "en",
        "existing",
    )
    app.state.database.update_manga(
        "selected-series", {"series_unit_override": "chapters"}
    )

    with TestClient(app) as client:
        summary = next(
            item
            for item in client.get("/api/manga").json()
            if item["id"] == "selected-series"
        )
        assert summary["effective_series_unit"] == "chapters"
        upload_id = client.post("/api/import/uploads").json()["upload_id"]
        uploaded = client.put(
            f"/api/import/uploads/{upload_id}/files",
            params={"path": "Selected Series v03.cbz"},
            content=selected.read_bytes(),
            headers={"Content-Type": "application/vnd.comicbook+zip"},
        )
        assert uploaded.status_code == 201, uploaded.text

        scan = client.get(f"/api/import/uploads/{upload_id}/scan")
        assert scan.status_code == 200, scan.text
        group = scan.json()["groups"][0]
        group.update(
            {
                "upload_id": upload_id,
                "target_manga_id": "selected-series",
                "language": "en",
                "unit": "volumes",
                "set_series_unit": True,
            }
        )
        group["items"][0].update({"volume": "3", "chapter": None})
        started = client.post("/api/import", json={"groups": [group], "language": "en"})
        assert started.status_code == 202, started.text
        assert wait_for_import(client)["errors"] == []

        detail = client.get("/api/manga/selected-series").json()
        imported = [item for item in detail["chapters"] if item["provider"] == "manual"]
        assert len(imported) == 1
        assert imported[0]["volume"] == "3"
        assert imported[0]["chapter"] is None
        assert imported[0]["release_unit"] == "volume"
        assert Path(imported[0]["library_path"]).is_file()
        assert "v003" in Path(imported[0]["library_path"]).name
        assert detail["series_unit_override"] == "volumes"
        assert all(
            item["provider"] != "local" for item in client.get("/api/manga").json()
        )


def test_browser_upload_rejects_paths_outside_its_private_session(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    with TestClient(create_app(settings)) as client:
        upload_id = client.post("/api/import/uploads").json()["upload_id"]
        response = client.put(
            f"/api/import/uploads/{upload_id}/files",
            params={"path": "../outside.cbz"},
            content=b"not an archive",
        )

        assert response.status_code == 400
        assert not (settings.data_dir / "outside.cbz").exists()


@pytest.mark.skipif(
    not shutil.which("pdfinfo") or not shutil.which("pdftoppm"),
    reason="Poppler tools are not installed",
)
def test_pdf_is_normalized_to_a_managed_cbz(tmp_path: Path):
    import_root = tmp_path / "incoming"
    make_pdf(import_root / "Singles" / "Creator - PDF Comic.pdf", pages=2)
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        import_dir=import_root,
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    with TestClient(create_app(settings)) as client:
        scan = client.get("/api/import/scan").json()
        group = next(item for item in scan["groups"] if item["title"] == "PDF Comic")
        assert group["items"][0]["images"] == 2
        response = client.post(
            "/api/import", json={"groups": [group], "language": "en"}
        )
        assert response.status_code == 202
        state = wait_for_import(client)
        assert state["errors"] == []

        manga = next(
            item
            for item in client.get("/api/manga").json()
            if item["title"] == "PDF Comic"
        )
        chapter = client.get(f"/api/manga/{manga['id']}").json()["chapters"][0]
        assert Path(chapter["library_path"]).suffix == ".cbz"
        assert Path(chapter["library_path"]).is_file()


def test_interrupted_import_manifest_is_resumed_after_restart(tmp_path: Path):
    import_root = tmp_path / "incoming"
    make_volume_zip(import_root / "Singles" / "Creator - Resume Me.cbz")
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        import_dir=import_root,
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    first_app = create_app(settings)
    with TestClient(first_app) as client:
        group = client.get("/api/import/scan").json()["groups"][0]

        async def block_import(*_args, **_kwargs):
            await asyncio.Event().wait()

        first_app.state.importer._import_item = block_import
        assert (
            client.post(
                "/api/import", json={"groups": [group], "language": "en"}
            ).status_code
            == 202
        )
        for _ in range(100):
            state = client.get("/api/import/status").json()
            if state.get("current"):
                break
            time.sleep(0.01)
        assert state["running"] is True

    persisted = json.loads(settings.import_operation_path.read_text(encoding="utf-8"))
    assert persisted["state"]["finished"] is False
    assert persisted["state"]["resumable"] is True

    with TestClient(create_app(settings)) as client:
        state = wait_for_import(client)
        assert state["finished"] is True
        assert state["imported"] == 1
        manga = next(
            item
            for item in client.get("/api/manga").json()
            if item["title"] == "Resume Me"
        )
        assert manga["downloaded_count"] == 1


def test_import_creates_local_series_with_files_and_cover(tmp_path: Path):
    import_root = tmp_path / "incoming"
    build_import_tree(import_root)
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        import_dir=import_root,
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    with TestClient(create_app(settings)) as client:
        scan = client.get("/api/import/scan").json()
        group = next(g for g in scan["groups"] if g["title"] == "Example Saga")
        started = client.post(
            "/api/import",
            json={"groups": [group], "language": "en"},
        )
        assert started.status_code == 202
        for _ in range(100):
            state = client.get("/api/import/status").json()
            if state.get("finished"):
                break
            time.sleep(0.1)
        assert state["finished"] is True
        assert state["errors"] == []
        assert state["imported"] == 2

        manga_list = client.get("/api/manga").json()
        local = next(m for m in manga_list if m["provider"] == "local")
        assert local["title"] == "Example Saga"
        assert local["downloaded_count"] == 2
        assert local["monitor_mode"] == "none"
        assert local["cover_url"].startswith("/api/covers/local/")

        cover = client.get(local["cover_url"])
        assert cover.status_code == 200
        assert cover.headers["content-type"].startswith("image/")

        detail = client.get(f"/api/manga/{local['id']}").json()
        chapters = detail["chapters"]
        assert len(chapters) == 2
        for chapter in chapters:
            assert chapter["downloaded"] is True
            assert chapter["library_path"] is not None
            assert Path(chapter["library_path"]).exists()
            assert "v00" in Path(chapter["library_path"]).name

        # Local series are skipped by refresh instead of failing.
        refresh = client.post(f"/api/manga/{local['id']}/refresh")
        assert refresh.status_code == 200
        assert refresh.json()["seen"] == 0


def test_reimport_skips_an_occupied_managed_release_without_repacking(
    tmp_path: Path,
):
    import_root = tmp_path / "incoming"
    make_volume_zip(import_root / "Singles" / "Creator - Managed Once.cbz")
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        import_dir=import_root,
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    app = create_app(settings)
    with TestClient(app) as client:
        first = import_group(client, "Managed Once")
        assert first["imported"] == 1
        assert first["reused"] == 0

        def unexpected_repack(*_args, **_kwargs):
            raise AssertionError("an occupied managed release must not be repacked")

        app.state.importer._extract_pages = unexpected_repack
        second = import_group(client, "Managed Once")

        assert second["errors"] == []
        assert second["imported"] == 1
        assert second["reused"] == 1


def test_reimport_reuses_series_chapters_and_exact_cbz_files(tmp_path: Path):
    import_root = tmp_path / "incoming"
    build_import_tree(import_root)
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        import_dir=import_root,
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    with TestClient(create_app(settings)) as client:
        first = import_group(client, "Example Saga")
        assert first["errors"] == []
        assert first["reused"] == 0

        manga_before = [
            manga
            for manga in client.get("/api/manga").json()
            if manga["provider"] == "local"
        ]
        assert len(manga_before) == 1
        manga_id = manga_before[0]["id"]
        chapters_before = client.get(f"/api/manga/{manga_id}").json()["chapters"]
        snapshot = {
            chapter["id"]: {
                "path": chapter["library_path"],
                "sha256": sha256(Path(chapter["library_path"])),
                "inode": Path(chapter["library_path"]).stat().st_ino,
                "mtime_ns": Path(chapter["library_path"]).stat().st_mtime_ns,
            }
            for chapter in chapters_before
        }

        second = import_group(client, "Example Saga")
        assert second["errors"] == []
        assert second["imported"] == 2
        assert second["reused"] == 2

        manga_after = [
            manga
            for manga in client.get("/api/manga").json()
            if manga["provider"] == "local"
        ]
        assert [manga["id"] for manga in manga_after] == [manga_id]
        chapters_after = client.get(f"/api/manga/{manga_id}").json()["chapters"]
        assert {chapter["id"] for chapter in chapters_after} == set(snapshot)
        for chapter in chapters_after:
            before = snapshot[chapter["id"]]
            path = Path(chapter["library_path"])
            assert str(path) == before["path"]
            assert sha256(path) == before["sha256"]
            assert path.stat().st_ino == before["inode"]
            assert path.stat().st_mtime_ns == before["mtime_ns"]
            assert chapter["local_import_sha256"] is not None


def test_reimport_merges_new_volume_without_rewriting_existing_files(tmp_path: Path):
    import_root = tmp_path / "incoming"
    build_import_tree(import_root)
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        import_dir=import_root,
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    with TestClient(create_app(settings)) as client:
        assert import_group(client, "Example Saga")["errors"] == []
        manga = next(
            manga
            for manga in client.get("/api/manga").json()
            if manga["provider"] == "local"
        )
        before = {
            chapter["volume"]: (
                chapter["id"],
                chapter["library_path"],
                sha256(Path(chapter["library_path"])),
            )
            for chapter in client.get(f"/api/manga/{manga['id']}").json()["chapters"]
        }
        make_volume_zip(
            import_root
            / "Manga Series"
            / "Author Name - Example Saga"
            / "Author Name - Example Saga 3.cbz"
        )

        merged = import_group(client, "Example Saga")
        assert merged["errors"] == []
        assert merged["imported"] == 3
        assert merged["reused"] == 2
        locals_after = [
            item
            for item in client.get("/api/manga").json()
            if item["provider"] == "local"
        ]
        assert [item["id"] for item in locals_after] == [manga["id"]]
        chapters = client.get(f"/api/manga/{manga['id']}").json()["chapters"]
        assert {chapter["volume"] for chapter in chapters} == {"1", "2", "3"}
        for chapter in chapters:
            if chapter["volume"] not in before:
                continue
            old_id, old_path, old_sha256 = before[chapter["volume"]]
            assert chapter["id"] == old_id
            assert chapter["library_path"] == old_path
            assert sha256(Path(chapter["library_path"])) == old_sha256


def test_reimport_merges_new_volume_after_metadata_changes_display_title(
    tmp_path: Path,
):
    import_root = tmp_path / "incoming"
    series_dir = import_root / "Manga Series" / "Author Name - Example Saga"
    make_volume_zip(series_dir / "Author Name - Example Saga 1.zip")
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        import_dir=import_root,
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    app = create_app(settings)
    with TestClient(app) as client:
        assert import_group(client, "Example Saga")["errors"] == []
        manga = next(
            item
            for item in client.get("/api/manga").json()
            if item["provider"] == "local"
        )
        app.state.database.set_manga_metadata_title(
            manga["id"], "Canonical Example Saga"
        )
        make_volume_zip(series_dir / "Author Name - Example Saga 2.zip")

        merged = import_group(client, "Example Saga")

        assert merged["errors"] == []
        assert merged["imported"] == 2
        assert merged["reused"] == 1
        stored = client.get(f"/api/manga/{manga['id']}").json()
        assert stored["title"] == "Canonical Example Saga"
        assert stored["source_title"] == "Example Saga"
        assert {chapter["volume"] for chapter in stored["chapters"]} == {"1", "2"}


def test_reimport_refuses_different_content_for_same_logical_volume(tmp_path: Path):
    import_root = tmp_path / "incoming"
    build_import_tree(import_root)
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        import_dir=import_root,
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    with TestClient(create_app(settings)) as client:
        assert import_group(client, "Example Saga")["errors"] == []
        manga = next(
            manga
            for manga in client.get("/api/manga").json()
            if manga["provider"] == "local"
        )
        volume_one = next(
            chapter
            for chapter in client.get(f"/api/manga/{manga['id']}").json()["chapters"]
            if chapter["volume"] == "1"
        )
        original_path = Path(volume_one["library_path"])
        original_archive_sha256 = sha256(original_path)
        original_content_sha256 = volume_one["local_import_sha256"]
        make_volume_zip(
            import_root
            / "Manga Series"
            / "Author Name - Example Saga"
            / "Author Name - Example Saga 1.zip",
            payload=PNG + b"different-content",
        )

        result = import_group(client, "Example Saga")
        assert result["imported"] == 1
        assert result["reused"] == 1
        assert len(result["errors"]) == 1
        assert "Different content already exists" in result["errors"][0]["error"]
        persisted = client.get(f"/api/manga/{manga['id']}").json()["chapters"]
        volume_one_after = next(
            chapter for chapter in persisted if chapter["volume"] == "1"
        )
        assert volume_one_after["id"] == volume_one["id"]
        assert volume_one_after["local_import_sha256"] == original_content_sha256
        assert sha256(original_path) == original_archive_sha256


def test_reimport_adopts_legacy_local_ids_and_backfills_content_ledger(tmp_path: Path):
    import_root = tmp_path / "incoming"
    build_import_tree(import_root)
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        import_dir=import_root,
        monitor_enabled=False,
    )
    settings.ensure_directories()
    provision_library_identity(settings)
    database = Database(settings.database_path)
    database.initialize()
    manga = {
        "id": "local-example-saga",
        "provider": "local",
        "title": "Example Saga",
        "description": "",
        "cover_url": None,
        "authors": ["Author Name"],
        "available_languages": ["en"],
    }
    database.upsert_manga(manga, "en", "none")
    original_paths: dict[str, tuple[str, str]] = {}
    for index in (1, 2):
        chapter = {
            "id": f"local-example-saga-{index:04d}",
            "volume": str(index),
            "chapter": None,
            "title": f"Volume {index}",
            "language": "en",
            "provider": "local",
            "groups": [],
            "publish_at": None,
            "source_url": "",
            "pages": 3,
            "version": 1,
        }
        database.upsert_chapters(manga["id"], [chapter])
        pages_dir = tmp_path / f"legacy-pages-{index}"
        pages_dir.mkdir()
        pages = []
        for page_index in range(1, 4):
            page = pages_dir / f"{page_index:04d}.png"
            page.write_bytes(PNG)
            pages.append(page)
        destination = final_library_path(settings.library_dir, manga, chapter)
        package_cbz(destination, pages, manga, chapter)
        archive_sha256 = validate_cbz(destination)["sha256"]
        database.mark_chapter_downloaded(chapter["id"], destination, archive_sha256)
        original_paths[str(index)] = (str(destination), archive_sha256)

    with TestClient(create_app(settings)) as client:
        result = import_group(client, "Example Saga")
        assert result["errors"] == []
        assert result["reused"] == 2
        locals_after = [
            item
            for item in client.get("/api/manga").json()
            if item["provider"] == "local"
        ]
        assert [item["id"] for item in locals_after] == ["local-example-saga"]
        chapters = client.get("/api/manga/local-example-saga").json()["chapters"]
        assert {chapter["id"] for chapter in chapters} == {
            "local-example-saga-0001",
            "local-example-saga-0002",
        }
        for chapter in chapters:
            original_path, original_sha256 = original_paths[chapter["volume"]]
            assert chapter["library_path"] == original_path
            assert sha256(Path(original_path)) == original_sha256
            assert chapter["local_import_sha256"] is not None


class NoopProvider:
    name = "mangadex"


class NoopKomga:
    configured = False

    async def scan(self, _expected_relative_paths=()) -> dict:
        return {"configured": False, "triggered": False}


class RecordingNotifier:
    def __init__(self):
        self.imported: list[tuple[dict, dict]] = []

    async def chapter_imported(self, manga: dict, chapter: dict) -> bool:
        self.imported.append((manga, chapter))
        return True

    async def job_failed(self, _manga_title: str, _detail: str) -> bool:
        return True


def test_external_import_adopts_matching_hand_import_without_comicinfo(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
    )
    settings.ensure_directories()
    provision_library_identity(settings)
    database = Database(settings.database_path)
    database.initialize()
    service = TankarrService(settings, database, NoopProvider(), NoopKomga())
    manga = database.upsert_manga(
        {
            "id": "local-hand-import-test",
            "provider": "local",
            "title": "A Single Match",
            "description": "",
            "cover_url": None,
            "authors": ["Oji Suzuki"],
            "available_languages": ["en"],
        },
        "en",
        "none",
    )
    chapter = {
        "id": "torrent-hand-import-volume-1",
        "volume": "1",
        "chapter": None,
        "title": "Volume 1",
        "language": "en",
        "provider": "prowlarr",
        "groups": [],
        "publish_at": None,
        "source_url": "https://example.invalid/torrent/1",
        "pages": 2,
        "version": 1,
    }
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    pages = []
    for index, payload in enumerate((b"page-one", b"page-two"), start=1):
        page = pages_dir / f"{index:04d}.jpg"
        page.write_bytes(payload)
        pages.append(page)
    staged = tmp_path / "staged.cbz"
    package_cbz(staged, pages, manga, chapter)
    destination = final_library_path(settings.library_dir, manga, chapter)
    destination.parent.mkdir(parents=True)
    with zipfile.ZipFile(destination, "w") as archive:
        for page in pages:
            archive.write(page, arcname=page.name)

    result = service._publish_external_import_locked(
        manga["id"],
        chapter,
        staged,
        validate_cbz(staged)["sha256"],
        local_page_content_sha256(pages),
    )

    assert result["reused"] is True
    assert validate_cbz(Path(result["path"]))["page_count"] == 2
    assert database.get_chapter(chapter["id"])["downloaded"] is True


def test_external_import_never_adopts_different_hand_import_content(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
    )
    settings.ensure_directories()
    provision_library_identity(settings)
    database = Database(settings.database_path)
    database.initialize()
    service = TankarrService(settings, database, NoopProvider(), NoopKomga())
    manga = database.upsert_manga(
        {
            "id": "local-hand-import-conflict-test",
            "provider": "local",
            "title": "Conflict Test",
            "description": "",
            "cover_url": None,
            "authors": ["Author"],
            "available_languages": ["en"],
        },
        "en",
        "none",
    )
    chapter = {
        "id": "torrent-hand-import-conflict-volume-1",
        "volume": "1",
        "chapter": None,
        "title": "Volume 1",
        "language": "en",
        "provider": "nyaa",
        "groups": [],
        "publish_at": None,
        "source_url": "https://example.invalid/torrent/2",
        "pages": 1,
        "version": 1,
    }
    page = tmp_path / "downloaded.jpg"
    page.write_bytes(b"downloaded-page")
    staged = tmp_path / "staged.cbz"
    package_cbz(staged, [page], manga, chapter)
    destination = final_library_path(settings.library_dir, manga, chapter)
    destination.parent.mkdir(parents=True)
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr("0001.jpg", b"different-page")
    before = destination.read_bytes()

    with pytest.raises(ExternalImportConflict, match="Different content"):
        service._publish_external_import_locked(
            manga["id"],
            chapter,
            staged,
            validate_cbz(staged)["sha256"],
            local_page_content_sha256([page]),
        )

    assert destination.read_bytes() == before
    with pytest.raises(KeyError):
        database.get_chapter(chapter["id"])


@pytest.mark.asyncio
async def test_local_cbz_publication_waits_for_service_mutation_lock(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
    )
    settings.ensure_directories()
    provision_library_identity(settings)
    database = Database(settings.database_path)
    database.initialize()
    notifier = RecordingNotifier()
    service = TankarrService(
        settings, database, NoopProvider(), NoopKomga(), notifier=notifier
    )
    manga = await service.register_local_import_series(
        {
            "id": "local-lock-test",
            "provider": "local",
            "title": "Lock Test",
            "description": "",
            "cover_url": None,
            "authors": ["Author"],
            "available_languages": ["en"],
        },
        "en",
    )
    chapter = {
        "id": f"{manga['id']}-release-lock",
        "volume": "1",
        "chapter": None,
        "title": "Volume 1",
        "language": "en",
        "provider": "local",
        "groups": [],
        "publish_at": None,
        "source_url": "",
        "pages": 3,
        "version": 1,
    }
    pages_dir = tmp_path / "prepared-pages"
    pages_dir.mkdir()
    pages = []
    for index in range(1, 4):
        page = pages_dir / f"{index:04d}.png"
        page.write_bytes(PNG)
        pages.append(page)
    staged = tmp_path / "prepared.cbz"
    package_cbz(staged, pages, manga, chapter)
    archive_sha256 = validate_cbz(staged)["sha256"]
    content_sha256 = local_page_content_sha256(pages)

    await service._mutation_lock.acquire()
    try:
        publication = asyncio.create_task(
            service.publish_local_import(
                manga,
                chapter,
                staged,
                archive_sha256,
                content_sha256,
                pages[0],
            )
        )
        await asyncio.sleep(0.05)
        assert publication.done() is False
        assert list(settings.library_dir.rglob("*.cbz")) == []
        with pytest.raises(KeyError):
            database.get_chapter(chapter["id"])
    finally:
        service._mutation_lock.release()

    result = await publication
    assert Path(result["path"]).exists()
    assert database.get_chapter(chapter["id"])["downloaded"] is True
    assert [(item[0]["title"], item[1]["volume"]) for item in notifier.imported] == [
        ("Lock Test", "1")
    ]


@pytest.mark.asyncio
async def test_local_cbz_publication_survives_catalogue_promotion(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
    )
    settings.ensure_directories()
    provision_library_identity(settings)
    database = Database(settings.database_path)
    database.initialize()
    service = TankarrService(settings, database, NoopProvider(), NoopKomga())
    manga = await service.register_local_import_series(
        {
            "id": "local-promotion-test",
            "provider": "local",
            "title": "Promotion Test",
            "description": "",
            "cover_url": None,
            "authors": ["Author"],
            "available_languages": ["en"],
        },
        "en",
    )
    chapter = {
        "id": f"{manga['id']}-release-1",
        "volume": "1",
        "chapter": None,
        "title": "Volume 1",
        "language": "en",
        "provider": "local",
        "groups": [],
        "publish_at": None,
        "source_url": "",
        "pages": 1,
        "version": 1,
    }
    page = tmp_path / "page.png"
    page.write_bytes(PNG)
    staged = tmp_path / "prepared.cbz"
    package_cbz(staged, [page], manga, chapter)

    assert database.adopt_catalogue_identity(manga["id"], "12345") is True
    result = await service.publish_local_import(
        manga,
        chapter,
        staged,
        validate_cbz(staged)["sha256"],
        local_page_content_sha256([page]),
        page,
    )

    assert Path(result["path"]).exists()
    assert database.get_manga(manga["id"])["provider"] == "catalogue"
    imported = database.get_chapter(chapter["id"])
    assert imported["provider"] == "local"
    assert imported["downloaded"] is True

    resolved_again = await service.register_local_import_series(
        {
            "id": "a-new-deterministic-id",
            "provider": "local",
            "title": "Promotion Test",
            "description": "",
            "cover_url": None,
            "authors": ["Author"],
            "available_languages": ["en"],
        },
        "en",
    )
    assert resolved_again["id"] == manga["id"]
    assert resolved_again["provider"] == "catalogue"
    assert resolved_again["_local_import_target"] is True


@pytest.mark.asyncio
async def test_local_cbz_is_rolled_back_when_database_publication_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        monitor_enabled=False,
    )
    settings.ensure_directories()
    provision_library_identity(settings)
    database = Database(settings.database_path)
    database.initialize()
    service = TankarrService(settings, database, NoopProvider(), NoopKomga())
    manga = await service.register_local_import_series(
        {
            "id": "local-rollback-test",
            "provider": "local",
            "title": "Rollback Test",
            "description": "",
            "cover_url": None,
            "authors": ["Author"],
            "available_languages": ["en"],
        },
        "en",
    )
    chapter = {
        "id": f"{manga['id']}-release-rollback",
        "volume": "1",
        "chapter": None,
        "title": "Volume 1",
        "language": "en",
        "provider": "local",
        "groups": [],
        "publish_at": None,
        "source_url": "",
        "pages": 3,
        "version": 1,
    }
    pages_dir = tmp_path / "rollback-pages"
    pages_dir.mkdir()
    pages = []
    for index in range(1, 4):
        page = pages_dir / f"{index:04d}.png"
        page.write_bytes(PNG)
        pages.append(page)
    staged = tmp_path / "rollback.cbz"
    package_cbz(staged, pages, manga, chapter)

    def fail_publication(*args, **kwargs):
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(database, "publish_local_chapter", fail_publication)
    with pytest.raises(RuntimeError, match="simulated database failure"):
        await service.publish_local_import(
            manga,
            chapter,
            staged,
            validate_cbz(staged)["sha256"],
            local_page_content_sha256(pages),
            pages[0],
        )

    assert list(settings.library_dir.rglob("*.cbz")) == []
    with pytest.raises(KeyError):
        database.get_chapter(chapter["id"])


def test_rar_tool_prefers_bsdtar_and_drives_it_with_a_bare_listing(
    monkeypatch, tmp_path
):
    """libarchive is maintained in Debian main, so it wins over unrar and unar."""

    import subprocess

    from tankarr import importer as importer_module

    monkeypatch.setattr(
        importer_module.shutil,
        "which",
        lambda name: (
            f"/usr/bin/{name}" if name in {"bsdtar", "unrar", "unar", "lsar"} else None
        ),
    )
    assert importer_module.rar_tool() == "bsdtar"

    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(
            command, 0, stdout="Vol.01/\nVol.01/001.jpg\nVol.01/002.jpg\n", stderr=""
        )

    monkeypatch.setattr(
        importer_module,
        "run_decoder",
        lambda command, *_args, **kwargs: fake_run(command),
    )
    archive = tmp_path / "book.cbr"
    assert importer_module._rar_list(archive) == [
        "Vol.01/",
        "Vol.01/001.jpg",
        "Vol.01/002.jpg",
    ]
    assert importer_module._rar_image_count(archive) == 2
    assert calls[-1][:2] == ["bsdtar", "-tf"]

    importer_module._rar_extract(archive, tmp_path / "out")
    assert calls[-1] == ["bsdtar", "-xf", str(archive), "-C", str(tmp_path / "out")]


def test_rar_tool_falls_back_to_unrar_and_drives_it_with_bare_listing(
    monkeypatch, tmp_path
):
    """Without libarchive and unar, an operator-installed unrar still works."""

    import subprocess

    from tankarr import importer as importer_module

    monkeypatch.setattr(
        importer_module.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name == "unrar" else None,
    )
    assert importer_module.rar_tool() == "unrar"

    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(list(command))
        return subprocess.CompletedProcess(
            command, 0, stdout="Vol.01/001.jpg\nVol.01/002.jpg\n", stderr=""
        )

    monkeypatch.setattr(
        importer_module,
        "run_decoder",
        lambda command, *_args, **kwargs: fake_run(command),
    )
    archive = tmp_path / "book.cbr"
    assert importer_module._rar_list(archive) == ["Vol.01/001.jpg", "Vol.01/002.jpg"]
    assert calls[-1][:2] == ["unrar", "lb"]

    monkeypatch.setattr(
        importer_module,
        "run_decoder",
        lambda command, *_args, **kwargs: fake_run(command),
    )
    importer_module._rar_extract(archive, tmp_path / "out")
    assert calls[-1][:4] == ["unrar", "x", "-o+", "-inul"]
    assert calls[-1][-1].endswith("/")


def test_rar_tool_prefers_unar_over_unrar_without_bsdtar(monkeypatch):
    from tankarr import importer as importer_module

    monkeypatch.setattr(
        importer_module.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"unar", "lsar", "unrar"} else None,
    )
    assert importer_module.rar_tool() == "unar"


def test_a_hash_number_is_a_bare_number_token():
    from tankarr.importer import infer_numbered_name

    assert infer_numbered_name("Ding Dong Circus #01") == (
        "Ding Dong Circus",
        "1",
        None,
    )
    assert infer_numbered_name("Cinderalla #3") == ("Cinderalla", "3", None)
    # Explicit markers still win over the bare token.
    assert infer_numbered_name("Example #2 c05") == ("Example #2", None, "5")


def test_release_year_after_volume_is_not_part_of_volume_number():
    from tankarr.importer import infer_numbered_name

    assert infer_numbered_name("Demon Slayer Vol.23.2021 Hybrid Comic") == (
        "Demon Slayer Hybrid Comic",
        "23",
        None,
    )
    assert infer_numbered_name("Series Vol.01.5") == ("Series", "1.5", None)


def test_scan_groups_scene_release_packs_as_one_series(tmp_path: Path):
    """The folder names the pack ranges; the UI keeps its two units separate."""
    import_root = tmp_path / "incoming"
    pack = import_root / "Chainsaw Man v19-20 + 187-190"
    make_volume_zip(pack / "Chainsaw Man v19 (2025) (Digital) (1r0n).cbz")
    make_volume_zip(pack / "Chainsaw Man v20 (2026) (Digital) (1r0n) (f2).cbz")
    make_volume_zip(pack / "Chainsaw Man 187 (2024) (Digital) (1r0n).cbz")
    scans = import_root / "The Color of the End"
    make_volume_zip(scans / "v1 c001 [#EverydayHeroes Scans].cbz")
    make_volume_zip(scans / "v1 c002 [Nextyle Stadia].cbz")
    settings = Settings(
        data_dir=tmp_path / "data",
        library_dir=tmp_path / "library",
        import_dir=import_root,
        monitor_enabled=False,
    )
    provision_library_identity(settings)
    with TestClient(create_app(settings)) as client:
        payload = client.get("/api/import/scan").json()
    assert {group["title"] for group in payload["groups"]} == {
        "Chainsaw Man",
        "The Color of the End",
    }
    packs = [group for group in payload["groups"] if group["title"] == "Chainsaw Man"]
    assert len(packs) == 2
    assert len({group["key"] for group in packs}) == 2
    numbering = {
        (item["volume"], item["chapter"]) for group in packs for item in group["items"]
    }
    assert numbering == {("19", None), ("20", None), (None, "187")}
    scans = next(
        group for group in payload["groups"] if group["title"] == "The Color of the End"
    )
    assert [item["chapter"] for item in scans["items"]] == [
        "1",
        "2",
    ]
    for group in payload["groups"]:
        for item in group["items"]:
            assert "bare_number" not in item and "explicit_volume" not in item


def test_torrent_scan_reads_bare_numbers_next_to_volumes_as_chapters(tmp_path: Path):
    """A pack of "v01-17 + 148-224" holds chapters 148-224, not volume 148."""
    pack = tmp_path / "Dandadan (Digital) (1r0n)"
    make_volume_zip(pack / "Dandadan v01 (2022) (Digital) (1r0n).cbz")
    make_volume_zip(pack / "Dandadan v05 (2023) (Digital) (1r0n) (f).cbz")
    make_volume_zip(pack / "Dandadan 148 (2024) (Digital) (1r0n).cbz")
    make_volume_zip(pack / "Dandadan 224 (2026) (Digital) (1r0n).cbz")

    importer = LibraryImporter.__new__(LibraryImporter)
    scan = importer.scan_torrent_content(
        pack, release_title="Dandadan v01-17 + 148-224"
    )

    numbering = {
        item["path"]: (item["volume"], item["chapter"]) for item in scan["items"]
    }
    assert numbering == {
        "Dandadan v01 (2022) (Digital) (1r0n).cbz": ("1", None),
        "Dandadan v05 (2023) (Digital) (1r0n) (f).cbz": ("5", None),
        "Dandadan 148 (2024) (Digital) (1r0n).cbz": (None, "148"),
        "Dandadan 224 (2026) (Digital) (1r0n).cbz": (None, "224"),
    }
    for item in scan["items"]:
        assert "bare_number" not in item and "explicit_volume" not in item


def test_torrent_scan_keeps_a_bare_numbered_pack_as_volumes(tmp_path: Path):
    """With no explicit volume beside them, bare numbers keep their meaning."""
    pack = tmp_path / "Series"
    make_volume_zip(pack / "Series 01.cbz")
    make_volume_zip(pack / "Series 02.cbz")

    scan = LibraryImporter.__new__(LibraryImporter).scan_torrent_content(pack)

    assert {(item["volume"], item["chapter"]) for item in scan["items"]} == {
        ("1", None),
        ("2", None),
    }


def test_a_bare_number_range_is_not_read_as_its_last_volume():
    for stem in ("Pineapple Army 1-6 (ja)", "Pineapple Army 1-6"):
        assert infer_numbered_name(stem) == ("Pineapple Army 1-6", None, None)
    assert infer_numbered_name("Series 05 (2024) (Digital) (1r0n)") == (
        "Series",
        "5",
        None,
    )


@pytest.mark.parametrize(
    "stem, expected",
    [
        ("Series [c001]", ("Series", None, "1")),
        ("Series (v02)", ("Series", "2", None)),
        ("Series [c001] (2024) [group]", ("Series", None, "1")),
        ("Series (v02) (Digital)", ("Series", "2", None)),
        ("Series (Full Color)", ("Series (Full Color)", None, None)),
        ("Series v01-02 (c2c)", ("Series", "1-2", None)),
    ],
)
def test_release_tags_preserve_numbering_and_unnumbered_titles(stem, expected):
    assert infer_numbered_name(stem) == expected


@pytest.mark.parametrize("separator", ["-", " - ", "–", " – ", "—", " — "])
def test_range_archives_are_not_imported_as_a_single_volume(tmp_path, separator):
    name = f"Series 1{separator}6 (Digital).cbz"
    make_volume_zip(tmp_path / name)
    importer = LibraryImporter.__new__(LibraryImporter)
    scan = importer.scan_torrent_content(tmp_path)
    assert scan["items"][0]["volume"] is None
    assert scan["items"][0]["chapter"] is None
    groups = importer._scan_root(tmp_path)["groups"]
    # Grouping may not turn a known ambiguous omnibus into volume one.
    assert groups[0]["items"][0]["volume"] is None


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Series v01-02", ("2", None)),
        ("Series v01 + c002-003", (None, "2")),
        ("Series v01 + 002-003", (None, "2")),
        ("Series v01-02 + c002-003", (None, None)),
        ("Series v01 + c003-004", (None, None)),
        ("Different Series v01 + c002-003", (None, None)),
        ("Series", (None, None)),
    ],
)
def test_mixed_numbering_requires_matching_unambiguous_pack_context(
    tmp_path, title, expected
):
    make_volume_zip(tmp_path / "Series v01.cbz")
    make_volume_zip(tmp_path / "Series 02.cbz")
    scan = LibraryImporter.__new__(LibraryImporter).scan_torrent_content(
        tmp_path, release_title=title
    )
    item = next(item for item in scan["items"] if item["path"] == "Series 02.cbz")
    assert (item["volume"], item["chapter"]) == expected
    assert not {"bare_number", "explicit_volume", "ambiguous_number"} & item.keys()


def test_folder_and_release_disagreement_stays_unassigned(tmp_path):
    pack = tmp_path / "Series v01-02"
    make_volume_zip(pack / "Series v01.cbz")
    make_volume_zip(pack / "Series 02.cbz")
    scan = LibraryImporter.__new__(LibraryImporter).scan_torrent_content(
        pack, release_title="Series v01 + c002-003"
    )
    item = next(item for item in scan["items"] if item["path"] == "Series 02.cbz")
    assert item["volume"] is None and item["chapter"] is None


def test_an_unassigned_import_item_requires_a_number():
    with pytest.raises(ValueError, match="needs a volume or chapter number"):
        LibraryImporter._items_for_unit(
            [{"path": "Series 02.cbz", "volume": None, "chapter": None}], None
        )


def test_library_keeps_ambiguous_files_apart_from_known_books(tmp_path):
    make_volume_zip(tmp_path / "Series v01.cbz")
    make_volume_zip(tmp_path / "Series 02.cbz")
    groups = LibraryImporter.__new__(LibraryImporter)._scan_root(tmp_path)["groups"]
    assert len(groups) == 2
    assert {group["title"] for group in groups} == {"Series"}
    assert len({group["key"] for group in groups}) == 2
    assert {
        (group["items"][0]["volume"], group["items"][0]["chapter"]) for group in groups
    } == {("1", None), (None, None)}
