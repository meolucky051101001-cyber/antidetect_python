"""Durable quarantine filesystem transaction for profile deletion.

The caller owns the profile lifecycle lock and the metadata transaction lock.
Capsules contain one profile's metadata and secret entry encrypted with DPAPI;
JSON journals contain identifiers, phases and a ciphertext digest only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from typing import Any, Dict, Optional

from . import secrets as secret_store
from .models import ProxyConfig, REDACTED_SECRET
from .profile_copy import _require_no_reparse, _remove_tree_checked, validate_profile_id

PHASES = {"prepared", "quarantined", "metadata_removed", "secret_removed", "committed", "rolled_back"}
_OPERATION = re.compile(r"^[0-9a-f]{32}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
MAX_JOURNAL_BYTES = 8192
MAX_CAPSULE_BYTES = 16 * 1024 * 1024


class DeletionTransactionError(RuntimeError):
    pass


def _checked(path: str) -> str:
    path = os.path.abspath(path)
    _require_no_reparse(path)
    return path


def _sync_directory(path: str) -> None:
    # Windows directory handles cannot be fsynced through os.open. Each file
    # is flushed before atomic replace; actual power-cut durability is separate.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _atomic_bytes(path: str, content: bytes) -> None:
    _checked(path)
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    _checked(parent)
    pending = path + ".tmp." + uuid.uuid4().hex
    try:
        with open(pending, "xb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        _checked(pending)
        _checked(path)
        os.replace(pending, path)
        _sync_directory(parent)
    finally:
        try:
            _checked(pending)
            os.remove(pending)
        except FileNotFoundError:
            pass


def journal_path(data_dir: str, profile_id: str) -> str:
    validate_profile_id(profile_id)
    return _checked(os.path.join(data_dir, "delete_operations", profile_id + ".json"))


def _paths(data_dir: str, profiles_dir: str, journal: Dict[str, Any]):
    return (
        _checked(os.path.join(profiles_dir, journal["profile_id"])),
        _checked(os.path.join(profiles_dir, ".quarantine", journal["operation_id"])),
        _checked(os.path.join(data_dir, "delete_operations", journal["operation_id"] + ".dpapi")),
    )


def preflight_tree(path: str) -> bool:
    _checked(path)
    if not os.path.lexists(path):
        return False
    if not os.path.isdir(path):
        raise DeletionTransactionError("Profile deletion source is not a directory")
    for directory, dirs, files in os.walk(path, followlinks=False):
        _checked(directory)
        for name in dirs + files:
            _checked(os.path.join(directory, name))
    return True


def read_journal(data_dir: str, profiles_dir: str, profile_id: str) -> Optional[Dict[str, Any]]:
    path = journal_path(data_dir, profile_id)
    try:
        with open(path, "rb") as source:
            raw = source.read(MAX_JOURNAL_BYTES + 1)
    except FileNotFoundError:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise DeletionTransactionError("Deletion journal is unreadable; data retained") from exc
    if len(raw) > MAX_JOURNAL_BYTES or not isinstance(value, dict):
        raise DeletionTransactionError("Deletion journal has an invalid structure")
    if (type(value.get("schema")) is not int or value["schema"] != 1
            or value.get("profile_id") != profile_id
            or not isinstance(value.get("operation_id"), str)
            or not _OPERATION.fullmatch(value["operation_id"])
            or value.get("phase") not in tuple(PHASES)
            or type(value.get("had_directory")) is not bool
            or not isinstance(value.get("capsule_sha256"), str)
            or not _DIGEST.fullmatch(value["capsule_sha256"])):
        raise DeletionTransactionError("Deletion journal validation failed")
    for key, expected in (("data_root", data_dir), ("profiles_root", profiles_dir)):
        actual = value.get(key)
        if (not isinstance(actual, str) or not os.path.isabs(actual)
                or os.path.normcase(os.path.abspath(actual)) != os.path.normcase(os.path.abspath(expected))):
            raise DeletionTransactionError("Deletion journal belongs to another data root")
    _paths(data_dir, profiles_dir, value)
    return value


def write_phase(data_dir: str, journal: Dict[str, Any], phase: str) -> Dict[str, Any]:
    if phase not in PHASES:
        raise DeletionTransactionError("Unsupported deletion phase")
    updated = dict(journal, phase=phase, updated_at=int(time.time()))
    _atomic_bytes(journal_path(data_dir, journal["profile_id"]),
                  json.dumps(updated, sort_keys=True).encode("utf-8"))
    return updated


def _secret_entry(profile_id: str):
    target = str(secret_store._target_file())
    _checked(target)
    with secret_store._lock, secret_store._StoreLock():
        store = secret_store._read_all_secrets()
        return str(profile_id) in store, store.get(str(profile_id))


def prepare(data_dir: str, profiles_dir: str, profile_id: str, profile_snapshot: Dict[str, Any]):
    if read_journal(data_dir, profiles_dir, profile_id) is not None:
        raise DeletionTransactionError("An unfinished deletion already exists")
    source_path = _checked(os.path.join(profiles_dir, profile_id))
    had_directory = os.path.lexists(source_path)
    if had_directory and not os.path.isdir(source_path):
        raise DeletionTransactionError("Profile deletion source is not a directory")
    operation_id = uuid.uuid4().hex
    journal = {
        "schema": 1, "profile_id": profile_id, "operation_id": operation_id,
        "data_root": os.path.abspath(data_dir), "profiles_root": os.path.abspath(profiles_dir),
        "had_directory": had_directory, "phase": "prepared",
    }
    _source, quarantine, capsule_path = _paths(data_dir, profiles_dir, journal)
    _checked(os.path.dirname(quarantine))
    os.makedirs(os.path.dirname(quarantine), exist_ok=True)
    _checked(quarantine)
    if os.path.lexists(quarantine):
        raise DeletionTransactionError("Quarantine target already exists")
    present, entry = _secret_entry(profile_id)
    capsule = {
        "schema": 1, "operation_id": operation_id, "profile_id": profile_id,
        "profile": profile_snapshot, "entry_present": present, "entry": entry,
    }
    encrypted = secret_store._crypt(json.dumps(capsule, ensure_ascii=False).encode("utf-8"), True)
    if len(encrypted) > MAX_CAPSULE_BYTES:
        raise DeletionTransactionError("Deletion capsule exceeds configured size")
    _atomic_bytes(capsule_path, encrypted)
    journal["capsule_sha256"] = hashlib.sha256(encrypted).hexdigest()
    try:
        return write_phase(data_dir, journal, "prepared")
    except Exception:
        # No mutation is allowed before this journal exists. If the atomic
        # journal write did not publish, remove the unneeded encrypted capsule.
        if not os.path.lexists(journal_path(data_dir, profile_id)):
            _checked(capsule_path)
            os.remove(capsule_path)
        raise


def read_capsule(data_dir: str, profiles_dir: str, journal: Dict[str, Any]) -> Dict[str, Any]:
    _source, _quarantine, capsule_path = _paths(data_dir, profiles_dir, journal)
    with open(capsule_path, "rb") as source:
        raw = source.read(MAX_CAPSULE_BYTES + 1)
    if len(raw) > MAX_CAPSULE_BYTES or hashlib.sha256(raw).hexdigest() != journal["capsule_sha256"]:
        raise DeletionTransactionError("Deletion recovery capsule hash mismatch")
    try:
        capsule = json.loads(secret_store._crypt(raw, False))
    except Exception as exc:
        raise DeletionTransactionError("Cannot decrypt deletion recovery capsule") from exc
    if (not isinstance(capsule, dict) or capsule.get("schema") != 1
            or capsule.get("operation_id") != journal["operation_id"]
            or capsule.get("profile_id") != journal["profile_id"]
            or not isinstance(capsule.get("profile"), dict)
            or capsule["profile"].get("id") != journal["profile_id"]
            or type(capsule.get("entry_present")) is not bool
            or (capsule.get("entry_present") and not isinstance(capsule.get("entry"), (str, dict)))):
        raise DeletionTransactionError("Deletion recovery capsule validation failed")
    return capsule


def quarantine(data_dir: str, profiles_dir: str, journal: Dict[str, Any]) -> None:
    source, target, _capsule = _paths(data_dir, profiles_dir, journal)
    if journal["had_directory"]:
        # Both paths have the same profiles root. Refuse EXDEV; never copy or
        # recursively delete as a fallback for a failed same-volume rename.
        if not os.path.isdir(source) or os.path.lexists(target):
            raise DeletionTransactionError("Profile/quarantine state changed before rename")
        os.replace(source, target)
        _sync_directory(os.path.dirname(source))
        _sync_directory(os.path.dirname(target))


def restore_directory(data_dir: str, profiles_dir: str, journal: Dict[str, Any]) -> None:
    source, quarantine_path, _capsule = _paths(data_dir, profiles_dir, journal)
    source_exists, quarantined = os.path.lexists(source), os.path.lexists(quarantine_path)
    if source_exists and quarantined:
        raise DeletionTransactionError("A replacement source conflicts with quarantine; both retained")
    if quarantined:
        preflight_tree(quarantine_path)
        os.replace(quarantine_path, source)
        _sync_directory(os.path.dirname(source))
    elif journal["had_directory"] and not source_exists:
        raise DeletionTransactionError("Both profile and quarantine are missing; recovery stopped")
    elif source_exists:
        preflight_tree(source)


def _recovery_entry(capsule: Dict[str, Any]):
    """Return the deterministic post-rollback secret, including legacy input.

    A metadata snapshot can contain the only copy of a legacy plaintext
    password. Publish that password once into DPAPI before saving metadata with
    password=None. A retry must recognize exactly this operation's own migration
    as well as its original entry; arbitrary newer entries still conflict.
    """
    original = capsule["entry"] if capsule["entry_present"] else None
    proxy = ProxyConfig.model_validate(capsule["profile"].get("proxy") or {})
    password = proxy.password
    if password and password != REDACTED_SECRET and str(password).strip():
        if isinstance(original, dict):
            entry = dict(original)
        elif isinstance(original, str):
            entry = {"proxy_password": original}
        else:
            entry = {}
        entry.update(proxy_password=password, proxy_identity=proxy.identity_key())
        return True, entry
    return capsule["entry_present"], original


def restore_entry(profile_id: str, capsule: Dict[str, Any]) -> None:
    _checked(str(secret_store._target_file()))
    with secret_store._lock, secret_store._StoreLock():
        store = secret_store._read_all_secrets()
        present = profile_id in store
        wanted_present, wanted_entry = _recovery_entry(capsule)
        matches_original = capsule["entry_present"] and store.get(profile_id) == capsule["entry"]
        matches_recovery = wanted_present and store.get(profile_id) == wanted_entry
        if present and not (matches_original or matches_recovery):
            raise DeletionTransactionError("A newer secret entry conflicts with deletion recovery")
        if wanted_present and (not present or store[profile_id] != wanted_entry):
            store[profile_id] = wanted_entry
            secret_store._write_all_secrets(store)


def remove_entry(profile_id: str, capsule: Dict[str, Any]) -> None:
    _checked(str(secret_store._target_file()))
    with secret_store._lock, secret_store._StoreLock():
        store = secret_store._read_all_secrets()
        if profile_id in store:
            if not capsule["entry_present"] or store[profile_id] != capsule["entry"]:
                raise DeletionTransactionError("Secret changed during deletion; entry retained")
            store.pop(profile_id)
            secret_store._write_all_secrets(store)


def require_entry_absent(profile_id: str) -> None:
    present, _entry = _secret_entry(profile_id)
    if present:
        raise DeletionTransactionError("Committed deletion has a replacement secret; purge refused")


def finish(data_dir: str, profiles_dir: str, journal: Dict[str, Any], *, purge: bool) -> None:
    source, quarantine_path, capsule_path = _paths(data_dir, profiles_dir, journal)
    if purge:
        if journal["phase"] != "committed":
            raise DeletionTransactionError("Cannot purge an uncommitted deletion")
        if os.path.lexists(source):
            raise DeletionTransactionError("Committed deletion has a replacement source; purge refused")
        if os.path.lexists(quarantine_path):
            _remove_tree_checked(quarantine_path)
    elif os.path.lexists(quarantine_path):
        raise DeletionTransactionError("Rollback quarantine still exists")
    # Journal remains until purge/rollback completed. A committed journal no
    # longer needs its capsule, so crash between these unlinks is retryable.
    for path in (capsule_path, journal_path(data_dir, journal["profile_id"])):
        _checked(path)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    _sync_directory(os.path.dirname(capsule_path))


def pending_profile_ids(data_dir: str):
    directory = _checked(os.path.join(data_dir, "delete_operations"))
    if not os.path.isdir(directory):
        return []
    return sorted(validate_profile_id(name[:-5]) for name in os.listdir(directory)
                  if name.endswith(".json") and ".tmp." not in name)
