"""Sessiyani QAYTA ISHLATISH — har ishga tushirishda login qilmaslik.

Muammo: `app.py` ham, `single.py` ham har ishga tushirishda `login()` qilardi.
Bulut buni yangi kirish deb qabul qiladi va tez-tez takrorlanganda CAPTCHA
(code 1015) yoki "yangi qurilma" 2FA sini so'raydi — 24/7 rejimda dastur
qayta ishga tushgan sayin shu. `ezviz-ha-addon` aynan shu sababdan sessiyani
diskda saqlaydi, `hikconnect` esa yangilash kerakligini SESSIYA YOSHIGA qarab
hal qiladi.
"""
import json
import time

import pytest

from cloudcam.core import identity
import requests

from cloudcam.sources.cloud.client import CloudClient, SessionExpired
from cloudcam.settings import Settings, set_active


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLOUDCAM_FEATURE_CODE", "f" * 32)
    set_active(Settings(token_file=str(tmp_path / "token.json")))
    identity.reset_cache()
    yield
    identity.reset_cache()
    set_active(Settings())


def _client():
    return CloudClient("a@b.c", "pw", platform="ezviz")


def test_saved_session_is_resumed_without_any_network(tmp_path):
    """Yangi sessiya -> keyingi ishga tushirish HECH QANDAY so'rov qilmaydi."""
    c = _client()
    c.session_id, c.rf_session_id = "SID", "RF"
    c.session_time = time.time()
    path = str(tmp_path / "token.json")
    c.save_token(path)

    c2 = _client()
    c2.login = lambda: pytest.fail("login chaqirilmasligi kerak edi")
    c2.refresh_session = lambda **kw: pytest.fail("refresh ham kerak emas edi")
    assert c2.connect(path) == "resumed"
    assert c2.session_id == "SID"
    assert c2.session.headers["sessionId"] == "SID"


def test_stale_session_is_refreshed_not_re_logged_in(tmp_path):
    """Eskirgan sessiya — refresh (arzon), login emas (qimmat va CAPTCHA'li)."""
    c = _client()
    c.session_id, c.rf_session_id = "SID", "RF"
    c.session_time = time.time() - (CloudClient.SESSION_MAX_AGE + 10)
    path = str(tmp_path / "token.json")
    c.save_token(path)

    c2 = _client()
    calls = []
    c2.refresh_session = lambda **kw: calls.append("refresh") or True
    c2.login = lambda: pytest.fail("refresh ishlaganda login kerak emas")
    assert c2.connect(path) == "refreshed"
    assert calls == ["refresh"]


def test_full_login_only_when_refresh_fails(tmp_path):
    path = str(tmp_path / "token.json")
    c = _client()
    c.session_id, c.rf_session_id = "SID", "RF"
    c.session_time = 0.0                      # juda eski
    c.save_token(path)

    c2 = _client()

    def _boom(**kw):
        raise SessionExpired("refresh token eskirgan")

    c2.refresh_session = _boom
    done = []
    c2.login = lambda: done.append("login")
    assert c2.connect(path) == "login"
    assert done == ["login"]


def test_token_from_another_machine_is_rejected(tmp_path, monkeypatch):
    """Boshqa kompyuterdan ko'chirilgan token ishlatilmasligi kerak — bulut
    baribir rad etadi, biz esa buni darrov bilib to'g'ri login qilamiz."""
    path = tmp_path / "token.json"
    path.write_text(json.dumps({
        "session_id": "SID", "rf_session_id": "RF",
        "username": "a@b.c", "api_url": "x", "feature_code": "0" * 32,
    }), encoding="utf-8")
    assert _client().load_token(str(path)) is False


def test_refresh_is_decided_by_age_not_by_a_timer():
    """`hikconnect.is_refresh_login_needed()` bilan bir xil g'oya."""
    c = _client()
    assert c.is_refresh_needed() is True           # sessiya umuman yo'q
    c.session_id = "SID"
    c.session_time = time.time()
    assert c.is_refresh_needed() is False
    c.session_time = time.time() - (CloudClient.SESSION_MAX_AGE + 1)
    assert c.is_refresh_needed() is True


