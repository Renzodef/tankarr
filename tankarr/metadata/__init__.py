from __future__ import annotations

from tankarr.config import Settings
from tankarr.metadata.anilist import AniListMetadataSource
from tankarr.metadata.base import MetadataSource
from tankarr.metadata.kitsu import KitsuMetadataSource
from tankarr.metadata.mangabaka import MangaBakaMetadataSource
from tankarr.metadata.mangaupdates import MangaUpdatesMetadataSource
from tankarr.metadata.myanimelist import MyAnimeListMetadataSource


def build_metadata_sources(settings: Settings) -> list[MetadataSource]:
    """Build the metadata catalogues Tankarr queries.

    MangaBaka is the whole spine: identity, counts, description, official
    links - and the identifiers of the same work on MangaUpdates, AniList,
    Kitsu and MyAnimeList. Those four are followers: they are never searched
    by title, only read by the id MangaBaka hands over, so each record is
    the same work by construction. What they add is a second, third and
    fourth opinion on the numbers, which is what lets the canonical record
    tell a corroborated count from a lone claim.
    """

    spine: MetadataSource = MangaBakaMetadataSource(settings)
    followers: list[MetadataSource] = [
        MangaUpdatesMetadataSource(settings),
        AniListMetadataSource(settings),
        KitsuMetadataSource(settings),
        MyAnimeListMetadataSource(settings),
    ]
    for source in followers:
        # Exact identifier only. A follower that could also search by title
        # would reintroduce the ambiguity MangaBaka exists to remove.
        source.automatic_matching = False
    return [spine, *followers]


__all__ = ["MetadataSource", "build_metadata_sources"]
