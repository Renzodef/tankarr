# Capability-aware translation workers

`contrib/dispatch_translation.py` is an optional example hook. It discovers
current availability by probing an **administrator-enrolled registry**, without
embedding device names or scanning the network. Installing another worker and
adding its endpoint, credential-file path and capacity makes it eligible on the
next invocation. Removing or disabling a worker prevents new assignments.
Hardware names, operating systems and model brands do not select a worker.

This hook requires the reference processor's authenticated version 1 `/health`
endpoint. Older processors without that endpoint remain usable through the
existing direct HTTP integration; upgrade their control plane before enrolling
them in this hook. OCR, translation and image models remain separately managed
on each worker.

## Install the hook

Install the dispatcher and `httpx` in an administrator-controlled environment.
Create an executable wrapper in Tankarr's configured hook directory:

```sh
#!/bin/sh
exec /path/to/python /path/to/contrib/dispatch_translation.py \
  --registry /path/to/workers.json --state /path/to/dispatch-state
```

Select **Script hook** and the wrapper's filename in Settings → Translation.
Configure concurrent hooks to match available worker capacity. All hook
invocations must share the **same local state directory on one coordinator**;
filesystem locks are not a cross-machine lease service. The processor enforces
its own admission limits even when other clients submit work. Keep credentials
in protected files, never in the registry or command arguments.

Example registry:

```json
{
  "workers": [
    {
      "id": "worker-a",
      "url": "https://worker-a.example.test",
      "token_file": "credentials/worker-a.token",
      "max_jobs": 1,
      "minimum_memory_bytes": 2147483648,
      "minimum_disk_bytes": 8589934592,
      "allow_battery": false,
      "pages_per_second": 0.1,
      "transfer_bytes_per_second": 10485760
    }
  ]
}
```

Relative token paths resolve against the registry directory. Set `enabled:false`
to drain a worker. Add six distinct worker entries to use six machines; there is
no code change or fixed device roster. Only register hosts and credentials that
you administer and have authorized. A newly powered-on unknown machine is not
silently enrolled. Enrollment includes a working runtime and actual access to
its input/output transport, not only its presence on the LAN.

## Scheduling and resource use

Health requests run concurrently (at most eight at once), with short timeouts.
New placement requires `protocol_version:1`, `accepting:true`, free capacity,
matching capabilities and sufficient reported disk. Workers on battery are
excluded by default. Optional memory minimums and language advertisements are
also enforced. Unavailable or malformed health responses skip that candidate.

Eligible workers are ranked by estimated processing time, current occupancy and
input/output transfer time. Registry rates are initial estimates; optional
health performance rates and observed elapsed time per input byte refine the
ranking separately for each pipeline. These estimates are heuristics, not
promises: page complexity, model warm-up, cache hits and compression affect them.
A `page_count` hint in the hook request improves initial estimates; without it,
archive size is used. Capacity is limited by both the registry and worker.

A coordinator slot lock prevents two concurrent hook invocations claiming the
same slot. A per-attempt lock prevents simultaneous resubmission of the same
job. The assignment is written durably **before** upload or submission. After
an ambiguous request, all retries go back to that same worker. A removed or
repointed worker with outstanding work leaves that attempt waiting rather than
risking a duplicate job. Drain and reconcile it before permanent removal; a
connection loss is not evidence that computation stopped.

The dispatcher sends the archive directly from its source path to the selected
HTTP processor and streams the result back. In a Tankarr hook deployment the
source path and coordinator are on Tankarr's host. This avoids extra staging
on another control host, but it does not eliminate storage traffic through the
host physically serving those disks. The dispatcher does not move storage,
restart machines, install runtimes or multiply GPU/CPU model instances itself.

## Review, resume and cancellation

The default is review before handoff. After download, the dispatcher writes
`<job>-<attempt>.review.json` containing job ID, attempt, source hash and result
hash. Inspect `dispatch-result.cbz` in that attempt's checkpoint directory, then
write those exact approved fields to `<job>-<attempt>.approved.json` in the
state directory. A retry verifies the cached result and hands it off without
repeating provider calls or downloads. A changed result invalidates approval.
Approval files are a deployment mechanism, not an assertion that an image has
been visually reviewed. Preserve the usual semantic, completeness and lettering
quality checks.

