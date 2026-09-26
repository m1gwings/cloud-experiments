# Setup from a clean laptop

This describes the manual `experiments` / `nbg1` setup used by this tooling.
Existing installations do not need to repeat it. The commands in this guide are
for you to run deliberately; implementation tests never create cloud resources
or contact Object Storage. Creating a worker incurs charges.

## 1. Install laptop tools

Install Python **3.11 or newer**, Git, OpenSSH client (`ssh` and `scp`), rclone,
and the current Hetzner Cloud CLI, using your OS package manager or their official
release instructions. On Ubuntu 24.04, Python, Git, OpenSSH and rclone are available
through apt; install `hcloud` from its official release if absent from your distro.

- [hcloud setup](https://github.com/hetznercloud/cli/blob/main/docs/tutorials/setup-hcloud-cli.md)
- [rclone installation](https://rclone.org/install/)

No Docker, Python dependency installation, or laptop root service is needed for
this repository. Worker OS installation is handled automatically on disposable
Ubuntu 24.04 VMs.

## 2. Create the project and private bucket

In Hetzner Console, create a Cloud project named **`experiments`**. Keep disposable
research compute separate from other projects.

Create a **private** Object Storage bucket named **`migwings-experiments`** in
**`nbg1`**. If that globally unique name is unavailable, choose your own and use
it consistently in the configuration. Do not enable public access. Retained
objects continue to incur storage charges after VMs are deleted.

## 3. Configure the laptop API token

Create a read/write Cloud API token for the `experiments` project in Console.
This is the laptop token used for creating workers and emergency deletion.
Configure the named context interactively:

```bash
hcloud context create experiments
```

Paste the token only at its prompt. hcloud stores its configuration under
`~/.config/hcloud/`. Do not put the token in a command argument, Git, or an
experiment repository. The tooling explicitly uses the named context and ignores
`HCLOUD_*` environment overrides/debug logging for its subprocesses.

## 4. Configure Object Storage

Generate S3 access credentials for the intended Object Storage project/bucket.
Create `~/.config/rclone/rclone.conf` with a text editor:

```ini
[hetzner]
type = s3
provider = Other
access_key_id = YOUR_ACCESS_KEY
secret_access_key = YOUR_SECRET_KEY
endpoint = nbg1.your-objectstorage.com
acl = private
region = nbg1
```

**`YOUR_ACCESS_KEY` and `YOUR_SECRET_KEY` are placeholders. You MUST replace
both with the actual generated S3 credentials.** They are not literal working
values, Cloud API tokens, or your Hetzner login password.

Restrict the file to your account:

```bash
chmod 600 ~/.config/rclone/rclone.conf
```

The current worker transfer supports a plain S3 remote with explicit access keys,
not encrypted rclone config, chained remotes, or environment-only credentials.
Only this named remote's S3 fields are copied; other rclone remotes are omitted.

You can verify access with this read-only command:

```bash
rclone lsd hetzner:
```

The bucket should appear. The endpoint is the regional endpoint, not a bucket
URL. See [Hetzner Object Storage documentation](https://docs.hetzner.com/storage/object-storage/)
and [rclone S3 documentation](https://rclone.org/s3/) for credential/endpoint setup.

## 5. Register the laptop's SSH public key

If you do not already have a suitable SSH key, create one (do not overwrite an
existing key):

```bash
ssh-keygen -t ed25519
```

Use a passphrase and load the key into your SSH agent if applicable. Register
**only the public key** with the intended project:

```bash
hcloud --context experiments ssh-key create \
  --name migwings-ideapad \
  --public-key-from-file ~/.ssh/id_ed25519.pub
```

If the `experiments` context is already active, this is equivalent to:

```bash
hcloud ssh-key create \
  --name migwings-ideapad \
  --public-key-from-file ~/.ssh/id_ed25519.pub
```

`hetzner.ssh_key` is the registered key's name, not a local filename. Set
`local.ssh_identity` if your private key has a nonstandard filename. Batch SSH
requires a usable key/agent without interactive password/passphrase prompts.
The private SSH key is never transferred.

## 6. Create a dedicated worker token

Create a **separate read/write Cloud API token** in the same `experiments` project.
It is used only by workers to verify their identity and delete themselves. A
read-only token cannot delete VMs. Hetzner project tokens are not restricted to
one VM; a dedicated project limits the impact of a compromised worker token.

Create `~/.config/cloud-experiments/worker.env` with an editor:

```dotenv
HCLOUD_WORKER_TOKEN=YOUR_ACTUAL_WORKER_TOKEN
# Optional:
# DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/REPLACE_WITH_REAL_WEBHOOK
```

Replace the token placeholder with the actual dedicated token. Keep the Discord
line absent/commented unless you have a real webhook. Values may be quoted;
shell substitutions are never evaluated. Unknown keys are not transferred.

```bash
chmod 700 ~/.config/cloud-experiments
chmod 600 ~/.config/cloud-experiments/worker.env
```

For Discord notifications, add your actual webhook as `DISCORD_WEBHOOK_URL` in
this same private file, keeping the existing `HCLOUD_WORKER_TOKEN` line. You
configure it once for all experiment repositories using this cloud setup. Do
not put it in the experiment YAML, `config.toml`, a shell command argument, or
Git. The cloud lifecycle notifier uses it automatically when present.

For EWS progress messages too, enable forwarding once in the non-secret cloud
configuration (section 7), and enable notifications in the experiment YAML.
There is no need to copy the webhook into a second file or export it before
each cloud launch. Direct local EWS runs still need their webhook environment
variable set locally.

Never commit either credential file. Do not paste their contents into diagnostics
or bug reports. The tool requires credential files to be owned by your account
and inaccessible to group/other users.

## 7. Create non-secret configuration

Copy [templates/config.example.toml](../templates/config.example.toml) to
`~/.config/cloud-experiments/config.toml` and adjust its non-secret values:

```toml
[hetzner]
context = "experiments"
location = "nbg1"
ssh_key = "migwings-ideapad"
default_server_type = "cpx32"
heavy_server_type = "cpx52"
max_runtime_hours = 24

[storage]
rclone_remote = "hetzner"
bucket = "migwings-experiments"

[ews]
repository = "https://github.com/m1gwings/experiments-wo-stress.git"
default_ref = "main"
```

For studies that want EWS Discord progress, add to the existing `[run]` table
(create it if absent; do not add duplicate TOML tables):

```toml
[run]
ews_discord = true
```

This opts in to sharing only the webhook with the experiment process. Pair it
with the following settings in the study's YAML, not in this TOML:

```yaml
notifications:
  discord:
    enabled: true
    webhook_env: EWS_DISCORD_WEBHOOK_URL
    interval_seconds: 300
    timeout_seconds: 5
```

No EWS code change is required. Forwarding defaults to off; enabling the YAML
alone does not supply a credential. With forwarding on, a missing webhook is
rejected before input uploads or compute creation. Changing `worker.env` affects
future launches and reproductions, not workers that already started. Read the
[Discord section](../README.md#discord-configure-once-reuse-across-studies) for
credential isolation and reproduction behavior.

There are no API keys in this file. `heavy_server_type` documents your preferred
larger machine; choose it explicitly with `--machine cpx52`. Hardware availability
can vary. Configure a type available in your project/location when needed.

Optional paths and execution settings are documented in the example. Defaults:

```text
config      ~/.config/cloud-experiments/config.toml
secrets     ~/.config/cloud-experiments/worker.env
state       ~/.local/state/cloud-experiments/RUN_ID/
results     ~/cloud-results/RUN_ID/
```

XDG configuration/state environment variables are honored. State holds source
snapshots, non-secret manifests, and per-server known-host files. Do not configure
it inside experiment repositories. Results and state are not automatically
pruned; delete local copies deliberately after confirming remote persistence.

## 8. Install and validate without creating compute

From the `cloud-experiments` checkout:

```bash
./install
cloud-doctor
python3 -m unittest discover -s tests -v
```

The installer checks executables and creates idempotent symlinks in `~/.local/bin`.
It never copies/modifies credentials. If instructed, add that directory to PATH.
`cloud-doctor` is offline: it reads only non-secret configuration and checks local
executables. It does not validate tokens, SSH authentication, account limits, or
cloud permissions. Tests use synthetic files and fake providers only.

Optional non-billable connectivity checks, run explicitly by you:

```bash
hcloud --context experiments server list -o json
hcloud --context experiments ssh-key list -o json
rclone lsd hetzner:
```

These do not test writes or prove that worker deletion credentials work. Do not
use commands that dump rclone/hcloud configuration when sharing diagnostics.

## 9. First real run and optional manual smoke test

Use a committed experiment config known to finish quickly, with a deliberately
short runtime limit. From its repository, after the offline checks pass:

```bash
cloud-run configs/pl_failure.yml --machine cpx32 --max-runtime 0.5 --name smoke
```

**This is the first billable command.** Replace the config path with your small
config if `pl_failure.yml` is a long study. The half-hour deadline includes OS and
Python setup; interruption at that deadline should still save partial results.
There is up to 15 minutes of bounded last-resort cleanup allowance, with further
API retry time if Hetzner deletion is unavailable. The implementation agent must
not run this command as an integration test.

Observe the printed run ID, attach if desired, and verify:

```bash
cloud-status RUN_ID
cloud-attach RUN_ID
cloud-results list
cloud-results pull RUN_ID
cloud-status
```

Inspect the final manifest, source/config checksums, logs, and output artifacts.
Confirm in both `cloud-status` and Hetzner Console that the VM disappeared. Once
this optional smoke test succeeds, ordinary runs do not need to repeat it.
Additional manual scenarios, only if intentionally testing them: cancel a small
run; use a very short deadline; make an experiment exit nonzero; and reproduce a
dirty source snapshot after changing your laptop checkout. Each new run is billable.

## 10. Normal operation

Launch at night:

```bash
cloud-run configs/pl_failure.yml --machine cpx52
```

The CLI returns after confirmed launch, not after experiment completion. Closing
the laptop is then safe for the worker. In tmux, press **Ctrl-b**, release, then
**d** to detach. Closing SSH also leaves the experiment running. **Ctrl-c in the
experiment terminal interrupts the experiment**; use `cloud-cancel` for a run
explicitly recorded as cancelled.

The next morning:

```bash
cloud-results list
cloud-results ls RUN_ID
cloud-results pull RUN_ID --plots
# Or fetch saved analysis / a specific report:
cloud-results pull RUN_ID --analysis
cloud-results pull RUN_ID --path artifacts/output/compute/summary.md
# For the complete archival bundle:
cloud-results pull RUN_ID
# or incrementally retrieve all runs:
cloud-results sync
```

For a custom destination, `cloud-results pull RUN_ID --dest /path/to/run-results`.
Selectors also accept `--dest`: it is the run root, with the remote-relative
subtree preserved below it. `ls` reads names/sizes only; `--plots` succeeds with
an explanation when an existing run has no figures. Plain `pull` and `sync`
download full runs; no analysis code is executed automatically. See the
[README's remote inspection and selective download reference](../README.md#inspect-remote-results-and-download-selected-files)
for exact paths, output formats, and validation rules. Check the current checkout
revision and `cloud-results --help` / `cloud-results pull --help` after updating.

Re-run an old result from its stored source and pinned EWS commit:

```bash
cloud-reproduce RUN_ID
```

This creates a new billable VM with a new run ID. It preserves the old config,
dirty state, machine/location, runtime limit, and execution settings. Git changes
since the original run do not affect its source. See the README for dependency
and public-EWS-availability limitations.

## 11. Cancellation and failure recovery

```bash
cloud-cancel RUN_ID
cloud-cancel RUN_ID --yes                 # explicit noninteractive confirmation
cloud-cancel RUN_ID --force-delete --yes  # last resort: unsaved results may be lost
```

Normal cancellation asks the worker to stop its process groups, collect partial
data, upload, notify, and delete. It returns once that request is accepted; check
status afterwards. Forced deletion verifies live management/run labels and the
immutable server ID before issuing a delete. It may leave the remote manifest at
its last known status. Never manually delete an unrelated VM to resolve a run.

`--keep-on-setup-failure` lets you debug failed setup via SSH until the original
deadline; the experiment tmux session might not exist yet. Its address is visible
in the Hetzner Console; use the run's known-host file and your existing key.
Inspect `/opt/cloud-experiments/out/logs/setup.log` or
`journalctl -u cloud-supervisor.service` on that VM. Do not print credential files
or whole cloud-init user-data. If initial SSH/bootstrap cannot be verified, the
laptop may delete the failed worker even with this flag, to protect billing.

An Object Storage outage does not keep compute alive: upload is bounded, failure
is logged/recorded where possible, and deletion still takes priority. A stale
remote `running` manifest with no VM can mean persistence failed. The preflight
source/config should still be available. If the CLI says cleanup was not
confirmed, check `cloud-status RUN_ID` and Console promptly.

**Hetzner bills stopped servers. Power-off is not cleanup.** Deadline timers
and deletion retries reduce risk but cannot defeat a frozen kernel, failed
cloud-init, revoked token, or a sustained provider outage. The worker API token
must remain valid until all workers have been deleted.

## 12. Security and a replacement laptop

The laptop retains permanent hcloud and SSH credentials. Only the dedicated
worker token, selected S3 remote credentials, and optional Discord webhook reach
the VM, through root-only cloud-init files. Cloud-init credentials pass through
Hetzner's API/user-data storage, not just SSH; project administrators remain
trusted. They disappear with the VM. Run/config manifests never contain them.
The unprivileged experiment cannot read Hetzner/S3 root credentials. An explicitly
enabled `run.ews_discord` shares only the webhook through a protected systemd
credential outside the captured workspace, then EWS's environment variable.
Do not print environment/credential dumps from experiments. Experiment source
and logs are private research data; protect the bucket and downloaded results.

On a new laptop: install the CLIs/Python; clone this private tooling repository;
recreate the `experiments` hcloud context using a stored or newly issued laptop
token; configure the S3 remote; create/register that laptop's SSH key; recreate
`worker.env` securely; copy the non-secret TOML and update `ssh_key`/identity;
run `./install`, `cloud-doctor`, and the optional read-only checks. Restore local
state from a trusted backup if you want existing host-key trust; a new laptop
otherwise uses first-use trust for active workers. `cloud-results sync` restores
results from Object Storage without needing the old state directory. There is no
need to recreate an existing project/bucket or repeat a successful smoke test.
