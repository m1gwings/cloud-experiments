# One persistent study, disposable execution attempts

```text
logical study
  ├── persistent EWS output (complete committed filesystem generations)
  ├── attempt 1: VM → timeout → persist → delete
  ├── attempt 2: VM → timeout → persist → delete
  └── attempt 3: VM → completed → persist → delete
```

Repeat the same `cloud-run CONFIG --ews-ref FULL_COMMIT` after a timeout, failure,
or cancellation. The VM is disposable; the logical study owns the output.
Machine type and `--max-runtime` may change between attempts. The runtime limit
includes setup and applies to one VM, never the study's total lifetime.

## Identity and fresh studies

The default ID is `s-` followed by the first 32 hex digits of SHA-256 over the
canonical JSON array `[repository_identity, config_path]`. Repository identity is
lowercase hostname plus repository path, without SSH username, leading/trailing
slashes, or `.git`; HTTPS and SSH origins for the same repository agree. The
configuration path is canonical and repository-relative. Renaming the repository
or config therefore selects a different default study. Config/source contents,
Git revisions, machine, runtime, timestamps, and VM IDs do not change it.

`--fresh` assigns a random UUID-based `s-...` lineage. To continue that specific
lineage, use the printed ID with `--study s-...`; repeating `--fresh` intentionally
creates another lineage. Plain repeated `cloud-run` continues the deterministic
default. `--study` checks the archived repository/config-path identity.
`--name` is a descriptive manifest label, not identity.

An execution attempt is `a-<32 study hex digits>-<20 random hex digits>`, so its
storage location is resolvable without a lookup database. Each attempt has its
own source/config archive, exact EWS commit, settings, machine/server metadata,
timestamps, elapsed runtime, exit/upload status, logs, and environment.

## Storage and publication

```text
studies/STUDY_ID/
  environments/ATTEMPT_ID.json       # verified setup environment records
  commits/ATTEMPT_ID.json            # immutable parent-linked state commits
  attempts/ATTEMPT_ID/
    manifest.json
    source/source.tar.gz
    source/index.json
    config/experiment-config
    machine/environment.json
    machine/pip-freeze.txt
    machine/runtime.json
    logs/...
    ews-state.json                   # every output file's SHA-256 and mode
    artifacts/output/...             # COMPLETE EWS output, including restored files
    artifacts/index.json             # other workspace deltas
    artifacts/source/...
```

There is no mutable shared `latest` pointer. A single parent-linked head derived
from `commits/` identifies authoritative state. Missing parents, competing heads,
cycles, and disconnected histories fail closed. Attempt manifests provide the
history; `cloud-results list --attempts STUDY_ID` displays it. Full study pulls
also write a synthesized local study `manifest.json` containing history/head.

Each successful final publication stores a complete generation of the same
logical EWS filesystem. This deliberately retains full copies per attempt for
recovery, rather than mutating one shared output prefix; storage use grows with
attempt count. All scientific files, including checkpoints, result chunks,
instances, catalogs, analysis and reports, survive. Empty directories are not S3
objects and EWS recreates them as needed. Links, special files, or secret-like
paths in the EWS tree cause publication to fail, never a silently partial state.
Custom artifact exclusions cannot remove files from the persistent EWS tree.

The worker refreshes the head **after acquiring its VM lease**, restores the
entire prior generation to `/work/output`, and verifies its inventory checksum,
all file SHA-256 values, canonical paths, and executable modes. Failed restore
or setup does not publish state. Finalization stops writers, copies/checks the
whole tree, uploads/checks the attempt, verifies lease/parent again, then writes
and reads back its immutable commit. A failed upload retains the previous head;
the new VM still gets deleted. There is no periodic live checkpoint mirroring:
a crash/force deletion or failed final upload can lose an entire attempt's new
progress. Older generations remain available. No automatic pruning/migration or
remote deletion is performed.

## Writer lease and stale recovery

