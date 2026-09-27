"""Configuration validation and narrowly scoped runtime credential loading."""

import configparser
import io
import math
import os
from pathlib import Path
import re
import shlex
import stat
import tomllib
from urllib.parse import urlsplit

from .common import Error

DEFAULT_COMMAND = ["ews", "run", "{config}", "--output", "{output}"]
DEPENDENCIES = ("git", "hcloud", "rclone", "ssh", "scp")


def config_home():
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cloud-experiments"


def runtime_hours(value):
    if isinstance(value, bool):
        raise Error("max_runtime_hours must be a positive finite number (at most 168 hours).")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise Error("max_runtime_hours must be a number.") from None
    if not math.isfinite(value) or not 0 < value <= 168:
        raise Error("max_runtime_hours must be positive and at most 168 hours.")
    return value


def repository_url(value, public=False):
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise Error("Repository must be a remote Git URL without whitespace or embedded credentials.")
    parsed = urlsplit(value)
    if parsed.scheme in ("https", "ssh") and parsed.hostname:
        if parsed.password or parsed.query or parsed.fragment or (parsed.scheme == "https" and parsed.username):
            raise Error("Repository URLs must not contain credentials, queries or fragments.")
        if public and parsed.scheme != "https":
            raise Error("EWS must use a public HTTPS Git URL.")
        return value
    if not public and re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+", value):
        return value
    raise Error("Use an HTTPS or SSH Git remote; EWS requires public HTTPS.")


def run_settings(settings):
    result = {"command": DEFAULT_COMMAND, "install_experiment": "auto", "artifact_exclude": [],
              "ews_discord": False, "sync_seconds": 300, "timezone": None}
    result.update(settings)
    if set(result) - {"command", "install_experiment", "artifact_exclude", "ews_discord", "sync_seconds", "timezone"}:
        raise Error("Unknown [run] setting.")
    if (not isinstance(result["command"], list) or not result["command"]
            or not all(isinstance(x, str) and x and "\x00" not in x for x in result["command"])):
        raise Error("run.command must be a nonempty TOML array of argument strings.")
    if result["install_experiment"] not in ("auto", "never"):
        raise Error("run.install_experiment must be 'auto' or 'never'.")
    if not isinstance(result["artifact_exclude"], list) or not all(isinstance(x, str) for x in result["artifact_exclude"]):
        raise Error("run.artifact_exclude must be an array of relative glob patterns.")
    if not isinstance(result["ews_discord"], bool):
        raise Error("run.ews_discord must be a boolean.")
    seconds = result["sync_seconds"]
    if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds) or seconds <= 0):
        raise Error("run.sync_seconds must be a positive finite number.")
    zone = result["timezone"]
    # The selected EWS revision resolves installed IANA names on the VM. Keep
    # laptop configuration dependency-free and do not implement a second resolver.
    if zone is not None and (not isinstance(zone, str) or not zone
                             or any(not char.isprintable() for char in zone)):
        raise Error("run.timezone must be an IANA timezone name such as Europe/Rome or UTC.")
    # Only literal replacement of these placeholders; no shell or format evaluation.
    return result


def ews_discord_webhook(settings, secrets):
    """Select only the explicitly enabled webhook; never expose provider credentials."""
    if not settings.get("ews_discord", False):
        return None
    url = secrets.get("DISCORD_WEBHOOK_URL")
    if not url:
        raise Error("run.ews_discord requires DISCORD_WEBHOOK_URL in worker.env before launch.")
    return url


def load(path=None):
    path = Path(path or os.environ.get("CLOUD_EXPERIMENTS_CONFIG", config_home() / "config.toml")).expanduser()
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        raise Error(f"Cannot read valid configuration at {path}; see docs/setup.md.") from None
    try:
        if any(not isinstance(data.get(key, {}), dict) for key in ("hetzner", "storage", "ews", "run", "local")):
            raise Error("Configuration sections must be TOML tables.")
        h, s, e = data["hetzner"], data["storage"], data["ews"]
        for table, keys in ((h, ("context", "location", "ssh_key", "default_server_type")),
                            (s, ("rclone_remote", "bucket"))):
            for key in keys:
                if not isinstance(table[key], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", table[key]):
                    raise Error(f"Invalid {key} in configuration.")
        h["max_runtime_hours"] = runtime_hours(h.get("max_runtime_hours", 24))
        e["repository"] = repository_url(e["repository"], public=True)
        e.setdefault("default_ref", "main")
        if not isinstance(e["default_ref"], str) or not e["default_ref"] or e["default_ref"].startswith("-"):
            raise Error("Invalid EWS default_ref.")
        data["run"] = run_settings(data.get("run", {}))
        local = data.setdefault("local", {})
        local["state_dir"] = str(Path(local.get("state_dir", Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "cloud-experiments")).expanduser().absolute())
        local["results_dir"] = str(Path(local.get("results_dir", Path.home() / "cloud-results")).expanduser().absolute())
        local["worker_env"] = str(Path(local.get("worker_env", config_home() / "worker.env")).expanduser().absolute())
        local["rclone_config"] = str(Path(local.get("rclone_config", Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "rclone/rclone.conf")).expanduser().absolute())
        if local.get("ssh_identity"):
            local["ssh_identity"] = str(Path(local["ssh_identity"]).expanduser().absolute())
    except (KeyError, TypeError, ValueError):
        raise Error("Missing or invalid configuration setting; see docs/setup.md.") from None
    return data


def private_file(path):
    path = Path(path)
    st = path.stat()
    if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077 or st.st_uid != os.getuid():
        raise Error(f"Credentials must be owned by you and mode 600: {path}")
    return path


def worker_secrets(path):
    """Read only known dotenv keys. Never source shell code or copy the original file."""
    result = {}
    for line in private_file(path).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, sep, value = line.partition("=")
        key = key.strip()
        if key not in ("HCLOUD_WORKER_TOKEN", "DISCORD_WEBHOOK_URL"):
            continue
        try:
            values = shlex.split(value, comments=True)
        except ValueError:
            raise Error("Invalid quoting in worker.env (contents withheld).") from None
        if not sep or len(values) != 1 or any(c in values[0] for c in "\n\r\x00"):
            raise Error("Invalid worker.env value (contents withheld).")
        result[key] = values[0]
    if not result.get("HCLOUD_WORKER_TOKEN"):
        raise Error("worker.env must define HCLOUD_WORKER_TOKEN.")
    if result.get("DISCORD_WEBHOOK_URL"):
        url = urlsplit(result["DISCORD_WEBHOOK_URL"])
        if url.scheme != "https" or url.hostname not in ("discord.com", "discordapp.com") or not url.path.startswith("/api/webhooks/"):
            raise Error("DISCORD_WEBHOOK_URL must be an HTTPS Discord webhook URL.")
    return result


def storage_credentials(path, remote):
    """Copy only the selected plain S3 remote; never other remotes or hcloud config."""
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(private_file(path).read_text())
        section = parser[remote]
        if section.get("type") != "s3" or not section.get("access_key_id") or not section.get("secret_access_key"):
            raise Error("Selected rclone remote must be plain S3 with explicit credentials.")
        allowed = ("type", "provider", "access_key_id", "secret_access_key", "endpoint", "region", "acl")
        clean = configparser.ConfigParser(interpolation=None)
        clean[remote] = {k: section[k] for k in allowed if k in section}
        output = io.StringIO()
        clean.write(output)
        return output.getvalue()
    except (configparser.Error, KeyError):
        raise Error("Cannot parse the selected rclone S3 remote (contents withheld).") from None
