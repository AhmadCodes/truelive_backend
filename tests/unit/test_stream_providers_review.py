"""
Regression tests of the LUM review findings (stream provider engine), on the
rig of tests/stream_provider_testkit.py: a camera whose address is minted but
does not play, a deploy that lands while a refresh is under way, two
refreshes at once, a station that only lost its link to the relay, cameras
that are merely listed in LocationUris, the unresolved code in the
placeholder, a probe that raises, lock ownership, the request timeout bound,
the live view's "in use" note, and the deploy answer.

    python -m pytest tests/unit/test_stream_providers_review.py -o addopts='' -p no:cacheprovider
"""

import json
import logging
import threading

import pytest

from app.schemas.deploy import DeploySummary
from app.services import deploy_service
from app.services.stream_providers import engine, registry
from tests.stream_provider_testkit import Rig, cell_urls, new_key, ref, register_fixed

REAL_CONFIG = engine.config  # the rig replaces engine.config


@pytest.fixture
def rig(monkeypatch):
    r = Rig(monkeypatch)
    yield r
    r.close()
    registry.PROVIDER_TYPES.pop("fixed", None)


def _wall(rig, pc_id="PC_A", cams=(1, 2), provider_id=None):
    provider_id = provider_id or rig.add_provider()
    rig.add_pc(pc_id)
    refs = [ref(provider_id, n) for n in cams]
    rig.set_layout(pc_id, refs)
    return provider_id, refs


def _path(url):
    return url.split("?", 1)[0]


def _starts(rig, camera):
    return sum(1 for entry in rig.lumana.log if entry[0] == "start" and entry[1] == camera)


def _dead_playlist(rig, monkeypatch, camera, dead):
    """The vendor still answers a start with 200, but the camera's playlist
    does not answer while ``dead["on"]`` (camera or edge down at the site)."""
    real = rig.lumana._playlist

    def playlist(handler, path, query):
        if dead["on"] and f"/{camera}/" in path:
            return rig.lumana._send(handler, 500, b"Failed to stream file...", "text/plain")
        return real(handler, path, query)

    monkeypatch.setattr(rig.lumana, "_playlist", playlist)


# --------------------------------------------------------------------------- #
# A camera that is minted but does not play
# --------------------------------------------------------------------------- #


def test_camera_that_mints_but_does_not_play_is_backed_off_and_never_delivered(rig, monkeypatch):
    _pid, refs = _wall(rig)
    rig.deploy("PC_A")
    url = rig.addr(refs[0])["url"]
    dead = {"on": True}
    _dead_playlist(rig, monkeypatch, "cam1", dead)
    before = _starts(rig, "cam1")
    for _ in range(120):  # one hour of ticks, default settings, no failure report
        rig.clock.advance(30)
        rig.tick()
    # Backoff 30 s, doubling to 15 minutes: a handful of starts, not one every 90 s (it was 40).
    assert 2 <= _starts(rig, "cam1") - before <= 8
    assert rig.station.refreshes == []  # the station keeps its address: no delivery, no view rebuild
    rec = rig.addr(refs[0])
    assert rec["url"] == url and rec["failures"] >= 2 and rec["next_try_at"] > rig.clock.t
    status = engine.status_for_cameras(refs)
    assert (status[refs[0]]["state"], status[refs[0]]["message"]) == ("error", engine.MSG_NOT_PLAYING)
    assert status[refs[1]]["state"] == "ok"  # the other camera is not touched
    with rig.db() as db:
        assert engine.resolve_refs(db, refs)[refs[0]].url == url
    # The camera is back: the next check drops the error; still nothing to deliver.
    dead["on"] = False
    rig.clock.advance(61)
    rig.tick()
    rec = rig.addr(refs[0])
    assert (rec["last_error"], rec["failures"], rec["next_try_at"], rec["url"]) == (None, 0, None, url)
    assert engine.status_for_cameras(refs)[refs[0]]["state"] == "ok" and rig.station.refreshes == []


def test_refused_token_is_replaced_by_the_fresh_one_and_delivered_once(rig):
    """The other half of the revive: the stored address is refused (403) and
    the fresh one plays, so it is taken and delivered; only the query changes."""
    _pid, refs = _wall(rig, cams=(1,))
    rig.deploy("PC_A")
    old = rig.addr(refs[0])["url"]
    rig.lumana.tokens.clear()  # the vendor withdrew the token
    for _ in range(6):
        rig.clock.advance(31)
        rig.tick()
    new = rig.addr(refs[0])["url"]
    assert new != old and _path(new) == _path(old) and rig.addr(refs[0])["last_error"] is None
    assert len(rig.station.refreshes) == 1 and cell_urls(rig.station.last("PC_A"))["src-0"] == new
    assert rig.lumana.count("start") == 2


