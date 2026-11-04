"""RTSP cameras: the saved list, MediaMTX config under `cloudcam serve`, and the CLI."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import pytest

from cloudcam import cli, serve, settings
from cloudcam.api import Camera
from cloudcam.sources.rtsp import channel_path, store
from test_rtsp_probe import SDP, FakeCamera

URL = "rtsp://admin:s3cret@192.168.1.64:554/Streaming/Channels/101"
SUB = "rtsp://admin:s3cret@192.168.1.64:554/Streaming/Channels/102"


@pytest.fixture
def rtsp_file(tmp_path, monkeypatch):
    path = str(tmp_path / "rtsp_cameras.json")
    monkeypatch.setattr(settings, "_active", settings.Settings(rtsp_file=path))
    return path


# ---- store ---------------------------------------------------------------

def test_store_roundtrip_replace_and_remove(rtsp_file):
    assert store.load() == []
    store.add("gate", URL, SUB)
    store.add("yard", URL)
    store.add("gate", URL)                                 # replaced, not duplicated
    assert [(c.name, c.sub_url) for c in store.load()] == [("gate", ""), ("yard", "")]
    assert store.remove("yard") and not store.remove("yard")
    assert [c.name for c in store.load()] == ["gate"]
    if os.name == "posix":
        assert os.stat(rtsp_file).st_mode & 0o077 == 0     # holds camera passwords


def test_store_validates(rtsp_file):
    for bad in ("", "a b", "x/y", "a" * 65):
        with pytest.raises(ValueError):
            store.add(bad, URL)
    with pytest.raises(ValueError):
        store.add("ok", "http://host/x")


def test_broken_file_is_never_overwritten(rtsp_file):
    with open(rtsp_file, "w") as f:
        f.write('{"gate": {"url": "rtsp://h/x"},}')          # trailing comma
    assert store.load() == []                              # lenient read
    with pytest.raises(store.RtspStoreError):
        store.add("yard", URL)
    with open(rtsp_file) as f:
        assert "gate" in f.read()


# ---- serve ---------------------------------------------------------------

RC = [store.RtspCamera("gate", URL, SUB), store.RtspCamera("door", URL),
      store.RtspCamera("bad name", URL)]


def test_rtsp_cameras_become_pull_paths_next_to_cloud_ones():
    cfg = serve.build_config([Camera("AB1234567", 1, "Yard")], python="py", rtsp_cameras=RC)
    p = cfg["paths"]
    assert set(p) == {"cam/AB1234567_1", "cam/AB1234567_1/sub",
                      "rtsp/gate", "rtsp/gate/sub", "rtsp/door"}
    assert p["rtsp/gate"] == {"source": URL, "sourceOnDemand": True,
                              "sourceOnDemandStartTimeout": f"{serve.START_TIMEOUT_S}s",
                              "sourceOnDemandCloseAfter": f"{serve.CLOSE_AFTER_S}s"}
    assert p["rtsp/gate/sub"]["source"] == SUB
    assert "runOnDemand" not in p["rtsp/gate"]            # no proxy process
    no_sub = serve.build_config([], python="py", substream=False, rtsp_cameras=RC)["paths"]
    assert set(no_sub) == {"rtsp/gate", "rtsp/door"}


def test_urls_list_rtsp_cameras_without_their_passwords():
    out = serve.urls([], host="nvr.local", rtsp_cameras=RC)
    assert out == [("gate", "rtsp://nvr.local:8554/rtsp/gate", "http://nvr.local:8889/rtsp/gate"),
                   ("door", "rtsp://nvr.local:8554/rtsp/door", "http://nvr.local:8889/rtsp/door")]


def test_serve_no_cloud_uses_only_rtsp_cameras(rtsp_file, capsys, monkeypatch):
    store.add("gate", URL, SUB)
    monkeypatch.setattr(cli, "_cam", lambda a: pytest.fail("must not log in"))
    assert cli.main(["serve", "--no-cloud", "--print-config"]) == 0
    cfg = json.loads(capsys.readouterr().out)
    assert set(cfg["paths"]) == {"rtsp/gate", "rtsp/gate/sub"}


def test_serve_without_account_falls_back_to_rtsp(rtsp_file, capsys, monkeypatch):
    store.add("gate", URL)
    monkeypatch.setattr(cli, "_has_account", lambda a: False)
    monkeypatch.setattr(cli, "_cam", lambda a: pytest.fail("must not log in"))
    assert cli.main(["serve", "--print-config"]) == 0
    assert "rtsp/gate" in json.loads(capsys.readouterr().out)["paths"]


# ---- CLI -----------------------------------------------------------------

@pytest.fixture
def fake_nvr():
    paths = {channel_path("hikvision", ch): SDP for ch in (1, 2)}
    paths[channel_path("hikvision", 1, "sub")] = SDP
    cam = FakeCamera(paths, user="admin", password="pw")
    yield cam
    cam.close()


def test_cli_probe_add_list_remove(rtsp_file, fake_nvr, capsys):
    url = f"rtsp://admin:pw@127.0.0.1:{fake_nvr.port}/Streaming/Channels/101"
    assert cli.main(["rtsp", "probe", url, "--timeout", "2"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("OK") and "H265" in out and "/Streaming/Channels/102" in out

    assert cli.main(["rtsp", "add", "gate", url, "--timeout", "2"]) == 0
    assert store.load()[0].sub_url.endswith("/Streaming/Channels/102")
    capsys.readouterr()
    assert cli.main(["rtsp", "list"]) == 0
    listed = capsys.readouterr().out
    assert "gate" in listed and "admin:***@" in listed and ":pw@" not in listed

    bad = url.replace(":pw@", ":nope@")
    assert cli.main(["rtsp", "add", "x", bad, "--timeout", "2"]) == 1   # checked, refused
    assert [c.name for c in store.load()] == ["gate"]
    assert cli.main(["rtsp", "remove", "gate"]) == 0
    assert store.load() == []


def test_cli_scan_saves_every_channel(rtsp_file, fake_nvr, capsys):
    rc = cli.main(["rtsp", "scan", "127.0.0.1", "--port", str(fake_nvr.port), "--user", "admin",
                   "--pass", "pw", "--channels", "4", "--timeout", "2", "--save", "nvr"])
    assert rc == 0
    cams = {c.name: c for c in store.load()}
    assert set(cams) == {"nvr_1", "nvr_2"}
    assert cams["nvr_1"].sub_url and not cams["nvr_2"].sub_url
    assert "pw" not in capsys.readouterr().out


# ---- real MediaMTX (optional) ---------------------------------------------

MTX = os.environ.get("CLOUDCAM_TEST_MEDIAMTX")


@pytest.mark.skipif(not MTX or not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="CLOUDCAM_TEST_MEDIAMTX=<mediamtx path> and ffmpeg/ffprobe needed")
def test_real_mediamtx_pulls_an_rtsp_camera_on_demand(tmp_path):
    """A test pattern published on one path plays the 'camera'; an rtsp/ path pulls it."""
    port = 18754
    cam_url = f"rtsp://127.0.0.1:{port}/fakecam"
    cfg = serve.build_config([], python="python", rtsp_port=port, hls_port=19088,
                             webrtc_port=19089, rtp_port=18200, webrtc_udp_port=18389,
                             close_after=5, mtx_version=serve.mediamtx_version(MTX),
                             rtsp_cameras=[store.RtspCamera("gate", cam_url)])
    cfg["paths"]["fakecam"] = {"runOnDemand": (
        "ffmpeg -hide_banner -loglevel error -re -f lavfi -i testsrc=size=320x240:rate=12 "
        "-c:v libx264 -preset ultrafast -tune zerolatency -g 12 "
        "-f rtsp -rtsp_transport tcp rtsp://127.0.0.1:$RTSP_PORT/$MTX_PATH")}
    path = serve.write_config(cfg, str(tmp_path / "mediamtx.yml"))
    log = open(tmp_path / "mtx.log", "w")
    proc = subprocess.Popen([MTX, path], cwd=tmp_path, stdout=log, stderr=subprocess.STDOUT)
    try:
        time.sleep(1.5)
        r = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp",
                            "-select_streams", "v:0", "-show_entries", "stream=codec_name",
                            "-of", "csv=p=0", f"rtsp://127.0.0.1:{port}/rtsp/gate"],
                           capture_output=True, text=True, timeout=40)
        assert r.stdout.strip() == "h264", (r.stderr, (tmp_path / "mtx.log").read_text())
    finally:
        proc.terminate()
        proc.wait(10)
        log.close()


# ---- snapshot / open_rtsp target resolution --------------------------------

def test_snapshot_target_is_url_or_saved_name(rtsp_file):
    store.add("gate", URL, SUB)
    assert cli._rtsp_target(URL).url == URL
    assert cli._rtsp_target("gate").url == URL
    assert cli._rtsp_target("AB1234567") is None              # a cloud serial


def test_open_rtsp_resolves_saved_name_without_login(rtsp_file, monkeypatch):
    from cloudcam.api import CloudCam
    store.add("gate", URL)
    cam = CloudCam(settings=settings.get_active())
    seen = {}
    monkeypatch.setattr(cam._mgr, "add", lambda serial, **kw: seen.update(serial=serial, **kw))
    cam.open_rtsp("gate")
    assert seen["url"] == URL and seen["serial"] == "rtsp:gate"
    with pytest.raises(ValueError):
        cam.open_rtsp("missing")
    assert cam._logged_in is False
