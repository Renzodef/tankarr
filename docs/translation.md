---
description: Optional machine-translation fallback in Tankarr for books and chapters missing in your language - local OCR and lettering or an external processor, with its version 1 protocol.
---

# Optional translation fallback

Fallback is off globally and for every series by default. Configure Settings →
Translation, then enable it in a series' Edit dialog. The series language remains
the destination language. Source languages are ordered codes such as
`original,ja,fr`; `original` uses known work metadata and is skipped when unknown.
Each series can override that order, or leave it empty to inherit the global list.

## On this computer (default)

Enter the AI provider API base URL, model and API key. No translation processor
URL or token is required. Tankarr runs local Tesseract OCR and basic lettering,
one job at a time, and sends recognized dialogue to the compatible chat API.
The Docker image includes Tesseract's language models and DejaVu Sans. Native
installations need Tesseract in PATH, the source-language models, and DejaVu Sans
installed as a system font. Missing prerequisites produce an actionable job error.

This is a basic CPU renderer: it places the translation in white rectangles over
recognized text. It does not reconstruct artwork or provide manga-specific neural
OCR. It supports Latin, Greek and Cyrillic destination text; complex pages and
other destination scripts need an external processor. Unrecognized dialogue can
remain in the original language. Language and archive checks cannot prove that
OCR found every text region or that the lettering is good. Review representative
pages before enabling a large queue.

Completed local pages are checkpointed in Tankarr's staging directory. Restarting
resumes from those pages; a provider call interrupted before its checkpoint may
be repeated. Changing the source, languages, model or endpoint invalidates the
checkpoint. Pausing or cancelling stops the local operation; any request already
sent to the AI provider may still be billed. Checkpoints and sources are retained
for retry, and removed after successful import. Incomplete output is removed on
handled interruption or failure. No separate service is needed.

Advanced settings offer an **External processor URL** and **access token** for
an optional compatible service. A nonempty URL selects it instead of local OCR;
clear the URL to return to local processing. The processor token authenticates
Tankarr to that service and is separate from the AI provider key. Never enter
your Tankarr login in these fields.

Tankarr searches normal sources first. Monitored missing slots without an
obtainable native release can use a verified foreign source with matching chapter
or book numbering. Existing identity and numbering checks apply to discovery;
foreign releases are not marked for ordinary downloads. Up to ten translations
are queued per series per Wanted pass, with at most two external processor jobs
outstanding or one local job running.
The original archive stays in staging. Only a validated translation enters the
library, with its source hash, AI model and machine translation provenance in
ComicInfo. A translated book does not retire human chapter files.

The series page lists jobs, permits retries and cancellation, and accepts a source
CBZ assigned to one book or chapter. Use this for a source archive obtained through
another channel, checking that its numbering matches the managed edition. Packs
must first be separated into their individual books. Native library imports keep
their usual language rules.

When Prowlarr is enabled, fallback also searches its configured indexers for
explicitly numbered foreign books and packs. It requires a matching series title
and a source-language marker, then stages only the requested missing books. OCR
on the processor must verify the actual source and target languages before import.
Automatic staging supports image ZIP/CBZ files; other formats and differently sized
editions with an `edition_book_count` override require explicit source assignment.
The configured torrent retention action runs only after the translations have
been imported. A native release appearing in the meantime pauses translation and
has priority; the foreign archive is never treated as a native library file.

Switching either toggle off pauses submissions, polling and publication. An external request
already running on the processor can finish and incur its existing API charges;
its cached result waits for re-enablement. Cancel prevents publication and sends
a durable cancellation request to the processor, retried after connection failures
even while fallback is disabled. Changing the destination language cancels old-language jobs.
Already imported files remain available. Failed jobs need an explicit retry;
temporary connection failures retry automatically with the same processor ID.

## Optional external reference processor

