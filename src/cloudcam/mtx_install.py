"""MediaMTX'ni rasmiy GitHub relizidan o'rnatish (`cloudcam serve --install-mediamtx`).

Repoga binar qo'yilmaydi (55 MB, platformaga xos, tez-tez yangilanadi) — o'rniga
foydalanuvchi platformasiga mos rasmiy arxiv yuklanadi:

  1. `checksums.sha256` (shu relizdan) olinadi;
  2. arxiv yuklanib, SHA-256 si solishtiriladi — mos kelmasa hech narsa yozilmaydi;
  3. arxivdan FAQAT `mediamtx`/`mediamtx.exe` va `LICENSE` olinadi (arxiv ichidagi
     `../` yoki boshqa fayllar e'tiborsiz) va `<data dir>/bin/` ga atomar yoziladi;
  4. `mediamtx --version` kutilgan versiyani berishi tekshiriladi.

Standart versiya — shu loyiha bilan sinalgani ([[DEFAULT_VERSION]]).
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import re
import tarfile
import tempfile
import zipfile

DEFAULT_VERSION = "1.20.0"          # `cloudcam serve` bilan jonli sinalgan (2026-11-04)
RELEASES = "https://github.com/bluenviron/mediamtx/releases/download"
MAX_ARCHIVE_BYTES = 200 << 20       # himoya: kutilmagan ulkan javob diskni to'ldirmasin
HTTP_TIMEOUT = 60

_ARCH = {"amd64": "amd64", "x86_64": "amd64", "x64": "amd64",
         "arm64": "arm64", "aarch64": "arm64",
         "armv7l": "armv7", "armv7": "armv7", "armv6l": "armv6", "armv6": "armv6"}
# Rasmiy relizda bor kombinatsiyalar (v1.20.0 assets ro'yxati bo'yicha).
_SUPPORTED = {("windows", "amd64"), ("linux", "amd64"), ("linux", "arm64"),
              ("linux", "armv6"), ("linux", "armv7"), ("darwin", "amd64"),
              ("darwin", "arm64")}


class InstallError(RuntimeError):
    """MediaMTX o'rnatilmadi (platforma, tarmoq, nazorat summasi yoki arxiv)."""


def _check_version(version: str) -> str:
    v = version.lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", v):
        raise InstallError(f"Invalid MediaMTX version: {version!r}")
    return v


def asset_name(version: str, system: str | None = None, machine: str | None = None) -> str:
    """Shu platforma uchun reliz arxivi nomi, masalan `mediamtx_v1.20.0_linux_amd64.tar.gz`."""
    v = _check_version(version)
    osname = (system or platform.system()).lower()
    arch = _ARCH.get((machine or platform.machine()).lower())
    if (osname, arch) not in _SUPPORTED:
        raw = machine or platform.machine()
        raise InstallError(f"No official MediaMTX build for {osname}/{raw}; "
                           f"install it manually and pass --mediamtx PATH")
    ext = "zip" if osname == "windows" else "tar.gz"
    return f"mediamtx_v{v}_{osname}_{arch}.{ext}"


def _get(url: str, limit: int) -> bytes:
    import requests
    if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
        raise InstallError(f"Refusing to download over an insecure URL: {url}")
    try:
        with requests.get(url, stream=True, timeout=HTTP_TIMEOUT) as r:
            r.raise_for_status()
            buf = io.BytesIO()
            for chunk in r.iter_content(1 << 16):
                buf.write(chunk)
                if buf.tell() > limit:
                    raise InstallError(f"{url}: response larger than {limit >> 20} MB")
            return buf.getvalue()
    except requests.RequestException as e:
        raise InstallError(f"Download failed: {url}: {e}") from e


def _expected_sha256(checksums: str, name: str) -> str:
    for line in checksums.splitlines():
        parts = line.strip().split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-f]{64}",
                                                                              parts[0].lower()):
            return parts[0].lower()
    raise InstallError(f"{name} is not listed in checksums.sha256")


def _members(data: bytes, name: str, wanted: set[str]) -> dict[str, bytes]:
    """Arxivdan faqat `wanted` dagi ILDIZ fayllarni oladi (yo'l bo'lsa — e'tiborsiz)."""
    out: dict[str, bytes] = {}
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                if info.filename in wanted and not info.is_dir():
                    out[info.filename] = z.read(info)
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            for m in t.getmembers():
                n = m.name[2:] if m.name.startswith("./") else m.name
                if n in wanted and m.isfile():
                    f = t.extractfile(m)
                    if f is not None:
                        out[n] = f.read()
    return out


def _write_atomic(path: str, data: bytes, mode: int) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def default_bin_dir() -> str:
    from cloudcam.settings import data_dir
    return os.path.join(data_dir(), "bin")


def install(version: str = DEFAULT_VERSION, *, bin_dir: str | None = None,
            force: bool = False, base_url: str = RELEASES, system: str | None = None,
            machine: str | None = None, log=print) -> str:
    """MediaMTX'ni o'rnatadi va dastur yo'lini qaytaradi.

    O'sha versiya allaqachon o'rnatilgan bo'lsa (va `force` emas) hech narsa yuklanmaydi."""
    from cloudcam import serve
    v = _check_version(version)
    name = asset_name(v, system, machine)
    exe = "mediamtx.exe" if name.endswith(".zip") else "mediamtx"
    bin_dir = bin_dir or default_bin_dir()
    target = os.path.join(bin_dir, exe)
    if not force and os.path.isfile(target) and serve.mediamtx_version(target) == \
            tuple(int(x) for x in v.split(".")):
        log(f"MediaMTX {v} is already installed: {target}")
        return target

    base = f"{base_url.rstrip('/')}/v{v}"
    log(f"Downloading {name} ...")
    checksums = _get(f"{base}/checksums.sha256", 1 << 20).decode("utf-8", "replace")
    expected = _expected_sha256(checksums, name)
    data = _get(f"{base}/{name}", MAX_ARCHIVE_BYTES)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise InstallError(f"Checksum mismatch for {name}: expected {expected}, got {actual}; "
                           f"nothing was installed")
    files = _members(data, name, {exe, "LICENSE"})
    if exe not in files:
        raise InstallError(f"{name} does not contain {exe}; nothing was installed")

    # Avval yonidagi vaqtinchalik nomga yozib TEKSHIRAMIZ — mavjud ishlayotgan nusxa
    # yangisi tasdiqlanmaguncha almashtirilmaydi.
    candidate = os.path.join(bin_dir, ".new-" + exe)
    _write_atomic(candidate, files[exe], 0o755)
    try:
        got = serve.mediamtx_version(candidate)
        if got is not None and got != tuple(int(x) for x in v.split(".")):
            raise InstallError(f"Downloaded binary reports version {serve.version_str(got)}, "
                               f"expected {v}; nothing was installed")
        os.replace(candidate, target)
    finally:
        if os.path.exists(candidate):
            os.unlink(candidate)
    if "LICENSE" in files:
        _write_atomic(os.path.join(bin_dir, "LICENSE-mediamtx"), files["LICENSE"], 0o644)
    log(f"Installed MediaMTX {v} (sha256 verified): {target}")
    return target
