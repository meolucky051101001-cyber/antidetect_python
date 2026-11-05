import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import antidetect.profile_copy as profile_copy
from antidetect.profile_copy import (
    COPY_PHASES,
    CopyJournal,
    CopyJournalError,
    CopyNoSpaceError,
    CopyReparsePointError,
    CopyStats,
    CopyTargetExistsError,
    CopyVerificationError,
    ProfileCopyError,
    check_disk_space,
    cleanup_staging,
    copy_user_data,
    measure_tree,
    prepare_staging,
    promote_staging,
    verify_copy,
)

@pytest.fixture
def source_tree(tmp_path):
    root = tmp_path / "source" / "user_data"
    (root / "Default").mkdir(parents=True)
    (root / "Default" / "Cookies").write_bytes(b"cookie" * 10)
    (root / "Local State").write_bytes(b"{}")
    (root / "DevToolsActivePort").write_text("9222", encoding="utf-8")
    (root / "SingletonLock").write_text("lock", encoding="utf-8")
    (root / "Default" / "lockfile").write_text("lock", encoding="utf-8")
    return root


def _copy_into_staging(tmp_path, source, operation_id="op-1"):
    staging = prepare_staging(str(tmp_path / "profiles"), operation_id)
    copy_user_data(str(source), staging)
    return Path(staging)


def _make_directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            pytest.skip(f"Không tạo được junction: {result.stderr or result.stdout}")
    else:
        os.symlink(target, link, target_is_directory=True)


def _remove_directory_link(link: Path) -> None:
    if os.name == "nt":
        subprocess.run(["cmd", "/c", "rmdir", str(link)], capture_output=True)
    else:
        link.unlink()


def test_measure_tree_ignores_browser_lock_files(source_tree):
    stats = measure_tree(str(source_tree))
    assert stats == CopyStats(total_files=2, total_bytes=62, ignored_entries=3)


def test_copy_user_data_excludes_locks_and_verifies(source_tree, tmp_path):
    staging = _copy_into_staging(tmp_path, source_tree)

    assert (staging / "Default" / "Cookies").read_bytes() == b"cookie" * 10
    assert not (staging / "DevToolsActivePort").exists()
    assert not (staging / "SingletonLock").exists()
    assert not (staging / "Default" / "lockfile").exists()
    assert verify_copy(str(source_tree), str(staging)).total_files == 2


def test_copy_user_data_refuses_existing_destination(source_tree, tmp_path):
    staging = Path(prepare_staging(str(tmp_path / "profiles"), "op-1"))
    staging.mkdir(parents=True)
    marker = staging / "keep.txt"
    marker.write_text("keep", encoding="utf-8")

    with pytest.raises(CopyTargetExistsError):
        copy_user_data(str(source_tree), str(staging))

    assert marker.read_text(encoding="utf-8") == "keep"


