"""Infer paged reading direction from the original work's metadata."""

from tankarr.languages import SUPPORTED_LANGUAGE_CODES

_KIND_DIRECTION = {
    "manga": "rtl",
    "doujinshi": "rtl",
    "manhwa": "ltr",
    "manhua": "ltr",
    "comic": "ltr",
    "comics": "ltr",
    "graphic_novel": "ltr",
    "book": "ltr",
}
_RTL_LANGUAGES = {"ar", "fa", "he", "ja"}
_LANGUAGE_DIRECTION = {
    code.split("-")[0]: "rtl" if code.split("-")[0] in _RTL_LANGUAGES else "ltr"
    for code in SUPPORTED_LANGUAGE_CODES
}


def infer_reading_direction(
    original_language: object, *, work_type: object = None, content_kind: object = None
) -> str | None:
    """Use the format when it is specific, then the original language."""
    work = str(work_type or "").strip().casefold().replace("-", "_").replace(" ", "_")
    kind = str(content_kind or "").strip().casefold()
    language = str(original_language or "").strip().casefold().replace("_", "-")
    language_direction = _LANGUAGE_DIRECTION.get(language.split("-", 1)[0])

    # Catalogues also use manga as a broad family for standalone stories.
    if work in {"one_shot", "oneshot"} and language_direction:
        return language_direction
    return _KIND_DIRECTION.get(work) or _KIND_DIRECTION.get(kind) or language_direction
