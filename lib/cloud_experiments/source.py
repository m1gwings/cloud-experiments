"""Git provenance and immutable, content-addressed worktree snapshots."""

import fnmatch
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import tempfile

from .common import Error, SHA_RE, command, sha256
from .config import repository_url

CACHE_DIRS = {".git", ".venv", "venv", "__pycache__", ".cache", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"}
SECRET_NAMES = {".env", "worker.env", "rclone.conf", "credentials", "secrets", ".ssh", ".aws", ".config", "ews-discord-webhook"}


def secret_path(path):
    return any(part in SECRET_NAMES or part.startswith(".env.") or part.endswith((".secret", ".pem", ".key")) for part in Path(path).parts)


def excluded(path, patterns=()):
    p = Path(path)
    return (secret_path(p) or any(part in CACHE_DIRS for part in p.parts)
            or p.suffix in (".pyc", ".pyo") or any(fnmatch.fnmatch(p.as_posix(), x) for x in patterns))


def git(*args, cwd=None):
    return command(["git", *args], cwd=cwd).stdout.decode("utf-8", "surrogateescape").strip()


def git_info(cwd, allow_dirty=False):
    root = Path(git("rev-parse", "--show-toplevel", cwd=cwd))
    commit = git("rev-parse", "HEAD^{commit}", cwd=root)
    if not SHA_RE.fullmatch(commit):
        raise Error("The experiment repository needs an existing Git commit.")
    remote = repository_url(git("remote", "get-url", "origin", cwd=root))
    status = git("status", "--porcelain=v1", "--untracked-files=all", cwd=root)
    if status and not allow_dirty:
        raise Error("Experiment working tree is dirty. Commit/stash changes or explicitly use --allow-dirty.")
    stages = git("ls-files", "--stage", cwd=root)
    if any(line.startswith("160000 ") for line in stages.splitlines()):
        raise Error("Git submodules are not supported; vendor their source before running.")
    return root, {"repository": remote, "commit": commit, "dirty": bool(status)}, status


def resolve_ref(repository, ref):
    repository_url(repository, public=True)
    if not isinstance(ref, str) or not ref or ref.startswith("-") or any(c.isspace() for c in ref):
        raise Error("Invalid EWS ref.")
    # Fetching handles branches, lightweight/annotated tags and exact commits alike.
    # Peeling FETCH_HEAD records the commit, never the annotated tag object.
    with tempfile.TemporaryDirectory(prefix="cloud-ews-") as directory:
        git("init", "--bare", directory)
        command(["git", "-C", directory, "fetch", "--quiet", "--depth=1", "--no-tags", repository, ref], timeout=300)
        commit = git("rev-parse", "FETCH_HEAD^{commit}", cwd=directory)
    if not SHA_RE.fullmatch(commit) or (SHA_RE.fullmatch(ref) and commit != ref):
        raise Error("EWS ref did not resolve to the requested exact commit.")
    return commit


def snapshot(cwd, config_arg, destination, allow_dirty=False):
    root, provenance, status = git_info(cwd, allow_dirty)
    config = (Path(cwd) / config_arg).resolve()
    try:
        config_relative = config.relative_to(root).as_posix()
    except ValueError:
        raise Error("The experiment config must be a file inside the experiment repository.") from None
    names = command(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root).stdout.split(b"\0")
    names = sorted({os.fsdecode(n) for n in names if n})
    index = {}
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cloud-source-") as directory:
        stage = Path(directory)
        for name in names:
            path = root / name
            if secret_path(name):
                raise Error("Snapshot contains a secret-like path. Remove it from Git/input files; contents were not read.")
            if excluded(name):
                continue
            try:
                before = path.lstat()
            except FileNotFoundError:  # A tracked deletion in an allowed dirty run.
                continue
            if not stat.S_ISREG(before.st_mode):
                raise Error("Snapshots require regular files; symlinks and special files are unsupported.")
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            after = path.stat()
            if (before.st_mtime_ns, before.st_size, before.st_ino) != (after.st_mtime_ns, after.st_size, after.st_ino):
                raise Error("Source changed during snapshot; retry with a quiescent working tree.")
            mode = 0o755 if before.st_mode & 0o111 else 0o644
            target.chmod(mode)
            index[name] = {"sha256": sha256(target), "mode": mode}
        if config_relative not in index:
            raise Error("Config is missing, ignored, excluded, or outside the snapshot.")
        if git("status", "--porcelain=v1", "--untracked-files=all", cwd=root) != status or git("rev-parse", "HEAD", cwd=root) != provenance["commit"]:
            raise Error("Git state changed during snapshot; retry.")
        with tarfile.open(destination, "w:gz") as archive:
            for name in index:
                info = archive.gettarinfo(str(stage / name), arcname=name)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                with (stage / name).open("rb") as f:
                    archive.addfile(info, f)
        shutil.copyfile(stage / config_relative, destination.parent / "experiment-config")
    return provenance, config_relative, index


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
