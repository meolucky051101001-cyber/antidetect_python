"""Profile RAM scans must not retain handles for unrelated Windows processes."""
from types import SimpleNamespace

import pytest

from web import services


class ProcessError(Exception):
    pass


class Process:
    def __init__(self, pid, name, arguments=(), rss=1, children=()):
        self.pid = pid
        self.info = {"pid": pid, "name": name}
        self.arguments, self.rss, self.descendants = arguments, rss, children
        self.reads = []

    def cmdline(self):
        self.reads.append("cmdline")
        return self.arguments

    def children(self, recursive):
        assert recursive
        return self.descendants

    def memory_info(self):
        self.reads.append("rss")
        return SimpleNamespace(rss=self.rss * 1048576)


@pytest.fixture
def scan(monkeypatch):
    monkeypatch.setattr(services, "_ram_cache", {"at": 0, "key": None, "data": {}})
    def install(processes, failure=False):
        events = []
        def iterate(attributes):
            assert attributes == ["pid", "name"]
            events.append("scan")
            yield from processes
            if failure:
                raise ProcessError("incomplete inventory")
        iterate.cache_clear = lambda: events.append("clear")
        monkeypatch.setattr(services, "psutil", SimpleNamespace(process_iter=iterate, Error=ProcessError))
        return events
    return install


def test_ram_scan_reads_only_matching_browser_tree_and_releases_cache(tmp_path, scan):
    directory = str(tmp_path / "profile")
    child = Process(3, "chrome.exe", rss=7)
    owned = Process(2, "chrome.exe", ["chrome", "--user-data-dir", directory], rss=5, children=[child])
    prefix_collision = Process(4, "chrome.exe", ["chrome", "--user-data-dir=" + directory + "-other"], rss=99)
    unrelated = Process(1, "unrelated.exe", rss=99)
    events = scan([unrelated, owned, child, prefix_collision])
    assert services.ram_by_profile_dir({"fixture": directory}) == {"fixture": 12.0}
    assert unrelated.reads == []
    assert "rss" not in prefix_collision.reads
    assert child.reads.count("rss") == 1
    assert events == ["scan", "clear"]
    result = services.ram_by_profile_dir({"fixture": directory})
    result.clear()
    assert services.ram_by_profile_dir({"fixture": directory}) == {"fixture": 12.0}
    assert events == ["scan", "clear"]


def test_ram_cache_is_invalidated_when_same_profile_changes_directory(tmp_path, scan):
    first, second = str(tmp_path / "first"), str(tmp_path / "second")
    process = Process(2, "msedge.exe", ["edge", "--user-data-dir=" + first], rss=3)
    events = scan([process])
    assert services.ram_by_profile_dir({"fixture": first}) == {"fixture": 3.0}
    assert services.ram_by_profile_dir({"fixture": second}) == {}
    assert events == ["scan", "clear", "scan", "clear"]


def test_ram_scan_releases_global_handles_even_if_inventory_fails(tmp_path, scan):
    events = scan([], failure=True)
    with pytest.raises(ProcessError):
        services.ram_by_profile_dir({"fixture": str(tmp_path)})
    assert events == ["scan", "clear"]


def test_empty_ram_request_does_not_scan_machine(scan):
    events = scan([])
    assert services.ram_by_profile_dir({}) == {}
    assert services.ram_by_profile_dir({"fixture": ""}) == {}
    assert events == []
