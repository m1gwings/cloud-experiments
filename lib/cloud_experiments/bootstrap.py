"""Small cloud-init failsafe, independent of the ordinary worker package."""

import base64
import datetime as dt
import gzip
import json
from pathlib import Path

from .common import Error

ROOT = Path(__file__).resolve().parents[2]
BASE = "/opt/cloud-experiments"
LIMIT = 32 * 1024
HEADROOM_LIMIT = 24 * 1024


def render(manifest, worker_token):
    if not worker_token or not isinstance(worker_token, str):
        raise Error("A Hetzner worker token is required for failsafe deletion.")
    deadline = dt.datetime.fromisoformat(manifest["deadline_at"])
    if deadline.tzinfo is None:
        raise Error("Absolute deadline must include a timezone.")
    files = []

    def add(path, content, mode="0600"):
        content = content.encode() if isinstance(content, str) else content
        compressed = gzip.compress(content, mtime=0)
        encoding, payload = ("gz+b64", compressed) if len(compressed) < len(content) else ("b64", content)
        files.append({"path": path, "encoding": encoding, "content": base64.b64encode(payload).decode(),
                      "permissions": mode, "owner": "root:root"})

    identity = {"run_id": manifest["run_id"], "study_id": manifest.get("study_id"),
                "name": manifest.get("study_id") or manifest["run_id"]}
    add(BASE + "/failsafe.py", (ROOT / "failsafe.py").read_bytes(), "0644")
    add(BASE + "/failsafe-identity.json", json.dumps(identity, separators=(",", ":")))
    add(BASE + "/failsafe-token", worker_token)
    for name in ("cloud-deadline.service", "cloud-deadline.timer"):
        content = (ROOT / "templates" / name).read_text()
        content = content.replace("@DEADLINE@", deadline.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"))
        content = content.replace("@MAX_SECONDS@", str(int(manifest["max_runtime_hours"] * 3600)))
        add("/etc/systemd/system/" + name, content, "0644")
    payload = "#cloud-config\n" + json.dumps({"write_files": files, "runcmd": [
        ["install", "-d", "-m", "0700", BASE + "/incoming", BASE + "/out"],
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "cloud-deadline.timer"],
        ["sh", "-c", "systemctl is-enabled --quiet cloud-deadline.timer && "
                      "systemctl is-active --quiet cloud-deadline.timer && touch " + BASE + "/armed"]
    ]}, separators=(",", ":"))
    if len(payload.encode()) > HEADROOM_LIMIT:
        raise Error("Minimal bootstrap exceeds its 24 KiB safety budget (Hetzner limit: 32 KiB).")
    return payload
