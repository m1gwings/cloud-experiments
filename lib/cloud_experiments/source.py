"""Git provenance and immutable, content-addressed worktree snapshots."""

import os
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile

from .common import Error, SHA_RE, command, sha256
from .config import repository_url

from .workspace import excluded, extract_snapshot, inventory, secret_path


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


def _fetch_ref(repository, ref, inspect=None):
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
        return commit, inspect(directory, commit) if inspect else None


def resolve_ref(repository, ref):
    """Resolve a Git ref exactly, retaining the historical source-only interface."""
    return _fetch_ref(repository, ref)[0]


def resolve_ews(repository, ref):
    """Resolve and reject unsupported EWS persistence contracts before VM creation."""
    from . import ews_contract

    def inspect(directory, commit):
        try:
            recovery = git("show", commit + ":" + ews_contract.SOURCE, cwd=directory)
            exports = git("show", commit + ":" + ews_contract.EXPORTS, cwd=directory)
        except Error:
            raise Error("EWS pin does not expose the supported cloud recovery contract; select a compatible EWS revision.") from None
        return ews_contract.inspect_source(recovery, exports)

    commit, contract = _fetch_ref(repository, ref, inspect)
    return {"repository": repository, "requested_ref": ref, "commit": commit, "cloud_contract": contract}


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
