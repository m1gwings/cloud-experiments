# Internal design

`bin/cloud-*` are identical entry points that dispatch by executable name. The
shared package in `lib/cloud_experiments` has no third-party Python dependencies:

| Module | Responsibility |
| --- | --- |
| `cli.py` | Commands, run orchestration, failure ownership, reproduction, downloads. |
| `config.py` | Non-secret TOML validation; runtime-only credential selection. |
| `common.py` | Safe subprocess wrapper, IDs, atomic JSON, hashing, label checks. |
| `progress.py` | Nested stderr activities, TTY detection, periodic updates, numeric transfer statistics. |
| `source.py` | Git inspection, exact source snapshots and EWS pin discovery. |
| `workspace.py` | Safe extraction, secret/path exclusions and workspace inventories. |
| `artifacts.py` | Discover and validate EWS semantic catalogs; resolve role paths without internal layout mappings. |
| `providers.py` | Structured hcloud/rclone arguments; quoted SSH; remote manifests. |
| `bootstrap.py`, `failsafe.py` | Fixed-size cloud-init deadline timer and standalone checked self-deletion. |
| `runtime.py` | Post-SSH worker archive, checksum and activation. |
| `studies.py` | Stable logical IDs, exact-request fingerprints and parent-linked state validation. |
| `environment.py` | Safe exact environment capture, requirements and validation. |
| `ews_contract.py` | Explicit supported EWS recovery envelope and public API adapter. |
| `provenance.py` | Cloud implementation revision and content fingerprint. |
| `persistence.py` | Verified recovery transfer, restore, delayed pruning and commits. |
| `physical.py` | Immutable packed containers and checked cloud physical layouts; EWS inventory stays logical. |
| `synchronization.py` | Cooperative periodic pause, seal, resume and retry. |
| `diagnostics.py` | Validated failure capsule schema, selected lifecycle fields and bounded redaction. |
| `worker.py` | Installation, PTY execution, supervision, finalization, direct API deletion. |

The default EWS argument array, explicit portable CPU/NumPy policy, inspect counts,
optional webhook environment name, recovery v1 and versioned artifact catalog are
EWS-specific. Checkpoint decoding/selection is exclusively EWS-owned. Paper-specific algorithms, instance generators, and
configuration belong in experiment repositories.

## Lifetime and ownership

1. Laptop validates Git, builds the tarball/index/config, resolves EWS, and uploads
   checked immutable inputs plus a provisioning manifest before creating compute.
2. `hcloud server create` receives only the small root-only failsafe files via stdin. No
   secret is embedded in an argument or written to laptop state. hcloud JSON is
   consumed privately; raw output, including possible generated passwords, is
   never printed or persisted.
3. First boot arms the persistent absolute deadline timer before apt or SSH
   upload. Readiness requires that timer. A boot-relative trigger covers first
   initialization after the deadline. The deadline service remains installed and
   active throughout provisioning. If the laptop disappears before runtime upload,
   its standalone handler verifies metadata identity and live Hetzner labels/name
   before deleting this VM; failed requests retry.
4. Once SSH confirms the armed marker, the laptop sends one private full-runtime
   archive with worker code, units and runtime-only credentials. The VM verifies
   its SHA-256 before installing files, reloads systemd without stopping the
   deadline timer, and atomically marks the runtime ready last. The stable deadline
   handler then runs normal worker expiry, with checked deletion as a fallback if
   that path fails. Corrupt or incomplete archives cannot start the supervisor.
   Laptop-side failure before activation requests checked provider deletion.
5. Laptop transfers source/index and starts `cloud-supervisor.service`. The root
   supervisor installs OS tools, verifies its provider lease, recreates the locked
   unprivileged environment, queries EWS compatibility and restores committed output.
   Source, EWS checkout, runtime and package checks precede execution.
