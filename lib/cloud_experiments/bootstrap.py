"""Generate first-boot files; credential-bearing cloud-init is streamed, not saved."""

import base64
import datetime as dt
import io
import json
from pathlib import Path
import zipfile

from .common import Error
from .config import ews_discord_webhook

ROOT = Path(__file__).resolve().parents[2]
ENTRY = "/usr/bin/python3 /opt/cloud-experiments/entry.py"


def render(manifest, secrets, rclone_config):
    files = []

    def add(path, content, mode="0600"):
        if isinstance(content, str):
            content = content.encode()
        files.append({"path": path, "permissions": mode, "owner": "root:root", "encoding": "b64",
                      "content": base64.b64encode(content).decode()})

    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("__init__.py", "common.py", "config.py", "source.py", "worker.py"):
            z.write(Path(__file__).parent / name, "cloud_experiments/" + name)
    add("/opt/cloud-experiments/code.zip", bundle.getvalue(), "0644")
    add("/opt/cloud-experiments/entry.py", "import sys\nsys.path.insert(0, '/opt/cloud-experiments/code.zip')\nfrom cloud_experiments.worker import main\nmain()\n", "0644")
    add("/opt/cloud-experiments/manifest.json", json.dumps(manifest))
    add("/opt/cloud-experiments/credentials.json", json.dumps(secrets))
    add("/opt/cloud-experiments/rclone.conf", rclone_config)
    # PID 1 supplies this one credential to the experiment service. The original
    # and runtime copy are outside /work and are never artifact inputs.
    webhook = ews_discord_webhook(manifest["settings"], secrets)
    credential = ""
    if webhook is not None:
        add("/opt/cloud-experiments/ews-discord-webhook", webhook)
        credential = "LoadCredential=ews-discord-webhook:/opt/cloud-experiments/ews-discord-webhook"
    deadline = dt.datetime.fromisoformat(manifest["deadline_at"])
    replacements = {"@ENTRY@": ENTRY, "@DEADLINE@": deadline.strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "@REAP@": (deadline + dt.timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "@MAX_SECONDS@": str(int(manifest["max_runtime_hours"] * 3600)),
                    "@REAP_SECONDS@": str(int(manifest["max_runtime_hours"] * 3600) + 900),
                    "@EWS_DISCORD_CREDENTIAL@": credential}
    for template in sorted((ROOT / "templates").glob("cloud-*")):
        content = template.read_text()
        for key, value in replacements.items():
            content = content.replace(key, value)
        add("/etc/systemd/system/" + template.name, content, "0644")
    payload = "#cloud-config\n" + json.dumps({"write_files": files, "runcmd": [
        ["install", "-d", "-m", "0700", "/opt/cloud-experiments/incoming", "/opt/cloud-experiments/out"],
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "cloud-deadline.timer", "cloud-reap.timer"],
        ["sh", "-c", "systemctl is-active --quiet cloud-deadline.timer && systemctl is-active --quiet cloud-reap.timer && touch /opt/cloud-experiments/armed"]
    ]})
    if len(payload.encode()) > 32 * 1024:
        raise Error("Bootstrap exceeds Hetzner's 32 KiB user-data limit; reduce payload size.")
    return payload
