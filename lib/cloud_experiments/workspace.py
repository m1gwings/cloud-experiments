"""Safe workspace paths, source extraction and nonscientific artifact inventories."""

import fnmatch
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile

from .common import Error, sha256

CACHE_DIRS = {".git", ".venv", "venv", "__pycache__", ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"}
SECRET_NAMES = {".env", "worker.env", "rclone.conf", "credentials", "secrets", ".ssh", ".aws", ".config", "ews-discord-webhook"}


def secret_path(path):
    return any(part in SECRET_NAMES or part.startswith(".env.") or part.endswith((".secret", ".pem", ".key")) for part in Path(path).parts)


def excluded(path, patterns=()):
    p = Path(path)
    return (secret_path(p) or any(part in CACHE_DIRS for part in p.parts)
            or p.suffix in (".pyc", ".pyo") or any(fnmatch.fnmatch(p.as_posix(), x) for x in patterns))


def extract_snapshot(archive_path, target, expected_sha=None):
    if expected_sha and sha256(archive_path) != expected_sha:
        raise Error("Source snapshot checksum mismatch.")
    target = Path(target)
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "r:gz") as archive:
        seen = set()
        for member in archive:
            name = PurePosixPath(member.name)
            if (name.is_absolute() or ".." in name.parts or not name.parts
                    or not member.isfile() or member.name in seen or secret_path(member.name)):
                raise Error("Unsafe or unsupported source archive member.")
            seen.add(member.name)
            dest = target / str(name)
            if not dest.resolve().is_relative_to(target.resolve()):
                raise Error("Source archive would escape its destination.")
            dest.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as src, dest.open("xb") as out:
                shutil.copyfileobj(src, out)
            dest.chmod(0o755 if member.mode & 0o111 else 0o644)


def inventory(root, patterns=()):
    """Never follow symlinks; record exclusions and nonregular files explicitly."""
    root = Path(root)
    files, omitted = {}, []
    if not root.exists():
        return files, omitted
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in list(dirs):
            p = Path(directory) / name
            relative = p.relative_to(root).as_posix()
            if excluded(relative, patterns) or p.is_symlink():
                dirs.remove(name)
                omitted.append(relative)
        for name in names:
            p = Path(directory) / name
            relative = p.relative_to(root).as_posix()
            st = p.lstat()
            if excluded(relative, patterns) or not stat.S_ISREG(st.st_mode):
                omitted.append(relative)
                continue
            files[relative] = {"sha256": sha256(p), "mode": 0o755 if st.st_mode & 0o111 else 0o644}
    return files, omitted