def test_probe_that_raises_does_not_fail_the_mint(rig):
    fixed = register_fixed()
    pid = rig.add_provider(name="Fixed one", type_id="fixed", settings={"host": "http://127.0.0.1:9"}, secrets_={})
    reference = ref(pid, 1)
    with rig.db() as db:
        first = engine.resolve_refs(db, [reference])[reference].url
        entry = engine.load_providers(db, [pid])[pid]
    calls = []

    def probe(address, http):
        calls.append(address.url)
        if len(calls) == 1:
            raise ValueError("not an httpx.HTTPError")  # the stored address
        return True  # the fresh one

    fixed.probe = probe
    out = engine._mint_one(entry, "cam1", dict(rig.cfg), revive=True)
    rec = rig.addr(reference)
    assert out.code == engine.OK and out.url == rec["url"] != first and calls == [first, rec["url"]]
    assert (rec["failures"], rec["last_error"], rec["next_try_at"]) == (0, None, None)
    assert len(fixed.mints) == 2


# --------------------------------------------------------------------------- #
# A refresh against a deploy, and against another refresh
# --------------------------------------------------------------------------- #


def test_deploy_during_a_refresh_is_not_followed_by_the_old_layout(rig, monkeypatch):
    pid, refs = _wall(rig)
    rig.deploy("PC_A")
    rig.clock.advance(9001)  # renewal due: the refresh has something to send
    real = engine.resolve_refs
    state = {"done": False}

    def slow_resolve(db, wanted, **kw):
        out = real(db, wanted, **kw)
        if not state["done"]:
            state["done"] = True
            # While the refresh waits for the provider, an admin deploys a new layout.
            rig.set_layout("PC_A", [ref(pid, 1), ref(pid, 2), ref(pid, 3)], title="NEW WALL")
            rig.deploy("PC_A")
        return out

    with rig.db() as db:
        real(db, refs, force=True)  # stands in for the tick's renewal step
    monkeypatch.setattr(engine, "resolve_refs", slow_resolve)
    assert rig.refresh("PC_A", "tick")["status"] == "superseded"
    monkeypatch.setattr(engine, "resolve_refs", real)
    order = [("refresh" if "refresh_id" in r else "deploy", r["content"]["screens"][0]["title"])
             for r in rig.station.order]
    assert order == [("deploy", "Screen 1"), ("deploy", "NEW WALL")]  # nothing after the new deploy
    rec = engine._load("pc:PC_A")
    assert rec["config_id"] == rig.station.deploys[-1]["config_id"]
    assert rec["layout"]["screens"][0]["title"] == "NEW WALL"
    assert rig.tick()["delivered"] == 0 and len(rig.station.order) == 2  # and nothing to heal


def test_deploy_holds_the_send_lock_and_a_refresh_waits_its_turn(rig):
    _pid, _refs = _wall(rig)
    lock = engine.PREFIX + "lock:pc:PC_A"
    seen = []
    station = rig.station

    def sender(requests):
        seen.append(bool(rig.redis.exists(lock)))
        return station(requests)

    rig.sender = sender
    rig.deploy("PC_A")
    assert seen == [True] and not rig.redis.exists(lock)  # held over the send, released after
    rig.clock.advance(9001)
    with rig.db() as db:
        engine.resolve_refs(db, _refs, force=True)
    held = engine.lock_pcs(["PC_A"])  # a deploy is sending right now
    assert rig.refresh("PC_A", "tick")["status"] == "busy" and station.refreshes == []
    engine.unlock_pcs(held)
    assert rig.refresh("PC_A", "tick")["status"] == "delivered" and not rig.redis.exists(lock)
    # A deploy does not wait for ever for a lock that is stuck: it goes on.
    stuck = engine.lock_pcs(["PC_A"])
    assert engine.lock_pcs(["PC_A"], wait_sec=0.2) == {}
    engine.unlock_pcs(stuck)


def test_layout_without_provider_cameras_takes_no_lock(rig):
    rig.add_pc("PC_A")
    rig.layouts["PC_A"] = {"pc_id": "PC_A", "screens": [{"id": "s", "source_groups": [[
        {"id": "a", "url": "rtsp://127.0.0.1/x", "LocationUris": []}]]}]}
    assert rig.deploy("PC_A")["deployed"] == 1 and not rig.fake_redis.touched()


