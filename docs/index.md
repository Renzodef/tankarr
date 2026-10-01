---
title: Tankarr - self-hosted manga and comics manager for the *arr stack
description: Tankarr is a free, open-source Sonarr for manga. It monitors manga, manhwa, webtoons and comics, downloads chapters and volumes through Suwayomi, Prowlarr, qBittorrent and SABnzbd, and builds a clean CBZ library for Komga, Kavita and Stump.
---

# Tankarr

<img src="assets/logo.svg" width="96" alt="Tankarr logo" align="right">

**Tankarr is a self-hosted manga, manhwa and comics manager for the *arr
stack: a Sonarr for manga.** It follows the series you read, downloads new
chapters and volumes in your language, and files them into a clean CBZ library
that Komga, Kavita, Stump or its own built-in reader can open.

It is free software under the GPL-3.0 licence, distributed as a Docker image
for `amd64` and `arm64`.

[Try the online demo](demo/){ .md-button .md-button--primary }
[Install Tankarr](installation.md){ .md-button }

The demo is the real interface on a fictional library: every page, the
Calendar, the Wanted list, Settings and the built-in reader. Nothing is
downloaded and nothing you change is saved.

[![The Tankarr Library: series covers with chapter counts and status](assets/screenshots/library.webp)](screenshots.md)

More in the [screenshots](screenshots.md); if you are weighing it against
Mylar3, Kapowarr or Suwayomi, read [how Tankarr compares](comparison.md).

## What it does

- **Monitors your series** with the familiar *arr profiles (all, future,
  existing or none) and a Wanted list that explains what was tried.
- **Identifies works through a catalogue**, [MangaBaka](https://mangabaka.org),
  cross-checked with MangaUpdates, AniList, Kitsu and MyAnimeList, instead of
  trusting download sites' titles and numbering.
- **Downloads from many channels**: a managed Suwayomi engine with
  Mihon/Tachiyomi-compatible extensions, Prowlarr indexers with qBittorrent and
  SABnzbd, the Internet Archive, and a local import folder.
- **Keeps the language on each release**, and checks indexer downloads with OCR
  before they enter the library.
- **Follows chapters or volumes**, whichever a work actually offers, and can
  assemble chapters into books.
- **Writes a portable library**: consistent names, `ComicInfo.xml`, covers,
  atomic hash-checked imports, a recycle bin and automatic backups.
- **Looks and feels like Sonarr**: Library, Calendar, Activity, Wanted, History,
  Settings and System, with interactive release search.
- **Speaks your language**: the interface is available in English and Italian,
  chosen per browser.

## Start here

1. [Install Tankarr](installation.md) with Docker Compose.
2. [Get started](getting-started.md): connect sources and add your first series.
3. Look up any option in the [configuration reference](configuration.md).
4. Connect your other services: [integrations](integrations.md).
5. Keep it up to date: [upgrading and release channels](upgrading.md).

Questions? Read the [FAQ](faq.md) or ask in the
[discussions](https://github.com/Renzodef/tankarr/discussions).
