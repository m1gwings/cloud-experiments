# One persistent study, disposable execution attempts

Repeat `cloud-run CONFIG --ews-ref FULL_COMMIT` to continue the same study after
an interruption, timeout, or cancellation. A replacement VM restores the newest
committed compatible recovery snapshot. EWS decides which simulations, metrics,
variants and checkpoints remain usable. Cloud tooling restores verified bytes;
it does not make scientific reuse decisions.

## Identity and fresh studies

A study ID derives from the canonical repository identity and config path.
HTTPS and SSH URLs for the same repository agree. Source/config contents, EWS
revisions, worker count, machine, display timezone and runtime limits do not
change the lineage. Renaming the repository or config selects another study.
`--fresh` deliberately creates an independent lineage; continue it with
`--study STUDY_ID`. `--name` labels an attempt without changing its identity.

Each attempt has its own exact source/config archive, EWS Git commit, cloud
implementation version/revision/content fingerprint, machine, executed command,
runtime allocation, environment, timestamps, compute and publication outcomes.
The completion shortcut requires an exact scientific request and verified EWS
completion. Operational machine/runtime, sync interval and timezone changes do
not invalidate that shortcut. EWS remains authoritative for changed requests.

## Versioned EWS boundary

This implementation supports **`experiments-wo-stress/recovery`, version 1**,
verified against EWS commit `d14d5c0fd334140ffd8f64e555a9f7112f274972`.
The commit is provenance, not the protocol version: other pins are accepted only
when they expose the explicitly supported contract. The laptop checks the fetched
public declarations before creating compute; the worker queries the installed API
again. Manifests, recovery commits and sync metadata retain the contract version.

