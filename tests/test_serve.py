"""`cloudcam serve` — MediaMTX konfiguratsiyasi va uning xavfsiz standartlari.

MediaMTX NOMA'LUM kalitni ko'rsa umuman ishga tushmaydi, standart holatda publish
hammaga ochiq va UDP/MoQ portlari barcha interfeyslarda ochiladi. Bular sinab
ko'rilib topilgan (MediaMTX v1.20.0) — shu yerda qo'riqlanadi.
"""
import json
import os
import shutil
import subprocess
import sys
import time

import pytest

from cloudcam import cli, serve
from cloudcam.api import Camera

CAMS = [Camera("AB1234567", 1, "Warehouse"), Camera("AB1234567", 2, "Yard")]


def _cfg(**kw):
    return serve.build_config(CAMS, python="/usr/bin/python3", **kw)


# ---- yo'llar va buyruqlar ----------------------------------------------------

def test_every_camera_gets_a_main_and_a_sub_path():
    paths = _cfg()["paths"]
    assert set(paths) == {"cam/AB1234567_1", "cam/AB1234567_1/sub",
                          "cam/AB1234567_2", "cam/AB1234567_2/sub"}
    assert set(_cfg(substream=False)["paths"]) == {"cam/AB1234567_1", "cam/AB1234567_2"}


def test_on_demand_command_publishes_back_to_the_same_path():
    p = _cfg()["paths"]
    main, sub = p["cam/AB1234567_2"], p["cam/AB1234567_2/sub"]
    assert main["runOnDemand"] == (
        "/usr/bin/python3 -m cloudcam stream AB1234567 --channel 2 "
        "--rtsp rtsp://127.0.0.1:$RTSP_PORT/$MTX_PATH --stream main --no-refresh")
    assert "--stream auto" in sub["runOnDemand"], "substream'siz kamerada asosiysiga qaytsin"
    assert main["runOnDemandRestart"] is True


def test_start_timeout_covers_the_cloud_first_frame():
    """Bulutdan birinchi kadr 7-10 s; MediaMTX standarti 10 s — kamera ochilmay qolardi."""
    p = _cfg()["paths"]["cam/AB1234567_1"]
    assert int(p["runOnDemandStartTimeout"].rstrip("s")) >= 20
    assert int(p["runOnDemandCloseAfter"].rstrip("s")) >= 10


def test_read_timeout_survives_cloud_pauses():
    """Bir kanalning bir sessiyasi uzilganda ikkinchisi ~9 s to'xtaydi (jonli o'lchov);
    MediaMTX standarti 10 s bunday publisher'ni uzib, qayta ulanishga tushirardi."""
    assert int(_cfg()["readTimeout"].rstrip("s")) >= 20


def test_python_path_with_spaces_is_quoted():
    cmd = serve.stream_command("/opt/my python/bin/python3", "AB1", 1, False, "127.0.0.1")
    assert cmd.startswith('"/opt/my python/bin/python3" -m cloudcam stream AB1 ')


def test_windows_backslashes_never_reach_mediamtx():
    r"""MediaMTX teskari slashni escape deb tashlab yuboradi: `C:\Users\...` ->
    `C:Users...` va `cloudcam serve` Windows'da birorta kamerani ocha olmasdi
    (MediaMTX v1.20.0 bilan jonli sinovda topilgan)."""
    cmd = serve.stream_command(r"C:\Program Files\Python\python.exe", "AB1", 1, False,
                               "127.0.0.1")
    assert "\\" not in cmd
    assert cmd.startswith('"C:/Program Files/Python/python.exe" -m cloudcam stream AB1 ')


def test_unsafe_serials_are_left_out():
    """Seriya yo'l nomiga va buyruq qatoriga tushadi."""
    cfg = serve.build_config([("AB1 ; rm -rf /", 1), ("../x", 1), ("OK_1-a", 3)], python="p")
    assert set(cfg["paths"]) == {"cam/OK_1-a_3", "cam/OK_1-a_3/sub"}


# ---- xavfsiz standartlar -----------------------------------------------------

