# cloud-experiments

Reusable commands for disposable Hetzner research workers: freeze your experiment
source, pin EWS, run in tmux, periodically persist recovery to Object Storage,
and delete the VM. Cloud CPU execution uses all available logical workers.
Repeated `cloud-run CONFIG` restores the latest committed recovery and continues
the same logical EWS study, including after VM loss.
Machine and runtime settings belong to each attempt, not the study identity.
See [automatic continuation](docs/continuation.md) for storage, locking, and compatibility.
There is no project-specific experiment code, Docker, database, or hosted service.
Python 3.11+ and the existing `hcloud`, `rclone`, `git`, `ssh`, and `scp` CLIs are
the only laptop dependencies.

```bash
./install
cloud-doctor

cd /path/to/your/experiment-repository
cloud-run configs/pl_failure.yml --machine cpx52
cloud-attach RUN_ID                 # detach with Ctrl-b, then d

# Later, after the worker has uploaded results and deleted itself:
cloud-results list
cloud-results ls RUN_ID             # inspect remote files and sizes
cloud-results pull RUN_ID --plots   # fetch just figures for inspection
cloud-results pull RUN_ID           # retain a complete reproducibility bundle
```

Start with [the complete setup guide](docs/setup.md). The supplied
[example configuration](templates/config.example.toml) matches the `experiments`
project, `nbg1`, and `migwings-experiments` bucket described there.

## Commands

| Command | Behavior |
| --- | --- |
| `cloud-run CONFIG` | Continue the repository/config study; restore verified state, or skip compute if exactly completed. |
| `cloud-results list --attempts STUDY_ID` | Inspect all attempts of a logical study. |
| `cloud-attach RUN_ID` | Attach to the active experiment's tmux terminal. |
| `cloud-status [RUN_ID]` | Show compute, recovery, archive and VM state; absent active workers are `interrupted`. |
| `cloud-results list` | Show stored studies with current VM presence and separate persistence outcomes. |
| `cloud-results ls RUN_ID [--json]` | Recursively list remote file paths and sizes without downloading contents. |
| `cloud-results pull RUN_ID [--dest PATH]` | Download study inputs/history/current output, an attempt, or a legacy run, by default to `~/cloud-results/RUN_ID`. |
| `cloud-results pull RUN_ID --plots` | Resolve the EWS `figures` role; succeed with an explanation if optional files are absent. |
| `cloud-results pull RUN_ID --analysis` | Resolve the EWS `analysis` role; does not execute analysis. |
| `cloud-results pull RUN_ID --report` | Resolve the EWS `compute_report` role. |
| `cloud-results pull RUN_ID --path RELATIVE_PATH` | Download one literal file or subtree, preserving its run-relative path. |
| `cloud-results sync` | Retrieve new/changed full runs; skip final manifests already fully downloaded. |
| `cloud-cancel RUN_ID [--yes]` | Ask the worker to stop, upload partial results, and delete itself. |
| `cloud-cancel RUN_ID --force-delete [--yes]` | Delete an unreachable managed VM; unsaved results can be lost. |
| `cloud-reproduce RUN_ID [--name NAME]` | Create an independent lineage from archived inputs/settings/environment; never resume its output. |
| `cloud-doctor` | Offline executable/config checks; never reads secrets or contacts providers. |

`cloud-run` options:

```text
--machine TYPE             override default_server_type
--ews-ref REF              branch, tag, or full 40-character Git commit
--name NAME                descriptive attempt label (does not change study identity)
--fresh                    intentionally start a new independent study lineage
--study STUDY_ID           continue an explicit fresh/reproduction lineage
--max-runtime HOURS        per-attempt limit including setup, at most 168 hours
--allow-dirty              explicitly execute a dirty working-tree snapshot
--keep-on-setup-failure    retain a failed setup until the original deadline
```

All commands accept `--config /path/to/config.toml` (place it before a
`cloud-results` subcommand). `CLOUD_EXPERIMENTS_CONFIG` also overrides the default.
No command installs this tooling as root. `./install` creates symlinks in
`~/.local/bin`, is idempotent, and refuses to replace unrelated commands. Keep
this checkout at its installed path; rerun the installer after moving it.

## Progress and terminal output

Long operations report activity automatically on **stderr**. In an interactive
terminal, downloads/uploads show a byte progress bar, speed and ETA when rclone
reports totals; file/check counts appear when available. Totals and estimates
can change as rclone discovers files or retries. Verification must finish before
a transfer is reported as successful. Source preparation, VM provisioning, SSH
readiness, source upload to the VM, environment setup, storage lookups,
cancellation/finalization requests, and reproduction show activity while waiting.
Reading a collection of manifests reports how many have been processed.

