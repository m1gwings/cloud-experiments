"""Build the complete post-SSH worker archive and verify it before activation."""

import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
import zipfile

from .config import ews_discord_webhook

ROOT = Path(__file__).resolve().parents[2]
BASE = "/opt/cloud-experiments"
ENTRY = "/usr/bin/python3 " + BASE + "/entry.py"

# Sent as the argument to python3 -c. It has no dependency on installed worker code.
INSTALLER = r'''
import hashlib, os, pathlib, subprocess, sys, tarfile
base = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else pathlib.Path('/opt/cloud-experiments')
unit_dir = pathlib.Path(sys.argv[3]) if len(sys.argv) > 3 else pathlib.Path('/etc/systemd/system')
archive = base / 'incoming/runtime.tar.gz'
h = hashlib.sha256()
with archive.open('rb') as stream:
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        h.update(chunk)
if h.hexdigest() != sys.argv[1]:
    raise SystemExit('Runtime archive integrity check failed')
subprocess.run(['systemctl', 'is-enabled', '--quiet', 'cloud-deadline.timer'], check=True)
subprocess.run(['systemctl', 'is-active', '--quiet', 'cloud-deadline.timer'], check=True)
os.umask(0o077)
installed = set()
with tarfile.open(archive, 'r:gz') as bundle:
    for member in bundle:
        parts = pathlib.PurePosixPath(member.name).parts
        if (not member.isfile() or len(parts) != 2 or parts[0] not in ('root', 'units')
                or member.size > 16 * 1024 * 1024 or member.name in installed):
            raise SystemExit('Invalid runtime archive member')
        installed.add(member.name)
        target = (base if parts[0] == 'root' else unit_dir) / parts[1]
        tmp = target.with_name(target.name + '.installing')
        with bundle.extractfile(member) as source, tmp.open('wb') as output:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                output.write(chunk)
        os.chmod(tmp, member.mode & 0o777)
        os.replace(tmp, target)
required = {'root/code.zip', 'root/entry.py', 'root/manifest.json',
            'root/credentials.json', 'root/rclone.conf', 'units/cloud-supervisor.service',
            'units/cloud-finalize.service', 'units/cloud-delete.service'}
if not required <= installed:
    raise SystemExit('Incomplete runtime archive')
subprocess.run(['systemctl', 'daemon-reload'], check=True)
subprocess.run(['systemctl', 'is-enabled', '--quiet', 'cloud-deadline.timer'], check=True)
subprocess.run(['systemctl', 'is-active', '--quiet', 'cloud-deadline.timer'], check=True)
marker = base / 'runtime-ready.installing'
marker.write_text(sys.argv[1] + '\n')
os.replace(marker, base / 'runtime-ready')
archive.unlink()
'''


def build(path, manifest, secrets, rclone_config):
    """Write a private, reproducible archive and return its SHA-256 digest."""
    package = io.BytesIO()
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for source in sorted((Path(__file__).parent).glob("*.py")):
            info = zipfile.ZipInfo("cloud_experiments/" + source.name)
            info.external_attr = 0o644 << 16
            bundle.writestr(info, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    files = {
        "root/code.zip": (package.getvalue(), 0o644),
        "root/entry.py": ("import sys\nsys.path.insert(0, '/opt/cloud-experiments/code.zip')\n"
                          "from cloud_experiments.worker import main\nmain()\n".encode(), 0o644),
        "root/manifest.json": (json.dumps(manifest, sort_keys=True).encode(), 0o600),
        "root/credentials.json": (json.dumps(secrets, sort_keys=True).encode(), 0o600),
        "root/rclone.conf": (rclone_config.encode(), 0o600),
    }
    webhook = ews_discord_webhook(manifest["settings"], secrets)
    credential = ""
    if webhook is not None:
        files["root/ews-discord-webhook"] = (webhook.encode(), 0o600)
        credential = "LoadCredential=ews-discord-webhook:/opt/cloud-experiments/ews-discord-webhook"
    for template in sorted((ROOT / "templates").glob("cloud-*")):
        if template.name in ("cloud-deadline.service", "cloud-deadline.timer"):
            continue
        content = template.read_text().replace("@ENTRY@", ENTRY).replace("@EWS_DISCORD_CREDENTIAL@", credential)
        files["units/" + template.name] = (content.encode(), 0o644)
    path = Path(path)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as raw:
        with gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as archive:
                for name, (content, mode) in sorted(files.items()):
                    info = tarfile.TarInfo(name)
                    info.size, info.mode, info.mtime = len(content), mode, 0
                    archive.addfile(info, io.BytesIO(content))
    path.chmod(0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return digest


def install(ssh, archive, digest):
    ssh.upload([archive])
    ssh.call(["/usr/bin/python3", "-c", INSTALLER, digest], timeout=180)