def test_listens_on_localhost_by_default_including_udp():
    """UDP (RTP/RTCP, WebRTC ICE) ham `--listen` ga bog'lanadi — MediaMTX
    standartida ular barcha interfeyslarda ochilardi."""
    c = _cfg()
    for key in ("rtspAddress", "rtpAddress", "rtcpAddress", "hlsAddress", "webrtcAddress",
                "webrtcLocalUDPAddress"):
        assert c[key].startswith("127.0.0.1:"), key
    assert c["rtmp"] is False and c["srt"] is False


def test_listen_everywhere_binds_all_interfaces():
    c = _cfg(listen="0.0.0.0", rtsp_port=9554, rtp_port=9000, webrtc_udp_port=9189)
    assert (c["rtspAddress"], c["rtpAddress"], c["rtcpAddress"], c["webrtcLocalUDPAddress"]) == \
        (":9554", ":9000", ":9001", ":9189")
    assert "rtsp://127.0.0.1:$RTSP_PORT" in c["paths"]["cam/AB1234567_1"]["runOnDemand"]


def test_only_this_host_may_publish():
    """MediaMTX standartida publish hammaga ochiq: tarmoqdagi kishi `cam/...` ga o'z
    videosini qo'yib kamerani almashtira olardi."""
    users = _cfg(listen="0.0.0.0")["authInternalUsers"]
    publishers = [u for u in users if {"action": "publish"} in u["permissions"]]
    assert len(publishers) == 1 and set(publishers[0]["ips"]) == {"127.0.0.1", "::1"}
    others = [u for u in users if u["ips"] == []]
    assert others and all(u["permissions"] == [{"action": "read"}] for u in others)


def test_specific_listen_address_is_used_for_publishing():
    c = _cfg(listen="192.168.1.10")
    assert "rtsp://192.168.1.10:$RTSP_PORT" in c["paths"]["cam/AB1234567_1"]["runOnDemand"]
    pub = next(u for u in c["authInternalUsers"] if {"action": "publish"} in u["permissions"])
    assert "192.168.1.10" in pub["ips"]


def test_read_credentials():
    readers = _cfg(read_user="frigate", read_pass="s3cret")["authInternalUsers"][-1]
    assert (readers["user"], readers["pass"]) == ("frigate", "s3cret")
    with pytest.raises(ValueError):
        _cfg(read_user="frigate")


@pytest.mark.parametrize("version, has_moq", [(None, False), ((1, 9, 3), False),
                                              ((1, 18, 0), False), ((1, 19, 0), True),
                                              ((1, 20, 0), True)])
def test_moq_is_disabled_only_where_the_key_exists(version, has_moq):
    """MoQ (1.19+) barcha interfeyslarda ochiladi va sertifikat yozadi; eski versiya
    esa `moq` kalitini bilmaydi va umuman ishga tushmaydi."""
    c = _cfg(mtx_version=version)
    assert ("moq" in c) is has_moq
    if has_moq:
        assert c["moq"] is False


def test_config_is_written_privately(tmp_path):
    p = serve.write_config(_cfg(read_user="u", read_pass="p"), str(tmp_path / "m.yml"))
    assert json.loads(open(p, encoding="utf-8").read())["paths"]
    if os.name == "posix":
        assert os.stat(p).st_mode & 0o077 == 0


def test_mediamtx_is_found_in_the_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDCAM_HOME", str(tmp_path))
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert serve.find_mediamtx() is None
    exe = tmp_path / "bin" / ("mediamtx.exe" if os.name == "nt" else "mediamtx")
    exe.parent.mkdir()
    exe.write_bytes(b"")
    assert serve.find_mediamtx() == str(exe)


# ---- CLI ----------------------------------------------------------------------

class _FakeCam:
    def __init__(self, settings):
        self.settings = settings
        self.closed = False

    def cameras(self):
        return CAMS

    def start_token_refresh(self):
        pass

    def close(self):
        self.closed = True


