"""Standalone absolute-deadline fallback. No cloud package imports."""

import json
from pathlib import Path
import subprocess
import urllib.error
import urllib.request

BASE = Path("/opt/cloud-experiments")
API = "https://api.hetzner.cloud/v1/servers/"
METADATA = "http://169.254.169.254/hetzner/v1/metadata/instance-id"


def own_id():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(METADATA, timeout=10) as response:
        value = response.read(128).decode().strip()
    if not value.isdigit() or int(value) <= 0:
        raise ValueError("Invalid VM metadata identity")
    return int(value)


def request(url, token, method="GET"):
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and method in ("GET", "DELETE"):
            return None
        raise RuntimeError("Hetzner API request failed") from None


def checked_delete(identity, token, metadata=own_id, api=request):
    server_id = metadata()
    url = API + str(server_id)
    response = api(url, token)
    if response is None:
        return  # Already deleted.
    server = response["server"]
    labels = server.get("labels", {})
    expected = {"managed-by": "cloud-experiments", "run-id": identity["run_id"]}
    if identity.get("study_id"):
        expected["study-id"] = identity["study_id"]
    if (type(server.get("id")) is not int or server["id"] != server_id
            or server.get("name") != identity["name"]
            or any(labels.get(key) != value for key, value in expected.items())):
        raise ValueError("VM identity or management labels mismatch; refusing deletion")
    api(url, token, method="DELETE")


def main():
    if (BASE / "runtime-ready").exists():
        try:
            result = subprocess.run(["/usr/bin/python3", str(BASE / "entry.py"), "expire"], timeout=180)
            if result.returncode == 0:
                return
        except (OSError, subprocess.TimeoutExpired):
            pass
    identity = json.loads((BASE / "failsafe-identity.json").read_text())
    token = (BASE / "failsafe-token").read_text()
    checked_delete(identity, token)


if __name__ == "__main__":
    main()