`--no-review` opts into automatic handoff to Tankarr's receipt/archive checks.
It is appropriate only where another review mechanism already guarantees the
required quality. This flag does not disable Tankarr validation. The dispatcher
itself never imports into a library. Results are streamed to bounded temporary
files, fsynced and renamed atomically. Empty or oversized output is refused.
Provider keys stay in memory; saved assignment identities exclude credentials.

Exit `75` means retry the same attempt, including a temporarily busy or unreachable
worker and pending review. Definitive processor failure exits `1`. Signals persist
cancellation intent and request `DELETE` for the pinned remote job. An uncertain
cancellation keeps the pin; another invocation of the same attempt retries the
cancellation until confirmed. No automatic migration or replacement worker runs
that attempt. If the caller never invokes a cancelled attempt again, an operator
must reconcile its durable cancellation record; this hook does not install a
background cancellation daemon.

## Reuse the pool for footer preparation

The same dispatcher accepts an explicit standalone preparation envelope:

```json
{
  "protocol_version": 1,
  "event": "lettering.prepare",
  "pipeline": "footer-reuse",
  "job_id": "0123456789abcdef0123456789abcdef",
  "attempt": 1,
  "source_path": "/path/to/source.cbz",
  "source_sha256": "<sha256>",
  "source_language": "ja",
  "target_language": "en",
  "result_path": "/path/to/preparation-artifacts.zip",
  "checkpoint_directory": "/path/to/checkpoints"
}
```

Only a worker explicitly advertising `footer-reuse` can receive this request.
Its runtime and options are configured by its administrator; the envelope does
not accept provider credentials or runtime overrides for this pipeline. The
worker's output is a ZIP of analysis evidence, **not a translated book**. Keep
that artifact outside the Tankarr media import flow. The normal Tankarr
`translation.requested` envelope always requires the `translation` capability.
The dispatcher can schedule both kinds of work against one enrolled registry,
while execution, semantic review and final publication remain explicit stages.

Footer preparation workers advertise a SHA-256 `pipeline_fingerprint` for the
runtime and fixed configuration. The dispatcher pins it and checks it again on
resume. An unavailable worker waits; a changed fingerprint refuses the old
attempt rather than handing off artifacts produced by a different runtime.
The source archive is hashed initially and whenever device/inode, size, mtime
or ctime changes, including same-size edits. Unchanged retries avoid reading the
whole archive again. Throughput history is separated by pipeline and excludes
resumed-only completions, so a quick result download does not masquerade as a
fast complete translation.

The processor uses separate checkpoint directories for each complete job and
attempt ID. Retrying a transport failure in the same attempt reuses its cache.
A new attempt has a new checkpoint directory; this prevents older cancellation
and overlapping temporary writes from corrupting a later attempt. Legacy caches
stored under only the base job ID are preserved but are not automatically reused
when their ownership cannot be established.

The reference processor's capacity flags are `--workers`, `--max-queue`,
`--min-memory-mib`, `--min-disk-mib`, `--max-load-per-cpu`, `--allow-battery` and
`--min-battery-percent`. Start conservatively, then measure the actual model
memory and throughput before increasing workers. When distributing a standalone
processor installation, include `worker_resources.py` alongside
`translation_processor.py`, plus the selected pipeline's runtime modules.

The MIT CLI also advertises a runtime fingerprint. Whenever a worker advertises
one, the dispatcher pins and rechecks it before returning locally cached results.
MIT's fingerprint covers the runner and vision helper, engine Git revision and
tracked diff, configuration and interpreter identity. It is sampled at worker
startup: restart the worker after changing that runtime. Model weights, Python
packages and untracked engine sources must still be pinned and managed by the
administrator. Legacy processors without an advertised fingerprint remain
compatible, with the corresponding weaker cache identity guarantee.

The MIT processor divides the available CPU count by `--workers` to choose each
engine process's thread budget, with a minimum of one thread. It accounts for
CPU affinity and Linux cgroup v2 quotas. `--threads-per-worker N` explicitly
overrides this budget. OpenMP, MKL, OpenBLAS, NumExpr and Accelerate limits are
set before the engine starts, overriding inherited library settings. The runner
also sets PyTorch intra-operation and OpenCV threads to this budget, PyTorch
inter-operation threads to one, and disables nested OpenMP parallelism. The
budget is included in the runtime fingerprint. If workers exceed available CPUs,
even one thread per worker can compete for CPU; choose the worker count using
measured memory and throughput, particularly for GPU jobs.