@pytest.fixture
def fake_cam(monkeypatch, tmp_path):
    from cloudcam.settings import Settings
    cam = _FakeCam(Settings(token_file=str(tmp_path / "t.json"),
                            camkey_file=str(tmp_path / "k.json")))
    monkeypatch.setattr(cli, "_cam", lambda args: cam)
    return cam


def test_cli_print_config(fake_cam, capsys, monkeypatch):
    monkeypatch.setattr(serve, "find_mediamtx", lambda explicit=None: None)
    assert cli.main(["serve", "--print-config", "--rtsp-port", "9554"]) == 0
    cfg = json.loads(capsys.readouterr().out)
    assert cfg["rtspAddress"] == "127.0.0.1:9554" and "cam/AB1234567_2/sub" in cfg["paths"]
    assert fake_cam.closed


def test_cli_without_mediamtx_writes_the_config_and_explains(fake_cam, capsys, monkeypatch,
                                                              tmp_path):
    monkeypatch.setattr(serve, "find_mediamtx", lambda explicit=None: None)
    out = tmp_path / "mtx.yml"
    assert cli.main(["serve", "--config", str(out)]) == 1
    assert json.loads(out.read_text(encoding="utf-8"))["paths"]
    err = capsys.readouterr()
    assert "rtsp://127.0.0.1:8554/cam/AB1234567_1" in err.out
    assert "MediaMTX" in err.err and str(out) in err.err


def test_cli_rejects_an_old_mediamtx(fake_cam, monkeypatch):
    monkeypatch.setattr(serve, "find_mediamtx", lambda explicit=None: "mediamtx")
    monkeypatch.setattr(serve, "mediamtx_version", lambda b: (1, 8, 2))
    with pytest.raises(SystemExit) as e:
        cli.main(["serve"])
    assert "1.9.0" in str(e.value)


def test_cli_runs_mediamtx_without_the_password(fake_cam, monkeypatch, tmp_path):
    monkeypatch.setenv("CLOUDCAM_PASSWORD", "should-not-leak")
    monkeypatch.setattr(serve, "find_mediamtx", lambda explicit=None: "mediamtx")
    monkeypatch.setattr(serve, "mediamtx_version", lambda b: (1, 20, 0))
    seen = {}
    monkeypatch.setattr(serve, "run_mediamtx",
                        lambda binary, path, env: seen.update(path=path, env=env) or 0)
    assert cli.main(["serve", "--config", str(tmp_path / "m.yml")]) == 0
    assert "CLOUDCAM_PASSWORD" not in seen["env"]
    assert os.path.isabs(seen["env"]["CLOUDCAM_TOKEN_FILE"])
    assert json.loads(open(seen["path"], encoding="utf-8").read())["moq"] is False


def test_cli_read_credentials_go_together(fake_cam):
    with pytest.raises(SystemExit):
        cli.main(["serve", "--read-user", "u"])


# ---- parolsiz sessiya (serve ostidagi `cloudcam stream`) ----------------------

def test_saved_session_is_enough_without_a_password(tmp_path, monkeypatch):
    from cloudcam.sources.cloud.client import CloudClient
    tok = tmp_path / "token.json"
    c = CloudClient("a@b.c", "pw")
    c.session_id, c.rf_session_id, c.session_time = "S", "R", time.time()
    c.save_token(str(tok))
    for k in ("CLOUDCAM_EMAIL", "CLOUDCAM_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CLOUDCAM_TOKEN_FILE", str(tok))
    from cloudcam import settings as settings_mod
    monkeypatch.setattr(settings_mod, "_load_config_module", lambda: None)
    args = cli.argparse.Namespace(email=None, password=None, platform=None)
    cam = cli._cam(args)
    assert cam.auth_mode == "resumed"
    cam.close()


def test_no_login_attempt_with_an_empty_password(tmp_path):
    """Bo'sh parol bilan login bulutda "noto'g'ri parol" bo'lib sanalib CAPTCHA'ga olib
    kelardi — sessiya bo'lmasa aniq xato."""
    from cloudcam.sources.cloud.client import CloudClient, LoginRequired
    c = CloudClient(None, None)
    c.login = lambda: pytest.fail("login qilinmasligi kerak")
    with pytest.raises(LoginRequired):
        c.connect(str(tmp_path / "missing.json"))


