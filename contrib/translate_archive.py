#!/usr/bin/env python3
"""Run inside an installed manga-image-translator environment.

Request JSON, including the AI credential, arrives on stdin. No shell command,
configuration file or receipt contains the credential. See docs/translation.md.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import logging
import os
import re
import sys
import zipfile
from contextlib import suppress
from pathlib import Path

LANGUAGES = {
    "en": "ENG",
    "it": "ITA",
    "ja": "JPN",
    "ko": "KOR",
    "zh": "CHS",
    "zh-hk": "CHT",
    "fr": "FRA",
    "de": "DEU",
    "es": "ESP",
    "es-la": "ESP",
    "pt": "PTB",
    "pt-br": "PTB",
    "ru": "RUS",
    "uk": "UKR",
    "vi": "VIN",
    "ar": "ARA",
    "tr": "TRK",
    "id": "IND",
    "th": "THA",
    "pl": "POL",
    "nl": "NLD",
    "cs": "CSY",
    "hu": "HUN",
    "ro": "ROM",
}
IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".avif"}
JAPANESE_OR_HAN = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]")
LATIN_LETTERS = re.compile(r"[A-Za-z\u00c0-\u024f]")
LATIN_SOURCE_LANGUAGES = {
    "en",
    "it",
    "fr",
    "de",
    "es",
    "pt",
    "vi",
    "id",
    "tr",
    "pl",
    "nl",
    "cs",
    "hu",
    "ro",
}


def source_language_sample(lines, language):
    """Classify Latin dialogue without letting retained Japanese headings dominate.

    Manga translated into a Latin language often keeps chapter titles and sound
    effects in Japanese. Exclude those mixed-script OCR regions only when the
    Latin text clearly dominates; otherwise leave the whole sample intact so
    a genuinely Japanese source cannot be accepted as a Latin translation.
    """
    text = "\n".join(lines).casefold()
    if language.split("-", 1)[0] not in LATIN_SOURCE_LANGUAGES:
        return text
    latin = sum(len(LATIN_LETTERS.findall(line)) for line in lines)
    cjk = sum(len(JAPANESE_OR_HAN.findall(line)) for line in lines)
    if cjk and latin >= 300 and latin >= 4 * cjk:
        return "\n".join(
            line for line in lines if not JAPANESE_OR_HAN.search(line)
        ).casefold()
    return text


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def transient_error(error):
    """Recognize transport/rate-limit failures, including wrapped SDK errors."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (InterruptedError, ConnectionError, TimeoutError)):
            return True
        status = getattr(error, "status_code", None) or getattr(
            getattr(error, "response", None), "status_code", None
        )
        if isinstance(status, int) and (status == 429 or 500 <= status < 600):
            return True
        for base in type(error).__mro__:
            if base.__module__.split(".")[0] in {
                "httpx",
                "httpcore",
                "requests",
                "openai",
            } and base.__name__ in {
                "TimeoutException",
                "Timeout",
                "NetworkError",
                "ConnectionError",
                "APIConnectionError",
            }:
                return True
        error = error.__cause__ or error.__context__
    return False


def natural_key(value):
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", value)
    ]


def region_counts(regions):
    translated, untranslated = 0, 0
    for region in regions:
        source = str(getattr(region, "text", "") or "").strip()
        if not any(character.isalpha() for character in source):
            continue
        target = str(getattr(region, "translation", "") or "").strip()
        if (
            not target
            or target.casefold() == source.casefold()
            or re.search(r"<\|\|?\d+\|>", target)
        ):
            untranslated += 1
        else:
            translated += 1
    return translated, untranslated


def checkpoint_identity(request, config):
    value = {
        "version": 1,
        **{
            key: request[key]
            for key in ("source_sha256", "source_language", "target_language")
        },
        "ai": {key: request["ai"][key] for key in ("base_url", "model")},
        "config": config,
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def load_checkpoint(directory, index, identity, size):
    from PIL import Image

    if directory is None:
        return None
    for path in sorted(directory.glob(f"{index:05d}-*.zip")):
        try:
            if path.stat().st_size > 128 * 1024**2:
                continue
            if digest(path) != path.stem.split("-", 1)[1]:
                continue
            with zipfile.ZipFile(path) as archive:
                if sorted(archive.namelist()) != ["page.json", "page.png"]:
                    continue
                if (
                    archive.getinfo("page.json").file_size > 2 * 1024**2
                    or archive.getinfo("page.png").file_size > 128 * 1024**2
                ):
                    continue
                page = json.loads(archive.read("page.json"))
                if page.get("identity") != identity or page.get("index") != index:
                    continue
                data = archive.read("page.png")
            with Image.open(io.BytesIO(data)) as image:
                image.load()
                if image.size != size:
                    continue
            if page["untranslated"] != 0 or page["translated"] < 0:
                continue
            return data, page
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile):
            continue
    return None


