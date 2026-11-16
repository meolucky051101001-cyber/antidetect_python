"""Lock metadata must preserve active extras without changing package pins."""
import pytest
from packaging.requirements import Requirement

from tools import lock_dependencies as locks


def test_transitive_extra_activation_and_missing_dependency(monkeypatch):
    monkeypatch.setattr(locks, "requirements", lambda _: iter([Requirement("audit==1")]))
    packages = {name: ("1", "hash") for name in ("audit", "cache", "filelock")}
    edges = {
        "audit": [Requirement("cache[filecache]>=1"), Requirement('unused; extra == "dev"')],
        "cache": [Requirement('filelock>=1; extra == "filecache"')],
        "filelock": [],
    }
    selected = locks.active_extras("audit", packages, edges)
    assert selected == {"audit": set(), "cache": {"filecache"}, "filelock": set()}
    with pytest.raises(ValueError, match="filelock"):
        locks.active_extras("audit", {k: v for k, v in packages.items() if k != "filelock"}, edges)


def test_lock_reader_accepts_explicit_extra_and_rejects_duplicate_base(tmp_path, monkeypatch):
    monkeypatch.setattr(locks, "ROOT", tmp_path)
    monkeypatch.setattr(locks, "requirements", lambda _: iter([Requirement("cache==1")]))
    target = tmp_path / locks.SCOPES["audit"][1]
    digest = "a" * 64
    entry = f"cache[filecache]==1 --hash=sha256:{digest}\n"
    target.write_text(entry, encoding="utf-8")
    assert locks.read_lock("audit") == {"cache": ("1", digest)}
    target.write_text(entry + f"cache==1 --hash=sha256:{digest}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="duplicated"):
        locks.read_lock("audit")