6. A dedicated systemd cgroup runs the experiment's tmux server as `experiment`.
   `script` gives the command a PTY and captures stdout/stderr together. A file
   records the command exit code; the root supervisor watches it and the service.
   Losing tmux is detected as a failure even if the exit record is absent.
   The runtime supplies all available CPU workers and validates display timezone
   through EWS. A periodic graceful pause lets EWS seal a recovery snapshot; the
   sole invocation resumes before incremental network transfer. Transfer failures
   leave the preceding committed snapshot usable and retry at later intervals.
   When `run.ews_discord` is enabled, systemd loads only the Discord webhook
   into this service's protected credential directory. The wrapper reads it
   through `CREDENTIALS_DIRECTORY` and constructs the EWS environment explicitly;
   it never copies the parent environment or passes the URL in argv. The source
   credential is root-owned mode 600 outside `/work`, and the original provider
   credential paths remain inaccessible to the experiment service.
7. Completion, failure, or cancellation starts a separate root
   finalizer. It stops setup, asks EWS to checkpoint through SIGINT with 90 seconds
   grace, then stops the experiment cgroup. It runs the same recovery sync one
   final time, collects other workspace deltas, and publishes final archive state
   only after verification. Final snapshot and transfer subprocesses have no
   independent wall-clock limit. Small lifecycle records precede large operations and
   carry failures independently of artifact transfer.
8. Discord distinguishes compute, recovery, archive and deletion requests. It is
   bounded and cannot prevent deletion. Failure alerts point to Object Storage
   capsules instead of carrying logs. The deletion service independently
   publishes a small status even if the finalizer has been killed. A
   finally block starts `cloud-delete.service`; an abnormal service exit routes
   through the bounded `cloud-failure@.service` before the next cleanup unit.
   If capture fails or times out, `OnFailure` starts deletion. Deletion failures
   restart after 30 seconds.
9. At the absolute deadline, the deadline service stops setup, experiment, and
   any unfinished finalizer, then requests deletion. Its failure path also
   requests deletion. Before that deadline, deletion retries defer to an active
   finalizer until it finishes publishing.

Failures caught in Python and abnormal systemd exits use the same capsule writer.
It selects a versioned set of lifecycle fields and records the current root-owned
stage, safe error context and at most 200 recent journal lines (256 KiB maximum).
The journal covers cloud worker units only and is redacted before transfer.
Each capsule is append-only under `attempts/ATTEMPT_ID/diagnostics/EVENT_ID/`
for a study attempt (or `runs/RUN_ID/diagnostics/EVENT_ID/` for a legacy run).
`failure.json` is the final marker, published after journal verification. A failed
partial upload is ignored by the reader. Normal success writes only the tiny
local stage file and no remote diagnostic artifacts. Diagnostics never enter
`artifacts/output`, alter a recovery commit, or delay cleanup without a bound.

The direct API deletion path never trusts a requested server ID alone. It reads
the VM's metadata identity and checks name, ID, and both labels against the live
API resource (study and attempt labels for modern workers). A 404 is idempotent success. The laptop similarly re-describes a
known ID before deleting. Label drift intentionally causes refusal and requires
manual investigation; safe targeting takes precedence over deleting an unknown VM.

Root-owned reason/finalization locks serialize concurrent cancellation and
completion. A deadline can interrupt an in-flight finalizer, but never rewrites an
already finalized successful/failed run during deletion retries. Repeated
finalization reuses its completion marker. A retained setup failure can still
transition to timeout or cancellation at a later request.

## Failure boundaries

| Failure | Response |
| --- | --- |
| Bad input / dirty tree / failed EWS fetch / failed preflight upload | No VM created. |
| Forwarding enabled but webhook missing | Reject before storage mutation or VM creation, including reproductions. |
| Creation response lost | Discover only exact matching managed labels/name; request cleanup. |
| SSH/runtime upload failure | Checked laptop deletion; armed minimal deadline retries independently if needed. |
| Source upload/setup failure after runtime activation | Worker finalization, or checked laptop deletion if unreachable. |
| Laptop dies before/during setup | The armed failsafe or full worker owns eventual deletion. |
| pip/apt hangs | Supervisor cgroup stopped at deadline; forced kill after 20 seconds. |
| Experiment exits / tmux dies | Capture exit when available; finalize as completed/failed. |
| Periodic sync failure | Retain preceding recovery; resume compute and retry later. |
| Final upload/collection failure | Record `finalization_failed` independently; delete anyway. |
| Finalizer hangs | Absolute deadline stops it and requests deletion; previous recovery remains usable. |
| Finalizer crashes | OnFailure requests checked deletion. |
| Worker API permission/availability failure | Retry deletion; user must restore access or intervene. |
| Guest never boots/cloud-init fails/kernel freezes | No in-guest guarantee; inspect Console. |

