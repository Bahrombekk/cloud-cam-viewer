"""Qatlam chegaralari va upstream kontrakti.

Tuzilma qatlamlarga bo'lingan — lekin qatlam faqat IZOHDA bo'lsa, u bir
necha oydan keyin yo'qoladi. Shu yerda u majburiy:

    core/   -> hech kimga (settings'dan tashqari)
    codec/  -> core, settings   (bulut YO'Q, tarmoq YO'Q)
    media/  -> core, settings
    sources/-> codec, media, core, settings
    yuqori  -> hammasi

Eng muhimi `codec/`: u bulutga bog'lanib qolsa, deshifrlash mantig'ini
bulutsiz test qilib bo'lmay qoladi — hozirgi 30+ test aynan shunga tayanadi.
"""
import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "cloudcam"

# Qatlam -> unga RUXSAT etilgan paketlar
ALLOWED = {
    "core":    {"settings"},
    "codec":   {"settings", "core"},
    "media":   {"settings", "core"},
    "sources": {"settings", "core", "codec", "media", "sources"},
}


def _imports(path: pathlib.Path) -> set[str]:
    """Fayl `cloudcam` ning qaysi yuqori paketlariga bog'langan."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    pkg_depth = len(path.relative_to(ROOT).parts) - 1   # fayl turgan chuqurlik
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module and node.module.startswith("cloudcam."):
                    out.add(node.module.split(".")[1])
                continue
            # Nisbiy import: necha daraja yuqoriga chiqilgani
            up = node.level - 1
            if up == pkg_depth:                  # cloudcam ildiziga chiqdi
                out.add((node.module or "").split(".")[0])
            elif up > pkg_depth:
                out.add("<cloudcam'dan tashqari>")
    return {m for m in out if m}


@pytest.mark.parametrize("layer", sorted(ALLOWED))
def test_layer_does_not_reach_upwards(layer):
    bad = []
    for f in sorted((ROOT / layer).rglob("*.py")):
        for mod in _imports(f) - ALLOWED[layer] - {layer}:
            bad.append(f"{f.relative_to(ROOT).as_posix()} -> {mod}")
    assert not bad, f"{layer}/ qatlamidan ruxsatsiz bog'liqlik: {bad}"


def test_codec_never_imports_the_cloud():
    """Alohida ta'kidlanadi: codec bulutsiz test qilinadi."""
    for f in sorted((ROOT / "codec").rglob("*.py")):
        text = f.read_text(encoding="utf-8")
        assert "pyezvizapi.cloud_stream" not in text, f"{f.name} bulutga bog'landi"
        assert "requests" not in text, f"{f.name} tarmoqqa bog'landi"


def test_every_package_has_an_init():
    for d in sorted(p for p in ROOT.rglob("*") if p.is_dir() and p.name != "__pycache__"):
        assert (d / "__init__.py").exists(), f"{d.name}/ paket emas"


# ── upstream kontrakti ([[sources.cloud.compat]]) ────────────────────

def test_required_upstream_names_exist():
    """`pyezvizapi` ning ICHKI nomlariga tayanamiz — ular bormi."""
    from cloudcam.sources.cloud.compat import check_upstream
    check_upstream()          # xato ko'tarmasa — mos


def test_a_renamed_upstream_name_is_reported_clearly(monkeypatch):
    """Upstream nom o'zgartirsa, `AttributeError` emas, ANIQ xabar chiqsin."""
    import pyezvizapi.cloud_stream as _cs
    from cloudcam.sources.cloud import compat

    monkeypatch.delattr(_cs, "build_vtm_url", raising=False)
    with pytest.raises(compat.IncompatibleUpstream, match="build_vtm_url"):
        compat.check_upstream()


def test_patches_are_idempotent():
    """`install_patches()` ko'p marta chaqirilsa ham patch USTIGA patch
    qo'yilmasligi kerak (aks holda sahifalovchi o'zini chaqirib ketardi)."""
    import pyezvizapi.cloud_stream as _cs
    from cloudcam.sources.cloud import compat

    compat.install_patches()
    first = _cs.get_vtm_page_list
    compat.install_patches()
    assert _cs.get_vtm_page_list is first
