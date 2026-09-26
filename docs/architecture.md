# Internal design

`bin/cloud-*` are identical entry points that dispatch by executable name. The
shared package in `lib/cloud_experiments` has no third-party Python dependencies:

| Module | Responsibility |
| --- | --- |
| `cli.py` | Commands, run orchestration, failure ownership, reproduction, downloads. |
| `config.py` | Non-secret TOML validation; runtime-only credential selection. |
| `common.py` | Safe subprocess wrapper, IDs, atomic JSON, hashing, label checks. |
| `progress.py` | Nested stderr activities, TTY detection, periodic updates, numeric transfer statistics. |
| `source.py` | Git inspection, exact snapshots, safe extraction, file inventories. |
| `providers.py` | Structured hcloud/rclone arguments; quoted SSH; remote manifests. |
| `bootstrap.py` | Compressed Python bundle and systemd units in cloud-init JSON/YAML. |
| `worker.py` | Installation, PTY execution, supervision, finalization, direct API deletion. |

The laptop makes no assumptions about EWS internals. The default argument
array and optional webhook environment name are EWS-specific. Paper-specific algorithms, instance generators, and
configuration belong in experiment repositories.

## Lifetime and ownership

1. Laptop validates Git, builds the tarball/index/config, resolves EWS, and uploads
   checked immutable inputs plus a provisioning manifest before creating compute.
2. `hcloud server create` receives the root-only cloud-init files via stdin. No
   secret is embedded in an argument or written to laptop state. hcloud JSON is
   consumed privately; raw output, including possible generated passwords, is
   never printed or persisted.
3. First boot arms persistent absolute deadline/reap timers before apt or SSH
   upload. Readiness requires both active timers. Boot-relative timers provide a
   second trigger if first boot initialization passed a deadline.
4. Laptop transfers source/index and starts `cloud-supervisor.service`. The root
   supervisor installs OS tools and an unprivileged Python environment. Source
   and EWS checkout checks precede installation/execution.
5. A dedicated systemd cgroup runs the experiment's tmux server as `experiment`.
   `script` gives the command a PTY and captures stdout/stderr together. A file
   records the command exit code; the root supervisor watches it and the service.
   Losing tmux is detected as a failure even if the exit record is absent.
   When `run.ews_discord` is enabled, systemd loads only the Discord webhook
   into this service's protected credential directory. The wrapper reads it
   through `CREDENTIALS_DIRECTORY` and constructs the EWS environment explicitly;
   it never copies the parent environment or passes the URL in argv. The source
   credential is root-owned mode 600 outside `/work`, and the original provider
   credential paths remain inaccessible to the experiment service.
6. Completion, failure, cancellation, or a deadline starts a separate root
   finalizer. It stops setup and experiment cgroups, collects workspace deltas,
   writes metadata, copies/checks payloads, then publishes/verifies the manifest.
   Upload errors get a bounded best-effort manifest-only retry.
7. Optional Discord notification is bounded and cannot prevent deletion. A
   finally block starts `cloud-delete.service`; systemd `OnFailure` handles an
   abnormal finalizer exit/timeout. Deletion failures restart after 30 seconds.
8. At the original deadline + 15 minutes, the independently installed reap timer
   starts that same delete service even if any other component is stuck.

The direct API deletion path never trusts a requested server ID alone. It reads
the VM's metadata identity and checks name, ID, and both labels against the live
API resource. A 404 is idempotent success. The laptop similarly re-describes a
known ID before deleting. Label drift intentionally causes refusal and requires
manual investigation; safe targeting takes precedence over deleting an unknown VM.

Root-owned reason/finalization locks serialize concurrent cancellation and
completion. A timeout may supersede an in-flight reason, but never rewrites an
already finalized successful/failed run during deletion retries. Repeated
finalization reuses its completion marker. A retained setup failure can still
transition to timeout or cancellation at a later request.

## Failure boundaries

| Failure | Response |
| --- | --- |
| Bad input / dirty tree / failed EWS fetch / failed preflight upload | No VM created. |
| Forwarding enabled but webhook missing | Reject before storage mutation or VM creation, including reproductions. |
| Creation response lost | Discover only exact matching managed labels/name; request cleanup. |
| SSH/upload/setup failure | Worker finalization, or checked laptop deletion if unreachable. |
| Laptop dies before/during setup | First-boot timers remain responsible for eventual deletion. |
| pip/apt hangs | Supervisor cgroup stopped at deadline; forced kill after 20 seconds. |
| Experiment exits / tmux dies | Capture exit when available; finalize as completed/failed. |
| Upload/collection failure | Record partial failure where possible; delete anyway. |
| Finalizer hangs or crashes | Bounded service, OnFailure deletion, and independent reap timer. |
| Worker API permission/availability failure | Retry deletion; user must restore access or intervene. |
| Guest never boots/cloud-init fails/kernel freezes | No in-guest guarantee; inspect Console. |

No filesystem or network API provides an atomic transaction spanning source
upload, manifest upload, and server deletion. A stale manifest does not prove a
VM is running; `cloud-status` consults actual managed servers. Last-resort deletion
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

Full dependency/environment bit-for-bit reproducibility is outside this first
version: Ubuntu image packages and unpinned pip dependencies may change. Git
source, config bytes, and EWS commit are fixed; environment details and pip freeze
are recorded. User lockfiles can tighten this boundary.

`run.ews_discord` is a boolean in the saved run settings, defaulting to false
for legacy manifests. Reproduction preserves the original setting but supplies
the current laptop webhook. Cloud lifecycle notifications remain independent.
The root webhook file and systemd runtime copy are outside artifact inputs, and
the known credential filename is excluded if accidentally copied into `/work`.
As with other trusted experiment code, arbitrary environment dumps or encoded
copies of secrets must not be written to scientific artifacts. The forwarding
option grants the research process webhook access, not Hetzner/S3 access.

## Verification

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
interruption, download markers, and a real rclone copy/check between temporary
local directories with an empty configuration; this never uses Object Storage.

The provider adapter follows the official [hcloud create flags](https://github.com/hetznercloud/cli/blob/main/docs/reference/manual/hcloud_server_create.md)
and [JSON/label-filtered list interface](https://github.com/hetznercloud/cli/blob/main/docs/reference/manual/hcloud_server_list.md).
Uploads use [rclone check](https://rclone.org/commands/rclone_check/) for sizes and
available remote hashes, followed by an exact manifest read-back. Source/config
reproduction additionally verifies SHA-256 locally. EWS CLI defaults were checked
against the local `experiments-wo-stress` README and `docs/CONFIGURATION.md` during
implementation; different EWS versions may require a custom argument array.