Tankarr itself has no GPU or platform-specific dependency. Run the reference
processor on a host with an installed
[manga-image-translator environment](https://github.com/zyddnys/manga-image-translator#installation).
Keep a working engine version and OCR/render configuration pinned. The reference
runner uses its Python `MangaTranslator` / `Config` API and compatible chat provider
configuration. The processor controls supported OCR/target languages; the
reference runner currently supports the language codes in `contrib/translate_archive.py`.

From a Tankarr checkout, using your own engine locations:

```sh
python3 contrib/translation_processor.py \
  --engine-python /srv/manga-image-translator/.venv/bin/python \
  --engine-root /srv/manga-image-translator \
  --state ./data/translation-processor
```

The default listener is `127.0.0.1:8095`. Set `TRANSLATION_PROCESSOR_TOKEN` in its
environment before exposing it with `--host`; configure the same token and a
reachable URL in Tankarr. Use HTTPS or a trusted private network: the processor is
trusted to receive archive contents and the AI credential. `--config` selects an
engine configuration file; `--cpu` opts into CPU execution. Do not run this heavy
engine on a small host unless it has enough resources.
The runner defaults to the engine's `chatgpt` adapter; a supplied configuration can
select any OpenAI-compatible adapter that the runner accepts (see
`contrib/translate_archive.py`). The selected adapter uses the endpoint, model and
credential from the job. Translation chains and language skip rules are cleared so
they cannot override the requested destination.

Completed pages are saved atomically in `state/checkpoints/<job-id>/`. A restart
or explicit retry can reuse those pages without calling the translator again.
The unfinished page may need to be repeated. Checkpoints contain rendered pages
and OCR/translation text, but no API credential. Their identity includes the
source archive, languages, model, endpoint and OCR/render settings; changed inputs
or corrupt checkpoints are regenerated. Custom dispatchers can enable this with
the runner's `--checkpoint-directory` argument and copy the immutable, hash-named
checkpoint files to durable storage before cleaning up a remote workspace.
Retain that directory alongside the processor state if restart recovery is needed.

Enter the AI provider's API base URL, model identifier and key in Tankarr. The
reference worker accepts providers implementing the compatible chat API. Provider
keys are masked in Settings and use the existing private secrets store, not the
job database. Worker credentials travel in request bodies and subprocess stdin;
they are excluded from command lines, receipts and persisted processor metadata.

The reference worker checks page count and dimensions, untranslated/echoed text,
leftover response markers and detected source/destination languages. These checks
can reject short or unusual text and cannot prove translation or lettering quality.
Test your OCR/render configuration on representative pages before a large queue.
Failed output stays outside the library for review.

## Processor protocol, version 1

Requests use a configured bearer token. IDs are a 32-character hexadecimal job ID,
followed by `-` and its retry number. A transport retry reuses the ID; an explicit
retry increments the number. Processors must treat source and request identity as
immutable and return cached results when the same job is submitted again.

1. `GET /jobs/{id}` returns `404` if submission is needed, or
   `{"status":"queued|running|completed|failed"}`.
2. `PUT /jobs/{id}/source` uploads the CBZ as `application/zip` with Content-Length.
3. `POST /jobs/{id}` submits JSON: `source_sha256`, `source_language`,
   `target_language`, and `ai: {base_url, model, api_key}`. Return a status immediately.
4. `GET /jobs/{id}/result` returns a completed ZIP with all image pages and
   `translation.json`: the source hash/languages, `page_count`, `translated_regions`
   (positive), `untranslated_regions` (zero), `detected_source_language`,
   `detected_target_language` (base language codes), and `model`.
5. `DELETE /jobs/{id}` requests cancellation and returns `202`. The processor
   persists the request before acknowledging it, stops its owned work, and reports
   `cancelling` then `cancelled`. A cancelled ID cannot be submitted again; a retry
   uses the next attempt number. Cancellation must not stop unrelated work.

Tankarr validates the receipt, archive limits and image geometry, rebuilds
ComicInfo, and uses its existing atomic publication and recovery machinery.
The reference processor retains input/output archives in its state directory for
retry recovery; include that directory in your storage/retention planning.

API clients can inspect `GET /api/translations?manga_id=...`, request a normal
search plus fallback with `POST /api/manga/{id}/translations/search`, upload a CBZ
to `POST /api/manga/{id}/translations/upload?source_language=ja&volume=1` (or
`chapter=1.5`), and use `POST /api/translations/{job}/retry` or `/cancel`.
These endpoints use Tankarr's normal authentication.
