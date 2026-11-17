"""
PC token renewal and the dual-secret rotation window (plan 06 D30, relay part;
OD-23, NEW-77).

Card tests: test_dual_secret_verify_and_window_end,
test_renew_before_expiry_once, test_renewed_token_sent_only_to_registered_sid.
Plus: the replaced token counts as current until the PC confirms (also with
RELAY_REQUIRE_CURRENT_TOKEN), confirmation by ACK or by registering with the
new token, refused and status-less ACKs, Clients without pc_token.v1 and
outdated tokens are never re-tokened (token_drop_needed), deferral for
duplicate and unverified sessions, an admin's generate-token cancels open
renewals, the fleet report, the startup line, settings parsing, and no token
in any log, audit entry or report.

The relay handlers are called directly (tests/relay_testkit.py: SQLite with
pcs, pc_token_renewals, pc_deployments and pc_health; inline background tasks;
audit entries recorded). The real eventlet relay with 200 stations is in
tests/integration/test_token_rotation_pg.py.

    python -m pytest tests/unit/test_pc_token_renewal.py -o addopts='' -p no:cacheprovider
"""

import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core import startup_check as sc
from app.core.config import Settings
from app.models.pc_tracking import PcHealth, PcTokenRenewal
from app.schemas import station_protocol as sp
from app.services import pc_tokens, relay_auth
from tests.relay_testkit import RelayHarness, import_relay, make_pc_token

relay = import_relay()

DAY = 86400
CAPS = [sp.FEATURE_DEPLOY_RESULT, sp.FEATURE_STATION_STATUS, sp.FEATURE_PC_TOKEN]
OLD_CLIENT_CAPS = [sp.FEATURE_DEPLOY_RESULT, sp.FEATURE_STATION_STATUS]  # no handler (Client <= 1.3)


@pytest.fixture
def h(monkeypatch):
    monkeypatch.setattr(relay.settings, "RELAY_SENDER_SECRET", "fixture-sender-" + "x" * 32)
    monkeypatch.setattr(relay.settings, "RELAY_REQUIRE_CURRENT_TOKEN", False)
    monkeypatch.setattr(relay.settings, "RELAY_STATION_REPORTS", True)
    monkeypatch.setattr(relay.settings, "JWT_SECRET_PREVIOUS", None)
    monkeypatch.setattr(relay.settings, "JWT_ROTATION_GRACE_UNTIL", None)
    monkeypatch.setattr(relay.settings, "PC_TOKEN_RENEW_BEFORE_DAYS", 30)
    harness = RelayHarness(relay, monkeypatch, deploy_tables=True)
    relay.enable_feature(sp.FEATURE_PC_TOKEN)  # the harness starts from no features
    yield harness
    relay.connected_pcs.clear()
    relay.session_data.clear()


@pytest.fixture
def rotation(h, monkeypatch):
    """An open rotation window: JWT_SECRET is new, JWT_SECRET_PREVIOUS old."""
    previous = "fixture-previous-" + secrets.token_hex(16)
    monkeypatch.setattr(relay.settings, "JWT_SECRET_PREVIOUS", previous)
    monkeypatch.setattr(relay.settings, "JWT_ROTATION_GRACE_UNTIL",
                        datetime.now(timezone.utc) + timedelta(days=14))
    return SimpleNamespace(h=h, previous=previous)


def _register(h, sid, pc_id, token, capabilities=CAPS):
    h.connect(sid)
    extra = {} if capabilities is None else {"capabilities": capabilities}
    h.register(sid, pc_id, token, **extra)


def _pushes(h, sid=None):
    """(data, target, callback) of every pc_token_renewed emit."""
    out = [(data, target, cb) for name, data, target, cb in h.emits if name == pc_tokens.EVENT_RENEWED]
    return [p for p in out if sid is None or p[1] == sid]


def _rows(h):
    db = h.Session()
    try:
        return [(r.id, r.pc_id, r.previous_token_sha256, r.issued_at, r.confirmed_at)
                for r in db.query(PcTokenRenewal).order_by(PcTokenRenewal.id)]
    finally:
        db.close()


def _audit_actions(h):
    return [a["action"] for a in h.audits]


def _decode(token, secret):
    return jwt.decode(token, secret, algorithms=["HS256"])


def _ack(callback, payload):
    callback(payload)


# --------------------------------------------------------------------------- #
# Verification and the window
# --------------------------------------------------------------------------- #


def _cfg(**overrides):
    values = dict(JWT_SECRET="fixture-current-" + "c" * 16, ALGORITHM="HS256", JWT_SECRET_PREVIOUS=None,
                  JWT_ROTATION_GRACE_UNTIL=None, PC_TOKEN_RENEW_BEFORE_DAYS=30, JWT_PC_TOKEN_EXPIRE_HOURS=8760)
    values.update(overrides)
    return SimpleNamespace(**values)


