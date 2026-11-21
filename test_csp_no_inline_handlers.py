import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"
INLINE_HANDLER = re.compile(r"""\son[a-z]+\s*=\s*["']""", re.IGNORECASE)
TEMPLATE_ACTION = re.compile(r"""data-on-(click|change|input)="([A-Za-z_]\w*)"(?:\s+data-on-\1-args='([^']*)')?""")
JS_ACTION = re.compile(r"""actionAttrs\('(click|change|input)', '([A-Za-z_]\w*)'""")


def _ui_sources():
    files = sorted((WEB / "templates").glob("*.html"))
    files += sorted(p for p in (WEB / "static" / "js").glob("*.js"))
    return files


def _defined_functions() -> set[str]:
    names = set()
    for name in ("dashboard.js", "fanpages.js"):
        text = (WEB / "static" / "js" / name).read_text(encoding="utf-8")
        names.update(re.findall(r"(?m)^\s*(?:async\s+)?function\s+([A-Za-z_]\w*)\s*\(", text))
    return names | {"navigateTo"}


def test_ui_has_no_inline_event_handlers():
    offenders = []
    for path in _ui_sources():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if INLINE_HANDLER.search(line):
                offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()[:120]}")
    assert not offenders, "inline handlers need script-src-attr 'unsafe-inline':\n" + "\n".join(offenders)


def test_declared_actions_resolve_to_global_functions():
    defined = _defined_functions()
    missing = []
    seen = 0
    for path in _ui_sources():
        if path.name == "actions.js":
            continue
        text = path.read_text(encoding="utf-8")
        for match in TEMPLATE_ACTION.finditer(text):
            seen += 1
            if match.group(2) not in defined:
                missing.append(f"{path.name}: {match.group(2)}")
            if match.group(3):
                assert isinstance(json.loads(match.group(3)), list)
        for match in JS_ACTION.finditer(text):
            seen += 1
            if match.group(2) not in defined:
                missing.append(f"{path.name}: {match.group(2)}")
    assert seen > 150
    assert not missing


def test_pages_load_action_dispatcher_before_page_script():
    for template, page in (("index.html", "dashboard.js"), ("fanpages.html", "fanpages.js")):
        text = (WEB / "templates" / template).read_text(encoding="utf-8")
        assert text.index("/static/js/actions.js") < text.index(f"/static/js/{page}")
