"""Credential and size boundaries for synthetic child-server diagnostics."""
from types import SimpleNamespace

from tools.live_test_diagnostics import sanitized_log_tail, server_failure_diagnostics


def test_log_tail_is_bounded_and_omits_old_content(tmp_path):
    path = tmp_path / "synthetic-server.log"
    path.write_bytes(b"old private fixture content\n" + b"padding line\n" * 10000 + b"final fixture failure\n")

    tail = sanitized_log_tail(path, max_bytes=1024, lines=5)

    assert "final fixture failure" in tail
    assert "old private fixture content" not in tail
    assert len(tail.encode("utf-8")) <= 1024
    assert len(tail.splitlines()) <= 5


def test_log_tail_redacts_credentials_and_control_characters(tmp_path):
    path = tmp_path / "synthetic-server.log"
    path.write_text(
        "Authorization: Bearer synthetic-auth\n"
        "Cookie: fixture_session=synthetic-cookie\n"
        "access_key=synthetic-key\n"
        "unlabelled synthetic-known-value\n"
        "RuntimeError: synthetic failure\x1b\x00\x07\x7f\n",
        encoding="utf-8",
    )

    tail = sanitized_log_tail(path, secrets=("synthetic-known-value",))

    for value in ("synthetic-auth", "synthetic-cookie", "synthetic-key", "synthetic-known-value"):
        assert value not in tail
    assert tail.count("[credential-bearing fixture log line omitted]") == 3
    assert "unlabelled [REDACTED]" in tail
    assert "RuntimeError: synthetic failure" in tail
    assert not any(char in tail for char in ("\x1b", "\x00", "\x07", "\x7f"))


def test_missing_log_is_reported_without_raising(tmp_path):
    assert sanitized_log_tail(tmp_path / "missing.log") == "[fixture log unavailable]"


def test_server_failure_keeps_exit_code_and_sanitized_log(tmp_path):
    path = tmp_path / "synthetic-server.log"
    path.write_text("RuntimeError: fixture startup failed\nCookie: synthetic-cookie\n", encoding="utf-8")
    proc = SimpleNamespace(poll=lambda: 3)

    diagnostic = server_failure_diagnostics(proc, (("child stdout/stderr", path),))

    assert "Fixture server exit_code=3" in diagnostic
    assert "child stdout/stderr (sanitized tail)" in diagnostic
    assert "RuntimeError: fixture startup failed" in diagnostic
    assert "synthetic-cookie" not in diagnostic