def test_dual_secret_verify_and_window_end(rotation, caplog):
    h, previous = rotation.h, rotation.previous
    now = int(time.time())
    cfg = _cfg(JWT_SECRET_PREVIOUS="fixture-previous-" + "p" * 16, JWT_ROTATION_GRACE_UNTIL=now + 3600)
    old = make_pc_token("PC_X", cfg.JWT_SECRET_PREVIOUS)
    new = make_pc_token("PC_X", cfg.JWT_SECRET)
    assert pc_tokens.window_state(cfg, now) == pc_tokens.WINDOW_OPEN
    assert pc_tokens.verify_pc_token(new, cfg)[1] == pc_tokens.SECRET_CURRENT
    assert pc_tokens.verify_pc_token(old, cfg)[1] == pc_tokens.SECRET_PREVIOUS
    with pytest.raises(jwt.InvalidSignatureError):
        pc_tokens.verify_pc_token(make_pc_token("PC_X", "fixture-third-" + "t" * 16), cfg)
    with pytest.raises(jwt.ExpiredSignatureError):  # expired stays expired, with either secret
        pc_tokens.verify_pc_token(make_pc_token("PC_X", cfg.JWT_SECRET_PREVIOUS, exp_delta=-5), cfg)
    with pytest.raises(jwt.DecodeError):
        pc_tokens.verify_pc_token("not-a-jwt", cfg)
    # The window's end, no end, the same secret, no previous secret: the old token fails.
    for state, closed in (
        (pc_tokens.WINDOW_CLOSED, _cfg(JWT_SECRET_PREVIOUS=cfg.JWT_SECRET_PREVIOUS, JWT_ROTATION_GRACE_UNTIL=now - 1)),
        (pc_tokens.WINDOW_NO_END, _cfg(JWT_SECRET_PREVIOUS=cfg.JWT_SECRET_PREVIOUS)),
        (pc_tokens.WINDOW_OFF, _cfg()),
    ):
        assert pc_tokens.window_state(closed, now) == state
        with pytest.raises(jwt.InvalidSignatureError):
            pc_tokens.verify_pc_token(old, closed)
        assert pc_tokens.verify_pc_token(new, closed)[1] == pc_tokens.SECRET_CURRENT
    same = _cfg(JWT_SECRET_PREVIOUS=cfg.JWT_SECRET, JWT_ROTATION_GRACE_UNTIL=now + 60)
    assert pc_tokens.window_state(same, now) == pc_tokens.WINDOW_SAME
    # A datetime window end (as Settings parses it) works like Unix seconds.
    as_dt = _cfg(JWT_SECRET_PREVIOUS=cfg.JWT_SECRET_PREVIOUS,
                 JWT_ROTATION_GRACE_UNTIL=datetime.fromtimestamp(now + 3600, tz=timezone.utc))
    assert pc_tokens.grace_until(as_dt) == now + 3600 and pc_tokens.previous_secret_active(as_dt, now)
    assert not pc_tokens.previous_secret_active(as_dt, now + 3600)  # the end itself is outside

    # Through the relay: an old-secret token registers during the window and is flagged ...
    old_token = h.add_pc("PC_A", token=make_pc_token("PC_A", previous))
    _register(h, "pc-a", "PC_A", old_token, capabilities=None)
    assert relay._registered_pc("pc-a") == "PC_A", h.errors("pc-a")
    assert relay.session_data["pc-a"]["flags"][relay.FLAG_TOKEN_PREVIOUS_SECRET] is True
    current_token = h.add_pc("PC_B")
    _register(h, "pc-b", "PC_B", current_token)
    assert relay._registered_pc("pc-b") == "PC_B" and relay.session_data["pc-b"]["flags"] == {}
    # ... and is refused once the window has ended (no restart needed).
    h.monkeypatch.setattr(relay.settings, "JWT_ROTATION_GRACE_UNTIL", datetime.now(timezone.utc) - timedelta(seconds=1))
    with caplog.at_level(logging.WARNING):
        _register(h, "pc-a2", "PC_A", old_token, capabilities=None)
    assert relay._registered_pc("pc-a2") is None
    assert h.errors("pc-a2") == [relay.MSG_INVALID_TOKEN]
    assert "InvalidSignatureError" in caplog.text and old_token not in caplog.text
    _register(h, "pc-b2", "PC_B", current_token)
    assert relay._registered_pc("pc-b2") == "PC_B"


