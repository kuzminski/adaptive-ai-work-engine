#!/usr/bin/env python3
"""Fail-closed recovery for edits left in an AAW execution worktree.

The recovery contract is deliberately narrower than a generic Git cleanup:

* a run begins from the runner-proven clean worktree baseline;
* once the run settles, the bridge freezes the exact dirty paths and their
  content fingerprints in the run's artifact directory;
* cleanup is permitted only while HEAD and every frozen path still match;
* tracked paths are restored explicitly from the run's proven baseline tree and
  explicit untracked files are removed one by one.

There is no ``reset --hard``, no glob and no deletion of an unproven path.
Runtime artifacts and Git history are outside this module's write surface.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import datetime as dt
import tempfile
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = "AAW_RUN_WORKTREE_OWNERSHIP_V0.3"
BASELINE_SCHEMA_VERSION = "AAW_AUTHORIZED_WORKSPACE_BASELINE_V0.3"
MAX_PREVIEW_BYTES = 200_000
OWNED = "RUN_OWNED"
AMBIGUOUS = "AMBIGUOUS_OWNERSHIP"
NO_CHANGES = "NO_CHANGES"
UNRESOLVED = "UNRESOLVED"
KEPT = "KEPT"
DISCARDED = "DISCARDED"
ADOPTED = "ADOPTED_AS_BASELINE"
BASELINE_AUTHORIZED = "AUTHORIZED_WORKSPACE_BASELINE"
BASELINE_DIVERGED = "BASELINE_DIVERGED"


class RecoveryError(RuntimeError):
    """A destructive recovery request that cannot be proved safe."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def acquire_worktree_lease(worktree: Path, run_id: str) -> Path:
    """Acquire a worktree-specific cross-process ownership marker."""
    raw = _git_text(Path(worktree), "rev-parse", "--git-path", "aaw-run-owner.lock")
    path = Path(raw)
    if not path.is_absolute():
        path = Path(worktree) / path
    path = path.resolve(strict=False)
    payload = json.dumps({
        "schema_version": SCHEMA_VERSION,
        "run_id": str(run_id),
        "pid": os.getpid(),
        "acquired_at": dt.datetime.now().astimezone().isoformat(timespec="milliseconds"),
        "worktree": str(Path(worktree).resolve()),
    }, ensure_ascii=True, sort_keys=True).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError as exc:
        try:
            owner = json.loads(path.read_text(encoding="utf-8")).get("run_id")
        except (OSError, json.JSONDecodeError):
            owner = "unknown"
        raise RecoveryError("WORKTREE_IN_USE", f"worktree lease is held by run {owner}") from exc
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)
    return path