def save_checkpoint(directory, index, data, page):
    if directory is None:
        return
    metadata = json.dumps(page)
    if len(data) > 128 * 1024**2 or len(metadata.encode()) > 2 * 1024**2:
        raise ValueError("Page checkpoint exceeds processor limits")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    partial = directory / f"{index:05d}.partial"
    try:
        with partial.open("wb") as handle:
            with zipfile.ZipFile(
                handle, "w", compression=zipfile.ZIP_STORED
            ) as archive:
                archive.writestr("page.png", data)
                archive.writestr("page.json", metadata)
            handle.flush()
            os.fsync(handle.fileno())
        partial.replace(directory / f"{index:05d}-{digest(partial)}.zip")
        if os.name == "posix":
            with suppress(OSError):
                descriptor = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
    finally:
        partial.unlink(missing_ok=True)


async def translate(
    source: Path,
    output: Path,
    request: dict,
    *,
    gpu=True,
    config=None,
    checkpoints=None,
):
    if digest(source) != request["source_sha256"]:
        raise ValueError("Source digest mismatch")
    target = LANGUAGES.get(request["target_language"])
    if target is None:
        raise ValueError(
            "This processor does not support the requested target language"
        )
    ai = request["ai"]
    previous_factory = logging.getLogRecordFactory()

    def safe_record(*args, **kwargs):
        record = previous_factory(*args, **kwargs)
        record.msg = record.getMessage().replace(ai["api_key"], "[redacted]")
        record.args = ()
        # Third-party exception bodies can echo credentials too.
        record.exc_info = None
        record.exc_text = None
        return record

    logging.setLogRecordFactory(safe_record)
    # The engine reads credentials at import time. Export the job's provider
    # settings for every supported OpenAI-compatible adapter, so whichever one
    # the engine configuration selects uses them.
    environment = {
        f"{prefix}_{key}": ai[value]
        for prefix in ("OPENAI", "DEEPSEEK")
        for key, value in (
            ("API_KEY", "api_key"),
            ("API_BASE", "base_url"),
            ("MODEL", "model"),
        )
    }
    previous_environment = {key: os.environ.get(key) for key in environment}
    os.environ.update(environment)
    try:
        await _translate_pages(
            source, output, request, target, gpu, config, checkpoints
        )
    finally:
        logging.setLogRecordFactory(previous_factory)
        for key, value in previous_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