def test_settings_parse_the_window(monkeypatch):
    base = dict(SECRET_KEY="k" * 40, JWT_SECRET="j" * 20, DATABASE_URL="postgresql://u@127.0.0.1:1/x")
    s = Settings(**base)
    assert (s.JWT_SECRET_PREVIOUS, s.JWT_ROTATION_GRACE_UNTIL, s.PC_TOKEN_RENEW_BEFORE_DAYS) == (None, None, 30)
    for raw in ("2027-01-31T00:00:00", "2027-01-31T00:00:00Z", "2027-01-31T05:00:00+05:00", "1801353600"):
        until = Settings(**base, JWT_ROTATION_GRACE_UNTIL=raw).JWT_ROTATION_GRACE_UNTIL
        assert until.tzinfo is not None and int(until.timestamp()) == 1801353600, raw
    assert Settings(**base, JWT_ROTATION_GRACE_UNTIL="").JWT_ROTATION_GRACE_UNTIL is None
    assert Settings(**base, JWT_SECRET_PREVIOUS="  ").JWT_SECRET_PREVIOUS is None
    with pytest.raises(ValueError):
        Settings(**base, JWT_SECRET_PREVIOUS="short")
    with pytest.raises(ValueError):
        Settings(**base, PC_TOKEN_RENEW_BEFORE_DAYS=-1)


# --------------------------------------------------------------------------- #
# Renewal before expiry, exactly once
# --------------------------------------------------------------------------- #


def test_renew_before_expiry_once(h, caplog):
    stored = h.add_pc("PC_A", token=make_pc_token("PC_A", h.secret, exp_delta=10 * DAY))
    before = int(time.time())
    with caplog.at_level(logging.DEBUG):
        _register(h, "pc-a", "PC_A", stored)
    assert relay.session_data["pc-a"]["flags"] == {relay.FLAG_TOKEN_EXPIRING: True}
    pushes = _pushes(h)
    assert len(pushes) == 1
    data, target, callback = pushes[0]
    assert target == "pc-a" and set(data) == {"token", "exp"}
    new = data["token"]
    claims = _decode(new, h.secret)
    assert claims["type"] == "pc_auth" and claims["pc_id"] == "PC_A" and claims["exp"] == data["exp"]
    assert before + 8760 * 3600 <= data["exp"] <= int(time.time()) + 8760 * 3600
    row = h.pc_row("PC_A")
    assert row.auth_token == new and row.token_expiry == data["exp"]
    assert (row.updated_by_type, row.updated_by_label) == ("system", "system")
    rows = _rows(h)
    assert len(rows) == 1 and rows[0][2] == relay_auth.token_digest(stored) and rows[0][4] is None
    assert _audit_actions(h) == [pc_tokens.AUDIT_ISSUED]
    assert h.audits[0]["changes"] == {"renewal_id": rows[0][0], "reason": "expiring", "token_expiry": data["exp"]}
    assert h.audits[0]["actor"] == ("system", None, "system")
    # registered went out before the renewal.
    names = h.events_for("pc-a")
    assert names.index("registered") < names.index(pc_tokens.EVENT_RENEWED)

    # The ACK is lost; the PC reconnects with the old token: it is still current
    # (no token_outdated) and gets the same new token again, without a second mint.
    relay.disconnect("pc-a")
    _register(h, "pc-a2", "PC_A", stored)
    assert relay._registered_pc("pc-a2") == "PC_A"
    assert relay.FLAG_TOKEN_OUTDATED not in relay.session_data["pc-a2"]["flags"]
    again = _pushes(h, "pc-a2")
    assert len(again) == 1 and again[0][0] == {"token": new, "exp": data["exp"]}
    assert len(_rows(h)) == 1 and _audit_actions(h) == [pc_tokens.AUDIT_ISSUED]
    assert (relay.relay_counters["token_renewals_issued"], relay.relay_counters["token_renewals_resent"]) == (1, 1)

    # This time the PC stores it and ACKs: the renewal is confirmed and the flags clear.
    _ack(again[0][2], {"status": "stored"})
    rows = _rows(h)
    assert rows[0][4] is not None
    assert relay.session_data["pc-a2"]["flags"] == {}
    assert relay.health_buffer.peek("PC_A")["relay_flags"] == {}
    assert _audit_actions(h) == [pc_tokens.AUDIT_ISSUED, pc_tokens.AUDIT_CONFIRMED]
    assert h.audits[1]["changes"] == {"renewal_ids": [rows[0][0]], "via": "ack"}

    # Next register uses the new token: nothing to renew, no new row.
    relay.disconnect("pc-a2")
    _register(h, "pc-a3", "PC_A", new)
    assert relay.session_data["pc-a3"]["flags"] == {} and _pushes(h, "pc-a3") == []
    assert len(_rows(h)) == 1 and relay.relay_counters["token_renewals_issued"] == 1
    # The old token, now that the renewal is confirmed, is only an outdated token.
    relay.disconnect("pc-a3")
    _register(h, "pc-a4", "PC_A", stored)
    assert relay.session_data["pc-a4"]["flags"][relay.FLAG_TOKEN_OUTDATED] is True
    assert _pushes(h, "pc-a4") == []
    for token in (stored, new):
        assert token not in caplog.text
    assert rows[0][2] not in caplog.text


