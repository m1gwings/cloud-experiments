"""Generate first-boot files; credential-bearing cloud-init is streamed, not saved."""

import base64
import ast
import datetime as dt
import gzip
import io
import json
import lzma
from pathlib import Path
import zipfile

from .common import Error
from .config import ews_discord_webhook

ROOT = Path(__file__).resolve().parents[2]
ENTRY = "/usr/bin/python3 /opt/cloud-experiments/entry.py"


class _TransportCode(ast.NodeTransformer):
    """Remove comments and docstrings only from the VM transport copy."""

    def visit(self, node):
        node = super().visit(node)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                node.body.pop(0)
                if not node.body:
                    node.body.append(ast.Pass())
        return node


def render(manifest, secrets, rclone_config):
    files = []

    def add(path, content, mode="0600"):
        if isinstance(content, str):
            content = content.encode()
        encoding = "b64"
        compressed = gzip.compress(content, mtime=0)
        if len(compressed) < len(content):
            content, encoding = compressed, "gz+b64"
        entry = {"path": path, "encoding": encoding, "content": base64.b64encode(content).decode()}
        if mode != "0644":
            entry.update(permissions=mode, owner="root:root")
        files.append(entry)

    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w", zipfile.ZIP_STORED) as z:
        for name in ("__init__.py", "common.py", "workspace.py", "worker.py", "studies.py", "environment.py", "persistence.py", "ews_contract.py", "synchronization.py", "diagnostics.py"):
            source = (Path(__file__).parent / name).read_text()
            # The repository retains explanatory text; the VM receives executable code.
            z.writestr("cloud_experiments/" + name, ast.unparse(_TransportCode().visit(ast.parse(source))))
    add("/opt/cloud-experiments/code.zip.xz", lzma.compress(bundle.getvalue(), preset=9), "0644")
    add("/opt/cloud-experiments/entry.py", "import sys, os, lzma\nfrom pathlib import Path\np=Path('/opt/cloud-experiments/code.zip')\nif not p.exists():\n t=p.with_name('code-'+str(os.getpid())+'.tmp')\n t.write_bytes(lzma.decompress(p.with_suffix('.zip.xz').read_bytes()))\n t.chmod(0o644)\n os.replace(t,p)\nsys.path.insert(0,str(p))\nfrom cloud_experiments.worker import main\nmain()\n", "0644")
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
                    "@MAX_SECONDS@": str(int(manifest["max_runtime_hours"] * 3600)),
                    "@EWS_DISCORD_CREDENTIAL@": credential}
    for template in sorted((ROOT / "templates").glob("cloud-*")):
        content = template.read_text()
        for key, value in replacements.items():
            content = content.replace(key, value)
        add("/etc/systemd/system/" + template.name, content, "0644")
    payload = "#cloud-config\n" + json.dumps({"write_files": files, "runcmd": [
        ["install", "-d", "-m", "0700", "/opt/cloud-experiments/incoming", "/opt/cloud-experiments/out"],
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "cloud-deadline.timer"],
        ["sh", "-c", "systemctl is-active --quiet cloud-deadline.timer && touch /opt/cloud-experiments/armed"]
    ]}, separators=(",", ":"))
    if len(payload.encode()) > 32 * 1024:
        raise Error("Bootstrap exceeds Hetzner's 32 KiB user-data limit; reduce payload size.")
    return payload