def release_worktree_lease(path: Path, run_id: str) -> None:
    """Release only the exact lease that names this run; otherwise fail closed."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, json.JSONDecodeError) as exc:
        raise RecoveryError("LEASE_OWNERSHIP_UNKNOWN", f"cannot verify worktree lease: {exc}") from exc
    if str(payload.get("run_id")) != str(run_id):
        raise RecoveryError("LEASE_OWNERSHIP_MISMATCH", "refusing to release another run's lease")
    Path(path).unlink()


def _git_bytes(worktree: Path, *args: str, env: Mapping[str, str] | None = None) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(worktree), *args], shell=False,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False, env=dict(env) if env is not None else None,
    )
    if completed.returncode:
        detail = completed.stderr.decode("utf-8", "replace").strip()
        raise RecoveryError("GIT_INSPECTION_FAILED", detail or f"git {' '.join(args)} failed")
    return completed.stdout


def _git_text(worktree: Path, *args: str) -> str:
    return _git_bytes(worktree, *args).decode("utf-8", "surrogateescape").strip()


def _baseline_path(worktree: Path) -> Path:
    raw = _git_text(Path(worktree), "rev-parse", "--git-path", "aaw-authorized-baseline.json")
    path = Path(raw)
    if not path.is_absolute():
        path = Path(worktree) / path
    return path.resolve(strict=False)


def _tree_from_workspace(worktree: Path) -> str:
    """Write a content-addressed tree for tracked + non-ignored untracked bytes.

    A private temporary index keeps both the user's index and branch untouched.
    The resulting object is an ordinary Git tree. Adoption later anchors it
    under a dedicated non-branch ref; no commit or branch ref is made.
    """
    root = Path(worktree).resolve()
    git_dir = Path(_git_text(root, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    git_dir = git_dir.resolve(strict=False)
    descriptor, raw_path = tempfile.mkstemp(prefix="aaw-baseline-", suffix=".index", dir=git_dir)
    os.close(descriptor)
    index_path = Path(raw_path)
    index_path.unlink()
    environment = dict(os.environ)
    environment["GIT_INDEX_FILE"] = str(index_path)
    try:
        _git_bytes(root, "read-tree", "HEAD", env=environment)
        _git_bytes(root, "add", "-A", "--", env=environment)
        return _git_bytes(root, "write-tree", env=environment).decode("ascii").strip()
    finally:
        for target in (index_path, Path(str(index_path) + ".lock")):
            try:
                target.unlink()
            except FileNotFoundError:
                pass


def _canonical_payload(payload: Mapping[str, Any]) -> bytes:
    clean = {key: value for key, value in payload.items() if key != "fingerprint"}
    return json.dumps(clean, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _manifest_fingerprint(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_payload(payload)).hexdigest()


def _fingerprint(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        target = os.readlink(path)
        raw = os.fsencode(target)
        return {"exists": True, "kind": "symlink", "size": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest()}
    if path.is_file():
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                digest.update(block)
        return {"exists": True, "kind": "file", "size": size,
                "sha256": digest.hexdigest()}
    if path.exists():
        return {"exists": True, "kind": "directory", "size": None, "sha256": None}
    return {"exists": False, "kind": "missing", "size": None, "sha256": None}


def status_snapshot(worktree: Path) -> dict[str, Any]:
    """Return a path-attributed, content-fingerprinted Git status snapshot."""
    root = Path(_git_text(worktree, "rev-parse", "--show-toplevel")).resolve()
    head = _git_text(root, "rev-parse", "HEAD")
    raw = _git_bytes(root, "-c", "core.quotepath=false", "status",
                     "--porcelain=v1", "-z", "--untracked-files=all")
    fields = raw.decode("utf-8", "surrogateescape").split("\0")
    entries: list[dict[str, Any]] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if not record:
            continue
        if len(record) < 4:
            raise RecoveryError("GIT_STATUS_MALFORMED", "git returned a malformed status record")
        status, path_text = record[:2], record[3:]
        original_path = None
        if status[0] in "RC" or status[1] in "RC":
            if index >= len(fields) or not fields[index]:
                raise RecoveryError("GIT_STATUS_MALFORMED", "rename record has no source path")
            original_path = fields[index]
            index += 1
        candidate = (root / Path(path_text)).resolve(strict=False)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise RecoveryError("PATH_OUTSIDE_WORKTREE", f"status path escapes worktree: {path_text}") from exc
        row = {"path": path_text, "status": status,
               "class": "untracked" if status == "??" else "modified",
               "original_path": original_path}
        row.update(_fingerprint(candidate))
        entries.append(row)
    entries.sort(key=lambda row: row["path"])
    return {"worktree": str(root), "head": head, "entries": entries}


def _baseline_refs(worktree: Path) -> tuple[str, str]:
    key = hashlib.sha256(str(Path(worktree).resolve()).casefold().encode("utf-8")).hexdigest()[:32]
    stem = f"refs/aaw/workspace-baselines/{key}"
    return f"{stem}/workspace", f"{stem}/index"


def adopt_manifest_as_baseline(manifest: Mapping[str, Any], *, recovery_path: Path) -> dict[str, Any]:
    """Authorize the exact settled workspace as the next-run baseline."""
    checked = inspect_manifest(manifest)
    if str(checked.get("resolution")) != UNRESOLVED:
        raise RecoveryError("RECOVERY_ALREADY_RESOLVED",
                            f"recovery was already resolved as {checked.get('resolution')}")
    if checked.get("ownership") != OWNED or checked.get("mixed_change_possible"):
        detail = "; ".join(checked.get("proof_reasons") or []) or "run ownership is not exact"
        raise RecoveryError("AMBIGUOUS_OWNERSHIP", detail)
    root = Path(str(checked["worktree"])).resolve()
    current = status_snapshot(root)
    workspace_tree = _tree_from_workspace(root)
    index_tree = _git_text(root, "write-tree")
    workspace_ref, index_ref = _baseline_refs(root)
    _git_bytes(root, "update-ref", workspace_ref, workspace_tree)
    try:
        _git_bytes(root, "update-ref", index_ref, index_tree)
    except Exception:
        _git_bytes(root, "update-ref", "-d", workspace_ref)
        raise
    baseline: dict[str, Any] = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "state": BASELINE_AUTHORIZED,
        "baseline_id": None,
        "worktree": str(root),
        "head": current["head"],
        "workspace_tree": workspace_tree,
        "index_tree": index_tree,
        "workspace_ref": workspace_ref,
        "index_ref": index_ref,
        "snapshot": current,
        "originating_run_id": str(checked["originating_run_id"]),
        "recovery_manifest": str(Path(recovery_path).resolve()),
        "adopted_at": dt.datetime.now().astimezone().isoformat(timespec="milliseconds"),
    }
    identity = hashlib.sha256(_canonical_payload(baseline)).hexdigest()
    baseline["baseline_id"] = f"AWB_{identity[:32]}"
    baseline["fingerprint"] = _manifest_fingerprint(baseline)
    path = _baseline_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(baseline, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    os.replace(temp, path)
    inspected = inspect_authorized_baseline(root)
    if inspected["state"] != BASELINE_AUTHORIZED:
        raise RecoveryError(BASELINE_DIVERGED, "; ".join(inspected["proof_reasons"]))
    return inspected


def inspect_authorized_baseline(worktree: Path) -> dict[str, Any]:
    """Load and re-prove this worktree's adopted baseline, without mutation."""
    root = Path(worktree).resolve()
    path = _baseline_path(root)
    if not path.is_file():
        return {"state": "NO_AUTHORIZED_BASELINE", "worktree": str(root),
                "manifest_path": str(path), "proof_reasons": []}
    reasons: list[str] = []
    try:
        baseline = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"state": BASELINE_DIVERGED, "worktree": str(root),
                "manifest_path": str(path), "proof_reasons": [f"baseline manifest unreadable: {exc}"]}
    if baseline.get("schema_version") != BASELINE_SCHEMA_VERSION:
        reasons.append("baseline manifest schema is not supported")
    if str(baseline.get("worktree") or "").casefold() != str(root).casefold():
        reasons.append("baseline belongs to a different worktree")
    if baseline.get("fingerprint") != _manifest_fingerprint(baseline):
        reasons.append("baseline manifest fingerprint does not match")
    try:
        current = status_snapshot(root)
        workspace_tree = _tree_from_workspace(root)
        index_tree = _git_text(root, "write-tree")
        if current["head"] != baseline.get("head"):
            reasons.append("worktree HEAD changed after baseline adoption")
        if workspace_tree != baseline.get("workspace_tree"):
            reasons.append("workspace contents changed after baseline adoption")
        if index_tree != baseline.get("index_tree"):
            reasons.append("worktree index changed after baseline adoption")
        if current != baseline.get("snapshot"):
            reasons.append("Git status or path fingerprints changed after baseline adoption")
        for key in ("workspace_ref", "index_ref"):
            expected = baseline.get("workspace_tree" if key == "workspace_ref" else "index_tree")
            if not baseline.get(key) or _git_text(root, "rev-parse", str(baseline.get(key))) != expected:
                reasons.append(f"durable {key} no longer identifies the approved tree")
    except (RecoveryError, OSError) as exc:
        current = {"worktree": str(root), "head": None, "entries": []}
        workspace_tree = index_tree = None
        reasons.append(f"baseline inspection failed: {type(exc).__name__}: {exc}")
    result = dict(baseline)
    result.update({
        "state": BASELINE_AUTHORIZED if not reasons else BASELINE_DIVERGED,
        "manifest_path": str(path), "proof_reasons": list(dict.fromkeys(reasons)),
        "current_snapshot": current, "current_workspace_tree": workspace_tree,
        "current_index_tree": index_tree,
    })
    return result


