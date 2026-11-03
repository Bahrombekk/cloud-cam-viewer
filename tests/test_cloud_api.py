"""Bulutning oqimdan tashqari API'lari (`cloudcam.sources.cloud.rest`) — tarmoqsiz.

Javob shakllari Hik-Connect hisobidan jonli olingan namunalarga asoslangan
(maxfiy qiymatlar olib tashlangan)."""
import base64
import datetime as dt
import json
import zlib

import pytest
from Crypto.Cipher import AES

from cloudcam.sources.cloud import rest as ca
from cloudcam.sources.cloud.vtm import image


class Resp:
    def __init__(self, data, status=200, content=b""):
        self._d, self.status_code, self.content = data, status, content

    def json(self):
        return self._d

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class Session:
    def __init__(self, *responses):
        self.r = list(responses)
        self.calls = []

    def _next(self, method, url, params=None, data=None, timeout=None):
        self.calls.append((method, url, dict(params or {}), dict(data or {})))
        return self.r.pop(0)

    def get(self, url, params=None, timeout=None):
        return self._next("GET", url, params=params)

    def put(self, url, data=None, timeout=None):
        return self._next("PUT", url, data=data)


class Client:
    api_url = "apiisgp.hik-connect.com"

    def __init__(self, *responses):
        self.session = Session(*responses)


OK = {"code": 200}

# Jonli javobdan (qisqartirilgan): NVR, 4-slotda muammoli disk.
NVR_STATUS = {
    "diskNum": 2, "diskState": "--01------------", "isEncrypt": 0, "privacyStatus": 0,
    "encryptPwd": image.code_hash("ABCDEF"),
    "optionals": {"OnlineStatus": "1",
                  "diskHealth": "-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1",
                  "diskCapacity": "-1,-1,1907729,0,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1,-1"},
}


def test_status_parses_online_disks_and_encryption():
    c = Client(Resp({"meta": OK, "statusInfos": {"S1": NVR_STATUS,
                                                 "S2": {"optionals": {"OnlineStatus": "0"},
                                                        "isEncrypt": 1}}}))
    st = ca.device_status(c, ["S2", "S1", "S1"])
    assert c.session.calls[0][2]["deviceSerials"] == "S1,S2"
    s1, s2 = st["S1"], st["S2"]
    assert s1.online is True and s1.encrypted is False
    assert [(d.slot, d.state, d.capacity_mb) for d in s1.disks] == [(3, "0", 1907729), (4, "1", 0)]
    assert not s1.disks_ok and s1.disks[0].ok
    assert s2.online is False and s2.encrypted is True and s2.disks == ()


def test_status_batches_large_accounts():
    serials = [f"S{i:03d}" for i in range(120)]
    c = Client(*[Resp({"meta": OK, "statusInfos": {}})] * 3)
    ca.device_status(c, serials)
    assert len(c.session.calls) == 3


def test_code_matches_uses_the_double_md5():
    s = ca.parse_status("S1", NVR_STATUS)
    assert s.code_matches("ABCDEF") is True
    assert s.code_matches("WRONG1") is False
    assert ca.parse_status("S1", {}).code_matches("ABCDEF") is None


def test_cloud_error_is_raised():
    with pytest.raises(ca.CloudApiError, match="code 2009"):
        ca.device_status(Client(Resp({"meta": {"code": 2009}})), ["S1"])


def _packed(items):
    return base64.b64encode(zlib.compress(json.dumps(items).encode())).decode()


def test_records_decode_and_page():
    page1 = [{"B": f"2026-11-03T0{h}:00:00", "E": f"2026-11-03T0{h}:30:00", "Type": 1}
             for h in range(2)]
    page2 = [{"B": "2026-11-03T01:00:00", "E": "2026-11-03T01:30:00", "Type": 1},  # takror
             {"B": "2026-11-03T02:00:00", "E": "2026-11-03T02:10:00", "Type": 4}]
    c = Client(Resp({"meta": OK, "records": _packed(page1), "isFinished": "0"}),
               Resp({"meta": OK, "records": _packed(page2), "isFinished": "1"}))
    recs = ca.records(c, "S1", 2, dt.datetime(2026, 11, 3), dt.datetime(2026, 11, 3, 3),
                      page_size=2)
    assert [(r.start.hour, r.start.minute, r.type) for r in recs] == [(0, 0, 1), (1, 0, 1),
                                                                     (2, 0, 4)]
    assert recs[0].seconds == 1800
    first, second = c.session.calls
    assert first[2]["startTime"] == "2026-11-03 00:00:00" and first[2]["channelNo"] == 2
    assert second[2]["startTime"] == "2026-11-03 01:30:00"   # oxirgi bo'lak oxiridan