@pytest.mark.parametrize("days_left,days_setting,renewed", [
    (31, 30, False), (30, 30, True), (29, 30, True), (1, 30, True), (10, 0, False), (100, 120, True),
])
def test_renewal_threshold(h, monkeypatch, days_left, days_setting, renewed):
    monkeypatch.setattr(relay.settings, "PC_TOKEN_RENEW_BEFORE_DAYS", days_setting)
    # A token minted a few seconds ago that expires exactly ``days_left`` days from now.
    stored = h.add_pc("PC_A", token=make_pc_token("PC_A", h.secret, exp_delta=days_left * DAY - 2))
    _register(h, "pc-a", "PC_A", stored)
    assert bool(_pushes(h)) is renewed
    assert (relay.FLAG_TOKEN_EXPIRING in relay.session_data["pc-a"]["flags"]) is renewed


def test_register_with_the_new_token_confirms_the_renewal(h):
    stored = h.add_pc("PC_A", token=make_pc_token("PC_A", h.secret, exp_delta=5 * DAY))
    _register(h, "pc-a", "PC_A", stored)
    new = _pushes(h)[0][0]["token"]
    relay.disconnect("pc-a")  # no ACK at all
    _register(h, "pc-a2", "PC_A", new)
    rows = _rows(h)
    assert rows[0][4] is not None and _pushes(h, "pc-a2") == []
    assert h.audits[-1]["action"] == pc_tokens.AUDIT_CONFIRMED
    assert h.audits[-1]["changes"]["via"] == "register"
    assert relay.relay_counters["token_renewals_confirmed"] == 1


def test_replaced_token_counts_as_current_until_confirmed_also_with_require_current(h, monkeypatch):
    """OD-23 (b) accepts either token while the renewal is open."""
    monkeypatch.setattr(relay.settings, "RELAY_REQUIRE_CURRENT_TOKEN", True)
    stored = h.add_pc("PC_A", token=make_pc_token("PC_A", h.secret, exp_delta=5 * DAY))
    _register(h, "pc-a", "PC_A", stored)
    push = _pushes(h)[0]
    relay.disconnect("pc-a")
    _register(h, "pc-a2", "PC_A", stored)  # the replaced token still registers
    assert relay._registered_pc("pc-a2") == "PC_A", h.errors("pc-a2")
    _ack(_pushes(h, "pc-a2")[0][2], {"status": "stored"})
    relay.disconnect("pc-a2")
    _register(h, "pc-a3", "PC_A", stored)  # confirmed: the old token is refused now
    assert h.errors("pc-a3") == [relay.MSG_TOKEN_REPLACED]
    _register(h, "pc-a4", "PC_A", push[0]["token"])
    assert relay._registered_pc("pc-a4") == "PC_A"


# --------------------------------------------------------------------------- #
# Only to the registered sid
# --------------------------------------------------------------------------- #


def test_renewed_token_sent_only_to_registered_sid(h):
    h.connect_sender("sender-1")
    other = h.add_pc("PC_B")
    _register(h, "pc-b", "PC_B", other)
    stored = h.add_pc("PC_A", token=make_pc_token("PC_A", h.secret, exp_delta=3 * DAY))
    _register(h, "pc-a", "PC_A", stored)
    pushes = _pushes(h)
    assert [p[1] for p in pushes] == ["pc-a"]
    new = pushes[0][0]["token"]
    # Nobody else ever received the token: not the other PC, not the sender (presence list included).
    for name, data, target, _cb in h.emits:
        if new in json.dumps(data, default=str):
            assert (name, target) == (pc_tokens.EVENT_RENEWED, "pc-a")
    assert h.received("pc-b", pc_tokens.EVENT_RENEWED) == []
    assert h.received("sender-1", pc_tokens.EVENT_RENEWED) == []


def test_renewal_is_not_sent_to_a_replaced_session_and_is_resent_later(h, monkeypatch):
    tasks = []
    monkeypatch.setattr(relay, "_spawn", lambda fn, *args: tasks.append((fn, args)))
    stored = h.add_pc("PC_A", token=make_pc_token("PC_A", h.secret, exp_delta=3 * DAY))
    _register(h, "pc-a", "PC_A", stored)
    assert [fn.__name__ for fn, _ in tasks] == ["_renew_pc_token"]
    # Before the background task runs, another session takes over the PC (duplicate: deferred).
    _register(h, "pc-a2", "PC_A", stored)
    assert relay.session_data["pc-a2"]["flags"][relay.FLAG_DUPLICATE] is True
    assert len(tasks) == 1
    fn, args = tasks.pop()
    fn(*args)  # mints and stores, but pc-a is no longer the PC's session
    assert _pushes(h) == []
    assert len(_rows(h)) == 1 and relay.relay_counters["token_renewals_issued"] == 1
    # The next clean registration with the old token gets the stored new token.
    relay.disconnect("pc-a2")
    relay.disconnect("pc-a")
    _register(h, "pc-a3", "PC_A", stored)
    fn, args = tasks.pop()
    fn(*args)
    pushes = _pushes(h)
    assert [p[1] for p in pushes] == ["pc-a3"] and pushes[0][0]["token"] == h.pc_row("PC_A").auth_token
    assert len(_rows(h)) == 1 and relay.relay_counters["token_renewals_resent"] == 1