def test_copy_user_data_refuses_junction_outside_source(source_tree, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    link = source_tree / "escape"
    _make_directory_link(link, outside)
    try:
        with pytest.raises(CopyReparsePointError):
            measure_tree(str(source_tree))

        staging = Path(prepare_staging(str(tmp_path / "profiles"), "op-1"))
        with pytest.raises(CopyReparsePointError):
            copy_user_data(str(source_tree), str(staging))
        assert not staging.exists()
    finally:
        if link.exists():
            _remove_directory_link(link)


def test_verify_copy_detects_missing_file(source_tree, tmp_path):
    staging = _copy_into_staging(tmp_path, source_tree)
    (staging / "Default" / "Cookies").unlink()

    with pytest.raises(CopyVerificationError):
        verify_copy(str(source_tree), str(staging))


def test_verify_copy_detects_size_mismatch(source_tree, tmp_path):
    staging = _copy_into_staging(tmp_path, source_tree)
    (staging / "Default" / "Cookies").write_bytes(b"short")

    with pytest.raises(CopyVerificationError):
        verify_copy(str(source_tree), str(staging))


def test_verify_copy_detects_extra_file(source_tree, tmp_path):
    staging = _copy_into_staging(tmp_path, source_tree)
    (staging / "unexpected.txt").write_text("extra", encoding="utf-8")

    with pytest.raises(CopyVerificationError):
        verify_copy(str(source_tree), str(staging))


def test_check_disk_space_rejects_insufficient_free(monkeypatch, tmp_path):
    free = 100 * 1024 * 1024
    monkeypatch.setattr(
        "antidetect.profile_copy.shutil.disk_usage",
        lambda _: SimpleNamespace(free=free),
    )

    with pytest.raises(CopyNoSpaceError) as exc:
        check_disk_space(1024, str(tmp_path))

    assert exc.value.code == "COPY_NO_SPACE"
    assert exc.value.free == free
    assert exc.value.required == 1024 + 102 + 512 * 1024 * 1024


def test_check_disk_space_accepts_enough_free(monkeypatch, tmp_path):
    source_size = 1024
    required = source_size + int(source_size * 0.10) + 512 * 1024 * 1024
    free = required + 1
    monkeypatch.setattr(
        "antidetect.profile_copy.shutil.disk_usage",
        lambda _: SimpleNamespace(free=free),
    )

    assert check_disk_space(source_size, str(tmp_path)) == free
    with pytest.raises(ValueError):
        check_disk_space(-1, str(tmp_path))


def test_prepare_staging_rejects_invalid_operation_id(tmp_path):
    with pytest.raises(ValueError):
        prepare_staging(str(tmp_path / "profiles"), "../escape")
    with pytest.raises(ValueError):
        prepare_staging(str(tmp_path / "profiles"), "op/1")


def test_prepare_staging_refuses_reuse_with_partial_data(tmp_path):
    staging = Path(prepare_staging(str(tmp_path / "profiles"), "op-1"))
    (staging.parent / "partial.tmp").write_text("partial", encoding="utf-8")

    with pytest.raises(CopyTargetExistsError):
        prepare_staging(str(tmp_path / "profiles"), "op-1")


def test_promote_staging_moves_tree(source_tree, tmp_path):
    staging = _copy_into_staging(tmp_path, source_tree)
    target = tmp_path / "profiles" / "target" / "user_data"

    promote_staging(str(staging), str(target))

    assert target.is_dir()
    assert (target / "Default" / "Cookies").exists()
    assert not staging.exists()


def test_promote_staging_refuses_existing_target(source_tree, tmp_path):
    target = tmp_path / "profiles" / "target" / "user_data"
    target.mkdir(parents=True)
    marker = target / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    staging = _copy_into_staging(tmp_path, source_tree)

    with pytest.raises(CopyTargetExistsError):
        promote_staging(str(staging), str(target))

    assert marker.read_text(encoding="utf-8") == "keep"
    assert staging.exists()


def test_promote_staging_requires_staging(tmp_path):
    with pytest.raises(ProfileCopyError):
        promote_staging(
            str(tmp_path / "missing"),
            str(tmp_path / "profiles" / "target" / "user_data"),
        )


def test_cleanup_staging_removes_only_operation_dir(tmp_path):
    profiles = tmp_path / "profiles"
    operation = profiles / ".staging" / "op-1"
    (operation / "user_data").mkdir(parents=True)

    assert cleanup_staging(str(operation), str(profiles)) is True
    assert not operation.exists()
    assert (profiles / ".staging").is_dir()


def test_cleanup_staging_rejects_unsafe_paths(tmp_path):
    profiles = tmp_path / "profiles"
    (profiles / ".staging").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    for unsafe in (profiles, profiles / ".staging", outside):
        with pytest.raises(ProfileCopyError) as exc:
            cleanup_staging(str(unsafe), str(profiles))
        assert exc.value.code == "COPY_CLEANUP_UNSAFE"


def _make_journal(tmp_path, operation_id="op-1"):
    return CopyJournal(
        str(tmp_path / "copy_operations" / f"{operation_id}.json"),
        operation_id=operation_id,
        source_id="source",
        target_id="target",
        source_dir=str(tmp_path / "source"),
        staging_dir=str(tmp_path / "profiles" / ".staging" / operation_id),
        target_dir=str(tmp_path / "profiles" / "target"),
    )


def test_journal_write_update_and_read(tmp_path):
    journal = _make_journal(tmp_path)
    created = journal.write("staging")

    assert created["phase"] == "staging"
    assert created["owner"].startswith(f"{os.getpid()}:")
    assert created["created_at"]
    assert created["updated_at"]
    assert journal.read()["operation_id"] == "op-1"
    assert list((tmp_path / "copy_operations").glob("*.tmp.*")) == []

    updated = journal.update("copied", verification={"files": 2, "bytes": 62})
    assert updated["phase"] == "copied"
    assert updated["verification"] == {"files": 2, "bytes": 62}
    stored = journal.read()
    assert stored["created_at"] == created["created_at"]
    assert stored["phase"] == "copied"


def test_journal_rejects_secret_fields_without_writing(tmp_path):
    journal = _make_journal(tmp_path)
    journal.write("staging")
    before = Path(journal.path).read_text(encoding="utf-8")

    with pytest.raises(ValueError):
        journal.update("copied", proxy_password="must-not-be-stored")
    with pytest.raises(ValueError):
        journal.write("staging", nested={"token": "must-not-be-stored"})

    assert Path(journal.path).read_text(encoding="utf-8") == before


def test_journal_rejects_invalid_phase(tmp_path):
    journal = _make_journal(tmp_path)
    with pytest.raises(ValueError):
        journal.write("published")
    assert "published" not in COPY_PHASES


def test_journal_rejects_operation_id_mismatch(tmp_path):
    journal = _make_journal(tmp_path)
    journal.write("staging")
    other = CopyJournal(
        journal.path,
        operation_id="op-2",
        source_id="source",
        target_id="target",
    )

    with pytest.raises(CopyJournalError):
        other.update("copied")


def test_journal_read_rejects_corrupt_file(tmp_path):
    path = tmp_path / "copy_operations" / "bad.json"
    path.parent.mkdir(parents=True)
    path.write_text("{", encoding="utf-8")
    journal = CopyJournal(
        str(path),
        operation_id="op-1",
        source_id="source",
        target_id="target",
    )

    with pytest.raises(CopyJournalError):
        journal.read()