EWS owns the [recovery contract and snapshot API](https://github.com/m1gwings/experiments-wo-stress/blob/d14d5c0fd334140ffd8f64e555a9f7112f274972/docs/CLOUD.md#versioned-recovery-snapshots).
Cloud code consumes its sealed inventory instead of interpreting checkpoint,
trajectory or analysis directories. Unknown contracts fail closed. Older cloud
state without this contract remains browsable, but cannot silently become a
modern recovery lineage: use `--fresh` or an explicit future migration.

## Periodic synchronization

`run.sync_seconds` defaults to 300. At each interval the wrapper requests graceful
EWS interruption. Once the sole writer exits, EWS creates a sealed recovery
snapshot; then compute resumes before network transfer. No independent EWS
invocations overlap. A completed invocation proceeds directly to finalization.

**Recovery v1 does not support live snapshots.** It takes the experiment lock,
validates committed artifacts, and copies retained output locally. Allow disk
space for this temporary second copy. Sealing costs local I/O proportional to
retained output; uploads transfer only new or changed content. Cloud tooling does
not bypass that contract with hardlinks to mutable live files.

The target recovery window is one interval plus the time needed to reach a safe
protocol boundary, seal and transfer. A long indivisible step, large initial
snapshot, slow disk/network, or outage can extend it. The displayed last durable
recovery timestamp is the reliable boundary. Failed transfers retain the previous
recovery point, compute continues, and a later interval retries. Cancellation,
the absolute deadline, and the independent deletion timer still take precedence.

## Storage and publication

Cloud-owned storage is organized as follows; EWS's internal layout is intentionally
not specified here:

```text
studies/STUDY_ID/
  environments/ATTEMPT_ID.json
  blobs/SHA256
  snapshots/SNAPSHOT_ID/recovery.json
  commits/COMMIT_ID.json
  attempts/ATTEMPT_ID/
    manifest.json
    lifecycle.json
    source/...
    config/...
    machine/...
    logs/...
    artifacts/...                 # other workspace deltas
```

An immutable parent-linked commit selects an EWS snapshot. There may be many
commits per attempt. Missing parents, competing heads and disconnected histories
fail closed. No mutable latest pointer decides the winner. Content-addressed
objects share unchanged bytes across syncs and attempts; EWS's manifest preserves
paths, empty directories and intentional pruning receipts.

Each synchronization:

1. Uses EWS's sealed inventory, with secret-path and regular-file checks.
2. Uploads newly required content and verifies transferred bytes by downloading
   for comparison; S3 multipart ETags are not assumed to be SHA-256 hashes.
3. Publishes and reads back the sealed EWS snapshot manifest.
4. Rechecks the provider lease and parent. Prunes obsolete committed objects
   referenced by neither the previous recovery point nor the candidate.
5. Publishes and reads back the new cloud recovery commit last.

Pruning deliberately lags by one successful synchronization. This preserves the
previous complete recovery point if deletion or publication is interrupted.
Trajectory removals are accepted only through EWS's validated inventory, including
its intentional-pruning receipts and required retained derivations; a missing
live file alone never authorizes deletion. Unknown orphan uploads are retained
conservatively. Historical commit metadata survives, but arbitrary older output
snapshots are not permanent archives. Download results you need to retain before
later study evolution prunes them. The latest committed snapshot remains complete.

Restore downloads precisely the selected inventory into an isolated directory,
verifies all hashes and sizes, and invokes EWS's atomic restore into a new output
tree. It never merges old remote tails into restored output. EWS then performs
ordinary checkpoint verification/fallback, selection, invalidation and
rematerialization. A damaged transfer fails before scientific execution.

## Writer lease and stale recovery

The provider server name is the study ID, with study and attempt labels. Atomic
unique-name creation acquires the lease. **One Hetzner project must own each
bucket's study namespace.** Do not share it across independent projects or rename
or relabel workers. A losing concurrent launch never deletes the winner.

The worker checks its metadata identity and live provider labels before restore,
environment publication, recovery publication and pruning. Parent-linked commits
expose delayed conflicting publications. A deleted VM releases its lease; a
powered-off or unreachable VM that still exists does not. Never steal a lease
based on elapsed time. Use cancellation or explicit force deletion, then retry.

`cloud-status` and `cloud-results list` derive **interrupted** when stored active
state has no managed server. Provider lookup failures mean unknown VM state,
not confirmed absence. Compute completion, last committed recovery, archive
publication and VM presence are separate columns. Small lifecycle records are
published independently of payload transfers, including finalization failures
and deletion requests. A deletion request is not confirmation; provider lookup
establishes actual absence.

## Environment recreation and EWS authority

The worker recreates the saved exact Python/runtime and index-package lock before
restoring output. It installs archived experiment source and the exact EWS commit,
then reapplies EWS after project dependencies and verifies the resulting lock.
Unrecreatable local/direct dependencies, missing binary wheels or incompatible
runtime/package changes fail setup. Use a compatible environment or `--fresh`.

Execution uses `/work/source`, `/work/.ews` and `/work/output`, with the standard
EWS command plus `--portable --workers N`. N is the available logical CPU count
reported by the runtime (including CPU affinity), not a provider machine table.
The override and optional `--timezone` are recorded in execution provenance and
do not modify YAML or scientific/RNG identity. Portable execution rejects GPU
and custom checkpoint backends through EWS's own validation.

EWS's [portable CPU policy](https://github.com/m1gwings/experiments-wo-stress/blob/d14d5c0fd334140ffd8f64e555a9f7112f274972/docs/PORTABILITY.md)
retains scientific, dependency and architecture compatibility while excluding
ephemeral host/kernel/core-count identity. This is not an OS image or a generic
native-library reproducibility guarantee.

## Finalization and bounded cleanup

Finalization first publishes compute/finalizing metadata, stops setup and the
periodic supervisor, requests a safe EWS boundary (90-second grace), then stops
the experiment cgroup (20-second systemd stop allowance). After EWS stops it uses
the same incremental recovery mechanism one final time. It uploads remaining
logs, provenance and other workspace deltas, then publishes final state.
There is no second EWS output copy or duplicate remote output archive beyond the
local sealed copy required by EWS v1.

EWS completion is distinct from successful final archive publication. Failure
records keep that distinction and the last durable recovery point. The finalizer
still has a 12-minute service limit, OnFailure deletion, and an independent
original-deadline-plus-15-minute reaper. The deletion service can publish a small
failure/deletion record even if the finalizer was killed during a large transfer.
No upload failure keeps compute alive indefinitely. Provider outages or revoked
deletion credentials still require intervention; powered-off VMs remain billable.

## Browsing and reproduction

Use `cloud-results list`, `cloud-status STUDY_ID`, then `cloud-results pull
STUDY_ID --plots` or plain `pull` for the complete current bundle. Result commands
materialize committed EWS output beneath `artifacts/output` from shared objects.
`ls` presents these logical paths; semantic selectors use EWS's artifact catalog.
Full study pulls include attempt inputs, provenance and logs plus current output,
without downloading the entire historical object pool. Earlier pruned attempt
output is explicitly unavailable, rather than silently presented as complete.

`cloud-reproduce ID` creates an independent lineage from archived inputs, exact
EWS revision/settings and environment lock, without restoring its old output.
Legacy archives remain readable. Every reproduction checks its archived exact
EWS commit against the supported recovery contract before creating compute;
unsupported legacy pins and custom legacy commands fail explicitly. Reproduce
those archives locally, or deliberately start a new cloud run with a compatible
EWS pin and standard command. Neither
retrieval nor tests execute downloaded research code or launch cloud VMs.