def test_renewal_deferred_without_a_database_check(h):
    stored = h.add_pc("PC_A", token=make_pc_token("PC_A", h.secret, exp_delta=3 * DAY))
    h.db_fails = True
    _register(h, "pc-a", "PC_A", stored)
    h.db_fails = False
    flags = relay.session_data["pc-a"]["flags"]
    assert flags[relay.FLAG_DB_UNVERIFIED] is True and relay.FLAG_TOKEN_DROP_NEEDED not in flags
    assert _pushes(h) == [] and _rows(h) == []


# --------------------------------------------------------------------------- #
# Who is never re-tokened by the relay
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("capabilities", [None, OLD_CLIENT_CAPS, ["pc_token.v2"]])
def test_client_without_the_handler_is_flagged_and_listed_not_renewed(rotation, capabilities, caplog):
    """A 1.1.x Client is unaffected during the window and is listed as needing a drop."""
    h, previous = rotation.h, rotation.previous
    stored = h.add_pc("PC_OLD", token=make_pc_token("PC_OLD", previous))
    with caplog.at_level(logging.WARNING):
        _register(h, "pc-old", "PC_OLD", stored, capabilities=capabilities)
    assert relay._registered_pc("pc-old") == "PC_OLD"
    assert h.received("pc-old", "registered")[0]["pc_id"] == "PC_OLD"
    assert relay.session_data["pc-old"]["flags"] == {relay.FLAG_TOKEN_PREVIOUS_SECRET: True,
                                                     relay.FLAG_TOKEN_DROP_NEEDED: True}
    assert _pushes(h) == [] and _rows(h) == [] and h.audits == []
    assert h.pc_row("PC_OLD").auth_token == stored
    assert "token_drop_needed" in caplog.text and "does not support pc_token.v1" in caplog.text
    assert relay.relay_counters["token_drop_needed"] == 1
    relay.flush_health_once()
    db = h.Session()
    try:
        assert db.get(PcHealth, "PC_OLD").relay_flags == {"token_previous_secret": True, "token_drop_needed": True}
        report = pc_tokens.fleet_report(db, relay.settings)
    finally:
        db.close()
    entry = next(p for p in report["pcs"] if p["pc_id"] == "PC_OLD")
    assert entry["status"] == pc_tokens.STATUS_NEEDS_DROP and "previous_secret" in entry["needs"]
    assert report["secrets"]["previous"] == 1 and report["window"] == "open"


def test_outdated_token_is_never_renewed(rotation):
    """Renewing an outdated token would launder a token minted with a leaked
    previous secret into a valid current-secret token."""
    h, previous = rotation.h, rotation.previous
    stored = h.add_pc("PC_A", token=make_pc_token("PC_A", previous))
    forged = make_pc_token("PC_A", previous, exp_delta=4000 * 3600)  # any holder of the old secret
    _register(h, "pc-x", "PC_A", forged)
    flags = relay.session_data["pc-x"]["flags"]
    assert flags[relay.FLAG_TOKEN_OUTDATED] and flags[relay.FLAG_TOKEN_DROP_NEEDED]
    assert flags[relay.FLAG_TOKEN_PREVIOUS_SECRET]
    assert _pushes(h) == [] and _rows(h) == []
    assert h.pc_row("PC_A").auth_token == stored
    # Even if a renewal were requested for it directly, issue_or_resume refuses.
    db = h.Session()
    try:
        assert pc_tokens.issue_or_resume(db, "PC_A", forged, pc_tokens.REASON_PREVIOUS_SECRET) is None
        assert pc_tokens.issue_or_resume(db, "PC_GONE", stored, pc_tokens.REASON_PREVIOUS_SECRET) is None
    finally:
        db.close()
    # A current-secret outdated token that expires soon is not renewed either.
    current_stored = h.add_pc("PC_B")
    _register(h, "pc-y", "PC_B", make_pc_token("PC_B", h.secret, exp_delta=2 * DAY))
    assert relay.session_data["pc-y"]["flags"][relay.FLAG_TOKEN_DROP_NEEDED] is True
    assert _pushes(h) == [] and h.pc_row("PC_B").auth_token == current_stored


