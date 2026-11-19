"""
Stream providers, pure parts (LUM-SRV): the reference format, the renewal
time, layout resolution, the fingerprint, the Core rule, Lumana's error
mapping (against tests/fake_lumana.py on 127.0.0.1) and the registry.

    python -m pytest tests/unit/test_stream_providers_unit.py -o addopts='' -p no:cacheprovider
"""

import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from app.services.stream_providers import engine, registry
from app.services.stream_providers.base import AUTH, NOT_FOUND, UNAVAILABLE, Address, ProviderError
from app.services.stream_providers.lumana import LumanaProvider, jwt_exp
from app.utils.url_processor import (encode_rtsp_password, is_hls_url, is_provider_ref, make_provider_ref,
                                     parse_provider_ref, redact_camera_url, try_encode_rtsp_password)
from tests.deploy_testkit import camera_url
from tests.fake_lumana import FakeLumana
from tests.stream_provider_testkit import Clock, new_key

PID = "0b0e8a52-6f0f-4d4e-9d0a-5f2a1c3e7b11"


# --------------------------------------------------------------------------- #
# References
# --------------------------------------------------------------------------- #


def test_reference_round_trip_and_encoding():
    ref = make_provider_ref(PID, "cam/1 a?b")
    assert ref == f"provider://{PID}/cam%2F1%20a%3Fb"
    assert is_provider_ref(ref) and parse_provider_ref(ref) == (PID, "cam/1 a?b")
    assert parse_provider_ref(f"  PROVIDER://{PID}/x ") == (PID, "x")


@pytest.mark.parametrize("value", ["provider://", "provider://only-id", "provider://id/", "provider:///cam",
                                   "rtsp://h/s", "https://h/a.m3u8?token=x", "", None, 7])
def test_not_a_reference(value):
    assert parse_provider_ref(value) is None


def test_reference_passes_the_url_helpers_unchanged():
    ref = make_provider_ref(PID, "a@b:c")
    assert encode_rtsp_password(ref) == ref and try_encode_rtsp_password(ref) == ref
    assert redact_camera_url(ref) == ref  # it holds no secret
    assert not is_hls_url(ref)
    # RTSP and HLS behave as before.
    assert redact_camera_url(camera_url()) == "rtsp://<redacted>@192.0.2.10/stream1"
    assert redact_camera_url("https://h/v2/file/a/b/manifest.m3u8?token=abc") == \
        "https://h/v2/file/a/b/manifest.m3u8?<redacted>"
    assert encode_rtsp_password("rtsp://u:p@ss@h/s") == "rtsp://u:p%40ss@h/s"


# --------------------------------------------------------------------------- #
# renew_at, fingerprint, Core rule
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("life,fraction,margin,expected", [
    (18000, 0.5, 1800, 9000),     # 5 h: half the lifetime
    (18000, 0.95, 1800, 16200),   # never later than 30 min before the expiry
    (600, 0.5, 1800, 300),        # 10 min: the margin is capped at half the lifetime
    (600, 0.9, 1800, 300),
    (None, 0.5, 1800, 1800),      # no expiry: an hour's rhythm
])
def test_renew_at(life, fraction, margin, expected):
    assert engine.renew_at(1000.0, None if life is None else 1000.0 + life, fraction, margin) == 1000.0 + expected


def test_fingerprint_follows_addresses_and_codes():
    a, b = make_provider_ref(PID, "a"), make_provider_ref(PID, "b")
    ok = {a: engine.Resolved("https://h/a?token=1", engine.OK, ""), b: engine.Resolved("https://h/b?token=1",
                                                                                      engine.OK, "")}
    base = engine.fingerprint([a, b], ok)
    assert base == engine.fingerprint([b, a, a], dict(reversed(list(ok.items()))))
    renewed = dict(ok, **{a: engine.Resolved("https://h/a?token=2", engine.OK, "")})
    failed = dict(ok, **{a: engine.Resolved(None, engine.CODE_KEY, "x")})
    assert len({base, engine.fingerprint([a, b], renewed), engine.fingerprint([a, b], failed),
                engine.fingerprint([a, b], {}), engine.fingerprint([a, b], ok, core_ok=False)}) == 5
    assert "token" not in base and len(base) == 64


@pytest.mark.parametrize("flag,health,expected", [
    (None, None, False),                                   # unknown counts as old
    (False, None, False),
    (True, None, True),                                    # the admin's flag
    (True, {"core_running_version": "1.2.0"}, False),      # a reported version wins over the flag
    (None, {"core_running_version": "1.3.0"}, True),
    (None, {"core_running_version": "1.3.1-3b228eb4"}, True),
    (None, {"core_installed_version": "v1.10.0"}, True),
    (None, {"core_running_version": "1.2.9", "core_installed_version": "1.3.0"}, False),  # the first set decides
    (None, {"core_version": "1.3"}, True),
    (True, {"core_running_version": "unknown"}, True),     # unreadable: fall through to the flag
    (None, {"core_running_version": ""}, False),
])
def test_core_supports_hls(flag, health, expected):
    names = ("core_running_version", "core_installed_version", "core_version")
    row = SimpleNamespace(**{n: (health or {}).get(n) for n in names}) if health is not None else None
    assert engine.core_supports_hls(SimpleNamespace(hls_capable=flag), row) is expected


