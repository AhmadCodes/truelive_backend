"""
The stream provider engine end to end (LUM-SRV), with a fake clock, a fake
Lumana on 127.0.0.1, FakeRedis and SQLite (tests/stream_provider_testkit.py):
resolve at deploy, the cache, the deploy budget, renewal before expiry,
reconnect, shared sessions, the server-side check and failure reports,
backoff, the vendor down and back, the session limit, a refused key, two
providers of one type, a removed provider, the "layout changed" block, a lost
Redis, release, a second provider type, old Core, and masking.

    python -m pytest tests/unit/test_stream_providers_engine.py -o addopts='' -p no:cacheprovider
"""

import json
import logging
import time

import pytest

from app.models.pc import PC
from app.models.pc_tracking import PcDeployment
from app.models.stream_provider import StreamProvider
from app.services import deploy_service, deploy_tracking
from app.services.stream_providers import engine, registry
from app.services.stream_providers.base import UNAVAILABLE, ProviderError
from tests.deploy_testkit import camera_url
from tests.stream_provider_testkit import Rig, cell_urls, new_key, ref, register_fixed


@pytest.fixture
def rig(monkeypatch):
    r = Rig(monkeypatch)
    yield r
    r.close()
    registry.PROVIDER_TYPES.pop("fixed", None)


def _wall(rig, pc_id="PC_A", cams=(1, 2), provider_id=None, **pc):
    provider_id = provider_id or rig.add_provider()
    rig.add_pc(pc_id, **pc)
    refs = [ref(provider_id, n) for n in cams]
    rig.set_layout(pc_id, refs)
    return provider_id, refs


def _path(url):
    return url.split("?", 1)[0]