No filesystem or network API provides an atomic transaction spanning source
upload, manifest upload, and server deletion. A stale manifest does not prove a
VM is running; both status and results listings consult managed servers and show
`interrupted` for active stored state with no VM. Unknown provider state is not
absence. Last-resort deletion
may leave partial artifacts, because limiting compute cost is the stated priority.

## Snapshot and artifact invariants

Git supplies candidate filenames and provenance; copied worktree bytes are the
execution source. Each source path has SHA-256 and executable bits in an index,
and the archive has its own manifest checksum. Unsafe archive paths, duplicate
members, links, and special files are rejected. Dirty provenance is preserved
through reproduction without consulting current Git. Configuration is extracted
from the verified archive and checked against its original checksum.

Artifact comparison covers `/work`, excludes enumerated dependency/control/secret
paths, and never follows links. Source comparison uses the pre-install source
index so build hooks cannot hide modified source. Other directories get a
pre-experiment baseline. Root credentials, cloud-init logs, and arbitrary system
directories are never artifact inputs. Deletions/exclusions are explicit index
entries. Terminal output is recorded as bytes rather than decoded/reformatted.

Study identity, atomic provider-name leases, append-only state publication,
environment locking and compatibility are specified in [continuation.md](continuation.md).
All workers for a bucket namespace must use one Hetzner project; deleted VMs
release leases while still-existing failed VMs require explicit cleanup.
Exact package/runtime locks are verified before portable EWS execution. This is
not a bitwise OS-image or arbitrary native-library reproducibility guarantee.

`run.ews_discord` is a boolean in the saved run settings, defaulting to false
for legacy manifests. Reproduction preserves the original setting but supplies
the current laptop webhook. Cloud lifecycle notifications remain independent.
The root webhook file and systemd runtime copy are outside artifact inputs, and
the known credential filename is excluded if accidentally copied into `/work`.
As with other trusted experiment code, arbitrary environment dumps or encoded
copies of secrets must not be written to scientific artifacts. The forwarding
option grants the research process webhook access, not Hetzner/S3 access.

## Verification

Result browsing uses recursive `rclone lsjson --files-only --no-modtime
--no-mimetype` at the validated run prefix. Only object paths and byte sizes are
retained for legacy archives. Modern recovery views also read committed recovery
inventories and their hashes to expose logical paths; output payloads are not
fetched by listing. The default redirected output
is headerless TSV, and `--json` provides structured records. Paths are validated
before rendering to keep controls/traversal out of terminal output and downloads.

Modern recovery pulls materialize only the selected committed inventory from
content-addressed loose objects or packed containers, verifying SHA-256 and sizes
for every materialized member. The checked physical layout is cloud-owned and
published before the authoritative commit. Descriptor-free older commits use
loose blobs, and mixed lineages require no migration. Pack garbage collection
waits until no retained state references a member; unknown uploads are untouched.
Listings expose logical
`artifacts/output` paths rather than implementation object keys. Study semantic
pulls select the authoritative committed snapshot first.
Legacy semantic pulls discover a unique `artifacts.json` under captured `artifacts/`
using the listing. Its parent is the EWS output root, even for a custom command.
The catalog discriminator is `experiments-wo-stress/artifacts`, version 1;
`figures`, `analysis`, and `compute_report` map to CLI selectors. Bounded metadata
reads use the existing activity adapter; both the advertised size and actual read
are capped at 64 KiB. JSON duplicate keys, unknown schema versions, invalid kinds,
non-boolean optional flags, and noncanonical/escaping paths are rejected before
copying. Unknown roles/fields can be added without a version change. EWS owns
layout and path updates, while the cloud run manifest retains the exact EWS commit.
No global Git SHA or EWS package version gates retrieval.

