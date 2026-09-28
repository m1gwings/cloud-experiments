"""Immutable cloud containers for EWS recovery digests.

The EWS inventory remains logical. This module owns the versioned physical
layout and the deliberately simple, uncompressed digest-named tar containers.
"""

import gzip
import hashlib
import io
import json
import re
import stat
import tarfile
from pathlib import Path

from .common import Error, sha256

SMALL_LIMIT = 64 * 1024
PACK_TARGET = 8 * 1024 * 1024
SCHEMA = "cloud-experiments/physical-layout"
VERSION = 1
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
MAX_LAYOUT_BYTES = 32 * 1024 * 1024
MAX_LAYOUT_JSON_BYTES = 64 * 1024 * 1024
MAX_PACK_BYTES = PACK_TARGET + 1024 * 1024


def inventory(recovery):
    result = {}
    for entry in recovery["files"].values():
        digest, size = entry["sha256"], entry["size"]
        if digest in result and result[digest] != size:
            raise Error("Inconsistent size for a committed recovery blob.")
        result[digest] = size
    return result


def legacy(recovery):
    return {"loose": set(inventory(recovery)), "packs": {}}


def locations(layout):
    result = {digest: None for digest in layout["loose"]}
    for pack, members in layout["packs"].items():
        for digest in members:
            result[digest] = pack
    return result


def validate(layout, recovery):
    if (not isinstance(layout, dict) or layout.get("schema") != SCHEMA
            or type(layout.get("schema_version")) is not int or layout["schema_version"] != VERSION
            or layout.get("snapshot_id") != recovery["snapshot_id"]):
        raise Error("Unsupported or invalid cloud physical layout.")
    loose, packs = layout.get("loose"), layout.get("packs")
    if not isinstance(loose, list) or not isinstance(packs, dict):
        raise Error("Invalid cloud physical layout inventory.")
    if loose != sorted(set(loose)) or any(not isinstance(x, str) or not DIGEST.fullmatch(x) for x in loose):
        raise Error("Invalid cloud physical layout standalone digests.")
    seen = set(loose)
    for pack, members in packs.items():
        if (not isinstance(pack, str) or not DIGEST.fullmatch(pack) or not isinstance(members, list)
                or members != sorted(set(members)) or not members):
            raise Error("Invalid cloud physical pack inventory.")
        for digest in members:
            if not isinstance(digest, str) or not DIGEST.fullmatch(digest) or digest in seen:
                raise Error("Duplicate or invalid cloud physical digest.")
            seen.add(digest)
    required = set(inventory(recovery))
    if not required <= seen or any(d not in required for d in loose):
        raise Error("Cloud physical layout does not cover the EWS recovery inventory.")
    if any(not set(members) & required for members in packs.values()):
        raise Error("Cloud physical layout contains an unused pack.")
    return {"loose": set(loose), "packs": {key: set(value) for key, value in packs.items()}}


def encode(snapshot_id, layout):
    document = {"schema": SCHEMA, "schema_version": VERSION, "snapshot_id": snapshot_id,
                "loose": sorted(layout["loose"]),
                "packs": {key: sorted(value) for key, value in sorted(layout["packs"].items())}}
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return gzip.compress(raw, compresslevel=1, mtime=0)


def decode(raw, recovery):
    if len(raw) > MAX_LAYOUT_BYTES:
        raise Error("Cloud physical layout exceeds its size limit.")
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate layout key")
            value[key] = item
        return value
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            payload = stream.read(MAX_LAYOUT_JSON_BYTES + 1)
        if len(payload) > MAX_LAYOUT_JSON_BYTES:
            raise ValueError("layout too large")
        document = json.loads(payload, object_pairs_hook=unique)
    except (ValueError, OSError, UnicodeError, TypeError, OverflowError):
        raise Error("Invalid cloud physical layout encoding.") from None
    return validate(document, recovery)


def pack_sources(sources, target):
    """Stream verified sealed files into one deterministic tar container."""
    class Reader:
        def __init__(self, stream):
            self.stream = stream
            self.checksum = hashlib.sha256()
            self.count = 0

        def read(self, size):
            chunk = self.stream.read(size)
            self.checksum.update(chunk)
            self.count += len(chunk)
            return chunk

    target = Path(target)
    with tarfile.open(target, "w", format=tarfile.USTAR_FORMAT) as archive:
        for digest, (source, size) in sorted(sources.items()):
            source = Path(source)
            if (not stat.S_ISREG(source.lstat().st_mode) or source.stat().st_size != size
                    or sha256(source) != digest):
                raise Error("Sealed recovery payload changed before packing.")
            info = tarfile.TarInfo(digest)
            info.size = size
            info.mode = 0o444
            info.mtime = 0
            with source.open("rb") as stream:
                reader = Reader(stream)
                archive.addfile(info, reader)
                if reader.count != size or reader.checksum.hexdigest() != digest:
                    raise Error("Sealed recovery payload changed before packing.")
    return sha256(target)


def extract(pack_path, pack_digest, expected, required, sizes, destination):
    """Reject unsafe tar members and verify each materialized digest."""
    pack_path = Path(pack_path)
    if pack_path.is_symlink() or not pack_path.is_file():
        raise Error("Recovery pack is missing or linked.")
    if pack_path.stat().st_size > MAX_PACK_BYTES:
        raise Error("Recovery pack exceeds the supported size limit.")
    if sha256(pack_path) != pack_digest:
        raise Error("Downloaded recovery pack failed SHA-256 verification.")
    seen = set()
    try:
        with tarfile.open(pack_path, "r:") as archive:
            for member in archive:
                digest = member.name
                if (not member.isfile() or not DIGEST.fullmatch(digest) or digest not in expected
                        or digest in seen or member.size < 0):
                    raise Error("Recovery pack contains an unsafe, unknown, or duplicate member.")
                seen.add(digest)
                if digest not in required:
                    continue
                if member.size != sizes[digest]:
                    raise Error("Restored EWS state failed size verification.")
                source = archive.extractfile(member)
                if source is None:
                    raise Error("Recovery pack member cannot be read.")
                target = destination / digest
                checksum = hashlib.sha256()
                count = 0
                with target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                        checksum.update(chunk)
                        count += len(chunk)
                if count != sizes[digest] or checksum.hexdigest() != digest:
                    raise Error("Restored EWS state failed SHA-256 verification.")
    except (tarfile.TarError, EOFError, OSError):
        raise Error("Invalid or truncated recovery pack.") from None
    if seen != expected:
        raise Error("Recovery pack member inventory mismatch.")
