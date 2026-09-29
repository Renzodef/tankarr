---
title: Tankarr compared with Mylar3, Kapowarr, Suwayomi and readers like Komga
description: How Tankarr differs from Mylar3, Kapowarr, Suwayomi and Mihon, and from readers such as Komga, Kavita and Stump - which tool does what in a self-hosted manga and comics setup, and how they fit together.
---

# Compared with other tools

Tankarr is a **manager**: it decides what a series is, what is missing, where
to get it and how to file it. It is not a reader and not a source. The table
puts it next to the tools people usually consider alongside it; every project
below is good at its own job, and several of them work together with Tankarr.

| | Tankarr | [Mylar3](https://github.com/mylar3/mylar3) | [Kapowarr](https://github.com/Casvt/Kapowarr) | [Suwayomi](https://github.com/Suwayomi/Suwayomi-Server) / [Mihon](https://mihon.app) | [Komga](https://komga.org), [Kavita](https://www.kavitareader.com), [Stump](https://www.stumpapp.dev) |
| --- | --- | --- | --- | --- | --- |
| Made for | Manga, manhwa, webtoons and comics followed like TV series in Sonarr | Western comic books | Western comic books, volume by volume | Reading online sources through extensions | Serving a folder of books to readers |
| Knows a work by | The [MangaBaka](https://mangabaka.org) catalogue, cross-checked with MangaUpdates, AniList, Kitsu and MyAnimeList | ComicVine | ComicVine | Each source's own listing | Folder and file names, `ComicInfo.xml` |
| Gets files from | Suwayomi/Mihon extensions, Prowlarr indexers with qBittorrent and SABnzbd, the Internet Archive, an import folder | Newznab/Torznab, torrents, direct downloads | Torrents, Usenet, direct downloads | Its own extensions | Nothing: you fill the folder |
| Follows | Chapters or volumes, in the language you choose, with monitoring profiles, a Wanted list and a Calendar | Issues | Volumes | New chapters per source | Nothing |
| Writes | One CBZ per chapter or book, `ComicInfo.xml`, covers, hash-checked imports, a recycle bin | CBZ/CBR | CBZ/CBR | Downloads per source | Reads what is there |
| Reads | A built-in web reader, or your Komga, Kavita or Stump | No | No | Yes, web and apps | Yes, web and apps |

## Where Tankarr fits

- **With a reader.** Point Komga, Kavita or Stump at the library Tankarr
  writes. Tankarr can also drive Komga and Stump directly (library scans,
  metadata and covers) and open chapters in the reader from its own pages.
- **With Suwayomi or Mihon extensions.** Tankarr runs a managed Suwayomi
  engine inside its container, or connects to yours, and uses it as one of
  several download channels. It adds what a reading server does not have: a
  catalogue identity for every series, monitoring, the Wanted backlog,
  indexers and a fixed library layout.
- **With the rest of the *arr stack.** Prowlarr, qBittorrent and SABnzbd are
  configured exactly as for Sonarr and Radarr; Tankarr shares the same Docker
  network, categories and download folders.
- **Instead of Mylar3 or Kapowarr** when your shelves are manga, manhwa and
  webtoons rather than Western comic books: those two are built around
  ComicVine and issue or volume numbering, Tankarr around manga catalogues,
  chapter numbering across editions and languages, and official publication
  schedules. For a shelf of Western comics, Mylar3 or Kapowarr remain the
  better fit.

## What Tankarr is not

- **Not a source.** It ships no download sources and no extension repository;
  you configure the ones you have the right to use.
- **Not a reader first.** The built-in reader exists so a library is usable
  on day one; Komga, Kavita and Stump are richer.
- **Not a scraper of sites' titles.** A download source never decides what a
  series is; the catalogue does. That is why a badly named mirror cannot
  corrupt a library, and why adding a series starts from a search of the
  catalogue rather than a URL.
