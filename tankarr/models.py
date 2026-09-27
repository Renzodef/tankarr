from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MonitorMode = Literal["all", "future", "existing", "none"]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=300)
    remember_me: bool = False


class AddMangaRequest(BaseModel):
    manga_id: str
    provider: str = Field(default="catalogue", min_length=2, max_length=32)
    language: str = Field(default="en", min_length=2, max_length=16)
    monitor_mode: MonitorMode = "all"
    # Sonarr-style options: one download unit per series, first search opt-out.
    series_unit: Literal["automatic", "chapters", "volumes"] = "automatic"
    search_now: bool = True


class UpdateMangaRequest(BaseModel):
    preferred_language: str | None = Field(default=None, min_length=2, max_length=16)
    monitor_mode: MonitorMode | None = None
    status_override: Literal["automatic", "continuing", "hiatus", "ended"] | None = None
    library_status_override: Literal["automatic", "up_to_date"] | None = None
    verified_chapter_count: int | Literal["automatic"] | None = None
    verified_chapter_source: str | None = Field(default=None, max_length=2000)
    expected_count_override: int | Literal["automatic"] | None = None
    series_unit: Literal["automatic", "chapters", "volumes"] | None = None
    reader_mode: Literal["automatic", "manga", "webtoon"] | None = None
    reader_direction: Literal["automatic", "ltr", "rtl"] | None = None
    assemble_books_automatically: bool | None = None
    translation_enabled: bool | None = None
    translation_source_languages: str | None = Field(default=None, max_length=200)
    # Books in the edition on disk when it differs from the catalogue's
    # (Viz's 12-book Master Keaton against the 18-tankobon map). "automatic"
    # clears it and the catalogue map decides again.
    edition_book_count: int | Literal["automatic"] | None = None
    # A hand-corrected creator list, or "automatic" to follow the catalogue.
    authors_override: list[str] | Literal["automatic"] | None = Field(
        default=None, max_length=20
    )


class ChapterMapBoundaryRequest(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True, extra="forbid")

    volume: str = Field(min_length=1, max_length=32, pattern=r"^\d+(?:\.\d+)?$")
    first_chapter: str = Field(min_length=1, max_length=32, pattern=r"^\d+(?:\.\d+)?$")


class BookReadingItem(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True, extra="forbid")

    volume: str = Field(min_length=1, max_length=32, pattern=r"^\d+(?:\.\d+)?$")
    first_chapter: str = Field(min_length=1, max_length=32, pattern=r"^\d+(?:\.\d+)?$")
    last_chapter: str = Field(min_length=1, max_length=32, pattern=r"^\d+(?:\.\d+)?$")


class BookReadingRequest(BaseModel):
    """What an external reader found printed in the books (see the ``ocr``
    map source)."""

    model_config = ConfigDict(extra="forbid")

    books: list[BookReadingItem] = Field(min_length=1, max_length=500)


class ChapterMapRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)

    boundaries: list[ChapterMapBoundaryRequest] = Field(min_length=1, max_length=500)
    mode: Literal["merge", "replace"] = "merge"
    last_chapter: str | None = Field(
        default=None, max_length=32, pattern=r"^\d+(?:\.\d+)?$"
    )


class LibraryOrphanDeleteRequest(BaseModel):
    """Which untracked library folders to remove; empty means all of them."""

    folders: list[str] | None = None


class DismissAlertRequest(BaseModel):
    # The alert's own content: dismissing "12 failed downloads" must not hide
    # the thirteenth.
    signature: str = Field(min_length=1, max_length=300)


class RenameMangaRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class MetadataCorrelationsRequest(BaseModel):
    correlations: dict[str, str | None] = Field(min_length=1, max_length=10)


class ArtworkSelectionRequest(BaseModel):
    candidate_id: str | None = Field(default=None, min_length=8, max_length=128)


class DownloadRequest(BaseModel):
    force: bool = False
    # Replace the file already in the library for this logical chapter -
    # deleted only once this download has passed every gate.
    replace: bool = False
    # "Download anyway": waive the length gate for this job (never the shape one).
    quality_override: bool = False