def test_two_refreshes_at_once_deliver_once(rig, monkeypatch):
    _pid, refs = _wall(rig)
    rig.deploy("PC_A")
    rig.clock.advance(9001)
    with rig.db() as db:
        engine.resolve_refs(db, refs, force=True)
    real = engine.resolve_refs
    barrier = threading.Barrier(2, timeout=10)

    def gated(db, wanted, **kw):
        out = real(db, wanted, **kw)
        barrier.wait()  # both have resolved before either sends
        return out

    monkeypatch.setattr(engine, "resolve_refs", gated)
    results = []
    threads = [threading.Thread(target=lambda: results.append(rig.refresh("PC_A", "connect")["status"]))
               for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) in (["busy", "delivered"], ["delivered", "rate_limited"])
    assert len(rig.station.refreshes) == 1
    assert engine._load("pc:PC_A")["sent_id"] == rig.station.refreshes[0]["refresh_id"]


def test_connect_checks_do_not_repeat_a_probe_that_just_answered(rig):
    """A relay restart with many stations, or one flapping station: every
    register queues a connect check, but a shared address that answered
    within the last 10 s is not asked again for each of them."""
    pid, refs = _wall(rig)
    rig.add_pc("PC_B")
    rig.set_layout("PC_B", refs)
    rig.deploy("PC_A", "PC_B")
    for _ in range(5):
        assert rig.refresh("PC_A", "connect")["status"] == "current"
        assert rig.refresh("PC_B", "connect")["status"] == "current"
    assert rig.lumana.count("playlist") == 2  # one per address, not 20
    rig.clock.advance(11)
    rig.refresh("PC_A", "connect")
    assert rig.lumana.count("playlist") == 4
    # An address that just failed is still checked at once (and revived).
    rig.lumana.close_session()
    rig.clock.advance(11)
    rig.refresh("PC_A", "connect")
    assert rig.lumana.open_sessions() == 2 and rig.lumana.count("start") == 4
    assert rig.station.refreshes == []  # same addresses: nothing to deliver
    # A station's failure report is checked even right after a probe that answered.
    rig.refresh("PC_A", "connect")
    rig.lumana.close_session()
    with rig.db() as db:
        engine.station_failed(db, "PC_A", ["src-0"], rig.sender)
    assert rig.lumana.open_sessions() == 1 and rig.lumana.count("start") == 5


def test_a_lock_is_released_only_by_its_owner(rig):
    first = engine._lock("tick", 120)
    assert first and engine._lock("tick", 120) is None
    rig.redis.delete(engine.PREFIX + "tick")  # it expired under a long run
    second = engine._lock("tick", 120)  # and another worker took it
    engine._unlock("tick", first)       # the late first worker must not free it
    assert rig.redis.get(engine.PREFIX + "tick").decode() == second
    engine._unlock("tick", second)
    assert not rig.redis.exists(engine.PREFIX + "tick")


def test_request_timeout_has_an_upper_bound():
    class Rows:
        def query(self, *_a):
            return self

        def filter(self, *_a):
            return self

        def all(self):
            return [("stream_providers.request_timeout_sec", "500")]

    assert REAL_CONFIG(Rows())["request_timeout_sec"] == engine.MAX_REQUEST_TIMEOUT_SEC == 10.0
    # A mint and its two probes at that bound fit the mint lock.
    assert engine.MINT_LOCK_SEC >= 3 * 2 * engine.MAX_REQUEST_TIMEOUT_SEC


# --------------------------------------------------------------------------- #
# Release
# --------------------------------------------------------------------------- #


def test_station_that_lost_the_relay_keeps_its_sessions(rig):
    _pid, refs = _wall(rig)
    rig.deploy("PC_A")
    before = cell_urls(rig.station.last("PC_A"))
    rig.connected.clear()  # only its socket to the relay is gone; Core keeps playing
    rig.clock.advance(301)
    assert rig.tick()["released"] == 0
    rig.clock.advance(3600)
    assert rig.tick()["released"] == 0
    assert rig.lumana.count("stop") == 0 and rig.lumana.open_sessions() == 2 and rig.addr(refs[0])
    rig.connected.append("PC_A")
    rig.tick()
    after = cell_urls(rig.station.last("PC_A"))
    assert after == before and rig.station.refreshes == []  # same sessions, same addresses, nothing sent
    assert rig.lumana.count("start") == 2