async def _translate_pages(source, output, request, target, gpu, config, checkpoints):
    import py3langid
    from manga_translator import MangaTranslator
    from manga_translator.args import reparse
    from manga_translator.config import Config
    from PIL import Image

    source_language = request["source_language"]
    engine_config = dict(config or {})
    adapter = engine_config.get("translator", {}).get("translator", "chatgpt")
    if adapter not in {"chatgpt", "deepseek"}:
        raise ValueError("Configure the chatgpt or deepseek compatible API adapter")
    engine_config["translator"] = {
        **engine_config.get("translator", {}),
        "translator": adapter,
        "target_lang": target,
        "no_text_lang_skip": True,
        "translator_chain": None,
        "selective_translation": None,
        "skip_lang": None,
    }
    engine_config.setdefault(
        "ocr",
        {
            "ocr": "48px" if source_language in {"ja", "zh", "zh-hk"} else "48px_ctc",
            "use_mocr_merge": False,
        },
    )
    engine_config.setdefault(
        "render",
        {
            "renderer": "manga2eng" if target == "ENG" else "default",
            "rtl": source_language == "ja",
        },
    )
    engine_config.setdefault("upscale", {"upscale_ratio": None})
    engine_config.setdefault("detector", {"detection_size": 1024})
    identity = checkpoint_identity(request, engine_config)
    parsed = Config(**engine_config)
    # The Python constructor requires some CLI defaults (notably kernel_size).
    # Read them from the installed engine, then apply our explicit GPU choice.
    params = vars(reparse([]))
    params.update(use_gpu=gpu, ignore_errors=False, verbose=False)

    class ArchiveTranslator(MangaTranslator):
        def _setup_log_file(self):
            # Keep job artifacts inside the processor workspace. The engine's
            # default file logger writes into its shared checkout's result/.
            pass

    translator = ArchiveTranslator(params)
    translated = untranslated = 0
    source_text, target_text = [], []
    partial = output.with_suffix(".partial")
    try:
        with (
            zipfile.ZipFile(source) as archive,
            zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_STORED) as result,
        ):
            pages = sorted(
                [
                    item
                    for item in archive.infolist()
                    if not item.is_dir()
                    and Path(item.filename).suffix.casefold() in IMAGES
                ],
                key=lambda item: natural_key(item.filename),
            )
            if (
                not pages
                or len(pages) > 20000
                or sum(item.file_size for item in pages) > 16 * 1024**3
            ):
                raise ValueError("Archive exceeds processor limits")
            for index, item in enumerate(pages, 1):
                if item.file_size > 128 * 1024**2:
                    raise ValueError("Image exceeds processor limit")
                with archive.open(item) as handle, Image.open(handle) as image:
                    image.load()
                    cached = load_checkpoint(checkpoints, index, identity, image.size)
                    if cached is None:
                        ctx = await translator.translate(image.convert("RGB"), parsed)
                        rendered = getattr(ctx, "result", None)
                        if rendered is None or rendered.size != image.size:
                            raise ValueError("Engine did not return a complete page")
                        regions = getattr(ctx, "text_regions", None) or []
                        good, bad = region_counts(regions)
                        if bad:
                            raise ValueError(
                                "Untranslated or invalid text regions; review the OCR configuration"
                            )
                        page = {
                            "identity": identity,
                            "index": index,
                            "translated": good,
                            "untranslated": bad,
                            "source_text": [
                                str(getattr(r, "text", "") or "") for r in regions
                            ],
                            "target_text": [
                                str(getattr(r, "translation", "") or "")
                                for r in regions
                            ],
                        }
                        if request["ai"]["api_key"] in json.dumps(
                            page, ensure_ascii=False
                        ):
                            raise ValueError("Provider response contains a credential")
                        buffer = io.BytesIO()
                        rendered.save(buffer, format="PNG")
                        data = buffer.getvalue()
                        save_checkpoint(checkpoints, index, data, page)
                    else:
                        data, page = cached
                    translated += page["translated"]
                    untranslated += page["untranslated"]
                    source_text.extend(page["source_text"])
                    target_text.extend(page["target_text"])
                    result.writestr(f"{index:05d}.png", data)
            if translated == 0:
                raise ValueError(
                    "No translated text was detected; review OCR before importing"
                )
            # Manga OCR often emits entire balloons in capitals. LangID's
            # casing bias can overwhelm an otherwise clear language signal.
            detected_source = py3langid.classify(
                source_language_sample(source_text, source_language)
            )[0]
            detected_target = py3langid.classify("\n".join(target_text).casefold())[0]
            if (
                detected_source != source_language.split("-")[0]
                or detected_target != request["target_language"].split("-")[0]
            ):
                raise ValueError(
                    "OCR or translated text does not match the requested languages"
                )
            receipt = {
                key: request[key]
                for key in ("source_sha256", "source_language", "target_language")
            }
            receipt.update(
                page_count=len(pages),
                translated_regions=translated,
                untranslated_regions=untranslated,
                model=request["ai"]["model"],
                detected_source_language=detected_source,
                detected_target_language=detected_target,
            )
            result.writestr("translation.json", json.dumps(receipt))
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--checkpoint-directory", type=Path)
    parser.add_argument("--cpu", action="store_true")
    args = parser.parse_args()
    request = json.loads(sys.stdin.buffer.read(64 * 1024))
    config = json.loads(args.config.read_text()) if args.config else None
    try:
        asyncio.run(
            translate(
                args.source,
                args.output,
                request,
                gpu=not args.cpu,
                config=config,
                checkpoints=args.checkpoint_directory,
            )
        )
    except Exception as error:  # noqa: BLE001 - do not print SDK bodies or credentials
        temporary = transient_error(error)
        print(
            "Temporary provider interruption"
            if temporary
            else "Translation or validation failed",
            file=sys.stderr,
        )
        raise SystemExit(75 if temporary else 1) from None


if __name__ == "__main__":
    main()
