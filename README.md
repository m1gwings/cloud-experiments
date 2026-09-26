# cloud-experiments

Reusable commands for disposable Hetzner research workers: freeze your experiment
source, pin EWS, run in tmux, save results to Object Storage, and delete the VM.
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
cloud-results pull RUN_ID
```

Start with [the complete setup guide](docs/setup.md). The supplied
[example configuration](templates/config.example.toml) matches the `experiments`
project, `nbg1`, and `migwings-experiments` bucket described there.

## Commands

| Command | Behavior |
| --- | --- |
| `cloud-run CONFIG` | Snapshot the current Git repository, provision, install, and return after launch. |
| `cloud-attach RUN_ID` | Attach to the active experiment's tmux terminal. |
| `cloud-status [RUN_ID]` | Show managed active VMs, or a stored manifest for a completed run. |
| `cloud-results list` | Read remote manifests, including failed and incomplete runs. |
| `cloud-results pull RUN_ID [--dest PATH]` | Download the full run, by default to `~/cloud-results/RUN_ID`. |
| `cloud-results sync` | Retrieve new/changed runs; skip final manifests already downloaded. |
| `cloud-cancel RUN_ID [--yes]` | Ask the worker to stop, upload partial results, and delete itself. |
| `cloud-cancel RUN_ID --force-delete [--yes]` | Delete an unreachable managed VM; unsaved results can be lost. |
| `cloud-reproduce RUN_ID [--name NAME]` | Launch a new run from stored input and pinned settings. |
| `cloud-doctor` | Offline executable/config checks; never reads secrets or contacts providers. |

`cloud-run` options:

```text
--machine TYPE             override default_server_type
--ews-ref REF              branch, tag, or full 40-character Git commit
--name NAME                readable prefix; UTC timestamp + random suffix added
--max-runtime HOURS        positive finite duration, at most 168 hours
--allow-dirty              explicitly execute a dirty working-tree snapshot
--keep-on-setup-failure    retain a failed setup until the original deadline
```

All commands accept `--config /path/to/config.toml` (place it before a
`cloud-results` subcommand). `CLOUD_EXPERIMENTS_CONFIG` also overrides the default.
No command installs this tooling as root. `./install` creates symlinks in
`~/.local/bin`, is idempotent, and refuses to replace unrelated commands. Keep
this checkout at its installed path; rerun the installer after moving it.

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
```

This is an argument array, not a shell snippet. `{config}` is the config's
repository-relative path; `{output}` is `/work/output`. Execution starts in
`/work/source` with the virtual environment on `PATH`. For other frameworks:

```toml
[run]
command = ["python", "run_study.py", "--config", "{config}", "--output", "{output}"]
```

Use your own checked-in script for pipelines or additional setup logic.
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
Both channels can use the same Discord webhook.

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

Object Storage layout is stable:

```text
hetzner:migwings-experiments/runs/RUN_ID/
  manifest.json
  source/source.tar.gz
  source/index.json
  config/experiment-config
  artifacts/index.json
  artifacts/source/...       # new/modified files under the source tree
  artifacts/output/...       # normal EWS outputs
  artifacts/home/...         # generated files under HOME/TMPDIR
  artifacts/...              # other new/modified files anywhere under /work
  logs/setup.log
  logs/console.log           # combined PTY stdout/stderr, including terminal codes
  logs/worker.log            # when setup/execution fails
  machine/runtime.json
  machine/pip-freeze.txt
```

The manifest records repository/commit/dirty state; source and config SHA-256;
requested and exact EWS revision; execution arguments; VM type/location/image;
server identity; UTC timestamps/deadline; Python/OS/machine information; status,
exit code (null if unavailable), elapsed seconds, and upload verification.
Missing runtime fields mean setup had not reached that stage. Inputs and a
`provisioning` manifest are uploaded and checked before compute creation. The
worker publishes `running` before launch and a final `completed`, `failed`,
`cancelled`, `timeout`, or `setup_failed` manifest after uploading artifacts.

Artifact collection compares file content and executable bits with the initial
snapshot. It also captures new files throughout `/work`, regardless of extension
or expected output directory. Deleted source paths and excluded paths are
recorded in `artifacts/index.json`; symlinks are never followed. Git, environments,
dependency caches, worker control files, and secret-like paths are excluded.
Custom `artifact_exclude` globs are relative to `/work` (e.g.
`"source/scratch/*"`) and persisted in the manifest. Avoid excluding outputs you
want to keep. Write persistent data under `/work`: explicit writes to `/tmp` or
other paths outside this workspace are not captured. No remote deletes or
destructive rclone sync operations are used for result retrieval.

`cloud-results sync` compares remote final manifests to local download markers;
running runs are refreshed using rclone's incremental copy behavior. Remove a
run's `.cloud-pulled.json` marker, or use `pull`, to restore deleted local files.
There is intentionally no automatic `--analysis` execution: a pull never executes
downloaded research code.

`cloud-reproduce` verifies the stored tarball and config checksums and reuses its
exact source, EWS commit, machine, location, command, artifact policy, and runtime
limit. It assigns a fresh ID/deadline and adds `reproduces_run_id`. It does not
consult today's experiment Git checkout or resolve EWS `main` again. It uses the
current laptop's storage destination, SSH key, and worker credentials. Dirty runs
are reproduced from their stored bytes. Python/OS package versions are recorded,
not frozen VM images: use lockfiles in experiment repositories for dependency
reproducibility. Deletion of the public EWS repository/commit remains a limitation.

## Cleanup and security

The first-boot cloud-init payload installs deadline and last-resort timers before
package installation or source transfer. The deadline includes setup time. At the
deadline the worker stops both setup and experiment cgroups, collects outputs,
uploads/checks them, publishes a final manifest, optionally notifies Discord, and
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
stale `running` manifest or partial data despite successful deletion.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q lib tests bin install
```

Tests use temporary Git repositories, synthetic credentials, mocks, and fake
providers. They never contact Hetzner or Object Storage. There are no automatic
live integration tests. See the setup guide's optional, explicitly manual smoke
test. Internal design and failure boundaries are in [docs/architecture.md](docs/architecture.md).