class AddReleaseSourceRequest(BaseModel):
    provider: str = Field(min_length=2, max_length=32)
    provider_manga_id: str = Field(min_length=1, max_length=4_000)


class ChapterSearchRequest(BaseModel):
    chapter: str | None = Field(default=None, min_length=1, max_length=32)
    volume: str | None = Field(default=None, min_length=1, max_length=32)


class CancelJobsRequest(BaseModel):
    job_ids: list[int] = Field(min_length=1, max_length=500)


class TorrentGrabRequest(BaseModel):
    provider: Literal["prowlarr", "internetarchive"] = "prowlarr"
    release_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


class AssignedBook(BaseModel):
    """What the operator says one file in a release actually is."""

    path: str = Field(min_length=1, max_length=1000)
    volume: str | None = Field(default=None, min_length=1, max_length=32)
    chapter: str | None = Field(default=None, min_length=1, max_length=32)


class TorrentImportRequest(BaseModel):
    confirm_language: bool = False
    # Import the books whose volume/chapter is unambiguous and leave the
    # rest: a pack often bundles extra works with no number of their own.
    skip_unnumbered: bool = False
    # The operator's explicit pick of which files to take, by relative path.
    # Two editions of one volume are a choice Tankarr must not make alone.
    selected_paths: list[str] | None = Field(default=None, max_length=500)
    # A book whose name carries no number is unplaceable on its own; naming it
    # is the one thing that makes such a release importable at all.
    assigned: list[AssignedBook] | None = Field(default=None, max_length=500)


class MonitorChapterRequest(BaseModel):
    monitored: bool


class MonitorVolumeRequest(BaseModel):
    state: Literal["automatic", "monitored", "ignored"]


class MangaDeletionLanguagePreview(BaseModel):
    language: str
    chapters: int
    chapter_releases: int
    downloaded_chapters: int
    files: int
    existing_files: int
    missing_files: int


class MangaDeletionPreview(BaseModel):
    manga_id: str
    title: str
    snapshot: str
    chapters: int
    chapter_releases: int
    downloaded_chapters: int
    files: int
    existing_files: int
    missing_files: int
    languages: list[MangaDeletionLanguagePreview]


class ImportItem(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    volume: str | None = None
    chapter: str | None = None
    chapter_title: str = ""
    source_provider: str | None = Field(default=None, min_length=2, max_length=32)
    source_url: str | None = Field(default=None, min_length=8, max_length=2000)


class ImportGroup(BaseModel):
    key: str | None = Field(default=None, min_length=1, max_length=1000)
    title: str = Field(min_length=1, max_length=200)
    authors: list[str] = Field(default_factory=list)
    items: list[ImportItem] = Field(min_length=1)
    upload_id: str | None = Field(default=None, min_length=32, max_length=32)
    target_manga_id: str | None = Field(default=None, min_length=1, max_length=500)
    language: str | None = Field(default=None, min_length=2, max_length=16)
    unit: Literal["chapters", "volumes"] | None = None
    set_series_unit: bool = False
    source_provider: str | None = Field(default=None, min_length=2, max_length=32)
    source_url: str | None = Field(default=None, min_length=8, max_length=2000)
    original_language: str | None = Field(default=None, min_length=2, max_length=16)


class ImportRequest(BaseModel):
    groups: list[ImportGroup] = Field(min_length=1)
    language: str = Field(default="en", min_length=2, max_length=16)


class ManualImportPart(BaseModel):
    volume: str | None = Field(default=None, min_length=1, max_length=32)
    chapter: str | None = Field(default=None, min_length=1, max_length=32)
    title: str = Field(default="", max_length=200)
    page_start: int = Field(ge=1, le=10000)
    page_end: int = Field(ge=1, le=10000)


class ManualImportRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    language: str = Field(default="en", min_length=2, max_length=16)
    source_url: str = Field(min_length=8, max_length=2000)
    source_name: str = Field(default="Manual import", min_length=1, max_length=120)
    parts: list[ManualImportPart] = Field(min_length=1, max_length=500)
    confirm_language: bool = False
