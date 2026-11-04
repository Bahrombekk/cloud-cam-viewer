"""Bulut domeni tekshiruvi (central-server audit 2026-11-03 dan ko'chirildi).

Login md5(parol) ni, keyingi so'rovlar `sessionId` ni yuboradigan domen sozlama,
bulut javobi va `token.json` dan keladi — hech biri tekshirilmasdi."""
import json

import pytest

from cloudcam.sources.cloud.client import CloudClient, UnsafeDomain, safe_domain


@pytest.mark.parametrize("d", ["api.hik-connect.com", "apiisgp.ezvizlife.com",
                               "apiieu.hik-connectru.com", "HTTPS://API.Hik-Connect.com/"])
def test_cloud_domains_allowed(d):
    assert safe_domain(d).endswith((".com",))


@pytest.mark.parametrize("d", ["evil.com", "api.hik-connect.com.evil.com", "127.0.0.1",
                               "hik-connect.com@evil.com", "api.hik-connect.com/x",
                               "", None])
def test_foreign_domains_rejected(d):
    with pytest.raises(UnsafeDomain):
        safe_domain(d)


def test_bad_region_rejected_at_construction():
    with pytest.raises(UnsafeDomain):
        CloudClient("a@b.c", "pw", region="attacker.example")


def test_tampered_token_is_not_restored(tmp_path):
    """token.json begona hostga ishora qilsa sessiya tiklanmaydi (sessionId u yerga ketardi)."""
    c = CloudClient("a@b.c", "pw")
    p = tmp_path / "token.json"
    p.write_text(json.dumps({"session_id": "S", "api_url": "attacker.example",
                             "feature_code": c.FEATURE_CODE}), encoding="utf-8")
    assert c.load_token(str(p)) is False
    assert c.session_id is None
    p.write_text(json.dumps({"session_id": "S", "api_url": "api.hik-connect.com",
                             "feature_code": c.FEATURE_CODE}), encoding="utf-8")
    assert c.load_token(str(p)) is True


def test_login_redirect_to_foreign_domain_rejected(monkeypatch):
    c = CloudClient("a@b.c", "pw", platform="ezviz")

    class _R:
        def json(self):
            return {"meta": {"code": 200},
                    "loginSession": {"sessionId": "s", "rfSessionId": "r"},
                    "loginArea": {"apiDomain": "evil.example"}}

    monkeypatch.setattr(c.session, "post", lambda *a, **k: _R())
    with pytest.raises(UnsafeDomain):
        c._login_ezviz()
