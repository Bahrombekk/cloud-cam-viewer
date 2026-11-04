"""Package layering is enforced here, not just described in a comment.

    settings, i18n            leaf modules, importable from anywhere
    core/                     may import: settings, i18n
    codec/                    + core            (no cloud, no sockets, no HTTP)
    media/                    + codec
    sources/                  + media
    api, cli, viewer, stream_manager, __init__, __main__   anything

The rule that matters most is codec/: once it can reach the cloud, the
decryption logic stops being testable without an account, and most of the
test suite rests on that.
"""
from __future__ import annotations

import ast
from pathlib import Path

import cloudcam

PKG = Path(cloudcam.__file__).resolve().parent

LEAF = {"settings", "i18n"}
ORDER = ["core", "codec", "media", "sources"]
# Third-party / stdlib modules a pure layer must never touch.
FORBIDDEN_IN_CODEC = {"socket", "http", "requests", "urllib", "subprocess"}


def _module(path: Path) -> str:
    rel = path.relative_to(PKG).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join(parts)


def _layer(mod: str) -> str:
    head = mod.split(".")[0]
    if head in LEAF:
        return "leaf"
    if head in ORDER:
        return head
    return "app"


def _imports(path: Path):
    """Every import in the file, including ones inside functions."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield node.lineno, a.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            yield node.lineno, node.module
            if node.module == "cloudcam":
                for a in node.names:
                    yield node.lineno, f"cloudcam.{a.name}"


def _allowed(src_layer: str, dst_layer: str) -> bool:
    if dst_layer == "leaf" or src_layer == "app":
        return True
    if src_layer == "leaf":
        return dst_layer == "leaf"
    if dst_layer == "app":
        return False
    return ORDER.index(dst_layer) <= ORDER.index(src_layer)


def test_no_relative_imports():
    """Absolute `cloudcam.*` imports only — they survive moves and grep well."""
    bad = []
    for p in PKG.rglob("*.py"):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level:
                bad.append(f"{p.relative_to(PKG)}:{node.lineno}")
    assert not bad, f"relative imports: {bad}"


def test_layers_only_import_downwards():
    violations = []
    for p in PKG.rglob("*.py"):
        src = _module(p)
        src_layer = _layer(src)
        for line, name in _imports(p):
            if not name.startswith("cloudcam.") or name == "cloudcam":
                continue
            dst = name[len("cloudcam."):]
            if not dst:
                continue
            if not _allowed(src_layer, _layer(dst)):
                violations.append(f"{src} ({src_layer}) -> {dst} ({_layer(dst)}) :{line}")
    assert not violations, "layer violations:\n" + "\n".join(violations)


def test_codec_stays_pure():
    """codec/ decodes bytes. Network access belongs to sources/."""
    bad = []
    for p in (PKG / "codec").rglob("*.py"):
        for line, name in _imports(p):
            if name.split(".")[0] in FORBIDDEN_IN_CODEC:
                bad.append(f"{p.name}:{line} imports {name}")
    assert not bad, bad


def test_every_layer_exists():
    """If a layer is renamed, this file must be updated with it."""
    for layer in ORDER:
        assert (PKG / layer / "__init__.py").is_file(), layer
