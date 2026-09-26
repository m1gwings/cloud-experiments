"""Logical identities and append-only state publication, independent of VM settings."""

import hashlib
import json
import re
import uuid
from urllib.parse import urlsplit

from .common import Error, valid_result_path

STUDY_RE = re.compile(r"s-[0-9a-f]{32}\Z")
ATTEMPT_RE = re.compile(r"a-([0-9a-f]{32})-[0-9a-f]{20}\Z")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def repository_identity(url):
    if "://" not in url:
        host, path = url.split(":", 1)
        host = host.rsplit("@", 1)[-1]
    else:
        parsed = urlsplit(url)
        host, path = parsed.hostname, parsed.path
    return host.lower() + "/" + path.strip("/").removesuffix(".git")


def study_id(repository, config_path, fresh=False):
    """Repository plus canonical config path; source versions/settings are not identity."""
    identity = [repository_identity(repository), valid_result_path(config_path)]
    return "s-" + (uuid.uuid4().hex if fresh else digest(identity)[:32])


def attempt_id(study):
    if not STUDY_RE.fullmatch(study):
        raise Error("Invalid study ID.")
    return "a-" + study[2:] + "-" + uuid.uuid4().hex[:20]


def attempt_study(attempt):
    match = ATTEMPT_RE.fullmatch(attempt)
    return "s-" + match[1] if match else None


def request_key(manifest, index):
    """Exact completion shortcut; all source bytes/settings count, machine/runtime do not."""
    return digest({"source": index, "experiment": manifest["experiment"], "config": manifest["config"], "ews": manifest["ews"]["commit"],
                   "ews_repository": manifest["ews"]["repository"], "settings": manifest["settings"],
                   "image": manifest["image"], "tool_version": manifest["tool_version"],
                   "portable_continuation": manifest.get("portable_continuation", True)})


def state_head(study, commits):
    """Require a single verified lineage; never silently choose between competing heads.

    VM uniqueness serializes live writers. Immutable parent-linked commits also
    expose a delayed publication after provider deletion as a conflict, rather
    than allowing an old upload to overwrite a newer mutable pointer.
    """
    by_id = {}
    for commit in commits:
        aid = commit.get("attempt_id")
        if (commit.get("schema_version") != 1 or commit.get("study_id") != study
                or not isinstance(aid, str) or attempt_study(aid) != study
                or aid in by_id or not re.fullmatch(r"[0-9a-f]{64}", commit.get("inventory_sha256", ""))):
            raise Error("Invalid or duplicate study state commit.")
        by_id[aid] = commit
    if not by_id:
        return None
    parents = [c.get("parent") for c in commits if c.get("parent") is not None]
    heads = set(by_id) - set(parents)
    if len(heads) != 1 or len(parents) != len(set(parents)):
        raise Error("Study state has competing publications; refusing ambiguous continuation. Preserve the archive and use --fresh.")
    head = by_id[next(iter(heads))]
    seen, current = set(), head
    while current:
        aid = current["attempt_id"]
        if aid in seen:
            raise Error("Cyclic study state history.")
        seen.add(aid)
        parent = current.get("parent")
        if parent is not None and parent not in by_id:
            raise Error("Incomplete study state history; a parent commit is missing.")
        current = by_id.get(parent)
    if len(seen) != len(by_id):
        raise Error("Disconnected study state history.")
    return head