Missing catalogs (including legacy runs), unsupported formats, or multiple
catalogs require `ls` and explicit `--path`; there is no hardcoded fallback.
Unpublished roles and optional roles without stored objects succeed with an
explanation. Required roles without stored objects fail. Presence is determined
from stored objects, not cached booleans. Neither discovery nor retrieval
executes experiment code or writes remote data.

Generic selective pulls match an exact path or its `/`-delimited descendants against
that listing. A private temporary `--files-from-raw` list drives `rclone copy`
from the run root to the local run root; paths are literal, never filter globs.
Destination symlinks and the reserved full-download marker are rejected before
copying. Semantic file roles match only the exact object; directory roles match
only descendants. Missing generic paths and provider failures remain errors.
Selective copies share the transfer progress adapter and never create/refresh
`.cloud-pulled.json`. Full pulls write that marker after success. Modern pulls include current recovery
and attempt archives; superseded output is explicitly unavailable once pruned.
A verified full output replaces the local output tree, preventing deleted tails
from reappearing. No result command deletes remote objects.

`python3 -m unittest discover -s tests -v` exercises real local Git snapshots with
temporary directories and mocked provider effects. Synthetic tokens never leave
the test process. Provider subprocess errors omit raw output and command
arguments; error redaction is tested. No live integration test runs by default.

CLI activities use a small stdlib background ticker so blocking library work
and retry loops remain visible. Nested activities share the outer display;
failure and interruption close it before error handling continues. Either
redirected output stream, `TERM=dumb`, or nonempty `NO_COLOR` disables animation.
Quick metadata calls defer their display to avoid noisy result listings.

Transfer commands opt into [rclone JSON statistics](https://rclone.org/docs/#use-json-log)
with `--use-json-log --stats 1s --stats-log-level NOTICE`, never raw `--progress`.
The subprocess wrapper drains stdout and stderr concurrently with selectors,
retains the captured bytes and return status, and feeds bounded stderr lines to
a numeric-field allowlist. Messages, object names, URLs, invalid values and
non-statistics records are never displayed. Capture-only commands (including
manifest reads and provider JSON) keep the original subprocess path. Transfer
timeouts and interruption kill/reap the process group before reporting failure.
Tests exercise both pipe modes, malformed/oversized records, error privacy,
interruption, download markers, recursive listings, literal path selection,
traversal rejection, and real rclone copy/check/selective retrieval between
temporary local directories with an empty configuration; this never uses
Object Storage.

The provider adapter follows the official [hcloud create flags](https://github.com/hetznercloud/cli/blob/main/docs/reference/manual/hcloud_server_create.md)
and [JSON/label-filtered list interface](https://github.com/hetznercloud/cli/blob/main/docs/reference/manual/hcloud_server_list.md).
Uploads use [rclone check](https://rclone.org/commands/rclone_check/) for sizes and
available remote hashes, followed by an exact manifest read-back. Source/config
reproduction additionally verifies SHA-256 locally. EWS CLI defaults were checked
against the local `experiments-wo-stress` README and `docs/CONFIGURATION.md` during
implementation. New continuation requires the standard command, explicit recovery v1 capability
and portable EWS policy. Reproduction checks the exact archived pin against the
supported contract and requires the standard command; legacy retrieval remains
available without reinterpreting old recovery state.
The detailed recovery transaction, delayed pruning and failure invariants live
in [continuation.md](continuation.md#storage-and-publication).

Cloud-init contains a fixed standalone deletion script, immutable attempt/study
identity, the dedicated worker deletion token and two deadline units. Its size
does not scale with ordinary worker features. Runtime code, Object Storage
credentials, and both optional Discord uses arrive only after SSH readiness.
Textual cloud-init files use supported [gzip/base64 encoding](https://docs.cloud-init.io/en/25.3/reference/yaml_examples/write_files.html)
when smaller. A 24 KiB preflight budget leaves at least 8 KiB beneath Hetzner's
32 KiB limit. Tests decode the payload, exercise the installer and verify all units.
