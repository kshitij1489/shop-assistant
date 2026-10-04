"""Deterministic identities and content provenance, without environment capture."""
import hashlib
import json
from pathlib import Path
import subprocess
from uuid import NAMESPACE_URL, uuid5

from evaluate.contracts.models import ApplicationRevision


def canonical_hash(value) -> str:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def scenario_id(namespace: str, source_id: str) -> str:
    if namespace not in {"sessions", "qa"}:
        raise ValueError("unknown scenario namespace")
    return f"{namespace}:{source_id}"


def instance_id(run_id: str, scenario_id: str, repetition: int = 0) -> str:
    if type(repetition) is not int or repetition < 0:
        raise ValueError("repetition must be a nonnegative integer")
    # Attempts deliberately do not change an instance. Repetitions do.
    return str(uuid5(NAMESPACE_URL, json.dumps(["evaluate/v1", run_id, scenario_id, repetition])))


def application_revision(root: Path) -> ApplicationRevision:
    """Hash HEAD/index/worktree and unignored files. Emit hashes only, never diffs.

    Git-ignored files (including local .env files) are outside this policy. Runtime
    configuration must be represented separately by the allowlisted config hash.
    """
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.DEVNULL)

    commit = git("rev-parse", "HEAD").decode().strip()
    status = git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    names = sorted(set(git("ls-files", "-z", "--cached", "--others", "--exclude-standard").split(b"\0")) - {b""})
    entries = []
    for raw in names:
        path = root / raw.decode()
        if path.is_symlink():
            import os
            digest = hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest()
            mode = "symlink"
        elif path.is_file():
            digest = file_hash(path)
            mode = "executable" if path.stat().st_mode & 0o111 else "file"
        elif not path.exists():
            digest, mode = None, "deleted"
        else:
            # Do not silently claim to fingerprint submodules or special files.
            raise ValueError("unsupported working-tree entry in fingerprint")
        entries.append([raw.decode(), mode, digest])
    fingerprint = canonical_hash({"commit": commit, "index": hashlib.sha256(git("ls-files", "--stage", "-z")).hexdigest(), "files": entries})
    return ApplicationRevision(commit=commit, working_tree_fingerprint=fingerprint, dirty=bool(status))
