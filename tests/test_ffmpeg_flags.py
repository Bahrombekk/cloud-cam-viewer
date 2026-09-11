"""ffmpeg bayroqlari — qaytib kelmasligi kerak bo'lgan xatolar.

Bular ishlab turgan tizimda KO'RINMAYDI (ffmpeg xato bermaydi), faqat
"tasvir boshida buzuq keladi" yoki "sekin ochiladi" bo'lib bilinadi —
shuning uchun manba matni test bilan qo'riqlanadi.
"""
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "cloudcam" / "stream_manager.py"
TEXT = SRC.read_text(encoding="utf-8")


def _args() -> str:
    """`_ffmpeg_cmd` tanasidagi argumentlar (IZOHLARSIZ — izohda bayroq nomi
    tilga olinishi mumkin, u kod emas)."""
    m = re.search(r"def _ffmpeg_cmd\(self\):(.*?)\n    def ", TEXT, re.S)
    assert m, "_ffmpeg_cmd topilmadi"
    return "\n".join(ln for ln in m.group(1).splitlines()
                     if not ln.lstrip().startswith("#"))


def test_nobuffer_never_comes_back():
    """`-fflags nobuffer` probe paytidagi VPS/SPS/PPS + birinchi IDR ni tashlaydi
    -> tasvir boshida kul rang/buzuq bo'ladi (o'lchandi: Y=125..127 vs 54..190)."""
    assert "nobuffer" not in _args()


def test_genpts_is_kept():
    """Bulut oqimida PTS ishonchsiz — genpts kerak."""
    assert "+genpts" in _args()


def test_probe_is_small_enough_for_fast_start():
    a = _args()
    for name in ("-analyzeduration", "-probesize"):
        m = re.search(rf'"{name}",\s*"(\d+)"', a)
        assert m, f"{name} yo'q"
        assert int(m.group(1)) <= 200000, f"{name} juda katta — ochilish sekinlashadi"


def test_output_is_not_forced_to_a_fixed_fps():
    """Majburiy CFR manba 20-25 fps bo'lganda kadrlarni TASHLAYDI (o'lchandi:
    15 fps ga majburlash 25-40% kadrni yo'qotgan) — passthrough qolishi kerak."""
    assert '"-fps_mode", "passthrough"' in _args()