def test_station_gone_past_the_address_lifetime_is_released(rig):
    _pid, refs = _wall(rig)
    rig.deploy("PC_A")
    rig.connected.clear()
    rig.clock.advance(18000)  # the token is over: nothing can still be playing it
    assert rig.tick()["released"] == 2 and rig.lumana.count("stop") == 2
    assert rig.redis.keys(engine.PREFIX + "addr:*") == []


def test_camera_taken_off_every_wall_is_released_after_five_minutes(rig):
    pid, refs = _wall(rig)
    rig.deploy("PC_A")
    rig.set_layout("PC_A", [refs[0]])
    rig.deploy("PC_A")
    rig.clock.advance(299)
    assert rig.tick()["released"] == 0 and rig.lumana.open_sessions() == 2
    rig.clock.advance(2)
    assert rig.tick()["released"] == 1
    assert [entry[1] for entry in rig.lumana.log if entry[0] == "stop"] == ["cam2"]
    assert rig.addr(refs[1]) is None and rig.addr(refs[0])["url"]


def test_live_view_address_is_kept_while_it_is_noted_as_in_use(rig):
    """An address minted for the portal's live view (no wall shows the
    camera) is given back after 5 minutes without a note, not while it plays."""
    pid = rig.add_provider()
    rig.add_pc("PC_A")  # connected, without a deploy
    rig.add_camera("CAM_LIVE", ref(pid, 5))
    reference = ref(pid, 5)
    with rig.db() as db:
        assert engine.resolve_refs(db, [reference])[reference].url
    for _ in range(8):  # 8 minutes of viewing
        rig.clock.advance(60)
        engine.touch(reference)
        assert rig.tick()["released"] == 0
    assert rig.lumana.count("stop") == 0
    rig.clock.advance(301)  # the viewer left
    assert rig.tick()["released"] == 1 and rig.lumana.count("stop") == 1


# --------------------------------------------------------------------------- #
# Cameras that are only listed in LocationUris
# --------------------------------------------------------------------------- #


def _site_wall(rig, on_wall, in_site):
    """``on_wall`` provider cells; each lists all ``in_site`` cameras of the
    site in LocationUris, as the generator does."""
    pid = rig.add_provider()
    rig.add_pc("PC_A")
    refs = [ref(pid, n) for n in range(1, in_site + 1)]
    uris = [{"url": r, "osd_text": f"Camera {i}"} for i, r in enumerate(refs)]
    cells = [{"id": f"src-{i}", "camera_id": f"CAM_{i}", "camera_name": f"Camera {i}", "url": refs[i],
              "LocationUris": json.loads(json.dumps(uris)), "use_tcp": False} for i in range(on_wall)]
    rig.layouts["PC_A"] = {"pc_id": "PC_A", "pc_name": "PC_A wall", "width": 640, "height": 480,
                           "screens": [{"id": "PC_A_screen1", "display_idx": 0, "switchInterval": 10,
                                        "title": "Screen 1", "source_groups": [cells]}]}
    return pid, refs


def test_cameras_only_listed_in_location_uris_open_no_session(rig):
    pid, refs = _site_wall(rig, on_wall=2, in_site=12)
    result = rig.deploy("PC_A")["results"][0]
    assert result["provider_streams"] == {"total": 2, "unresolved": []}
    assert rig.lumana.count("start") == 2 and rig.lumana.open_sessions() == 2  # it was 12 starts, 10 held
    content = rig.station.last("PC_A")
    assert "provider://" not in json.dumps(content)
    cells = content["screens"][0]["source_groups"][0]
    wall = [cell["url"] for cell in cells]
    assert all("?token=" in url for url in wall)
    for cell in cells:
        listed = [item["url"] for item in cell["LocationUris"]]
        assert listed[:2] == wall  # a camera that is on the wall plays in the location list too
        assert listed[2:] == [engine.PLACEHOLDER + "not-on-wall"] * 10
    rig.clock.advance(61)
    before = rig.lumana.count("playlist")
    out = rig.tick()
    assert (out["in_use"], out["probed"], out["delivered"]) == (2, 2, 0)
    assert rig.lumana.count("playlist") - before == 2 and rig.lumana.open_sessions() == 2
    with rig.db() as db:
        status = engine.status_for_pc(db, "PC_A")
    assert (status["cameras"], status["unresolved"]) == (2, [])
    assert engine.status_for_cameras(refs)[refs[5]]["state"] == "unused"
    # Renewal: only the two addresses on the wall, one delivery, the listed ones unchanged.
    rig.clock.advance(9001)
    out = rig.tick()
    assert (out["renewed"], out["delivered"]) == (2, 1) and rig.lumana.count("start") == 4
    listed = [item["url"] for item in rig.station.last("PC_A")["screens"][0]["source_groups"][0][0]["LocationUris"]]
    assert listed[2:] == [engine.PLACEHOLDER + "not-on-wall"] * 10