The provider server name is the study ID, while labels identify both study and
attempt. Hetzner enforces [unique server names within a project](https://docs.hetzner.com/cloud/servers/getting-started/creating-a-server/).
This atomic provider create is the lease acquisition; object existence or
timestamp checks are not used as a substitute for compare-and-swap. Concurrent
launches report an existing attempt or the losing create fails safely. Cleanup
only discovers/deletes its own attempt ID, never the winner's VM.

**One Hetzner project must own a bucket's study namespace.** Laptop and worker
tokens must address that same project. Do not share this bucket namespace among
independent Cloud projects or rename/relabel managed servers; unique names do
not coordinate across projects. Workers verify live metadata ID/name/labels
before restore, environment publication, and state publication. Immutable commits
also expose a delayed conflicting publication instead of overwriting newer state.

A deleted VM automatically releases the lease, regardless of stale `running`
objects. A powered-off, crashed, or unreachable VM that still exists retains its
lease; never steal it based on elapsed time. Use normal cancellation/reaper, or
explicit force deletion as a last resort, then retry. Check provider state when
deletion credentials are revoked or the provider is unavailable. A VM uniqueness
collision with an unrelated server is an error; it is never deleted for reuse.

## Environment recreation and EWS authority

Source is extracted at `/work/source`, EWS at `/work/.ews`, and outputs at
`/work/output`. Execution uses a normal system-Python venv and
`ews run CONFIG --output /work/output --portable`. New `cloud-run` requests require
the standard EWS command. Custom commands remain reproducible in legacy archives
but do not opt into portable checkpoint guarantees.

The first successfully prepared environment publishes schema-version-1 JSON:
`runtime`, exact index `packages`, and `local_projects` with `ews`/`experiment`
roles. Runtime contains exact Python version/implementation/SOABI, OS family,
architecture, byte order, pointer width, and libc. Raw editable paths, VCS URLs,
credentials, and `pip freeze` directives are not lock inputs. Unsupported extra
direct/local dependencies are rejected. `pip-freeze.txt` remains diagnostic.

Later attempts install exact index versions from this lock using binary wheels,
constrain subsequent installs, install the archived experiment source, and
reapply the exact requested EWS commit. Runtime is checked before installation;
the final package set/runtime/local project roles must match before execution.
Changed local project versions/source are permitted for EWS to evaluate. Missing
wheels, incompatible new dependencies, Python/architecture/libc drift, or changed
package sets fail setup clearly. No incompatible checkpoint is loaded; recreate
the original environment or deliberately use `--fresh`. Environment records
survive even a setup-complete attempt that later fails before simulation.

EWS's [portable CPU/NumPy contract](https://github.com/m1gwings/experiments-wo-stress/blob/main/docs/PORTABILITY.md)
excludes ephemeral hostname/kernel/core-count identity while retaining exact
runtime/numerical-package, architecture, scientific/source/input, recording,
budget, and checkpoint validation. Changed source/config/EWS restores the same
output tree, then EWS selects compatible retained variants or creates new ones.
Cloud tooling never parses or bypasses checkpoint compatibility itself. GPU and
custom checkpoint backends are rejected. This is not a frozen OS image or a
bitwise BLAS/wheel-build reproducibility guarantee. Stronger native numerical
requirements remain the study author's responsibility.

The laptop avoids compute only when the committed head records successful EWS
completion and an exact request fingerprint matches all source index bytes,
source/config provenance, EWS commit/repository, run settings, image family,
portability policy, and tool version. Machine/runtime are excluded. Unknown
completion counts, changed inputs, or setup/failure/timeout/cancel status cannot
claim completion. Otherwise EWS inspects restored work on a new VM.

## Timeout and cancellation

Finalization stops setup first, requests SIGINT to the EWS coordinator through an
unprivileged process wrapper, and waits up to **90 seconds** for safe checkpointing.
It then stops the experiment cgroup with a **20-second** systemd stop allowance
before forced termination. A verified stopped cgroup is required before copying
the output. EWS owns safe protocol-step boundaries and checkpoint fallback;
forced termination may lose work since the previous committed checkpoint.

The attempt retains `timeout` or `cancelled`, uploads complete partial state,
sends best-effort Discord lifecycle notification, and deletes the VM. EWS's own
progress notifications/PTY dashboard remain supported. Finalization has a
12-minute service limit, bounded upload/verification calls, OnFailure deletion,
and an independent original-deadline-plus-15-minute reaper. Upload failure never
keeps a VM alive indefinitely. Provider/guest outages and revoked deletion
credentials still require intervention, as in the existing cleanup guarantees.

## Browsing and reproduction

`cloud-status`, `cloud-attach`, and `cloud-cancel` accept study or attempt IDs.
`cloud-results list` shows logical studies and legacy runs; `list --attempts
STUDY_ID` shows history. `ls STUDY_ID` lists the full hierarchy. Plain `pull
STUDY_ID` and `sync` archive every attempt; `pull ATTEMPT_ID` archives one.
Semantic selectors on a study use only its committed head's EWS `artifacts.json`;
on an attempt they use that attempt's catalog. `--path` is literal relative to
the supplied ID's root; use `ls` instead of remembering paths. Progress/non-TTY
behavior and download markers are unchanged.

Legacy `runs/RUN_ID` is read without reinterpretation. Legacy outputs without an
EWS semantic catalog still require `--path`; no guessed figure/report mapping.
There is no automatic migration of their state into new default studies.

`cloud-reproduce ID` creates an independent lineage from archived source/config,
exact EWS revision/settings, and its verified environment lock when available.
It never restores the original EWS output. Old archives without portable support
keep their original command/strict behavior and have no portable promise or
environment lock. Ordinary continuation always uses `cloud-run`, optionally
`--study` for an explicit fresh/reproduction lineage.

## Manual validation (billable; never run by automated tests)

From the experiment repository, use its exact EWS pin and a config long enough
to exceed setup plus the chosen runtime:

```bash
cloud-run configs/cloud_grid.yml --machine cpx52 --max-runtime 0.25 --ews-ref FULL_EWS_COMMIT
cloud-results list --attempts STUDY_ID
cloud-status STUDY_ID
# After timeout, verified publication and VM deletion, repeat the identical launch:
cloud-run configs/cloud_grid.yml --machine cpx52 --max-runtime 0.25 --ews-ref FULL_EWS_COMMIT
```

Check first-attempt status/upload and its `commits/` entry before repeating.
If setup consumed the whole deadline, increase the per-attempt runtime until
EWS actually starts. Confirm the next attempt logs verified restore and EWS
reports compatible reuse/checkpoint progress. Completed unchanged studies skip
compute. Tests use fake provider trees and local rclone directories only.
