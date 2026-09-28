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
    """Exact completion shortcut, excluding display and operational allocations."""
    settings = {key: value for key, value in manifest["settings"].items()
                if key not in ("sync_seconds", "timezone", "ews_discord")}
    return digest({"source": index, "experiment": manifest["experiment"], "config": manifest["config"], "ews": manifest["ews"]["commit"],
                   "ews_repository": manifest["ews"]["repository"], "settings": settings,
                   "image": manifest["image"], "tool_version": manifest["tool_version"],
                   "portable_continuation": manifest.get("portable_continuation", True)})


def state_head(study, commits, *, allow_legacy=False):
    """Validate one parent-linked lineage and reject incompatible recovery state.

    Schema 2 permits multiple durable commits per attempt. Schema 1 remains
    readable for archival listings only; continuation requires a fresh lineage.
    """
    from .ews_contract import validate_contract

    by_id = {}
    if any(not isinstance(commit, dict) or type(commit.get("schema_version")) is not int
           for commit in commits):
        raise Error("Invalid cloud recovery state contract.")
    versions = {commit["schema_version"] for commit in commits}
    if versions and versions != {2} and not (allow_legacy and versions == {1}):
        raise Error("Unsupported cloud recovery state contract; preserve the archive and use --fresh.")
    legacy = versions == {1}
    for commit in commits:
        aid = commit.get("attempt_id")
        identifier = aid if legacy else commit.get("commit_id")
        checksum = commit.get("inventory_sha256") if legacy else commit.get("recovery_sha256")
        if (commit.get("study_id") != study or not isinstance(aid, str)
                or attempt_study(aid) != study or not isinstance(identifier, str)
                or identifier in by_id or not isinstance(checksum, str)
                or not re.fullmatch(r"[0-9a-f]{64}", checksum)):
            raise Error("Invalid or duplicate study state commit.")
        if not legacy:
            if (not re.fullmatch(r"[0-9a-f]{32}", identifier)
                    or not isinstance(commit.get("snapshot_id"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", commit["snapshot_id"])
                    or not isinstance(commit.get("committed_at"), str)
                    or type(commit.get("final")) is not bool
                    or type(commit.get("completed")) is not bool):
                raise Error("Invalid cloud recovery commit metadata.")
            validate_contract(commit.get("contract"))
            if "layout_sha256" in commit and (not isinstance(commit["layout_sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", commit["layout_sha256"])):
                raise Error("Invalid cloud physical layout reference.")
            provenance = commit.get("provenance")
            if not isinstance(provenance, dict):
                raise Error("Recovery commit is missing tooling provenance.")
            ews = provenance.get("ews", {})
            cloud = provenance.get("cloud", {})
            if (not isinstance(ews, dict) or not isinstance(ews.get("commit"), str)
                    or not re.fullmatch(r"[0-9a-f]{40}", ews["commit"])
                    or not isinstance(cloud, dict) or not isinstance(cloud.get("version"), str)
                    or not cloud["version"]):
                raise Error("Recovery commit is missing exact EWS/cloud tooling provenance.")
        parent = commit.get("parent")
        if parent is not None and not isinstance(parent, str):
            raise Error("Invalid study recovery parent.")
        by_id[identifier] = commit
    if not by_id:
        return None
    parents = [c.get("parent") for c in commits if c.get("parent") is not None]
    heads = set(by_id) - set(parents)
    if len(heads) != 1 or len(parents) != len(set(parents)):
        raise Error("Study state has competing publications; refusing ambiguous continuation. Preserve the archive and use --fresh.")
    head = by_id[next(iter(heads))]
    seen, current = set(), head
    while current:
        identifier = current["attempt_id"] if legacy else current["commit_id"]
        if identifier in seen:
            raise Error("Cyclic study state history.")
        seen.add(identifier)
        parent = current.get("parent")
        if parent is not None and parent not in by_id:
            raise Error("Incomplete study state history; a parent commit is missing.")
        current = by_id.get(parent)
    if len(seen) != len(by_id):
        raise Error("Disconnected study state history.")
    return head
