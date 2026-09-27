"""Identify the cloud implementation that produced a persisted lifecycle record."""

import hashlib
from pathlib import Path

from . import __version__
from .common import Error, SHA_RE, command


def current():
    """Record package version, available Git revision, and exact implementation bytes.

    The content fingerprint also identifies a modified checkout or an installation
    without Git metadata; it covers bundled Python modules and service templates.
    """
    package = Path(__file__).resolve().parent
    root = package.parents[1]
    digest = hashlib.sha256()
    files = sorted([*package.glob("*.py"), *(root / "templates").glob("cloud-*")])
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    commit = None
    if (root / ".git").exists():
        try:
            result = command(["git", "rev-parse", "HEAD^{commit}"], cwd=root, timeout=10)
            value = result.stdout.decode().strip()
            if SHA_RE.fullmatch(value):
                commit = value
        except (Error, UnicodeError):
            pass
    return {"version": __version__, "git_commit": commit, "implementation_sha256": digest.hexdigest()}