def test_records_empty_payload():
    c = Client(Resp({"meta": OK, "records": None, "isFinished": None}))
    assert ca.records(c, "S1", 1, dt.datetime(2026, 1, 1), dt.datetime(2026, 1, 2)) == []


def test_messages_become_events_with_cursor():
    msg = {"msgId": "m1", "deviceSerial": "S1", "channel": "3", "time": 1762160000000,
           "timeStr": "2026-11-03 12:00:00", "title": "Motion", "subType": "2701",
           "picCrypt": 2, "ext": {"pics": ";https://img/x.jpg;", "alarmType": "10000"}}
    c = Client(Resp({"meta": OK, "message": [msg], "hasNext": True}))
    evs, nxt = ca.messages(c, ["S1"], limit=500)
    ev = evs[0]
    assert (ev.serial, ev.channel, ev.kind, ev.title) == ("S1", 3, "10000", "Motion")
    assert ev.picture_url == "https://img/x.jpg" and ev.encrypted is True
    assert ev.time is not None and nxt == "m1"
    assert c.session.calls[0][2]["limit"] == 50 and c.session.calls[0][2]["serials"] == "S1"


def test_alarms_become_events():
    a = {"alarmId": 7, "deviceSerial": "S1", "channelNo": 1, "alarmStartTime": 1762160000,
         "alarmStartTimeStr": "2026-11-03 12:00:00", "alarmType": 10000,
         "sampleName": "PIR", "picUrl": "https://img/a.jpg", "isEncrypt": 0}
    ev = ca.alarms(Client(Resp({"meta": OK, "alarms": [a]})))[0]
    assert (ev.msg_id, ev.kind, ev.encrypted, ev.picture_url) == ("7", "10000", False,
                                                                 "https://img/a.jpg")


def _encrypted_jpeg(code, plain):
    pad = 16 - len(plain) % 16
    ct = AES.new(code.ljust(16, "\0")[:16].encode(), AES.MODE_CBC,
                 b"01234567" + b"\0" * 8).encrypt(plain + bytes([pad]) * pad)
    return image.HEADER + image.code_hash(code).encode() + ct


def test_download_image_decrypts_without_sending_the_session(monkeypatch):
    seen = {}

    def fake_get(url, timeout=None, **kw):
        seen.update(url=url, kw=kw)
        return Resp({}, content=_encrypted_jpeg("ABCDEF", b"\xff\xd8JPEG"))

    monkeypatch.setattr(ca.requests, "get", fake_get)
    assert ca.download_image("https://img/x.jpg", "ABCDEF") == b"\xff\xd8JPEG"
    assert "headers" not in seen["kw"], "sessionId begona rasm serveriga ketmasligi kerak"
    with pytest.raises(image.ImageKeyError):
        ca.download_image("https://img/x.jpg", "WRONG1")
    with pytest.raises(ValueError, match="verification code"):
        ca.download_image("https://img/x.jpg")
    with pytest.raises(ValueError):
        ca.download_image("file:///etc/passwd")


def test_ptz_validates_and_passes_the_channel():
    c = Client(Resp({"meta": OK}))
    ca.ptz(c, "S1", "left", "start", channel=4, speed=3)
    method, url, _, data = c.session.calls[0]
    assert method == "PUT" and url.endswith("/v3/devices/S1/ptzControl")
    assert (data["command"], data["action"], data["channelNo"], data["speed"]) == \
        ("LEFT", "START", 4, 3)
    for bad in (("JUMP", "START", 5), ("LEFT", "GO", 5), ("LEFT", "START", 9)):
        with pytest.raises(ValueError):
            ca.ptz(c, "S1", bad[0], bad[1], speed=bad[2])