def test_previous_secret_renewal_with_the_handler(rotation):
    """The rotation path: an old-secret stored token is re-issued with the new
    secret; the renewed token's expiry never goes backwards."""
    h, previous = rotation.h, rotation.previous
    stored = h.add_pc("PC_A", token=make_pc_token("PC_A", previous, exp_delta=9000 * 3600))
    _register(h, "pc-a", "PC_A", stored)
    data, target, callback = _pushes(h)[0]
    claims = _decode(data["token"], h.secret)  # signed with the new secret
    with pytest.raises(jwt.InvalidSignatureError):
        _decode(data["token"], previous)
    assert claims["exp"] > _decode(stored, previous)["exp"]  # min_exp: later than the token it replaces
    assert h.audits[0]["changes"]["reason"] == "previous_secret"
    _ack(callback, {"status": "stored"})
    assert relay.session_data["pc-a"]["flags"] == {}


# --------------------------------------------------------------------------- #
# ACK shapes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("ack,reason", [
    ({"status": "refused", "reason": "other_pc"}, "other_pc"),
    ({"status": "refused", "reason": "older_exp"}, "older_exp"),
    ({"status": "refused", "reason": "Bad Reason With eyJ.payload"}, "unspecified"),
    ({"status": "refused"}, "unspecified"),
])
def test_refused_ack_is_audited_and_leaves_the_renewal_open(h, ack, reason, caplog):
    stored = h.add_pc("PC_A", token=make_pc_token("PC_A", h.secret, exp_delta=3 * DAY))
    _register(h, "pc-a", "PC_A", stored)
    with caplog.at_level(logging.WARNING):
        _ack(_pushes(h)[0][2], ack)
    assert _rows(h)[0][4] is None
    assert h.audits[-1]["action"] == pc_tokens.AUDIT_REFUSED
    assert h.audits[-1]["changes"] == {"renewal_id": _rows(h)[0][0], "reason": reason}
    assert relay.session_data["pc-a"]["flags"] == {relay.FLAG_TOKEN_EXPIRING: True}
    assert f"refused token renewal {_rows(h)[0][0]} ({reason})" in caplog.text
    assert relay.relay_counters["token_renewals_refused"] == 1
    relay.disconnect("pc-a")
    _register(h, "pc-a2", "PC_A", stored)  # the old token keeps working
    assert relay._registered_pc("pc-a2") == "PC_A"


@pytest.mark.parametrize("ack", [None, True, "stored", {"status": "ok"}, {"stored": True}, []])
def test_ack_without_a_stored_status_does_not_confirm(h, ack):
    stored = h.add_pc("PC_A", token=make_pc_token("PC_A", h.secret, exp_delta=3 * DAY))
    _register(h, "pc-a", "PC_A", stored)
    callback = _pushes(h)[0][2]
    if ack is None:
        callback()
    else:
        callback(ack)
    assert _rows(h)[0][4] is None and _audit_actions(h) == [pc_tokens.AUDIT_ISSUED]
    assert relay.FLAG_TOKEN_EXPIRING in relay.session_data["pc-a"]["flags"]


def test_parse_renewal_ack():
    assert pc_tokens.parse_renewal_ack({"status": "stored"}) == ("stored", None)
    assert pc_tokens.parse_renewal_ack({"status": "refused", "reason": "wrong_type"}) == ("refused", "wrong_type")
    assert pc_tokens.parse_renewal_ack({"status": "refused", "reason": "x" * 33}) == ("refused", "unspecified")
    assert pc_tokens.parse_renewal_ack("stored") == (None, None)


# --------------------------------------------------------------------------- #
# Admin generate-token cancels open renewals
# --------------------------------------------------------------------------- #


def test_generate_token_cancels_open_renewals(h, monkeypatch):
    from app.api import deps
    from app.api.v1 import pcs as pcs_api
    from app.database import get_db

    stored = h.add_pc("PC_A", token=make_pc_token("PC_A", h.secret, exp_delta=3 * DAY))
    _register(h, "pc-a", "PC_A", stored)
    assert len(_rows(h)) == 1
    relay.disconnect("pc-a")
    monkeypatch.setattr("app.services.audit_service.record_update", lambda db, **kw: None)

    app = FastAPI()
    app.include_router(pcs_api.router, prefix="/pcs")
    admin = SimpleNamespace(role="admin", is_active=True, username="admin-1")

    def _db():
        session = h.Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[deps.get_admin_user] = lambda: admin
    response = TestClient(app).post("/pcs/PC_A/generate-token")
    assert response.status_code == 200, response.text
    issued = response.json()["auth_token"]
    assert _rows(h) == []
    cancel = [a for a in h.audits if a["action"] == pc_tokens.AUDIT_CANCELLED]
    assert len(cancel) == 1 and cancel[0]["changes"]["via"] == "generate_token"
    assert stored not in json.dumps(h.audits, default=str) and issued not in json.dumps(h.audits, default=str)
    # The token the renewal replaced is outdated now; the admin's token is current.
    _register(h, "pc-a2", "PC_A", stored)
    assert relay.session_data["pc-a2"]["flags"].get(relay.FLAG_TOKEN_OUTDATED) is True
    relay.disconnect("pc-a2")
    _register(h, "pc-a3", "PC_A", issued)
    assert relay.session_data["pc-a3"]["flags"] == {}