# ---- haqiqiy MediaMTX bilan (ixtiyoriy) ----------------------------------------

MTX = os.environ.get("CLOUDCAM_TEST_MEDIAMTX")


@pytest.mark.skipif(not MTX or not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="CLOUDCAM_TEST_MEDIAMTX=<mediamtx yo'li> va ffmpeg/ffprobe kerak")
def test_real_mediamtx_starts_on_demand_shares_and_rejects_unknown_paths(tmp_path):
    version = serve.mediamtx_version(MTX)
    cfg = serve.build_config([("TEST1", 1), ("TEST2", 1)], python="python", rtsp_port=18654,
                             hls_port=18988, webrtc_port=18989, rtp_port=18100,
                             webrtc_udp_port=18289, close_after=5, mtx_version=version)
    # Kamera o'rnida ffmpeg sinov tasviri; $RTSP_PORT/$MTX_PATH haqiqiy formatda qoladi.
    cfg["paths"]["cam/TEST1_1"]["runOnDemand"] = (
        "ffmpeg -hide_banner -loglevel error -re -f lavfi -i testsrc=size=320x240:rate=12 "
        "-c:v libx264 -preset ultrafast -tune zerolatency -g 12 "
        "-f rtsp -rtsp_transport tcp rtsp://127.0.0.1:$RTSP_PORT/$MTX_PATH")
    # TEST2: haqiqiy Python yo'li serve'dagi bilan bir xil qoida bo'yicha MediaMTX'dan
    # o'tadi (Windows'da teskari slash yo'qolib, "cannot find the file" bo'lgan edi).
    helper = tmp_path / "pub.py"
    helper.write_text(
        "import subprocess, sys\n"
        "subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-re', '-f', 'lavfi',"
        " '-i', 'testsrc=size=320x240:rate=12', '-c:v', 'libx264', '-preset', 'ultrafast',"
        " '-tune', 'zerolatency', '-g', '12', '-f', 'rtsp', '-rtsp_transport', 'tcp',"
        " 'rtsp://127.0.0.1:' + sys.argv[1] + '/' + sys.argv[2]])\n", encoding="utf-8")

    def fwd(x):
        return serve._quote(str(x).replace("\\", "/"))
    cfg["paths"]["cam/TEST2_1"]["runOnDemand"] = (
        f"{fwd(sys.executable)} {fwd(helper)} $RTSP_PORT $MTX_PATH")
    path = serve.write_config(cfg, str(tmp_path / "mediamtx.yml"))
    log = open(tmp_path / "mtx.log", "w")
    proc = subprocess.Popen([MTX, path], cwd=tmp_path, stdout=log, stderr=subprocess.STDOUT)

    def probe(p):
        return subprocess.run(
            ["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0",
             f"rtsp://127.0.0.1:18654/{p}"], capture_output=True, text=True, timeout=40)

    try:
        time.sleep(2)
        assert proc.poll() is None, (tmp_path / "mtx.log").read_text()
        assert probe("cam/TEST1_1").stdout.strip() == "h264"
        assert probe("cam/TEST1_1").stdout.strip() == "h264"
        assert probe("cam/NOPE_1").returncode != 0
        assert probe("cam/TEST2_1").stdout.strip() == "h264", "python yo'li MediaMTX'dan o'tmadi"
        time.sleep(8)                              # close_after=5
    finally:
        proc.terminate()
        proc.wait(timeout=10)
        log.close()
    text = (tmp_path / "mtx.log").read_text()
    starts = text.count("[path cam/TEST1_1] runOnDemand command started")
    assert starts == 1, "ikkinchi ko'ruvchi o'sha oqimga ulanishi kerak edi"
    assert "cannot find" not in text
    assert "not configured" in text
    assert "not needed by anyone" in text
    assert not (tmp_path / "auto.key").exists(), "MoQ o'chirilmagan"