def freeze_manifest(*, run_id: str, worktree: Path, baseline_head: str | None,
                    baseline_status: str | None, runner_status: str | None,
                    authorized_baseline: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Freeze the first post-run snapshot used to prove later cleanup safety."""
    try:
        snapshot = status_snapshot(worktree)
        inspection_error = None
    except RecoveryError as exc:
        snapshot = {"worktree": str(Path(worktree).resolve()), "head": None, "entries": []}
        inspection_error = f"{exc.code}: {exc}"
    unsupported = [row["path"] for row in snapshot["entries"]
                   if "U" in row["status"] or row.get("original_path") or row["kind"] == "directory"]
    proof_reasons: list[str] = []
    adopted = dict(authorized_baseline or {})
    if baseline_status not in (None, "") and not adopted:
        proof_reasons.append("the run did not record a clean worktree baseline")
    if not baseline_head:
        proof_reasons.append("the run recorded no baseline HEAD")
    if inspection_error:
        proof_reasons.append(inspection_error)
    if baseline_head and snapshot.get("head") != baseline_head:
        proof_reasons.append("worktree HEAD changed since the run baseline")
    if unsupported:
        proof_reasons.append("rename, conflict, or directory-only status cannot be cleaned safely")
    if adopted:
        if adopted.get("state") != BASELINE_AUTHORIZED:
            proof_reasons.append("the adopted pre-run baseline was not authorized")
        if adopted.get("head") != baseline_head:
            proof_reasons.append("the adopted baseline HEAD does not match the runner baseline")
    clean_tree = None
    if baseline_head and not adopted:
        try:
            clean_tree = _git_text(Path(worktree), "rev-parse", f"{baseline_head}^{{tree}}")
        except RecoveryError as exc:
            proof_reasons.append(f"cannot resolve baseline tree: {exc}")
    ownership = (NO_CHANGES if not snapshot["entries"] and not proof_reasons else
                 OWNED if not proof_reasons else AMBIGUOUS)
    return {
        "schema_version": SCHEMA_VERSION,
        "originating_run_id": str(run_id),
        "runner_status": runner_status,
        "worktree": snapshot["worktree"],
        "baseline_head": baseline_head,
        "baseline_status": baseline_status,
        "authorized_baseline": adopted or None,
        "baseline_snapshot": adopted.get("snapshot") if adopted else {
            "worktree": snapshot["worktree"], "head": baseline_head, "entries": []},
        "baseline_workspace_tree": adopted.get("workspace_tree") if adopted else clean_tree,
        "baseline_index_tree": adopted.get("index_tree") if adopted else clean_tree,
        "baseline_id": adopted.get("baseline_id") if adopted else None,
        "settled_snapshot": snapshot,
        "ownership": ownership,
        "mixed_change_possible": ownership == AMBIGUOUS,
        "proof_reasons": proof_reasons,
        "resolution": UNRESOLVED,
    }


def inspect_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Re-check a frozen manifest against the worktree without changing it."""
    frame = json.loads(json.dumps(dict(manifest)))
    reasons = list(frame.get("proof_reasons") or [])
    try:
        current = status_snapshot(Path(str(frame["worktree"])))
    except (RecoveryError, OSError) as exc:
        current = {"worktree": frame.get("worktree"), "head": None, "entries": []}
        reasons.append(f"current inspection failed: {type(exc).__name__}: {exc}")
    frozen = frame.get("settled_snapshot") or {}
    if current.get("head") != frame.get("baseline_head"):
        reasons.append("worktree HEAD no longer matches the run baseline")
    if current.get("entries") != frozen.get("entries"):
        reasons.append("worktree changes no longer match the run-owned settled snapshot")
    reasons = list(dict.fromkeys(reasons))
    resolution = str(frame.get("resolution") or UNRESOLVED)
    ownership = str(frame.get("ownership") or AMBIGUOUS)
    exact = not reasons and ownership in (OWNED, NO_CHANGES)
    baseline_snapshot = frame.get("baseline_snapshot") or {
        "worktree": frame.get("worktree"), "head": frame.get("baseline_head"), "entries": []}
    current_by = {row["path"]: row for row in (current.get("entries") or [])}
    baseline_by = {row["path"]: row for row in (baseline_snapshot.get("entries") or [])}
    changed_paths = sorted(path for path in set(current_by) | set(baseline_by)
                           if current_by.get(path) != baseline_by.get(path))
    entries: list[dict[str, Any]] = []
    root_path = Path(str(frame.get("worktree"))) if frame.get("worktree") else None
    for path_text in changed_paths:
        row = dict(current_by.get(path_text) or baseline_by[path_text])
        if path_text not in current_by and root_path is not None:
            row.update({"status": " D", "class": "modified", "original_path": None})
            row.update(_fingerprint(root_path / Path(path_text)))
        entries.append(row)
    previews: list[dict[str, Any]] = []
    remaining = MAX_PREVIEW_BYTES
    root = root_path
    for row in entries:
        preview = ""
        truncated = False
        if remaining > 0 and root is not None:
            if row["class"] == "modified":
                raw = _git_bytes(root, "diff", "--no-ext-diff", "--binary",
                                 str(frame.get("baseline_workspace_tree") or
                                     frame.get("baseline_head") or "HEAD"), "--", row["path"])
            else:
                target = root / Path(row["path"])
                try:
                    raw = target.read_bytes() if target.is_file() else b""
                except OSError as exc:
                    raw = f"[unreadable: {exc}]".encode("utf-8", "replace")
                if b"\0" in raw[:8192]:
                    raw = f"[binary untracked file: {row['path']}, {len(raw)} bytes]".encode()
                else:
                    raw = (f"--- /dev/null\n+++ b/{row['path']}\n".encode("utf-8") + raw)
            truncated = len(raw) > remaining
            preview = raw[:remaining].decode("utf-8", "replace")
            remaining -= min(len(raw), remaining)
        previews.append({**row, "diff": preview, "preview_truncated": truncated})
    frame.update({
        "current_snapshot": current,
        "modified_files": [row["path"] for row in entries if row["class"] == "modified"],
        "untracked_files": [row["path"] for row in entries if row["class"] == "untracked"],
        "changed_files": [row["path"] for row in entries],
        "changes": previews,
        "proof_reasons": reasons,
        "mixed_change_possible": not exact,
        "cleanup_available": bool(exact and entries and resolution == UNRESOLVED),
    })
    if not exact:
        frame["ownership"] = AMBIGUOUS
    return frame


def discard_owned_changes(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Restore only exact frozen paths, or refuse before touching the tree."""
    checked = inspect_manifest(manifest)
    if not checked["cleanup_available"]:
        detail = "; ".join(checked.get("proof_reasons") or []) or "run-owned cleanup is not available"
        raise RecoveryError("AMBIGUOUS_OWNERSHIP", detail)
    worktree = Path(str(checked["worktree"])).resolve()
    baseline = str(checked["baseline_head"])
    workspace_tree = str(checked.get("baseline_workspace_tree") or baseline)
    index_tree = str(checked.get("baseline_index_tree") or baseline)
    entries = list(checked.get("changes") or [])
    changed = [row["path"] for row in entries]
    restore_paths: list[str] = []
    remove_paths: list[str] = []
    for relative in changed:
        completed = subprocess.run(
            ["git", "-C", str(worktree), "cat-file", "-e", f"{workspace_tree}:{relative}"],
            shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=False,
        )
        (restore_paths if completed.returncode == 0 else remove_paths).append(relative)

    # Preflight every deletion target before the first mutation.
    untracked_paths: list[Path] = []
    for relative in remove_paths:
        target = (worktree / Path(relative)).resolve(strict=False)
        try:
            target.relative_to(worktree)
        except ValueError as exc:
            raise RecoveryError("PATH_OUTSIDE_WORKTREE", f"refusing cleanup of {relative}") from exc
        if target.is_dir() and not target.is_symlink():
            raise RecoveryError("AMBIGUOUS_OWNERSHIP", f"refusing recursive cleanup of directory {relative}")
        if target.exists() or target.is_symlink():
            untracked_paths.append(target)

    if restore_paths:
        _git_bytes(worktree, "restore", f"--source={workspace_tree}", "--worktree", "--",
                   *restore_paths)
    for target in untracked_paths:
        target.unlink()
        parent = target.parent
        while parent != worktree:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    # Restore the exact approved index only after worktree bytes are in place.
    _git_bytes(worktree, "read-tree", index_tree)

    after = status_snapshot(worktree)
    expected = checked.get("baseline_snapshot") or {
        "worktree": str(worktree), "head": baseline, "entries": []}
    if (after != expected or _tree_from_workspace(worktree) != workspace_tree or
            _git_text(worktree, "write-tree") != index_tree):
        raise RecoveryError("CLEANUP_INCOMPLETE", "explicit cleanup did not restore the proven run baseline")
    result = dict(checked)
    result.update({"resolution": DISCARDED, "cleanup_available": False,
                   "owned_snapshot": result.get("settled_snapshot"),
                   "settled_snapshot": after,
                   "discarded_modified_files": [row["path"] for row in entries
                                                if row["class"] == "modified"],
                   "discarded_untracked_files": [row["path"] for row in entries
                                                 if row["class"] == "untracked"],
                   "current_snapshot": after, "changed_files": [],
                   "modified_files": [], "untracked_files": [],
                   "mixed_change_possible": False})
    return result