def test_create_pc_auth_token_expiry_is_utc():
    """The portal's token expiry is now + 8760 h whatever the host's time zone
    (a naive utcnow().timestamp() was hours off on a host not on UTC)."""
    from app.core.security import create_pc_auth_token

    token, exp = create_pc_auth_token("PC_A", "Wall A")
    expected = int(time.time()) + 8760 * 3600
    assert abs(exp - expected) <= 5
    assert _decode(token, relay.settings.JWT_SECRET)["exp"] == exp


def test_mint_pc_token_min_exp():
    cfg = _cfg()
    now = int(time.time())
    token, exp = pc_tokens.mint_pc_token("PC_A", "Wall A", cfg, now=now)
    assert exp == now + 8760 * 3600
    assert _decode(token, cfg.JWT_SECRET) == {"pc_id": "PC_A", "name": "Wall A", "exp": exp, "type": "pc_auth"}
    assert pc_tokens.mint_pc_token("PC_A", None, cfg, now=now, min_exp=exp + 50)[1] == exp + 50
    assert pc_tokens.mint_pc_token("PC_A", None, cfg, now=now, min_exp=exp - 50)[1] == exp


# --------------------------------------------------------------------------- #
# Fleet report and startup line
# --------------------------------------------------------------------------- #


def test_fleet_report_classifies_every_pc_without_tokens(rotation, monkeypatch, capsys):
    h, previous = rotation.h, rotation.previous
    now = int(time.time())
    tokens = {
        "PC_OK": make_pc_token("PC_OK", h.secret),
        "PC_PREV_NEW": make_pc_token("PC_PREV_NEW", previous),  # can take a renewal
        "PC_PREV_OLD": make_pc_token("PC_PREV_OLD", previous),  # cannot
        "PC_EXPIRING": make_pc_token("PC_EXPIRING", h.secret, exp_delta=5 * DAY),
        "PC_EXPIRED": make_pc_token("PC_EXPIRED", h.secret, exp_delta=-DAY),
        "PC_GARBAGE": "not-a-token",
        "PC_OTHER": make_pc_token("PC_SOMEONE_ELSE", h.secret),
        "PC_UNKNOWN": make_pc_token("PC_UNKNOWN", "fixture-third-" + "t" * 16),
        "PC_NONE": None,
        "PC_OPEN": make_pc_token("PC_OPEN", h.secret),
        "PC_OUTDATED": make_pc_token("PC_OUTDATED", h.secret),
    }
    for pc_id, token in tokens.items():
        h.add_pc(pc_id, token=token)
    db = h.Session()
    try:
        for pc_id, caps, flags in (("PC_PREV_NEW", CAPS, {}), ("PC_PREV_OLD", ["deploy_result.v1"], {}),
                                   ("PC_EXPIRING", CAPS, {}), ("PC_OUTDATED", CAPS, {"token_outdated": True})):
            db.add(PcHealth(pc_id=pc_id, capabilities=caps, relay_flags=flags))
        db.add(PcTokenRenewal(pc_id="PC_OPEN", previous_token_sha256="0" * 64, issued_at=now))
        db.commit()
        report = pc_tokens.fleet_report(db, relay.settings)
    finally:
        db.close()
    status = {p["pc_id"]: p["status"] for p in report["pcs"]}
    assert "PC_OK" not in status
    assert status == {
        "PC_PREV_NEW": "awaiting_reconnect", "PC_PREV_OLD": "needs_drop", "PC_EXPIRING": "awaiting_reconnect",
        "PC_EXPIRED": "needs_drop", "PC_GARBAGE": "needs_drop", "PC_OTHER": "needs_drop", "PC_UNKNOWN": "needs_drop",
        "PC_NONE": "no_token", "PC_OPEN": "renewal_unconfirmed", "PC_OUTDATED": "needs_drop",
    }
    assert report["counts"] == {"ok": 1, "renewal_unconfirmed": 1, "awaiting_reconnect": 2, "needs_drop": 6,
                                "no_token": 1}
    # unknown: the third-secret token and the garbage one.
    assert report["secrets"] == {"current": 6, "previous": 2, "unknown": 2, "none": 1}
    assert report["total"] == 11 and report["window"] == "open" and report["renew_before_days"] == 30
    text = json.dumps(report)
    for token in tokens.values():
        if token:
            assert token not in text
    assert "eyJ" not in text

    # The CLI prints the same report (JSON, no token).
    monkeypatch.setattr("app.database.SessionLocal", h.Session)
    assert pc_tokens.main(["report"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["counts"] == report["counts"] and "eyJ" not in json.dumps(printed)


def _startup_cfg(**overrides):
    values = dict(APP_VERSION="1.0.0", DEPLOY_INJECT_CONFIG_ID=True, RELAY_STATION_REPORTS=True,
                  DEPLOY_CATCHUP_ENABLED=False, DEPLOY_ACK_TIMEOUT_SEC=300, RELAY_DEBUG_LOGGING=False,
                  RELAY_REQUIRE_CURRENT_TOKEN=False, RELAY_SENDER_SECRET="s" * 43, SETTINGS_ENCRYPTION_KEY=None,
                  JWT_SECRET="j" * 32, CELERY_BROKER_URL="redis://127.0.0.1:1/0", JWT_SECRET_PREVIOUS=None,
                  JWT_ROTATION_GRACE_UNTIL=None, PC_TOKEN_RENEW_BEFORE_DAYS=30)
    values.update(overrides)
    return SimpleNamespace(**values)


def test_startup_line_reports_the_window_without_values(monkeypatch, caplog):
    monkeypatch.delenv("SETTINGS_ENCRYPTION_KEY", raising=False)
    previous = "fixture-previous-" + "p" * 20
    until = datetime(2027, 1, 31, tzinfo=timezone.utc)
    line = sc.collect("websocket", cfg=_startup_cfg()).log_line()
    assert "jwt_rotation_window=off" in line and "jwt_secret_previous=unset" in line
    assert "pc_token_renew_before_days=30" in line
    report = sc.collect("websocket", cfg=_startup_cfg(JWT_SECRET_PREVIOUS=previous, JWT_ROTATION_GRACE_UNTIL=until,
                                                      PC_TOKEN_RENEW_BEFORE_DAYS=14))
    line = report.log_line()
    if time.time() < until.timestamp():
        assert "jwt_rotation_window=open until 2027-01-31T00:00:00+00:00" in line
    assert f"jwt_secret_previous={sc.fingerprint(previous)}" in line and previous not in line
    assert "pc_token_renew_before_days=14" in line
    assert "jwt_rotation_window" not in report.problems
    # No end: the previous secret is not accepted, an ERROR.
    log = logging.getLogger("test.d30.startup")
    with caplog.at_level(logging.INFO, logger="test.d30.startup"):
        report = sc.run_startup_check("websocket", cfg=_startup_cfg(JWT_SECRET_PREVIOUS=previous), log=log)
    assert report.jwt_rotation_window == "no_end"
    assert any(r.levelno == logging.ERROR and "NOT accepted" in r.getMessage() for r in caplog.records)
    # The end has passed: a WARNING to remove the old secret.
    report = sc.collect("websocket", cfg=_startup_cfg(JWT_SECRET_PREVIOUS=previous,
                                                      JWT_ROTATION_GRACE_UNTIL=int(time.time()) - 60))
    assert report.jwt_rotation_window == "closed" and "jwt_rotation_window" in report.problems
    assert sc._SEVERITY["jwt_rotation_window"] == logging.WARNING
    # Rotating away from a leaked Client constant (G-1) is called out while the window is open.
    monkeypatch.setattr(sc, "CLIENT_JWT_CONSTANT_DIGESTS",
                        (__import__("hashlib").sha256(sc._G1_DOMAIN + previous.encode()).hexdigest(),))
    report = sc.collect("websocket", cfg=_startup_cfg(JWT_SECRET_PREVIOUS=previous,
                                                      JWT_ROTATION_GRACE_UNTIL=int(time.time()) + 600))
    assert "jwt_secret_previous" in report.problems and "never renewed" in report.problems["jwt_secret_previous"]
    assert previous not in json.dumps(report.as_dict())


def test_pc_token_feature_is_advertised_after_the_report_features(h):
    assert sp.FEATURE_PC_TOKEN not in relay.FEATURE_EVENTS  # a push: no relay handler needed
    relay.enable_feature(sp.FEATURE_DEPLOY_RESULT)
    relay.enable_feature(sp.FEATURE_STATION_STATUS)
    assert relay.advertised_features() == [sp.FEATURE_DEPLOY_RESULT, sp.FEATURE_STATION_STATUS, sp.FEATURE_PC_TOKEN]
    h.monkeypatch.setattr(relay.settings, "RELAY_STATION_REPORTS", False)
    assert relay.advertised_features() == [sp.FEATURE_PC_TOKEN, sp.FEATURE_STATION_REPORTS_DISABLED]


def test_the_relay_enables_pc_token_at_import():
    """Outside the harness (which starts from no features), the module's own
    feature set: D12's report features and D30's pc_token.v1."""
    assert {sp.FEATURE_DEPLOY_RESULT, sp.FEATURE_STATION_STATUS, sp.FEATURE_PC_TOKEN} <= relay._enabled_features