def test_failed_login_backs_off_instead_of_hammering_the_cloud():
    """24/7 sikl xato parol bilan sekundiga bir marta urinmasligi kerak."""
    c = _client()
    c._login_ezviz = lambda: (_ for _ in ()).throw(RuntimeError("noto'g'ri parol"))
    with pytest.raises(RuntimeError, match="parol"):
        c.login()
    assert c._login_backoff >= CloudClient.LOGIN_BACKOFF_MIN
    with pytest.raises(RuntimeError, match="retried in"):
        c.login()                                   # ikkinchi urinish — kutish


def test_backoff_is_cleared_after_a_good_login():
    c = _client()
    c._login_fail_at = time.time() - 10_000
    c._login_backoff = 600.0
    c._login_ezviz = lambda: {"ok": True}
    c.login()
    assert c._login_backoff == 0.0 and c._login_fail_at == 0.0


def test_saved_token_carries_what_resume_needs(tmp_path):
    c = _client()
    c.session_id, c.rf_session_id = "SID", "RF"
    p = tmp_path / "token.json"
    c.save_token(str(p))
    blob = json.loads(p.read_text(encoding="utf-8"))
    for field in ("session_id", "rf_session_id", "api_url",
                  "platform", "feature_code", "session_time"):
        assert field in blob, f"{field} saqlanmagan -> qayta login qilinadi"


def test_network_error_during_refresh_does_not_trigger_a_password_login(tmp_path):
    """Qisqa tarmoq uzilishi -> CAPTCHA/2FA xavfli to'liq login EMAS."""
    path = str(tmp_path / "token.json")
    c = _client()
    c.session_id, c.rf_session_id, c.session_time = "SID", "RF", 0.0
    c.save_token(path)
    c2 = _client()

    def _net(**kw):
        raise requests.ConnectionError("net down")

    c2.refresh_session = _net
    c2.login = lambda: pytest.fail("tarmoq xatosida login qilinmasligi kerak")
    with pytest.raises(requests.ConnectionError):
        c2.connect(path)


def test_token_of_another_account_is_not_resumed(tmp_path):
    path = str(tmp_path / "token.json")
    c = _client()
    c.session_id, c.rf_session_id, c.session_time = "SID", "RF", time.time()
    c.save_token(path)
    other = CloudClient("other@b.c", "pw", platform="ezviz")
    assert other.load_token(path) is False
    assert CloudClient("A@B.C", "pw", platform="ezviz").load_token(path) is True
    # Proxy'lar email'siz quriladi — ular tokenni baribir tiklay oladi.
    assert CloudClient(None, None, platform="ezviz").load_token(path) is True


def test_refresh_and_save_writes_the_rotated_refresh_token(tmp_path):
    path = str(tmp_path / "token.json")
    c = _client()
    c.session_id, c.rf_session_id, c.session_time = "SID", "RF", time.time() - 100
    c.save_token(path)

    def _refresh(**kw):
        c.session_id, c.rf_session_id, c.session_time = "SID2", "RF2", time.time()
        return True

    c.refresh_session = _refresh
    assert c.refresh_and_save() == "refreshed"
    blob = json.loads(open(path, encoding="utf-8").read())
    assert blob["session_id"] == "SID2" and blob["rf_session_id"] == "RF2"


def test_refresh_and_save_adopts_a_session_another_process_already_refreshed(tmp_path):
    path = str(tmp_path / "token.json")
    old = _client()
    old.session_id, old.rf_session_id, old.session_time = "SID", "RF", time.time() - 100
    old.save_token(path)
    newer = _client()
    newer.session_id, newer.rf_session_id, newer.session_time = "SID2", "RF2", time.time()
    newer.save_token(path)                      # boshqa jarayon yangiladi
    old.refresh_session = lambda **kw: pytest.fail("eski refresh token ishlatilmasin")
    assert old.refresh_and_save(path) == "adopted"
    assert old.session_id == "SID2" and old.rf_session_id == "RF2"