When either stdout or stderr is redirected, `TERM=dumb`, or `NO_COLOR` is set
to a nonempty value, output uses plain stage messages and updates about every
15 seconds, with no animation or ANSI escapes. Interactive displays use no
color. Fast metadata lookups stay quiet unless they fail. Tables and downloaded
paths remain on stdout for scripts. `cloud-results sync` ends with downloaded
and unchanged counts, including when there is nothing to download.

Progress displays use only numeric rclone statistics: raw subprocess logs,
filenames, URLs and credential-bearing messages stay captured and are never
streamed by the progress display. Failed commands still return nonzero status with safe error
messages. Ctrl-C stops an active transfer; rerunning `pull` uses rclone's
incremental copy behavior. A full-download marker is written only after a
successful full pull; selective pulls never create or refresh it. Explicit
`ls` output includes validated file names and sizes; progress displays do not.
Cancelling a run reports that finalization was **requested**, not that uploads
or VM deletion have already finished; check `cloud-status` afterwards.

## What runs

The experiment repository must have an `origin` HTTPS/SSH remote and a commit.
The normal command rejects staged changes, unstaged changes, and non-ignored
untracked files. With `--allow-dirty`, it snapshots actual current file contents,
including non-ignored untracked files and tracked deletions, and sets both
`dirty` and `experiment.dirty` in the manifest. Nothing is pushed to that remote.
The VM receives a tar archive, so it needs no GitHub access for experiment code.

Snapshots exclude Git metadata, virtual environments, Python caches, and ignored
untracked files. Secret-like paths are rejected before reading them. This first
version rejects symlinks and submodules instead of silently producing incomplete
source. Quiesce edits while creating a snapshot. Git-ignored input datasets must
be made non-ignored before launching; they are not guessed or copied implicitly.
Large input datasets also need enough VM disk space for source and artifacts.

EWS is fetched locally to resolve the requested ref **before compute creation**.
The worker fetches that exact commit from the configured public HTTPS repository.
It installs EWS, optional experiment requirements, and an editable experiment
package when `pyproject.toml` or `setup.py` exists. It reapplies the pinned EWS
installation afterwards so project dependencies cannot silently choose another
EWS version. EWS itself must remain publicly fetchable at that commit.

The default command is based on the EWS checkout's documented CLI:

```toml
[run]
command = ["ews", "run", "{config}", "--output", "{output}"]
install_experiment = "auto"  # or "never"
artifact_exclude = []
ews_discord = false         # optional EWS progress messages; see below
sync_seconds = 300          # periodic recovery interval
# timezone = "Europe/Rome" # display override; "UTC" explicitly selects server UTC
```

