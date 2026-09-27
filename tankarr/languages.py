from __future__ import annotations

import re

# Translation languages exposed by MangaDex and understood by Suwayomi/Mihon
# sources.  Keep this as the canonical backend allow-list: accepting arbitrary
# locale strings would make a typo look like a valid (but permanently empty)
# search profile.
SUPPORTED_LANGUAGES: tuple[tuple[str, str], ...] = (
    ("en", "English"),
    ("it", "Italiano"),
    ("ar", "Arabic"),
    ("bn", "Bengali"),
    ("bg", "Bulgarian"),
    ("my", "Burmese"),
    ("ca", "Catalan"),
    ("zh", "Chinese (Simplified)"),
    ("zh-hk", "Chinese (Traditional)"),
    ("zh-ro", "Chinese (Romanized)"),
    ("cs", "Czech"),
    ("da", "Danish"),
    ("nl", "Dutch"),
    ("fil", "Filipino"),
    ("fi", "Finnish"),
    ("fr", "French"),
    ("de", "German"),
    ("el", "Greek"),
    ("he", "Hebrew"),
    ("hi", "Hindi"),
    ("hu", "Hungarian"),
    ("id", "Indonesian"),
    ("ja", "Japanese"),
    ("ja-ro", "Japanese (Romanized)"),
    ("ko", "Korean"),
    ("ko-ro", "Korean (Romanized)"),
    ("lt", "Lithuanian"),
    ("ms", "Malay"),
    ("mn", "Mongolian"),
    ("no", "Norwegian"),
    ("fa", "Persian"),
    ("pl", "Polish"),
    ("pt-br", "Portuguese (Brazil)"),
    ("pt", "Portuguese (Portugal)"),
    ("ro", "Romanian"),
    ("ru", "Russian"),
    ("sr", "Serbo-Croatian"),
    ("es", "Spanish (Spain)"),
    ("es-la", "Spanish (Latin America)"),
    ("sv", "Swedish"),
    ("th", "Thai"),
    ("tr", "Turkish"),
    ("uk", "Ukrainian"),
    ("vi", "Vietnamese"),
)

SUPPORTED_LANGUAGE_CODES = frozenset(code for code, _label in SUPPORTED_LANGUAGES)
LANGUAGE_CODE_PATTERN = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})?$")


def normalize_language_code(raw: object) -> str:
    code = str(raw).strip().casefold().replace("_", "-")
    if (
        not LANGUAGE_CODE_PATTERN.fullmatch(code)
        or code not in SUPPORTED_LANGUAGE_CODES
    ):
        raise ValueError(f"Unsupported translation language: {raw}")
    return code


def normalize_language_list(raw: object) -> str:
    values = [
        normalize_language_code(item) for item in str(raw).split(",") if item.strip()
    ]
    unique = tuple(dict.fromkeys(values))
    if not unique:
        raise ValueError("At least one search language must be enabled")
    return ",".join(unique)


def parse_language_list(raw: object) -> tuple[str, ...]:
    return tuple(normalize_language_list(raw).split(","))