# --------------------------------------------------------------------------- #
# resolve_layout
# --------------------------------------------------------------------------- #


def _layout():
    a, b = make_provider_ref(PID, "a"), make_provider_ref(PID, "b")
    rtsp = camera_url()
    uris = [{"url": a, "osd_text": "Cam A"}, {"url": b, "osd_text": "Cam B"}, {"url": rtsp, "osd_text": "R"}]
    return a, b, rtsp, {
        "pc_id": "PC", "config_id": "cfg-1",
        "screens": [
            {"id": "s1", "source_groups": [[
                {"id": "src-a", "camera_id": "CAM_A", "camera_name": "Cam A", "url": a, "LocationUris": copy.deepcopy(uris)},
                {"id": "src-r", "camera_id": "CAM_R", "url": rtsp, "LocationUris": copy.deepcopy(uris)},
                {"id": "", "url": "", "LocationUris": []},
            ]]},
            {"id": "s2", "views": {"v1": {"sources": [
                {"id": "src-b", "camera_id": "CAM_B", "camera_name": "Cam B", "url": b, "LocationUris": [a, rtsp]}]}}},
            {"id": "s3", "source_groups": []},
        ],
    }


def test_resolve_layout_both_shapes_and_location_uris():
    a, b, rtsp, layout = _layout()
    before = json.dumps(layout, sort_keys=True)
    resolved = {a: engine.Resolved("https://h/a.m3u8?token=T", engine.OK, ""),
                b: engine.Resolved(None, engine.CODE_KEY, "Acme: Lumana refused the API key")}
    out, cells, unresolved = engine.resolve_layout(layout, resolved)
    assert json.dumps(layout, sort_keys=True) == before  # the input is not changed
    assert cells == {"src-a": a, "src-b": b}
    text = json.dumps(out)
    assert "provider://" not in text and out["config_id"] == "cfg-1"
    group = out["screens"][0]["source_groups"][0]
    assert group[0]["url"] == "https://h/a.m3u8?token=T"
    assert [u["url"] for u in group[1]["LocationUris"]] == ["https://h/a.m3u8?token=T",
                                                            engine.PLACEHOLDER + "key-refused", rtsp]
    assert group[1]["url"] == rtsp and group[2]["url"] == ""
    view = out["screens"][1]["views"]["v1"]["sources"][0]
    assert view["url"] == "http://127.0.0.1:9/stream-unavailable?code=key-refused"
    assert view["LocationUris"] == ["https://h/a.m3u8?token=T", rtsp]
    assert unresolved == [{"ref": b, "camera_id": "CAM_B", "camera_name": "Cam B", "code": "key-refused",
                           "message": "Acme: Lumana refused the API key"}]
    assert engine.layout_refs(layout) == sorted([a, b])


def test_resolve_layout_placeholders():
    a, b, _rtsp, layout = _layout()
    out, _cells, unresolved = engine.resolve_layout(layout, {})
    assert {u["code"] for u in unresolved} == {"pending"}
    assert out["screens"][0]["source_groups"][0][0]["url"].endswith("/stream-unavailable?code=pending")
    full = {a: engine.Resolved("https://h/a?token=T", engine.OK, ""), b: engine.Resolved("https://h/b?token=T",
                                                                                        engine.OK, "")}
    out, _cells, unresolved = engine.resolve_layout(layout, full, core_ok=False)
    assert {u["code"] for u in unresolved} == {"core-too-old"} and "token=" not in json.dumps(out)
    assert "1.3.0 or later" in unresolved[0]["message"]
    for code in ("provider-removed", "camera-not-found", "limit-reached", "provider-unavailable"):
        out, _c, _u = engine.resolve_layout(layout, {a: engine.Resolved(None, code, "m"), b: full[b]})
        assert out["screens"][0]["source_groups"][0][0]["url"] == engine.PLACEHOLDER + code


def test_layout_without_references_is_copied_unchanged():
    _a, _b, rtsp, layout = _layout()
    for source in engine._sources(layout):
        source["url"] = rtsp
        source["LocationUris"] = [rtsp]
    out, cells, unresolved = engine.resolve_layout(layout, {})
    assert out == layout and cells == {} and unresolved == [] and engine.layout_refs(layout) == []


# --------------------------------------------------------------------------- #
# Lumana against the local fake
# --------------------------------------------------------------------------- #


@pytest.fixture
def lumana():
    key = new_key()
    clock = Clock()
    fake = FakeLumana({key}, {"cam1", "cam2"}, clock=clock)
    fake.key = key
    yield fake
    fake.stop()


def _settings(fake, key=None):
    return {"base_url": fake.url, "api_key": key or fake.key}


