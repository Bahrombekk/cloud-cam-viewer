"""`cloudcam serve --install-mediamtx` — rasmiy relizdan, nazorat summasi bilan.

Bulut/internet yo'q: lokal HTTP server soxta reliz (arxiv + `checksums.sha256`) beradi.
"""
import hashlib
import http.server
import io
import os
import tarfile
import threading
import zipfile

import pytest

from cloudcam import cli, mtx_install, serve
from cloudcam.mtx_install import InstallError, asset_name, install

V = "1.20.0"


# ---- platforma -> arxiv nomi ---------------------------------------------------

@pytest.mark.parametrize("system, machine, expected", [
    ("Windows", "AMD64", "mediamtx_v1.20.0_windows_amd64.zip"),
    ("Linux", "x86_64", "mediamtx_v1.20.0_linux_amd64.tar.gz"),
    ("Linux", "aarch64", "mediamtx_v1.20.0_linux_arm64.tar.gz"),
    ("Linux", "armv7l", "mediamtx_v1.20.0_linux_armv7.tar.gz"),
    ("Linux", "armv6l", "mediamtx_v1.20.0_linux_armv6.tar.gz"),
    ("Darwin", "arm64", "mediamtx_v1.20.0_darwin_arm64.tar.gz"),
    ("Darwin", "x86_64", "mediamtx_v1.20.0_darwin_amd64.tar.gz"),
])
def test_asset_names_match_the_official_release(system, machine, expected):
    assert asset_name("v1.20.0", system, machine) == expected


@pytest.mark.parametrize("system, machine", [("Windows", "ARM64"), ("Linux", "riscv64"),
                                             ("FreeBSD", "amd64")])
def test_unsupported_platforms_are_refused(system, machine):
    with pytest.raises(InstallError):
        asset_name(V, system, machine)


@pytest.mark.parametrize("bad", ["latest", "1.20", "1.20.0; rm -rf /", "../1.0.0"])
def test_version_must_be_a_plain_number(bad):
    with pytest.raises(InstallError):
        asset_name(bad, "Linux", "x86_64")


# ---- soxta reliz serveri -------------------------------------------------------

def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, d in files.items():
            z.writestr(n, d)
    return buf.getvalue()


def _tgz(files):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for n, d in files.items():
            info = tarfile.TarInfo(n)
            info.size = len(d)
            t.addfile(info, io.BytesIO(d))
    return buf.getvalue()


@pytest.fixture
def release(monkeypatch):
    """`serve(files, checksum_override=None)` -> base URL. `hits` — so'rovlar ro'yxati."""
    hits = []
    content = {}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            body = content.get(self.path)
            if body is None:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setattr(serve, "mediamtx_version", lambda path: (1, 20, 0))

    def publish(name, archive, sha=None):
        sha = sha or hashlib.sha256(archive).hexdigest()
        content[f"/v{V}/{name}"] = archive
        content[f"/v{V}/checksums.sha256"] = f"{'0' * 64}  other.zip\n{sha}  {name}\n".encode()
        return base

    publish.hits = hits
    yield publish
    srv.shutdown()
    srv.server_close()


def _install(base, tmp_path, system="windows", machine="amd64", **kw):
    return install(V, bin_dir=str(tmp_path / "bin"), base_url=base, system=system,
                   machine=machine, log=lambda *a: None, **kw)


# ---- o'rnatish -----------------------------------------------------------------

def test_zip_release_is_installed(release, tmp_path):
    name = asset_name(V, "windows", "amd64")
    base = release(name, _zip({"mediamtx.exe": b"EXE", "LICENSE": b"MIT", "mediamtx.yml": b"x"}))
    path = _install(base, tmp_path)
    assert path == str(tmp_path / "bin" / "mediamtx.exe")
    assert open(path, "rb").read() == b"EXE"
    assert (tmp_path / "bin" / "LICENSE-mediamtx").read_bytes() == b"MIT"
    assert sorted(os.listdir(tmp_path / "bin")) == ["LICENSE-mediamtx", "mediamtx.exe"], \
        "faqat dastur va litsenziya olinadi"


