"""Resolve EWS semantic roles from stored metadata, without knowing its layout."""

from .common import Error, valid_result_path

INDEX_FILENAME = "artifacts.json"
INDEX_SCHEMA = "experiments-wo-stress/artifacts"
MAX_INDEX_BYTES = 65536


def artifact_selection(storage, rid, files, role):
    """Find one output catalog and return its validated run-relative role entry.

    Only captured artifacts are candidates, never archived source/config inputs.
    Multiple catalogs are ambiguous; refuse to guess which experiment was meant.
    Missing roles are unsupported/absent, while malformed catalogs are errors.
    """
    candidates = [entry for entry in files
                  if entry["path"].startswith("artifacts/")
                  and entry["path"].endswith("/" + INDEX_FILENAME)]
    if not candidates:
        raise Error("No EWS artifacts.json found (legacy run or metadata not uploaded). "
                    "Use cloud-results ls RUN_ID, then pull RUN_ID --path RELATIVE_PATH.")
    if len(candidates) != 1:
        raise Error("Multiple artifact catalogs found; use cloud-results ls RUN_ID and --path "
                    "to select the intended output explicitly.")
    candidate = candidates[0]
    if candidate["size"] > MAX_INDEX_BYTES:
        raise Error("EWS artifact catalog exceeds the 64 KiB limit; use --path.")
    index = storage.artifact_index(rid, candidate["path"])
    if (not isinstance(index, dict) or index.get("schema") != INDEX_SCHEMA
            or type(index.get("schema_version")) is not int or index["schema_version"] != 1):
        raise Error("Unsupported EWS artifact schema/version; update cloud-experiments or use --path.")
    artifacts = index.get("artifacts")
    if not isinstance(artifacts, dict):
        raise Error("Invalid EWS artifact catalog: artifacts must be an object; use --path.")
    for entry in artifacts.values():
        if (not isinstance(entry, dict) or entry.get("kind") not in ("file", "directory")
                or type(entry.get("optional")) is not bool):
            raise Error("Invalid EWS artifact descriptor; use --path.")
        path = valid_result_path(entry.get("path"))
        if path != entry["path"]:
            raise Error("EWS artifact paths must be canonical relative paths; use --path.")
    entry = artifacts.get(role)
    if entry is None:
        return None
    output_root = candidate["path"].rsplit("/", 1)[0]
    return {**entry, "path": output_root + "/" + entry["path"]}