def test_lumana_mint_reuses_the_session(lumana):
    provider = LumanaProvider()
    with httpx.Client(timeout=5) as http:
        first = provider.mint(_settings(lumana), "cam1", None, http)
        lumana.clock.advance(100)
        second = provider.mint(_settings(lumana), "cam1", first, http)
        assert provider.probe(first, http) and provider.probe(second, http)  # the old token keeps working
    assert first.url.split("?")[0] == second.url.split("?")[0] and first.url != second.url
    assert first.handle == second.handle and first.handle["session_id"]
    assert first.expires_at == lumana.clock.t - 100 + 18000 and second.expires_at == lumana.clock.t + 18000
    assert jwt_exp(first.url.split("token=")[1]) == first.expires_at  # read from the JWT, not hard-coded
    assert first.info == {"sessions": 1} and lumana.open_sessions() == 1
    assert [entry[2] for entry in lumana.log if entry[0] == "start"] == [None, first.handle["session_id"]]


def test_lumana_lifetime_comes_from_the_token(lumana):
    lumana.lifetime = 777
    with httpx.Client(timeout=5) as http:
        assert LumanaProvider().mint(_settings(lumana), "cam1", None, http).expires_at == lumana.clock.t + 777
    assert jwt_exp("not-a-jwt") is None and jwt_exp("a.b.c") is None


def test_lumana_error_mapping(lumana):
    provider = LumanaProvider()
    with httpx.Client(timeout=5) as http:
        with pytest.raises(ProviderError) as bad_key:
            provider.mint(_settings(lumana, "wrong-" + lumana.key), "cam1", None, http)
        with pytest.raises(ProviderError) as unknown:
            provider.mint(_settings(lumana), "nope", None, http)
        lumana.down = True
        with pytest.raises(ProviderError) as down:
            provider.mint(_settings(lumana), "cam1", None, http)
        with pytest.raises(ProviderError) as limit:
            provider.mint(_settings(lumana), "cam1", None, http, {"sessions": 9})
        lumana.down = False
    assert (bad_key.value.code, bad_key.value.message) == (AUTH, "Lumana refused the API key")
    assert (unknown.value.code, unknown.value.message) == (NOT_FOUND, "Lumana does not know this camera id")
    assert (down.value.code, down.value.message) == (UNAVAILABLE, "Lumana answered HTTP 502")
    # What Lumana answers at its limit is not known: no status is called "limit", the count is named.
    assert (limit.value.code, limit.value.message) == (
        UNAVAILABLE, "Lumana answered HTTP 502; 9 live session(s) were open at the last count")
    for error in (bad_key, unknown, down, limit):
        assert lumana.key not in error.value.message and lumana.key not in str(error.value)


def test_lumana_no_answer_names_no_url():
    provider = LumanaProvider()
    with httpx.Client(timeout=1) as http:
        with pytest.raises(ProviderError) as error:
            provider.mint({"base_url": "http://127.0.0.1:9", "api_key": "k"}, "cam1", None, http)
    assert (error.value.code, error.value.message) == (UNAVAILABLE, "Lumana did not answer")
    assert error.value.__cause__ is None


def test_lumana_check_opens_no_session(lumana):
    provider = LumanaProvider()
    with httpx.Client(timeout=5) as http:
        assert provider.check(_settings(lumana), http) == "Lumana accepted the API key"
        with pytest.raises(ProviderError) as error:
            provider.check(_settings(lumana, "wrong"), http)
    assert error.value.code == AUTH and lumana.open_sessions() == 0


def test_lumana_release_and_closed_session(lumana):
    provider = LumanaProvider()
    with httpx.Client(timeout=5) as http:
        address = provider.mint(_settings(lumana), "cam1", None, http)
        provider.release(_settings(lumana), "cam1", address, http)
        assert lumana.open_sessions() == 0 and not provider.probe(address, http)  # 500 on a closed session
        provider.release(_settings(lumana), "cam1", address, http)               # 404 is fine
        lumana.down = True
        provider.release(_settings(lumana), "cam1", address, http)               # errors are ignored
        lumana.down = False
        provider.release(_settings(lumana), "cam1", Address("u", None), http)    # no session: nothing sent
        revived = provider.mint(_settings(lumana), "cam1", address, http)
        assert revived.url.split("?")[0] == address.url.split("?")[0] and provider.probe(revived, http)
    assert lumana.count("stop") == 3


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_registry_describes_fields_without_values():
    described = {entry["type"]: entry for entry in registry.describe()}
    lumana = described["lumana"]
    assert lumana["label"] == "Lumana"
    fields = {f["name"]: f for f in lumana["fields"]}
    assert fields["api_key"]["kind"] == "secret" and fields["api_key"]["required"] and \
        fields["api_key"]["default"] is None
    assert fields["base_url"] == {"name": "base_url", "label": "API address", "kind": "url", "required": False,
                                  "default": "https://access.lumana.ai",
                                  "help": "Leave the default unless Lumana gave you another address."}
    assert set(fields) == {"api_key", "base_url"} and registry.get("lumana") is not None
    assert registry.get("nope") is None