def test_tar_release_is_installed_and_executable(release, tmp_path):
    name = asset_name(V, "linux", "x86_64")
    base = release(name, _tgz({"./mediamtx": b"ELF", "LICENSE": b"MIT"}))
    path = _install(base, tmp_path, system="linux", machine="x86_64")
    assert open(path, "rb").read() == b"ELF"
    if os.name == "posix":
        assert os.stat(path).st_mode & 0o111


def test_checksum_mismatch_installs_nothing(release, tmp_path):
    name = asset_name(V, "windows", "amd64")
    base = release(name, _zip({"mediamtx.exe": b"EVIL"}), sha="ab" * 32)
    with pytest.raises(InstallError, match="Checksum mismatch"):
        _install(base, tmp_path)
    assert not (tmp_path / "bin" / "mediamtx.exe").exists()


def test_asset_missing_from_checksums_is_refused(release, tmp_path):
    base = release("something_else.zip", b"x")
    with pytest.raises(InstallError, match="not listed"):
        _install(base, tmp_path)


def test_paths_inside_the_archive_are_ignored(release, tmp_path):
    """Arxiv ichidagi `../` yoki papkadagi fayl dastur o'rniga olinmaydi."""
    name = asset_name(V, "linux", "x86_64")
    base = release(name, _tgz({"../mediamtx": b"X", "sub/mediamtx": b"Y"}))
    with pytest.raises(InstallError, match="does not contain"):
        _install(base, tmp_path, system="linux", machine="x86_64")
    assert not (tmp_path / "mediamtx").exists() and not (tmp_path / "bin" / "mediamtx").exists()


def test_wrong_version_keeps_the_existing_binary(release, tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "mediamtx.exe").write_bytes(b"OLD-WORKING")
    name = asset_name(V, "windows", "amd64")
    base = release(name, _zip({"mediamtx.exe": b"NEW"}))
    versions = {"calls": 0}

    def version(path):
        versions["calls"] += 1
        return (1, 9, 3) if path.endswith(".new-mediamtx.exe") else (1, 9, 3)

    monkeypatch.setattr(serve, "mediamtx_version", version)
    with pytest.raises(InstallError, match="expected 1.20.0"):
        _install(base, tmp_path)
    assert (bin_dir / "mediamtx.exe").read_bytes() == b"OLD-WORKING"
    assert sorted(os.listdir(bin_dir)) == ["mediamtx.exe"], "vaqtinchalik fayl qolmasin"


def test_same_version_is_not_downloaded_again(release, tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "mediamtx.exe").write_bytes(b"INSTALLED")
    base = release(asset_name(V, "windows", "amd64"), _zip({"mediamtx.exe": b"NEW"}))
    assert _install(base, tmp_path) == str(bin_dir / "mediamtx.exe")
    assert release.hits == [], "o'rnatilgan versiya qayta yuklanmasligi kerak"
    _install(base, tmp_path, force=True)
    assert (bin_dir / "mediamtx.exe").read_bytes() == b"NEW"


def test_insecure_download_urls_are_refused(tmp_path):
    with pytest.raises(InstallError, match="insecure"):
        install(V, bin_dir=str(tmp_path), base_url="http://example.com/releases",
                system="windows", machine="amd64", log=lambda *a: None)


def test_the_default_source_is_the_official_https_release():
    assert mtx_install.RELEASES == "https://github.com/bluenviron/mediamtx/releases/download"
    assert mtx_install.DEFAULT_VERSION == V


# ---- CLI -----------------------------------------------------------------------

def test_cli_installs_before_logging_in(monkeypatch):
    order = []
    monkeypatch.setattr(mtx_install, "install", lambda v: order.append(("install", v)) or "M")
    monkeypatch.setattr(cli, "_cam", lambda args: order.append(("login", args.mediamtx))
                        or (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit):
        cli.main(["serve", "--install-mediamtx", "--mediamtx-version", "1.19.0"])
    assert order == [("install", "1.19.0"), ("login", "M")]


def test_cli_install_error_stops_before_login(monkeypatch):
    def boom(v):
        raise InstallError("Checksum mismatch")

    monkeypatch.setattr(mtx_install, "install", boom)
    monkeypatch.setattr(cli, "_cam", lambda args: pytest.fail("login qilinmasligi kerak"))
    with pytest.raises(SystemExit) as e:
        cli.main(["serve", "--install-mediamtx"])
    assert "Checksum mismatch" in str(e.value)
