"""Generate/check reviewed wheel locks for CPython 3.10 on Windows x64.

Resolution is a separate explicit `pip download --only-binary=:all:` operation.
This tool never downloads or imports a wheel. Run it in the locked dev environment.
"""
import argparse
from email.parser import BytesParser
import hashlib
from pathlib import Path
import re
import zipfile

from packaging.requirements import Requirement
from packaging.tags import compatible_tags, cpython_tags
from packaging.utils import canonicalize_name, parse_wheel_filename

ROOT = Path(__file__).resolve().parent.parent
SCOPES = {
    "runtime": ("requirements.txt", "requirements-win-py310.lock.txt"),
    "dev": ("requirements-dev.txt", "requirements-dev-win-py310.lock.txt"),
    "audit": ("requirements-audit.txt", "requirements-audit-win-py310.lock.txt"),
}
LOCK_LINE = re.compile(r"([a-z0-9-]+(?:\[[a-z0-9,-]+\])?)==([^ ]+) --hash=sha256:([0-9a-f]{64})")
TARGET = {"python_version": "3.10", "python_full_version": "3.10.11", "sys_platform": "win32",
          "platform_system": "Windows", "platform_machine": "AMD64", "implementation_name": "cpython"}


def requirements(path):
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-r "):
            yield from requirements(path.parent / line[3:].strip())
        else:
            requirement = Requirement(line)
            if not requirement.marker or requirement.marker.evaluate(TARGET):
                yield requirement


def check_inputs(scope, packages):
    for requirement in requirements(ROOT / SCOPES[scope][0]):
        key = canonicalize_name(requirement.name)
        if key not in packages or packages[key][0] not in requirement.specifier:
            raise ValueError(f"{scope}: lock does not satisfy input {requirement}")
    if scope != "audit":
        for constraint in requirements(ROOT / "constraints.txt"):
            key = canonicalize_name(constraint.name)
            if key in packages and packages[key][0] not in constraint.specifier:
                raise ValueError(f"{scope}: lock violates constraint {constraint}")


def read_lock(scope):
    result = {}
    for raw in (ROOT / SCOPES[scope][1]).read_text(encoding="utf-8").splitlines():
        if not raw or raw.startswith("#"):
            continue
        match = LOCK_LINE.fullmatch(raw)
        if not match:
            raise ValueError(f"{scope}: malformed lock entry")
        name = canonicalize_name(Requirement(match[1]).name)
        if name in result:
            raise ValueError(f"{scope}: duplicated lock package")
        result[name] = (match[2], match[3])
    if not result:
        raise ValueError(f"{scope}: empty lock")
    check_inputs(scope, result)
    return result



def active_extras(scope, packages, dependencies):
    """Keep selected extras explicit, including transitive marker activation.

    Older pip resolves an unpinned package[extra] separately from a base-name
    hash pin; a newer index candidate can then make a previously valid lock fail.
    Only extras requested by an active edge are enabled, never all wheel extras.
    """
    selected = {name: set() for name in packages}
    for requirement in requirements(ROOT / SCOPES[scope][0]):
        selected[canonicalize_name(requirement.name)].update(
            canonicalize_name(extra) for extra in requirement.extras
        )
    changed = True
    while changed:
        changed = False
        for parent, entries in dependencies.items():
            contexts = {""} | selected[parent]
            for requirement in entries:
                if requirement.marker and not any(
                    requirement.marker.evaluate({**TARGET, "extra": extra})
                    for extra in contexts
                ):
                    continue
                name = canonicalize_name(requirement.name)
                if requirement.url or name not in packages or packages[name][0] not in requirement.specifier:
                    raise ValueError(f"{scope}: missing/incompatible wheel for {requirement}")
                extras = {canonicalize_name(extra) for extra in requirement.extras}
                if not extras <= selected[name]:
                    selected[name].update(extras)
                    changed = True
    return selected


def generate(scope, wheel_dir):
    accepted = set(cpython_tags((3, 10), ["cp310"], ["win_amd64"]))
    accepted.update(compatible_tags((3, 10), "cp310", ["win_amd64"]))
    result = {}
    dependencies = {}
    for wheel in sorted(wheel_dir.glob("*.whl")):
        name, version, _, tags = parse_wheel_filename(wheel.name)
        if not tags & accepted:
            raise ValueError(f"Incompatible target wheel: {wheel.name}")
        with zipfile.ZipFile(wheel) as archive:
            entries = [entry for entry in archive.namelist() if entry.endswith(".dist-info/METADATA")]
            if len(entries) != 1:
                raise ValueError(f"Ambiguous wheel metadata: {wheel.name}")
            metadata = BytesParser().parsebytes(archive.read(entries[0]))
        key = canonicalize_name(metadata["Name"])
        if key != canonicalize_name(name) or metadata["Version"] != str(version) or key in result:
            raise ValueError(f"Mismatched/duplicated package: {wheel.name}")
        digest = hashlib.sha256()
        with wheel.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        result[key] = (str(version), digest.hexdigest())
        dependencies[key] = [Requirement(raw) for raw in metadata.get_all("Requires-Dist", [])]
    if not result:
        raise ValueError("No wheels found; use a fresh wheel directory for each scope")
    check_inputs(scope, result)
    selected_extras = active_extras(scope, result, dependencies)
    source = SCOPES[scope][0] + (" + constraints.txt" if scope != "audit" else " (isolated tool environment)")
    lines = [
        "# Fully resolved wheel lock for CPython 3.10 / Windows x64 only.",
        "# Official PyPI wheels; no source distributions. Review every refresh diff.",
        "# Inputs: " + source,
        "# Install with --require-hashes --only-binary=:all:.",
        "# Refresh instructions: docs/DEPENDENCY_LOCKS.md", "",
    ]
    for name, (version, digest) in sorted(result.items()):
        extras = selected_extras[name]
        requirement_name = name + ("[" + ",".join(sorted(extras)) + "]" if extras else "")
        lines.append(f"{requirement_name}=={version} --hash=sha256:{digest}")
    (ROOT / SCOPES[scope][1]).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{scope}: wrote {len(result)} hashed packages")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("check", "generate"))
    parser.add_argument("--scope", choices=tuple(SCOPES))
    parser.add_argument("--wheel-dir", type=Path)
    args = parser.parse_args()
    if args.command == "generate":
        if not args.scope or not args.wheel_dir:
            parser.error("generate requires --scope and --wheel-dir")
        generate(args.scope, args.wheel_dir)
    else:
        locks = {scope: read_lock(scope) for scope in SCOPES}
        for name, entry in locks["runtime"].items():
            if locks["dev"].get(name) != entry:
                raise ValueError(f"Runtime/dev version or wheel hash differs: {name}")
        print("Locks satisfy source inputs/constraints; runtime/dev overlap is identical")


if __name__ == "__main__":
    main()