def test_layout_whose_references_are_all_off_the_wall_never_reaches_the_station_unresolved(rig):
    pid = rig.add_provider()
    rig.add_pc("PC_A")
    rig.layouts["PC_A"] = {"pc_id": "PC_A", "screens": [{"id": "s", "source_groups": [[
        {"id": "a", "url": "rtsp://127.0.0.1/x", "LocationUris": [{"url": ref(pid, 1), "osd_text": "Camera 1"}]}]]}]}
    result = rig.deploy("PC_A")["results"][0]
    assert result["provider_streams"] == {"total": 0, "unresolved": []}
    cell = rig.station.last("PC_A")["screens"][0]["source_groups"][0][0]
    assert cell["LocationUris"][0]["url"] == engine.PLACEHOLDER + "not-on-wall" and rig.lumana.count("start") == 0
    assert rig.tick()["delivered"] == 0 and rig.station.refreshes == []


# --------------------------------------------------------------------------- #
# The placeholder, the provider status and the deploy answer
# --------------------------------------------------------------------------- #


def test_change_of_the_unresolved_code_changes_only_the_query(rig):
    _pid, _refs = _wall(rig)
    rig.lumana.down = True
    rig.deploy("PC_A")
    first = cell_urls(rig.station.last("PC_A"))["src-0"]
    rig.lumana.down = False
    rig.lumana.keys.clear()  # now the key is refused instead
    rig.clock.advance(61)
    assert rig.tick()["delivered"] == 1
    second = cell_urls(rig.station.last("PC_A"))["src-0"]
    assert first == "http://127.0.0.1:9/stream-unavailable?code=provider-unavailable"
    assert second == "http://127.0.0.1:9/stream-unavailable?code=key-refused"
    assert _path(first) == _path(second)  # Core 1.3.1 adopts a query-only change: no view rebuild


def test_provider_status_carries_the_code(rig):
    provider_id, _refs = _wall(rig, provider_id=rig.add_provider(key="wrong-" + new_key()))
    rig.deploy("PC_A")
    status = engine.provider_status(provider_id)
    assert (status["state"], status["code"], status["message"]) == ("error", "key-refused",
                                                                    "Lumana refused the API key")
    assert engine.provider_status(rig.add_provider(name="Unused"))["code"] is None


def test_deploy_answer_carries_provider_streams_through_the_response_model(rig):
    rig.add_provider()
    provider_id, _refs = _wall(rig, provider_id=rig.add_provider(name="Second", key="wrong-" + new_key()))
    summary = DeploySummary(**rig.deploy("PC_A")).model_dump()
    result = summary["results"][0]
    assert result["provider_streams"]["total"] == 2
    assert [u["code"] for u in result["provider_streams"]["unresolved"]] == ["key-refused", "key-refused"]
    assert result["message"] == ("Delivered to PC PC_A; this station cannot confirm whether it applied the layout. "
                                 "2 provider camera(s) are not available: Second: Lumana refused the API key")
    # A layout without provider cameras: the field is absent, as before.
    rig.add_pc("PC_B")
    rig.layouts["PC_B"] = {"pc_id": "PC_B", "screens": [{"id": "s", "source_groups": [[
        {"id": "a", "url": "rtsp://127.0.0.1/x", "LocationUris": []}]]}]}
    assert DeploySummary(**rig.deploy("PC_B")).model_dump()["results"][0]["provider_streams"] is None


# --------------------------------------------------------------------------- #
# The sender's transport log
# --------------------------------------------------------------------------- #


def test_sender_transport_logs_no_error_lines(monkeypatch):
    """engine.io's client logs "websocket-client package not installed" and
    "packet queue is empty, aborting" at ERROR on every healthy send; the
    sender hands it a logger that drops them."""
    seen = {}

    class Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def connect(self, *_a, **_k):
            raise ConnectionError("no relay in this test")

        def disconnect(self):
            pass

    monkeypatch.setattr("socketio.Client", Client)
    with pytest.raises(deploy_service.RelayUnreachable):
        deploy_service.refresh_sender([{"refresh_id": "r-1", "targetId": "PC_A", "content": {}}])
    log = seen["engineio_logger"]
    assert isinstance(log, logging.Logger) and not log.isEnabledFor(logging.ERROR)
    assert log.level != logging.NOTSET  # engine.io would reset an unset level to ERROR
