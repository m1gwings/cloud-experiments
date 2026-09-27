"""Safe package locks: index requirements plus explicitly rebuilt local projects."""

import json
import re

from .common import Error

# Run only inside the study venv. Never archive raw direct URLs or editable paths.
CAPTURE = r'''
import json, platform, re, struct, sys, sysconfig
from pathlib import Path
from importlib.metadata import distributions
packages, local = {}, {}
for dist in distributions():
    name = re.sub(r"[-_.]+", "-", dist.metadata["Name"]).lower()
    origin = json.loads(dist.read_text("direct_url.json") or "{}")
    if origin:
        roots = {Path("/work/.ews").as_uri(): "ews", Path("/work/source").as_uri(): "experiment"}
        if origin.get("url") not in roots or not origin.get("dir_info", {}).get("editable"):
            raise SystemExit("Unsupported direct/local dependency; use index packages and the two archived source projects.")
        local[name] = {"source": roots[origin["url"]], "version": dist.version}
    else:
        packages[name] = dist.version
# Editable source metadata can appear twice; the local project wins.
for name in local:
    packages.pop(name, None)
print(json.dumps({"schema_version": 1, "runtime": {"python": platform.python_version(),
    "implementation": platform.python_implementation(), "abi": sysconfig.get_config_var("SOABI"),
    "system": platform.system(), "machine": platform.machine().lower(), "byteorder": sys.byteorder,
    "pointer_bits": struct.calcsize("P")*8, "libc": list(platform.libc_ver())},
    "packages": packages, "local_projects": local}, sort_keys=True))
'''


def validate(lock):
    if (not isinstance(lock, dict) or lock.get("schema_version") != 1
            or not isinstance(lock.get("runtime"), dict) or not isinstance(lock.get("packages"), dict)
            or not isinstance(lock.get("local_projects"), dict)):
        raise Error("Invalid continuation environment manifest.")
    required = {"python", "implementation", "abi", "system", "machine", "byteorder", "pointer_bits", "libc"}
    if set(lock["runtime"]) != required:
        raise Error("Incomplete continuation runtime fingerprint.")
    for name, version in lock["packages"].items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) or not isinstance(version, str) or not re.fullmatch(r"[A-Za-z0-9_.+!-]+", version):
            raise Error("Unsafe package requirement in continuation environment.")
    for name, project in lock["local_projects"].items():
        if (not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) or not isinstance(project, dict)
                or set(project) != {"source", "version"} or project["source"] not in ("ews", "experiment")
                or not isinstance(project["version"], str)):
            raise Error("Invalid local project in continuation environment.")
        if name in lock["packages"] and lock["packages"][name] != project["version"]:
            raise Error("Conflicting local and index package versions in continuation environment.")
    return lock


def index_packages(lock):
    validate(lock)
    # Older locks may record an editable project twice. It is rebuilt from the
    # archived source, so never ask the package index to supply that duplicate.
    return {name: version for name, version in lock["packages"].items()
            if name not in lock["local_projects"]}


def constraints(lock):
    return "".join(f"{name}=={version}\n" for name, version in sorted(index_packages(lock).items()))


def verify(expected, actual):
    validate(expected)
    validate(actual)
    if expected["runtime"] != actual["runtime"] or index_packages(expected) != index_packages(actual):
        raise Error("Continuation environment differs from the saved runtime/package lock. No EWS checkpoint was loaded; recreate the environment or use --fresh.")
    # New archived scientific source/EWS may have new project versions. EWS's
    # source and environment fingerprints select new variants in that case.
    if {k: v["source"] for k, v in expected["local_projects"].items()} != {k: v["source"] for k, v in actual["local_projects"].items()}:
        raise Error("Continuation local project set changed; use --fresh.")


def decode(raw):
    try:
        return validate(json.loads(raw))
    except (ValueError, TypeError):
        raise Error("Cannot read the worker environment manifest.") from None