This is an argument array, not a shell snippet. `{config}` is the config's
repository-relative path; `{output}` is `/work/output`. Execution starts in
`/work/source` with the virtual environment on `PATH`. Automatic continuation
requires this standard EWS argument array and adds `--portable --workers N`, where
N is the available logical CPU count discovered on the worker. The YAML worker
count is an operational local default; cloud execution overrides it without
changing scientific identity or RNG behavior. An optional `run.timezone` is
validated by EWS and forwarded as `--timezone`. GPU/custom
checkpoint backends are unsupported. Old custom-command archives remain readable;
new reproductions require the standard command and supported EWS contract. The first prepared environment
locks exact runtime and index package versions. Subsequent attempts recreate and
validate that lock before EWS can load checkpoints. See
[continuation compatibility](docs/continuation.md#environment-recreation-and-ews-authority).

`HOME=/work/home`, `TMPDIR=/work/home`, and `CLOUD_EXPERIMENTS_OUTPUT=/work/output`
keep ordinary generated files within the captured workspace. The experiment
runs as `experiment`; credentials and finalization run as root. The real command
has a PTY inside the named `experiment` tmux session, preserving EWS's live
dashboard. systemd supervises the worker and bounds its lifetime independently
of tmux and the laptop connection.

## Discord: configure once, reuse across studies

The optional `DISCORD_WEBHOOK_URL` in `~/.config/cloud-experiments/worker.env`
sends cloud startup and final lifecycle messages. To also receive EWS progress,
add this setting once to the existing `[run]` table in
`~/.config/cloud-experiments/config.toml` (create that table if absent):

```toml
[run]
ews_discord = true
```

Keep the URL only in the private `worker.env`, alongside the existing worker
token. See [setup](docs/setup.md#6-create-a-dedicated-worker-token) for the file
format and permissions. No per-run export or URL in the experiment repo is
needed. Each EWS study must enable `notifications.discord` and use
`webhook_env: EWS_DISCORD_WEBHOOK_URL`; its YAML controls the message interval.
Cloud forwarding does not rewrite the YAML or enable EWS notifications itself.
Both channels can use the same Discord webhook. EWS notifications describe compute;
cloud messages separately identify last durable recovery, archive publication and
VM deletion requests. A compute-completed message does not claim an archive or
confirmed VM deletion.
An EWS message that says a durable run checkpoint is available refers to state
committed on that worker. It does not mean the cloud recovery snapshot has been
uploaded. Use `cloud-status` to see the last durable remote recovery point.

`run.ews_discord` defaults to `false`. With it enabled, a missing webhook fails
before any input upload or VM creation. Launch validates presence and URL syntax;
it does not contact Discord to test whether the webhook is still valid.
Delivery failures remain best effort: they must not stop simulations or cleanup.
`cloud-doctor` continues to check only non-secret configuration and executables.

The worker uses systemd `LoadCredential` to give only the experiment service a
protected runtime copy of the webhook, outside `/work`. The execution wrapper
sets `EWS_DISCORD_WEBHOOK_URL` in the research process environment; the URL is
absent from command arguments, unit text, manifests, and automatic artifact
collection. Hetzner/S3 credentials remain root-only. The experiment can read its
webhook when this option is on: do not dump the environment or write credentials
to output files. Dependency installation does not receive the webhook.

The non-secret forwarding choice is stored in each run's settings. Reproduction
retains that choice and reads the current laptop's webhook, so rotation needs
one edit to `worker.env` for future workers. Existing VMs keep the credential
they received at launch. Older manifests without this setting reproduce with
forwarding disabled. EWS summaries cover execution; cloud final messages also
report upload outcomes and precede the deletion request, not confirmed deletion.

## Results and provenance

IDs in result commands can be logical study IDs (`s-...`), individual attempt IDs
(`a-...`), or legacy run IDs. New storage uses `studies/STUDY_ID/attempts/ATTEMPT_ID/`
for the following attempt archive, with parent-linked state commits and environment
locks beside `attempts/`. See the [complete layout](docs/continuation.md#storage-and-publication).
Legacy `runs/RUN_ID/` remains readable without migration.

Every attempt archives immutable inputs, logs, environment and runtime provenance,
plus other generated workspace artifacts. EWS output is stored as shared verified
recovery objects; downloads materialize its normal tree under `artifacts/output`.
This avoids duplicating a many-GB output for every sync or attempt. Full study
pulls retain attempt inputs/logs and the latest committed output. Historical
recovery metadata remains, but intentionally pruned old output is not a permanent
archive. Download any historical bytes you need to keep.

Manifests record the exact experiment and EWS commits, source/config checksums,
EWS cloud-contract version, cloud implementation version/revision/content digest,
executed arguments and worker allocation, machine, timestamps, and separate
compute, recovery, archive and deletion outcomes. Inputs are verified before
compute creation. Unknown recovery contracts and legacy continuation state fail
closed; old result archives remain readable.

Recovery normally runs every five minutes. The supported EWS recovery v1 API
requires a graceful pause and a sealed local copy; compute resumes before the
incremental network transfer. The first sync can be large, and long protocol
steps or storage outages extend the window. Use the last durable recovery time
to see what is protected. Finalization uses the same sync mechanism once more,
then publishes the final archive state. A failed upload retains the preceding
recovery and never postpones deletion indefinitely. See
[the recovery protocol and its limits](docs/continuation.md).

EWS intentionally pruned trajectories are eventually removed remotely only after
replacement state is verified, while preserving the preceding recovery point.
Recovery history reads only commit and environment records; pruning checks each
obsolete blob directly, so neither step lists the whole growing result pool.
Continuation fetches the sealed blob inventory in one bounded parallel transfer,
then checks every size and SHA-256 before starting EWS.
Other artifact collection captures new/modified regular files under `/work`,
compared with the initial source snapshot. Dependency/control/secret-like paths
and configured `artifact_exclude` globs are excluded; links are never followed.
Write persistent output under `/work`; arbitrary paths elsewhere are not captured.

`cloud-results sync` compares final manifests to local full-download markers;
active runs refresh incrementally. Remove `.cloud-pulled.json` or use `pull` to
restore deleted local files. Selective pulls never mark a bundle fully downloaded
and never execute analysis or downloaded research code. Retrieval never deletes
remote objects.

## Inspect remote results and download selected files

Use `list` to find studies, `list --attempts STUDY_ID` for execution history, then
`ls` to inspect the chosen study or attempt:

```bash
cloud-results list
cloud-results ls RUN_ID
cloud-results ls RUN_ID --json
```

`ls` reads object names, sizes and committed recovery inventory metadata, without
downloading output payloads, creating local result directories, or contacting a VM. Paths are
relative to the run root and sorted by path. Interactive output has a size/path
table with human-readable sizes. Redirected output is headerless TSV,
`BYTES<TAB>PATH`, with integer byte counts; `--json` always emits an array of
`{"path": "...", "size": 123}` records. Activity and empty-list explanations go
to stderr. An empty listing means no stored files were found at that run prefix;
check the run ID and storage configuration. Storage/access errors return nonzero.

For a quick look at plots or analysis, download just the relevant subtree:

```bash
cloud-results pull RUN_ID --plots
cloud-results pull RUN_ID --analysis
cloud-results pull RUN_ID --report
cloud-results pull RUN_ID --path logs
cloud-results pull RUN_ID --path manifest.json
```

`--plots`, `--analysis`, and `--report` resolve the EWS semantic roles `figures`,
`analysis`, and `compute_report`. On a study ID, they select the latest committed output generation. On an attempt
or legacy ID, they discover the single `artifacts.json` under captured `artifacts/`
(including archived custom output roots), read its schema/version,
and resolve paths relative to its parent. EWS owns these paths; cloud-experiments
has no mapping of EWS internal figure/analysis/report locations. The small
catalog is fetched in addition to the file listing; unrelated result contents
are not downloaded. `ls` reads recovery metadata to expose logical output paths.
For EWS custom figures partitioned by aggregation labels, completed plots may
appear while other simulation groups are still running. They become available
through `--plots` after a periodic recovery snapshot containing them is committed;
this command does not read the VM's live filesystem. Per-group summaries can be
retrieved with `--analysis` after that same commit, while the combined summary
CSV is exported when EWS finishes the invocation.

The supported [EWS contract](https://github.com/m1gwings/experiments-wo-stress/blob/main/docs/ARTIFACTS.md)
is `schema: "experiments-wo-stress/artifacts"`, integer `schema_version: 1`, and
`artifacts: {role: {path, kind, optional}}`. Each path is a canonical relative
POSIX path; `kind` is `file` or `directory`, and `optional` is boolean. File roles
select exactly one object; directory roles select descendants. Unknown roles
and extra fields are allowed. Catalogs have a 64 KiB cap; malformed/unsafe JSON,
duplicate keys, unsupported versions, and ambiguous multiple catalogs fail with
a clear explanation. Missing optional files or unpublished roles succeed with
an explanation and no download. A missing required artifact fails.

**Legacy uploads without a catalog require `--path`.** Use `cloud-results ls
RUN_ID` to choose the literal stored file/subtree. No legacy layout is silently
assumed. Use `--path` for ambiguous multiple EWS outputs or arbitrary artifacts
as well. A newer EWS checkout cannot retrofit an already uploaded legacy run.
Legacy full pulls and listing work without an EWS artifact catalog. Support is detected
per run, never from a globally required EWS Git SHA; the cloud run manifest still
records the exact EWS commit for provenance and reproduction.

Selectors are mutually exclusive. `--path` matches
one exact file or a directory and all descendants, not a glob or a partial name.
Quote paths containing spaces or shell metacharacters. One trailing `/` is
accepted; absolute paths, `.`/`..` components, repeated `/`, backslashes, colons,
and control characters are rejected before storage access. Stored paths are
validated too; the local full-download marker cannot be selected or overwritten.
Existing symlinks within a selected local subtree are rejected.

All selectors preserve the remote-relative tree inside the local run root. For
example, for an **attempt ID** with the current default EWS layout (examples, not
mappings; a study ID adds `attempts/ATTEMPT_ID/` before these artifact paths):

```text
--plots                         -> ~/cloud-results/RUN_ID/artifacts/output/analysis/figures/
--analysis                      -> ~/cloud-results/RUN_ID/artifacts/output/analysis/
--report                        -> ~/cloud-results/RUN_ID/artifacts/output/compute/summary.md
--path logs                     -> ~/cloud-results/RUN_ID/logs/
--path manifest.json            -> ~/cloud-results/RUN_ID/manifest.json
```

The configured `local.results_dir` replaces `~/cloud-results`. `--dest` sets the
local **run root**, including for selective pulls:

```bash
cloud-results pull RUN_ID --plots --dest /path/to/run-results
# The manifest-selected figure path is preserved below /path/to/run-results/
```

A successful selective pull prints the selected local file/directory path on
stdout and a completion message on stderr. It lists metadata first, copies only
matching objects, and uses the same interactive progress/plain output as a full
pull. Repeating it copies new/changed files without deleting local extras. If an
catalog declares optional figures but no figure objects are stored, `--plots`
exits successfully, explains that nothing was downloaded, and prints no
destination. The same rule applies to other optional roles. Missing generic
paths, required artifacts, unknown/empty runs, and storage errors fail clearly. A selector does not run
analysis or create figures; a run without configured/generated/uploaded figures
will still have none after a pull.

For archival and reproduction, use plain `cloud-results pull RUN_ID` to keep the
complete source/config/manifest/log/artifact bundle. `cloud-results sync` retains
full-run semantics and accepts no selectors. A partial download alone is not a
complete archive. These commands never modify or delete remote objects.

## Reproduce a stored run

`cloud-reproduce` verifies the stored tarball and config checksums and reuses its
exact source, EWS commit, machine, location, command, artifact policy, and runtime
limit. It assigns an independent study/attempt/deadline and adds `reproduces_run_id`.
It never restores old output; ordinary continuation uses repeated `cloud-run`. It does not
consult today's experiment Git checkout or resolve EWS `main` again. It uses the
current laptop's storage destination, SSH key, and worker credentials. Dirty runs
are reproduced from their stored bytes. Modern attempts also recreate their saved
exact environment lock. Legacy archives remain readable, but reproduction rejects unsupported EWS pins
or custom commands before creating compute. This does not freeze an OS image or
provide generic native/GPU portability. Deletion of the public EWS repository/commit remains a limitation.

## Cleanup and security

The first-boot cloud-init payload installs deadline and last-resort timers before
package installation or source transfer. The deadline includes setup time. At the
deadline the worker stops setup, requests graceful EWS interruption for up to
90 seconds, then stops its cgroup (20 seconds before forced kill), performs a final
incremental recovery sync and archive upload, publishes lifecycle state, and
requests its own deletion. Upload failures are recorded when possible and **never
prevent deletion**. Finalization is bounded; the separate reap timer starts API
deletion at deadline + 15 minutes even if finalization is stuck. Deletion retries
every 30 seconds on errors. Absolute persistent timers survive reboot; boot-time
timers also cover first initialization after a deadline. `--keep-on-setup-failure`
retains only setup failures, and never disables either timer.

Deletion checks the live server's ID, name, `managed-by=cloud-experiments`, and
`run-id=RUN_ID`. Worker deletion also checks its link-local metadata ID. An
ambiguous laptop create failure is recovered using those labels. A failed SSH
setup requests worker finalization or falls back to checked laptop deletion.
No volume, snapshot, backup, floating IP, or other persistent resource is created.

Only the dedicated worker token and the selected S3 remote's required credentials
are transferred, with optional Discord webhook. The laptop's hcloud credentials
are never copied. Credentials are not in manifests, arguments, or tool logs;
worker files are root-owned mode 600. Cloud-init receives them through stdin to
hcloud, with no credential-bearing temporary file. They necessarily pass through
Hetzner's user-data system and remain in root-only cloud-init state until the VM
is deleted. Anyone with project administrative access can access a worker;
project-scoped API tokens are not per-server least-privilege tokens. Use a
dedicated research project and narrowly scoped storage credentials where possible.

The experiment never receives Hetzner/S3 credentials and cannot read cloud-init
state. With `run.ews_discord = true`, only the Discord webhook is shared through
the runtime credential described above. Trusted research code and dependency installers are assumed; this is not
a security sandbox for hostile code. Never put secrets in source/configs or print
them from experiments. Filename protections and redaction are defense in depth,
not secret discovery. SSH uses trust-on-first-use with separate known-host files
per run/server, and rejects later key changes without changing global known_hosts.

No guest-side mechanism can promise deletion if cloud-init never runs, the guest
kernel is frozen, credentials are revoked, or the Hetzner API is unavailable.
The CLI reports unconfirmed cleanup prominently. Check `cloud-status` and the
Hetzner Console if anything looks wrong. **Powered-off VMs still incur charges;
successful deletion is what stops compute billing.** A failed upload can leave a
stale stored `running` manifest despite successful deletion; status derives
`interrupted` from the absent VM and shows the last durable recovery separately.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q lib tests bin install
```

Tests use temporary Git repositories, synthetic credentials, mocks, and fake
providers. They never contact Hetzner or Object Storage. There are no automatic
live integration tests. See the setup guide's optional, explicitly manual smoke
test. Internal design and failure boundaries are in [docs/architecture.md](docs/architecture.md).
