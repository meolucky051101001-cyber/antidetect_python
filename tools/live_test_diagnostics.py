"""Bounded, redacted diagnostics for synthetic live-test child servers only."""
from pathlib import Path
import re


_SENSITIVE_LINE = re.compile(
    r"authorization|proxy-authorization|cookie|access[_ -]?key|session[_ -]?key|"
    r"password|credential|\btoken\b|\bsecret\b|/unlock|[#?&]key=",
    re.IGNORECASE,
)


def sanitized_log_tail(path, *, secrets=(), max_bytes=65536, lines=80):
    """Read only a fixture log tail; omit lines that could contain credentials."""
    try:
        with Path(path).open("rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes))
            content = stream.read(max_bytes).decode("utf-8", errors="replace")
    except OSError:
        return "[fixture log unavailable]"
    safe_lines = []
    for line in content.splitlines()[-lines:]:
        if _SENSITIVE_LINE.search(line):
            safe_lines.append("[credential-bearing fixture log line omitted]")
            continue
        for value in secrets:
            if value:
                line = line.replace(str(value), "[REDACTED]")
        # Avoid terminal control/escape sequences in retained child output.
        safe_lines.append("".join(char for char in line if char == "\t" or ord(char) >= 32 and ord(char) != 127))
    return "\n".join(safe_lines)


def server_failure_diagnostics(proc, paths, *, secrets=()):
    sections = [f"Fixture server exit_code={proc.poll()!r}"]
    for label, path in paths:
        sections.append(f"--- {label} (sanitized tail) ---\n" + sanitized_log_tail(path, secrets=secrets))
    return "\n".join(sections)
