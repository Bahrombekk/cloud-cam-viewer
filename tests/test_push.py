"""Push (MQTT) hodisalari — xabarni ochish va bulut bilan ro'yxatdan o'tish oqimi."""
import json
import sys
import types

import pytest

from cloudcam.sources.cloud import push


def _payload(ext_values, **top):
    return json.dumps({"ext": ",".join(ext_values), **top}).encode()


EXT = ["1", "1762160000", "S1", "2", "10000", "https://img/x.jpg", "", "", "0", "0",
       "f", "1", "chk", "0", "", "msg9", "", "Gate", "", "5"]


def test_decode_maps_ext_fields_and_ints():
    d = push.decode_message(_payload(EXT, alert="Motion"))
    ext = d["ext"]
    assert ext["device_serial"] == "S1" and ext["channel_no"] == 2
    assert ext["alert_type_code"] == 10000 and ext["is_encrypted"] == 1
    assert ext["msgId"] == "msg9" and ext["sequence_number"] == 5


def test_short_ext_is_padded_with_none():
    ext = push.decode_message(_payload(["1", "123"]))["ext"]
    assert ext["device_serial"] is None and ext["sequence_number"] is None


def test_to_event():
    ev = push.to_event(push.decode_message(_payload(EXT, alert="Motion")))
    assert (ev.serial, ev.channel, ev.kind, ev.title) == ("S1", 2, "10000", "Motion")
    assert ev.picture_url == "https://img/x.jpg" and ev.encrypted is True
    assert ev.time is not None and ev.msg_id == "msg9"
    assert push.to_event({"ext": {}}) is None


def test_bad_payload_raises():
    with pytest.raises(push.PushError):
        push.decode_message(b"not json")


# ---- ro'yxatdan o'tish -----------------------------------------------------

class Resp:
    def __init__(self, d):
        self._d = d

    def json(self):
        return self._d

    def raise_for_status(self):
        pass


class Http:
    def __init__(self):
        self.posts = []

    def post(self, url, data=None, timeout=None, allow_redirects=True, headers=None):
        self.posts.append((url, dict(data or {}), headers))
        if url.endswith("/v1/getClientId"):
            return Resp({"status": 200, "data": {"clientId": "CID"}})
        return Resp({"status": 200, "ticket": "T"})


class CloudClient:
    api_url = "apiisgp.hik-connect.com"
    session_id = "SESSION"

    def __init__(self, push_addr="pushsgp.hik-connect.com"):
        self._push = push_addr
        self.session = types.SimpleNamespace(
            get=lambda url, timeout=None: Resp({"userInfo": {"username": "internal_u"}}))

    def get_service_urls(self):
        return {"pushAddr": self._push}


class FakeMqtt:
    MQTTv311 = 4
    instances = []

    class Client:
        def __init__(self, **kw):
            self.kw = kw
            FakeMqtt.instances.append(self)
            self.calls = []

        def username_pw_set(self, u, p):
            self.calls.append(("auth", u))

        def reconnect_delay_set(self, **kw):
            pass

        def connect(self, host, port, keepalive):
            self.calls.append(("connect", host, port))

        def loop_start(self):
            self.calls.append(("loop",))

        def loop_stop(self):
            self.calls.append(("stop",))

        def disconnect(self):
            pass


@pytest.fixture
def fake_paho(monkeypatch):
    pkg = types.ModuleType("paho")
    sub = types.ModuleType("paho.mqtt")
    mod = types.ModuleType("paho.mqtt.client")
    mod.Client, mod.MQTTv311 = FakeMqtt.Client, FakeMqtt.MQTTv311
    pkg.mqtt, sub.client = sub, mod
    monkeypatch.setitem(sys.modules, "paho", pkg)
    monkeypatch.setitem(sys.modules, "paho.mqtt", sub)
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", mod)
    FakeMqtt.instances.clear()


def test_start_registers_starts_and_connects_then_stops(fake_paho):
    pc = push.PushClient(CloudClient())
    pc._http = Http()
    with pc:
        m = FakeMqtt.instances[0]
        assert ("connect", "pushsgp.hik-connect.com", 1882) in m.calls
        assert m.kw["client_id"] == "CID"
    urls = [u.rsplit("/", 2)[-2] + "/" + u.rsplit("/", 1)[-1] for u, _, _ in pc._http.posts]
    assert urls == ["v1/getClientId", "push/start", "push/stop"]
    reg, start = pc._http.posts[0], pc._http.posts[1]
    assert reg[2]["Authorization"].startswith("Basic ")
    assert start[1]["username"] == "internal_u" and start[1]["sessionId"] == "SESSION"
    assert "sessionId" not in reg[1]


def test_foreign_push_host_is_rejected(fake_paho):
    pc = push.PushClient(CloudClient(push_addr="push.evil.example"))
    pc._http = Http()
    with pytest.raises(ValueError):
        pc.start()
    assert pc._http.posts == [], "sessionId begona hostga yuborilmasligi kerak"


def test_missing_paho_gives_a_clear_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "paho.mqtt.client", None)
    with pytest.raises(push.PushError, match="paho-mqtt"):
        push.PushClient(CloudClient()).start()


def test_callback_errors_do_not_kill_the_loop():
    pc = push.PushClient(CloudClient(), on_event=lambda ev: 1 / 0)
    pc._on_message(None, None, types.SimpleNamespace(payload=_payload(EXT)))   # xato yutiladi