def _wait(predicate, seconds=10.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------------- #
# Deploy
# --------------------------------------------------------------------------- #


def test_resolve_at_deploy(rig):
    _pid, refs = _wall(rig)
    summary = rig.deploy("PC_A")
    result = summary["results"][0]
    assert result["status"] == "deployed" and result["provider_streams"] == {"total": 2, "unresolved": []}
    request = rig.station.deploys[0]
    assert request["track_ack"] is True
    urls = cell_urls(request["content"])
    assert urls["src-rtsp"] == camera_url()  # RTSP untouched
    for source_id in ("src-0", "src-1"):
        assert urls[source_id].startswith(rig.lumana.url + "/v2/file/") and "?token=" in urls[source_id]
    text = json.dumps(request["content"])
    assert "provider://" not in text and rig.key not in text  # stations never see the reference or the key
    uris = request["content"]["screens"][0]["source_groups"][0][2]["LocationUris"]
    assert [u["url"] for u in uris] == [urls["src-0"], urls["src-1"], camera_url()]
    assert request["content"]["config_id"] == result["config_id"]
    # The stored hash is the one of the unresolved layout (catch-up and the "layout changed" rule need it).
    with rig.db() as db:
        row = db.get(PcDeployment, result["config_id"])
        assert row.config_hash == deploy_tracking.config_hash(rig.layouts["PC_A"])
    record = engine._load("pc:PC_A")
    assert record["config_id"] == result["config_id"] and record["sent_id"] == result["config_id"]
    assert record["refs"] == {"src-0": refs[0], "src-1": refs[1]}
    assert "provider://" in json.dumps(record["layout"]) and "token=" not in json.dumps(record["layout"])
    assert rig.lumana.count("start") == 2 and rig.queued == []


def test_cache_hit_makes_no_request(rig):
    _pid, refs = _wall(rig)
    rig.deploy("PC_A")
    with rig.db() as db:
        again = engine.resolve_refs(db, refs, budget_sec=5)
    assert all(r.url for r in again.values()) and rig.lumana.count("start") == 2
    rig.deploy("PC_A")
    assert rig.lumana.count("start") == 2
    assert cell_urls(rig.station.deploys[0]["content"]) == cell_urls(rig.station.deploys[1]["content"])


def test_twenty_cameras_resolve_within_the_budget(rig):
    rig.lumana.start_delay = 1.0
    rig.lumana.limit = 100  # this test is about time, not Lumana's 10 sessions per edge
    _wall(rig, cams=range(1, 21))
    started = time.monotonic()
    result = rig.deploy("PC_A")["results"][0]
    elapsed = time.monotonic() - started
    assert result["provider_streams"] == {"total": 20, "unresolved": []}
    assert elapsed < rig.cfg["deploy_resolve_budget_sec"], f"20 cameras took {elapsed:.1f} s"
    assert rig.lumana.count("start") == 20


def test_slow_provider_does_not_block_the_deploy(rig):
    rig.lumana.start_delay = 2.5
    rig.cfg["deploy_resolve_budget_sec"] = 0.5
    _wall(rig)
    started = time.monotonic()
    result = rig.deploy("PC_A")["results"][0]
    assert time.monotonic() - started < 2.0
    assert result["status"] == "deployed"
    assert [u["code"] for u in result["provider_streams"]["unresolved"]] == ["pending", "pending"]
    assert "2 provider camera(s) are not available" in result["message"]
    urls = cell_urls(rig.station.deploys[0]["content"])
    assert urls["src-0"] == "http://127.0.0.1:9/stream-unavailable/pending" and urls["src-rtsp"] == camera_url()
    assert rig.queued == [(deploy_service.REFRESH_TASK, ["PC_A", "deploy"], 10)]
    # The mints finish in the background; the follow-up delivers them (not rate-limited).
    assert _wait(lambda: rig.lumana.count("start") == 2 and all(rig.addr(r) for r in engine.layout_refs(
        rig.layouts["PC_A"])))
    assert rig.refresh("PC_A", "deploy")["status"] == "delivered"
    urls = cell_urls(rig.station.last("PC_A"))
    assert "?token=" in urls["src-0"] and "?token=" in urls["src-1"]
    assert rig.station.last("PC_A")["config_id"] == result["config_id"]  # the same config_id: not a deploy
    assert rig.refresh("PC_A", "deploy")["status"] == "current"


def test_layout_without_references_touches_nothing(rig):
    rig.add_provider()
    rig.add_pc("PC_A")
    rig.set_layout("PC_A", [])
    result = rig.deploy("PC_A")["results"][0]
    assert result["status"] == "deployed" and "provider_streams" not in result
    assert "track_ack" not in rig.station.deploys[0]
    assert rig.station.deploys[0]["content"]["screens"] == rig.layouts["PC_A"]["screens"]
    assert not rig.fake_redis.touched() and rig.lumana.log == []


def test_renewals_are_not_deploys(rig):
    _wall(rig)
    rig.deploy("PC_A")
    rig.clock.advance(9001)
    assert rig.tick()["delivered"] == 1
    with rig.db() as db:
        assert db.query(PcDeployment).count() == 1  # no deploy row for the renewal
    assert set(rig.station.refreshes[0]) == {"refresh_id", "targetId", "content"}


# --------------------------------------------------------------------------- #
# Renewal, reconnect, sharing
# --------------------------------------------------------------------------- #


def test_renewal_before_expiry(rig):
    _pid, refs = _wall(rig)
    rig.deploy("PC_A")
    first = cell_urls(rig.station.last("PC_A"))
    assert rig.addr(refs[0])["renew_at"] == rig.clock.t + 9000  # half of the 5 hours in the token
    rig.clock.advance(8990)
    out = rig.tick()
    assert (out["renewed"], out["delivered"]) == (0, 0) and rig.lumana.count("start") == 2
    rig.clock.advance(20)
    out = rig.tick()
    # Every in-use address of the provider is renewed together: one delivery for the station.
    assert (out["renewed"], out["delivered"]) == (2, 1) and len(rig.station.refreshes) == 1
    renewed = cell_urls(rig.station.last("PC_A"))
    for source_id in ("src-0", "src-1"):
        assert _path(renewed[source_id]) == _path(first[source_id]) and renewed[source_id] != first[source_id]
    assert renewed["src-rtsp"] == camera_url()
    starts = [entry for entry in rig.lumana.log if entry[0] == "start"]
    assert len(starts) == 4 and all(entry[2] for entry in starts[2:])  # renewed with the stored session id
    assert rig.lumana.open_sessions() == 2
    assert rig.addr(refs[0])["expires_at"] == rig.clock.t + 18000
    # Acknowledged and unchanged: the next ticks send nothing.
    rig.clock.advance(120)
    assert rig.tick()["delivered"] == 0 and len(rig.station.refreshes) == 1


def test_renewal_never_later_than_the_margin(rig):
    rig.cfg["renew_fraction"] = 0.95
    _pid, refs = _wall(rig)
    rig.deploy("PC_A")
    assert rig.addr(refs[0])["renew_at"] == rig.clock.t + 18000 - 1800


def test_reconnect(rig):
    _wall(rig)
    rig.deploy("PC_A")
    # Unchanged and acknowledged: nothing is sent.
    assert rig.refresh("PC_A", "connect")["status"] == "current" and rig.station.refreshes == []
    # Renewed while the station was away: the tick finds it offline, the reconnect delivers once.
    rig.station.offline.add("PC_A")
    rig.clock.advance(9001)
    assert rig.tick()["delivered"] == 0
    rig.station.offline.clear()
    assert rig.refresh("PC_A", "connect")["status"] == "delivered"
    assert rig.refresh("PC_A", "connect")["status"] == "current" and len(rig.station.refreshes) == 1


def test_station_that_never_acknowledges_is_not_flooded(rig):
    _wall(rig)
    rig.station.silent.add("PC_A")
    rig.deploy("PC_A")
    statuses = []
    for _ in range(6):
        rig.clock.advance(61)
        statuses.append(rig.refresh("PC_A", "tick")["status"])
    assert statuses == ["delivered", "delivered", "unacknowledged", "unacknowledged", "unacknowledged",
                        "unacknowledged"]
    with rig.db() as db:
        assert engine.status_for_pc(db, "PC_A")["acknowledged"] is False


def test_automatic_deliveries_are_rate_limited_per_station(rig):
    _wall(rig)
    rig.deploy("PC_A")
    rig.redis.delete(engine.PREFIX + "ack:PC_A")
    rig.clock.advance(30)
    assert rig.refresh("PC_A", "tick")["status"] == "rate_limited"
    rig.clock.advance(31)
    assert rig.refresh("PC_A", "tick")["status"] == "delivered"


def test_two_stations_share_one_session(rig):
    provider_id, _refs = _wall(rig, "PC_A", cams=(1,))
    _wall(rig, "PC_B", cams=(1,), provider_id=provider_id)
    rig.deploy("PC_A", "PC_B")
    assert rig.lumana.count("start") == 1 and rig.lumana.open_sessions() == 1
    assert cell_urls(rig.station.last("PC_A"))["src-0"] == cell_urls(rig.station.last("PC_B"))["src-0"]
    rig.clock.advance(9001)
    out = rig.tick()
    assert (out["renewed"], out["delivered"]) == (1, 2) and rig.lumana.count("start") == 2
    assert rig.lumana.open_sessions() == 1
    assert cell_urls(rig.station.last("PC_A"))["src-0"] == cell_urls(rig.station.last("PC_B"))["src-0"]


# --------------------------------------------------------------------------- #
# Failures
# --------------------------------------------------------------------------- #


def test_closed_session_is_revived_without_a_delivery(rig):
    _pid, refs = _wall(rig, cams=(1,))
    rig.deploy("PC_A")
    url = rig.addr(refs[0])["url"]
    rig.clock.advance(61)
    assert rig.tick()["probed"] == 1 and rig.lumana.count("start") == 1  # healthy
    rig.lumana.close_session()
    rig.clock.advance(61)
    rig.tick()
    assert rig.lumana.count("start") == 1  # one failure is not enough
    rig.clock.advance(30)
    assert rig.tick()["probed"] == 0  # checked at most once per watch interval
    rig.clock.advance(31)
    out = rig.tick()
    # Two failures in a row: a start with the kept session id reopens it at the same address,
    # the address the station plays stays valid, so nothing is delivered.
    assert rig.lumana.count("start") == 2 and rig.lumana.open_sessions() == 1
    assert out["delivered"] == 0 and rig.station.refreshes == []
    assert rig.addr(refs[0])["url"] == url


def test_watch_interval_zero_turns_the_check_off(rig):
    rig.cfg["watch_interval_sec"] = 0
    _wall(rig, cams=(1,))
    rig.deploy("PC_A")
    rig.clock.advance(120)
    assert rig.tick()["probed"] == 0 and rig.lumana.count("playlist") == 0


def test_failure_report_trigger(rig):
    _pid, refs = _wall(rig)
    rig.deploy("PC_A")
    rig.lumana.close_session()
    with rig.db() as db:
        # A failed RTSP cell, or an unknown id, is no reason to ask a provider.
        assert engine.station_failed(db, "PC_A", ["src-rtsp", "nope"], rig.sender)["status"] == "no_provider_cameras"
        assert rig.lumana.count("start") == 2
        out = engine.station_failed(db, "PC_A", ["src-0"], rig.sender)
    # Only the reported camera is checked, at once, and revived in place.
    assert out["status"] == "current" and rig.lumana.count("start") == 3
    assert [entry[1] for entry in rig.lumana.log if entry[0] == "start"][-1] == "cam1"


def test_broken_camera_cannot_cause_a_storm(rig):
    _pid, refs = _wall(rig, cams=(1,))
    rig.deploy("PC_A")
    rig.lumana.close_session()
    rig.lumana.down = True
    for _ in range(5):
        with rig.db() as db:
            engine.station_failed(db, "PC_A", ["src-0"], rig.sender)
    assert rig.lumana.count("start") == 2  # one attempt, then the backoff holds
    assert rig.addr(refs[0])["next_try_at"] == rig.clock.t + 30


def test_backoff_sequence(rig):
    provider_id = rig.add_provider()
    rig.lumana.down = True
    target = ref(provider_id, 1)
    waits = []
    for _ in range(8):
        with rig.db() as db:
            result = engine.resolve_refs(db, [target], budget_sec=5)[target]
        assert result.url is None and result.code == "provider-unavailable"
        record = rig.addr(target)
        waits.append(record["next_try_at"] - rig.clock.t)
        with rig.db() as db:  # inside the backoff: no request
            count = rig.lumana.count("start")
            assert engine.resolve_refs(db, [target], budget_sec=5)[target].code == "provider-unavailable"
            assert rig.lumana.count("start") == count
        rig.clock.advance(waits[-1])
    assert waits == [30, 60, 120, 240, 480, 900, 900, 900]
    rig.lumana.down = False
    with rig.db() as db:
        assert engine.resolve_refs(db, [target], budget_sec=5)[target].url
    assert rig.addr(target)["failures"] == 0 and rig.addr(target)["last_error"] is None


def test_lumana_down_then_back(rig):
    provider_id, refs = _wall(rig)
    rig.cfg["watch_interval_sec"] = 0
    rig.deploy("PC_A")
    before = cell_urls(rig.station.last("PC_A"))
    rig.lumana.down = True
    rig.clock.advance(9001)
    out = rig.tick()
    assert out["delivered"] == 0  # the cached addresses stay in use
    with rig.db() as db:
        assert engine.resolve_refs(db, refs)[refs[0]].url == before["src-0"]
        status = engine.status_for_cameras(refs)[refs[0]]
        assert (status["state"], status["message"]) == ("error", "Lumana answered HTTP 502")
        assert status["valid_until"] == rig.clock.t - 9001 + 18000
        assert engine.provider_status(provider_id)["state"] == "error"
    # 30 minutes before the expiry with no fresh address: "expiring".
    rig.clock.advance(18000 - 9001 - 1700)
    assert engine.status_for_cameras(refs)[refs[0]]["state"] == "expiring"
    assert engine.provider_status(provider_id)["state"] == "expiring"
    rig.lumana.down = False
    out = rig.tick()
    assert (out["renewed"], out["delivered"]) == (2, 1)
    after = cell_urls(rig.station.last("PC_A"))
    assert _path(after["src-0"]) == _path(before["src-0"]) and after["src-0"] != before["src-0"]
    assert engine.status_for_cameras(refs)[refs[0]]["state"] == "ok"
    assert engine.provider_status(provider_id)["state"] == "ok"


def test_expired_address_with_the_vendor_down_fails_the_cell_alone(rig):
    _pid, refs = _wall(rig, cams=(1,))
    rig.cfg["watch_interval_sec"] = 0
    rig.deploy("PC_A")
    rig.lumana.down = True
    rig.clock.advance(18001)
    rig.tick()
    urls = cell_urls(rig.station.last("PC_A"))
    assert urls["src-0"] == "http://127.0.0.1:9/stream-unavailable/provider-unavailable"
    assert urls["src-rtsp"] == camera_url()


def test_limit_reached(rig):
    provider_id, _refs = _wall(rig, cams=(1, 2))
    rig.lumana.extra_sessions = 8
    rig.lumana.limit = 9
    rig.set_layout("PC_A", [ref(provider_id, 1)])
    assert rig.deploy("PC_A")["results"][0]["provider_streams"]["unresolved"] == []  # the ninth session
    assert engine.provider_status(provider_id)["sessions"] == 9
    rig.set_layout("PC_A", [ref(provider_id, 1), ref(provider_id, 2)])
    result = rig.deploy("PC_A")["results"][0]
    assert result["status"] == "deployed"  # the rest of the layout deploys
    unresolved = result["provider_streams"]["unresolved"]
    assert [(u["camera_id"], u["code"]) for u in unresolved] == [("CAM_1", "limit-reached")]
    assert unresolved[0]["message"] == ("Lumana main: Lumana refused a new session; the limit of 10 live sessions "
                                        "per edge is probably reached")
    urls = cell_urls(rig.station.last("PC_A"))
    assert "?token=" in urls["src-0"] and urls["src-1"].endswith("/stream-unavailable/limit-reached")
    with rig.db() as db:
        status = engine.status_for_pc(db, "PC_A")
    assert [(u["camera_name"], u["code"]) for u in status["unresolved"]] == [("Camera 1", "limit-reached")]


def test_bad_key(rig):
    provider_id, refs = _wall(rig, provider_id=rig.add_provider(key="wrong-" + new_key()))
    result = rig.deploy("PC_A")["results"][0]
    assert result["status"] == "deployed"
    assert {u["code"] for u in result["provider_streams"]["unresolved"]} == {"key-refused"}
    assert result["message"].endswith("2 provider camera(s) are not available: Lumana main: Lumana refused the "
                                      "API key")
    urls = cell_urls(rig.station.last("PC_A"))
    assert urls["src-0"] == "http://127.0.0.1:9/stream-unavailable/key-refused" and urls["src-rtsp"] == camera_url()
    status = engine.status_for_cameras(refs)[refs[0]]
    assert (status["state"], status["code"], status["message"]) == ("error", "key-refused",
                                                                    "Lumana refused the API key")
    assert engine.provider_status(provider_id)["message"] == "Lumana refused the API key"
    with rig.db() as db:
        with pytest.raises(ProviderError) as error:
            engine.check_provider(db, provider_id)
    assert error.value.code == "auth"


def test_two_providers_of_one_type_use_their_own_keys(rig):
    other_key = new_key()
    rig.lumana.keys.add(other_key)
    first = rig.add_provider("Account one")
    second = rig.add_provider("Account two", key=other_key)
    rig.add_pc("PC_A")
    rig.set_layout("PC_A", [ref(first, 1), ref(second, 2)])
    assert rig.deploy("PC_A")["results"][0]["provider_streams"]["unresolved"] == []
    used = {entry[1]: entry[3] for entry in rig.lumana.log if entry[0] == "start"}
    assert used == {"cam1": rig.key, "cam2": other_key}
    # Renewal is per provider: only the one that is due is renewed.
    record = rig.addr(ref(second, 2))
    record["renew_at"] = rig.clock.t + 50
    engine._save(engine._ref_key("addr", ref(second, 2)), record)
    rig.clock.advance(61)
    assert rig.tick()["renewed"] == 1
    assert [entry[3] for entry in rig.lumana.log if entry[0] == "start"][-1] == other_key


def test_provider_removed(rig):
    provider_id, refs = _wall(rig)
    rig.deploy("PC_A")
    with rig.db() as db:
        db.query(StreamProvider).filter(StreamProvider.id == provider_id).delete()
        db.commit()
        assert engine.resolve_refs(db, refs)[refs[0]] == engine.Resolved(
            None, "provider-removed", "The stream provider of this camera was removed.")
    rig.clock.advance(61)
    assert rig.tick()["delivered"] == 1
    urls = cell_urls(rig.station.last("PC_A"))
    assert urls["src-0"].endswith("/stream-unavailable/provider-removed") and urls["src-rtsp"] == camera_url()
    with rig.db() as db:
        assert {u["code"] for u in engine.status_for_pc(db, "PC_A")["unresolved"]} == {"provider-removed"}


def test_unreadable_provider_key_is_an_operator_message(rig):
    provider_id, refs = _wall(rig)
    with rig.db() as db:
        db.get(StreamProvider, provider_id).secrets = "not-a-ciphertext"
        db.commit()
        result = engine.resolve_refs(db, refs)[refs[0]]
    assert result.code == "provider-unavailable" and "SETTINGS_ENCRYPTION_KEY" in result.message
    assert rig.lumana.log == []


# --------------------------------------------------------------------------- #
# The PC record: lost, stale, blocked
# --------------------------------------------------------------------------- #


def test_layout_changed_since_the_deploy_blocks(rig):
    provider_id, _refs = _wall(rig)
    rig.deploy("PC_A")
    rig.redis.delete(engine.PREFIX + "pc:PC_A")  # the record is lost ...
    rig.set_layout("PC_A", [ref(provider_id, 1), ref(provider_id, 3)], title="edited, not deployed")
    rig.clock.advance(9001)
    out = rig.tick()
    # ... and the layout in the database is no longer what the station runs: nothing is sent.
    assert out["delivered"] == 0 and rig.station.refreshes == []
    assert rig.refresh("PC_A", "connect")["status"] == "blocked"
    with rig.db() as db:
        status = engine.status_for_pc(db, "PC_A")
    assert status["blocked"] == ("The layout changed since the last deploy; deploy to this PC to keep its provider "
                                 "cameras renewed.")
    # A deploy clears it.
    rig.deploy("PC_A")
    with rig.db() as db:
        assert engine.status_for_pc(db, "PC_A")["blocked"] is None
    assert rig.refresh("PC_A", "connect")["status"] == "current"


def test_redis_emptied_then_rebuilt(rig):
    _pid, refs = _wall(rig)
    config_id = rig.deploy("PC_A")["results"][0]["config_id"]
    old = cell_urls(rig.station.last("PC_A"))
    rig.add_camera("CAM_0", refs[0])  # with no key and no provider camera at all the tick is idle
    rig.redis.flushall()
    rig.clock.advance(61)
    out = rig.tick()
    # Rebuilt from the database (the layout still hashes to the deploy's): new sessions, one delivery.
    assert (out["stations"], out["delivered"]) == (1, 1)
    content = rig.station.last("PC_A")
    assert content["config_id"] == config_id and "provider://" not in json.dumps(content)
    new = cell_urls(content)
    assert "?token=" in new["src-0"] and _path(new["src-0"]) != _path(old["src-0"])
    assert new["src-rtsp"] == camera_url()
    rig.clock.advance(61)
    assert rig.tick()["delivered"] == 0


def test_stale_record_of_an_older_deploy_is_never_sent(rig):
    provider_id, _refs = _wall(rig)
    rig.deploy("PC_A")
    rig.set_layout("PC_A", [])  # the PC is re-deployed without provider cameras (no Redis call at deploy)
    rig.deploy("PC_A")
    rig.clock.advance(9001)
    out = rig.tick()
    assert (out["stations"], out["delivered"]) == (0, 0) and rig.station.refreshes == []
    assert engine._load("pc:PC_A")["none"] is True
    with rig.db() as db:
        assert engine.status_for_pc(db, "PC_A") is None


def test_deploy_that_never_reached_the_station_is_not_refreshed(rig):
    _wall(rig)
    rig.station.offline.add("PC_A")
    assert rig.deploy("PC_A")["results"][0]["delivery"] == "offline"
    assert engine._load("pc:PC_A") is None
    rig.station.offline.clear()
    assert rig.refresh("PC_A", "connect")["status"] == "no_deploy" and rig.station.refreshes == []


def test_newer_deploy_that_missed_the_station_does_not_stop_renewals(rig):
    provider_id, _refs = _wall(rig)
    first = rig.deploy("PC_A")["results"][0]["config_id"]
    # An operator deploys an edited layout while the station is offline: it keeps running the first one.
    rig.station.offline.add("PC_A")
    rig.clock.advance(5)
    assert rig.deploy("PC_A")["results"][0]["delivery"] == "offline"
    rig.station.offline.clear()
    rig.clock.advance(9001)
    out = rig.tick()
    # What it runs is renewed, under the config_id it runs.
    assert (out["renewed"], out["delivered"]) == (2, 1)
    assert rig.station.last("PC_A")["config_id"] == first
    assert rig.lumana.count("start") == 4


def test_release_after_five_minutes_unused(rig):
    _pid, refs = _wall(rig)
    rig.deploy("PC_A")
    rig.connected.clear()  # the station went away
    rig.clock.advance(299)
    assert rig.tick()["released"] == 0 and rig.lumana.open_sessions() == 2
    rig.clock.advance(2)
    assert rig.tick()["released"] == 2
    assert rig.lumana.count("stop") == 2 and rig.lumana.open_sessions() == 0
    assert rig.addr(refs[0]) is None and rig.redis.keys(engine.PREFIX + "addr:*") == []
    # Back again: fresh sessions, delivered.
    rig.connected.append("PC_A")
    assert rig.tick()["delivered"] == 1 and rig.lumana.open_sessions() == 2


def test_tick_is_idle_without_provider_cameras(rig):
    rig.add_pc("PC_A")
    rig.set_layout("PC_A", [])
    rig.deploy("PC_A")
    assert rig.tick() == {"status": "idle"}


def test_tick_skips_when_the_relay_cannot_be_asked(rig, monkeypatch):
    _wall(rig)
    rig.deploy("PC_A")
    monkeypatch.setattr(engine, "connected_pcs", lambda: None)
    rig.clock.advance(9001)
    assert rig.tick() == {"status": "skipped", "reason": "relay"} and rig.lumana.count("start") == 2


def test_two_ticks_never_overlap(rig):
    _wall(rig)
    rig.deploy("PC_A")
    rig.redis.set(engine.PREFIX + "tick", "1", ex=60)
    assert rig.tick() == {"status": "busy"}
    rig.redis.delete(engine.PREFIX + "tick")
    assert rig.tick()["status"] == "ok" and not rig.redis.exists(engine.PREFIX + "tick")


def test_kill_switch(rig, monkeypatch):
    _wall(rig)
    monkeypatch.setattr(engine.settings, "STREAM_PROVIDERS_ENABLED", False)
    result = rig.deploy("PC_A")["results"][0]
    assert {u["code"] for u in result["provider_streams"]["unresolved"]} == {"provider-unavailable"}
    assert rig.tick() == {"status": "disabled"} and rig.lumana.log == []


# --------------------------------------------------------------------------- #
# Old Core
# --------------------------------------------------------------------------- #


def test_old_core_gets_the_placeholder_and_the_reason(rig):
    _pid, refs = _wall(rig, hls_capable=None)
    result = rig.deploy("PC_A")["results"][0]
    assert result["status"] == "deployed"
    assert {u["code"] for u in result["provider_streams"]["unresolved"]} == {"core-too-old"}
    assert "not confirmed to run TrueLive Live 1.3.0 or later" in result["message"]
    content = rig.station.last("PC_A")
    urls = cell_urls(content)
    assert urls["src-0"] == "http://127.0.0.1:9/stream-unavailable/core-too-old" and urls["src-rtsp"] == camera_url()
    assert "token=" not in json.dumps(content) and rig.lumana.log == []  # no session is opened for it
    rig.clock.advance(61)
    out = rig.tick()
    assert (out["in_use"], out["delivered"]) == (0, 0) and rig.lumana.log == []
    with rig.db() as db:
        assert {u["code"] for u in engine.status_for_pc(db, "PC_A")["unresolved"]} == {"core-too-old"}
        # The admin confirms the station runs Core 1.3.0: the next tick delivers real addresses.
        db.get(PC, "PC_A").hls_capable = True
        db.commit()
    assert rig.tick()["delivered"] == 1
    assert "?token=" in cell_urls(rig.station.last("PC_A"))["src-0"]


def test_reported_core_version_wins_over_the_flag(rig):
    _wall(rig, hls_capable=True, core_running_version="1.2.0")
    result = rig.deploy("PC_A")["results"][0]
    assert {u["code"] for u in result["provider_streams"]["unresolved"]} == {"core-too-old"}
    _wall(rig, "PC_B", provider_id=engine.parse_provider_ref(engine.layout_refs(rig.layouts["PC_A"])[0])[0],
          hls_capable=None, core_running_version="1.3.1")
    assert rig.deploy("PC_B")["results"][0]["provider_streams"]["unresolved"] == []


# --------------------------------------------------------------------------- #
# A second provider type, test-only: the engine knows no vendor
# --------------------------------------------------------------------------- #


def test_fixed_type_runs_through_the_same_engine(rig):
    fixed = register_fixed()
    provider_id = rig.add_provider("Fixed one", type_id="fixed", settings={"host": "https://fixed.example"},
                                   secrets_={})
    _wall(rig, provider_id=provider_id)
    result = rig.deploy("PC_A")["results"][0]
    assert result["provider_streams"] == {"total": 2, "unresolved": []}
    urls = cell_urls(rig.station.last("PC_A"))
    assert urls["src-0"].startswith("https://fixed.example/fixed/cam1.m3u8?n=")
    target = ref(provider_id, 1)
    assert rig.addr(target)["expires_at"] == rig.clock.t + 600 and rig.addr(target)["handle"] == {}
    assert rig.addr(target)["renew_at"] == rig.clock.t + 300  # a different expiry, the same rule
    rig.clock.advance(299)
    assert rig.tick()["renewed"] == 0
    rig.clock.advance(2)
    out = rig.tick()
    assert (out["renewed"], out["delivered"]) == (2, 1) and len(fixed.mints) == 4
    assert cell_urls(rig.station.last("PC_A"))["src-0"] != urls["src-0"]
    # Its errors reach the operator the same way.
    fixed.fail = ProviderError(UNAVAILABLE, "Fixed is on holiday")
    rig.clock.advance(301)
    rig.tick()
    assert engine.status_for_cameras([target])[target]["message"] == "Fixed is on holiday"
    # No session concept: release is a no-op and the address is simply dropped.
    fixed.fail = None
    rig.connected.clear()
    rig.clock.advance(400)
    assert rig.tick()["released"] == 2 and rig.lumana.log == []


def test_unknown_provider_type_is_reported(rig):
    provider_id, refs = _wall(rig, provider_id=None)
    with rig.db() as db:
        db.get(StreamProvider, provider_id).type = "gone"
        db.commit()
        result = engine.resolve_refs(db, refs)[refs[0]]
        assert engine.load_provider(db, provider_id) is None
    assert result.code == "provider-unavailable" and "not installed" in result.message


# --------------------------------------------------------------------------- #
# Snapshots and masking
# --------------------------------------------------------------------------- #


def test_peek_never_mints(rig):
    _pid, refs = _wall(rig, cams=(1,))
    assert engine.peek(refs[0]) is None and rig.lumana.log == []
    rig.deploy("PC_A")
    assert engine.peek(refs[0]) == rig.addr(refs[0])["url"] and rig.lumana.count("start") == 1
    rig.clock.advance(18000)
    assert engine.peek(refs[0]) is None  # expired
    assert engine.peek("rtsp://h/s") is None


def test_nothing_secret_is_logged_or_stored_in_clear(rig, caplog):
    caplog.set_level(logging.DEBUG)
    _pid, refs = _wall(rig)
    rig.deploy("PC_A")
    rig.clock.advance(9001)
    rig.tick()
    rig.lumana.close_session()
    with rig.db() as db:
        engine.station_failed(db, "PC_A", ["src-0"], rig.sender)
    rig.lumana.down = True
    rig.clock.advance(9001)
    rig.tick()
    rig.connected.clear()
    rig.lumana.down = False
    rig.clock.advance(400)
    rig.tick()
    tokens = list(rig.lumana.tokens)
    assert len(tokens) >= 4 and rig.lumana.count("playlist") >= 2
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "Stream addresses for PC PC_A" in logged and "Lumana answered HTTP 502" in logged
    assert rig.key not in logged and "token=" not in logged and "/v2/file/" not in logged
    assert not any(token in logged for token in tokens)
    # Redis holds addresses and layouts only encrypted.
    rig.connected.append("PC_A")
    rig.tick()
    stored = b"\n".join(value for value, _ttl in rig.fake_redis.data.values())
    assert rig.redis.keys(engine.PREFIX + "addr:*") and rig.redis.keys(engine.PREFIX + "pc:*")
    for secret in [rig.key, "token=", "/v2/file/", "fixture-pass", "provider://"] + list(rig.lumana.tokens):
        assert secret.encode() not in stored
    # The status the portal gets holds no address either.
    with rig.db() as db:
        shown = json.dumps([engine.status_for_cameras(refs), engine.status_for_pc(db, "PC_A"),
                            engine.provider_status(_pid)])
    assert "token" not in shown and "/v2/file/" not in shown and rig.key not in shown
