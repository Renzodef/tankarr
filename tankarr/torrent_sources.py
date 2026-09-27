from __future__ import annotations

# "nyaa" survives only as the source of records imported before direct
# Nyaa search was removed; new grabs always go through Prowlarr.
TORRENT_IMPORT_PROVIDERS = frozenset({"nyaa", "prowlarr", "internetarchive"})
EXTERNAL_IMPORT_PROVIDERS = frozenset(
    {*TORRENT_IMPORT_PROVIDERS, "manual", "assembled", "translated"}
)


def torrent_source_label(source: object, indexer: object = None) -> str:
    normalized = str(source or "").casefold()
    indexer_name = " ".join(str(indexer or "").split()).strip()
    if normalized == "prowlarr":
        return f"Prowlarr · {indexer_name}" if indexer_name else "Prowlarr"
    if normalized == "nyaa":
        return "Nyaa.si · direct"
    return indexer_name or "Torrent"
