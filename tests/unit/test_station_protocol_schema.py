"""
Station protocol contract (plan 06, task D1).

Run with the coverage gate off (plan 6.1):
    python -m pytest tests/unit/test_station_protocol_schema.py -o addopts='' -p no:cacheprovider

Covers: deterministic export and drift detection, PROTOCOL_HASH, the manifest,
pydantic/JSON Schema parity on every fixture, the acceptance cases (51 failed
ids, URIs with userinfo), credential-free files, and the Client's golden
payloads (docs/station_protocol/golden, copied from shomer_client/tests/contract).
"""

import copy
import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.schemas import station_protocol as sp

BACKEND_ROOT = Path(__file__).resolve().parents[2]
EXPORT_SCRIPT = BACKEND_ROOT / "scripts" / "export_station_protocol.py"
CONTRACT_DIR = BACKEND_ROOT / "docs" / "station_protocol"

CREDENTIAL_URI_RE = re.compile(r"rtsp://[^/@ ]+:[^/@ ]+@")
JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
IPV4_RE = re.compile(r"(?<![0-9.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![0-9.])")
TEST_NETS = ((192, 0, 2), (198, 51, 100), (203, 0, 113))


def _load_exporter():
    spec = importlib.util.spec_from_file_location("export_station_protocol", EXPORT_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


exporter = _load_exporter()


@pytest.fixture(scope="module")
def json_validators():
    return {name: Draft202012Validator(schema) for name, schema in sp.json_schemas().items()}


def _schema_of(name: str) -> str:
    return name.split("__", 1)[0]


def _pydantic_ok(name: str, payload) -> bool:
    try:
        sp.validate(name, payload)
        return True
    except ValidationError:
        return False


def _both_reject(json_validators, name: str, payload) -> bool:
    return (not _pydantic_ok(name, payload)) and (not json_validators[name].is_valid(payload))


def _credential_uri(scheme: str = "rtsp", password: str = "secret") -> str:
    # Built at run time, so that no file in either repo holds a user:pass URI.
    return scheme + "://" + "fixture" + ":" + password + "@" + "192.0.2.10:554/stream"


def _files(sub: str):
    return sorted((CONTRACT_DIR / sub).glob("*.json"))


# --------------------------------------------------------------------------- #
# Export: deterministic, current, drift detected
# --------------------------------------------------------------------------- #


def test_export_is_byte_identical_on_rerun(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"
    exporter.write(first, exporter.build_export())
    exporter.write(second, exporter.build_export())
    files_a = {p.relative_to(first).as_posix(): p.read_bytes() for p in first.rglob("*") if p.is_file()}
    files_b = {p.relative_to(second).as_posix(): p.read_bytes() for p in second.rglob("*") if p.is_file()}
    assert files_a == files_b
    assert exporter.write(first, exporter.build_export()) == []  # a re-run changes nothing
    assert all(b"\r\n" not in data for data in files_a.values())


def test_export_cli_is_deterministic_across_processes(tmp_path):
    outputs = []
    for name in ("one", "two"):
        out = tmp_path / name
        result = subprocess.run(
            [sys.executable, "-I", str(EXPORT_SCRIPT), "--out", str(out)],
            capture_output=True, text=True, timeout=120,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        outputs.append({p.relative_to(out).as_posix(): p.read_bytes() for p in out.rglob("*") if p.is_file()})
    assert outputs[0] == outputs[1]
    assert outputs[0] == exporter.existing_generated(CONTRACT_DIR)


def test_committed_export_is_current():
    assert exporter.diff(CONTRACT_DIR, exporter.build_export()) == []


def test_check_cli_passes_on_the_committed_tree():
    result = subprocess.run(
        [sys.executable, "-I", str(EXPORT_SCRIPT), "--check"], capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert sp.protocol_hash() in result.stdout


def test_check_cli_fails_on_drift(tmp_path):
    out = tmp_path / "contract"
    exporter.write(out, exporter.build_export())
    schema = out / "schemas" / "StationStatusV1.schema.json"
    schema.write_bytes(schema.read_bytes().replace(b'"maxItems": 50', b'"maxItems": 51'))
    (out / "fixtures" / "valid" / "DeployResultV1__applied.json").unlink()
    (out / "schemas" / "ExtraV1.schema.json").write_bytes(b"{}\n")
    result = subprocess.run(
        [sys.executable, "-I", str(EXPORT_SCRIPT), "--check", "--out", str(out)],
        capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 1
    assert "changed: schemas/StationStatusV1.schema.json" in result.stdout
    assert "missing: fixtures/valid/DeployResultV1__applied.json" in result.stdout
    assert "unexpected: schemas/ExtraV1.schema.json" in result.stdout
    # A plain export repairs the tree and removes the stale file.
    exporter.write(out, exporter.build_export())
    assert exporter.diff(out, exporter.build_export()) == []
    assert not (out / "schemas" / "ExtraV1.schema.json").exists()


def test_model_change_changes_hash_and_fails_the_check(monkeypatch):
    before = sp.protocol_hash()
    monkeypatch.setitem(sp._DESCRIPTIONS, "RegisterV1", "changed description")
    sp._schemas_json.cache_clear()
    sp.protocol_hash.cache_clear()
    try:
        assert sp.protocol_hash() != before
        problems = exporter.diff(CONTRACT_DIR, exporter.build_export())
        assert "changed: schemas/RegisterV1.schema.json" in problems
        assert "changed: PROTOCOL_HASH" in problems
    finally:
        monkeypatch.undo()
        sp._schemas_json.cache_clear()
        sp.protocol_hash.cache_clear()
    assert sp.protocol_hash() == before


def test_export_preserves_golden_and_other_files(tmp_path):
    out = tmp_path / "contract"
    exporter.write(out, exporter.build_export())
    golden = out / "golden" / "RegisterV1__x.json"
    golden.parent.mkdir()
    golden.write_bytes(b"{}\n")
    exporter.write(out, exporter.build_export())
    assert golden.read_bytes() == b"{}\n"
    assert exporter.diff(out, exporter.build_export()) == []


# --------------------------------------------------------------------------- #
# Hash and manifest
# --------------------------------------------------------------------------- #


def test_protocol_hash_file_matches_the_bundle():
    recorded = (CONTRACT_DIR / "PROTOCOL_HASH").read_text(encoding="ascii")
    assert recorded == sp.protocol_hash() + "\n"
    # Recompute exactly as a Client holding only the files would.
    bundle = {
        path.name[: -len(".schema.json")]: json.loads(path.read_text(encoding="ascii"))
        for path in sorted((CONTRACT_DIR / "schemas").glob("*.schema.json"))
    }
    encoded = json.dumps(bundle, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    assert hashlib.sha256(encoded).hexdigest() == sp.protocol_hash()
    assert encoded == sp.bundle_bytes()
    assert re.fullmatch(r"[0-9a-f]{64}", sp.protocol_hash())


def test_manifest_lists_every_generated_file():
    entries = {}
    for line in (CONTRACT_DIR / "MANIFEST.sha256").read_text(encoding="ascii").splitlines():
        digest, path = line.split("  ", 1)
        entries[path] = digest
    generated = exporter.existing_generated(CONTRACT_DIR)
    generated.pop("MANIFEST.sha256")
    assert set(entries) == set(generated)
    for path, data in generated.items():
        assert hashlib.sha256(data).hexdigest() == entries[path], path


def test_schemas_are_valid_draft_2020_12():
    schemas = sp.json_schemas()
    assert set(schemas) == {
        "RegisterV1", "RegisteredV1", "ReportAckV1", "DeployResultV1", "StationStatusV1",
        "CoreHelloV1", "CoreHeartbeatV1", "CoreAckV1", "CoreStreamEventV1", "CoreLogV1",
    }
    for name, schema in schemas.items():
        Draft202012Validator.check_schema(schema)
        assert schema["$id"] == sp.SCHEMA_ID_PREFIX + name


# --------------------------------------------------------------------------- #
# Fixtures: pydantic and the exported JSON Schema agree
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("path", _files("fixtures/valid"), ids=lambda p: p.stem)
def test_valid_fixture_validates_in_both(json_validators, path):
    payload = json.loads(path.read_text(encoding="ascii"))
    name = _schema_of(path.stem)
    sp.validate(name, payload)
    assert [e.message for e in json_validators[name].iter_errors(payload)] == []


@pytest.mark.parametrize("path", _files("fixtures/invalid"), ids=lambda p: p.stem)
def test_invalid_fixture_rejected_by_both(json_validators, path):
    payload = json.loads(path.read_text(encoding="ascii"))
    assert _both_reject(json_validators, _schema_of(path.stem), payload)


def test_fixture_sets_are_complete():
    valid = {_schema_of(p.stem) for p in _files("fixtures/valid")}
    assert valid == set(sp.MODELS)
    assert len(_files("fixtures/invalid")) == len(exporter.INVALID_CASES) >= 20


def test_each_invalid_fixture_is_one_mutation_of_a_valid_one(json_validators):
    valid = exporter.valid_fixtures()
    for name, base, mutate, _rule in exporter.INVALID_CASES:
        assert _schema_of(name) == _schema_of(base)
        assert _pydantic_ok(_schema_of(base), valid[base]), base
        assert _both_reject(json_validators, _schema_of(name), mutate(copy.deepcopy(valid[base]))), name


def test_every_model_allows_extra_fields(json_validators):
    for path in _files("fixtures/valid"):
        payload = json.loads(path.read_text(encoding="ascii"))
        payload["x_future_field"] = {"list": [1, "two", None, True], "nested": {"k": 1.5}}
        name = _schema_of(path.stem)
        model = sp.validate(name, payload)
        assert model.model_extra["x_future_field"]["nested"] == {"k": 1.5}
        assert json_validators[name].is_valid(payload), path.stem


def test_known_fields_are_strict(json_validators):
    payload = json.loads((CONTRACT_DIR / "fixtures/valid/StationStatusV1__full.json").read_text(encoding="ascii"))
    for mutate in (
        lambda p: p["wall"].__setitem__("deploying", "false"),
        lambda p: p["core"].__setitem__("pid", 4321.0),
        lambda p: p["wall"].__setitem__("screens", True),
        lambda p: p.__setitem__("ts", "1794850802"),
    ):
        broken = copy.deepcopy(payload)
        mutate(broken)
        assert not _pydantic_ok("StationStatusV1", broken)


# --------------------------------------------------------------------------- #
# Acceptance: 51 failed ids, URIs with userinfo, forbidden keys
# --------------------------------------------------------------------------- #


def test_51_failed_ids_rejected_50_accepted(json_validators):
    payload = exporter.valid_fixtures()["StationStatusV1__50_failed_ids"]
    assert len(payload["wall"]["streams_failed"]) == sp.MAX_FAILED_SOURCE_IDS == 50
    sp.validate("StationStatusV1", payload)
    assert json_validators["StationStatusV1"].is_valid(payload)
    payload["wall"]["streams_failed"].append("SITE_FIXTURE_EXTRA")
    assert _both_reject(json_validators, "StationStatusV1", payload)


URIS_WITH_USERINFO = [
    _credential_uri(),
    _credential_uri("rtsps"),
    _credential_uri("http"),
    _credential_uri(password="pa/ss#1?"),
    _credential_uri(password="p@ss"),
    "rtsp://" + "fixture" + "@" + "198.51.100.7/stream",
    "RTSP://" + "Fixture" + ":" + "x" + "@" + "203.0.113.5/a",
]


@pytest.mark.parametrize("uri", URIS_WITH_USERINFO, ids=lambda u: str(URIS_WITH_USERINFO.index(u)))
def test_uri_with_userinfo_rejected_everywhere(json_validators, uri):
    valid = exporter.valid_fixtures()
    status = valid["StationStatusV1__full"]
    deploy = valid["DeployResultV1__failed_with_reason"]
    cases = [
        ("StationStatusV1", {**status, "debug": {"camera": [uri]}}),
        ("StationStatusV1", {**status, "flags": {**status["flags"], uri: True}}),
        ("StationStatusV1", {**status, "debug": {uri: 1}}),
        ("StationStatusV1", {**status, "wall": {**status["wall"], "streams_failed": [uri]}}),
        ("StationStatusV1", {**status, "readiness": [{"code": "AUTOLOGON_OFF", "detail": uri}]}),
        ("DeployResultV1", {**deploy, "detail": f"camera {uri} failed"}),
        ("DeployResultV1", {**deploy, "extra": [[{"deep": uri}]]}),
        ("RegisterV1", {**valid["RegisterV1__client_full"], "name": uri}),
        ("RegisteredV1", {**valid["RegisteredV1__legacy_relay"], "message": uri}),
        ("ReportAckV1", {**valid["ReportAckV1__rejected"], "reason": uri}),
        ("CoreStreamEventV1", {**valid["CoreStreamEventV1__failed"], "uri": uri}),
    ]
    heartbeat = copy.deepcopy(valid["CoreHeartbeatV1__playing"])
    heartbeat["screen_details"][0]["views"][0]["sources"][1]["uri"] = uri
    cases.append(("CoreHeartbeatV1", heartbeat))
    for name, payload in cases:
        assert _both_reject(json_validators, name, payload), name


def test_uris_without_userinfo_are_accepted(json_validators):
    for uri in (
        "rtsp://203.0.113.10:554/Streaming/channels/402",
        "rtsp://198.51.100.20:554/cam/realmonitor?channel=1&subtype=1",
        "user@example.com is not a URI",
    ):
        payload = {**exporter.valid_fixtures()["DeployResultV1__applied"], "detail": uri}
        sp.validate("DeployResultV1", payload)
        assert json_validators["DeployResultV1"].is_valid(payload)


def test_core_log_text_is_not_constrained():
    # Core log lines may contain whatever Core logs; receivers redact them (D4).
    log = {**exporter.valid_fixtures()["CoreLogV1__info"], "message": "opening " + _credential_uri()}
    sp.validate("CoreLogV1", log)


@pytest.mark.parametrize("model", ["DeployResultV1", "StationStatusV1"])
@pytest.mark.parametrize("key", sp.REPORT_FORBIDDEN_KEYS)
def test_reports_never_carry_forbidden_keys(json_validators, model, key):
    base = {
        "DeployResultV1": exporter.valid_fixtures()["DeployResultV1__applied"],
        "StationStatusV1": exporter.valid_fixtures()["StationStatusV1__minimal"],
    }[model]
    assert _both_reject(json_validators, model, {**base, key: "x"})


def test_validation_errors_never_echo_values():
    secret_uri = _credential_uri(password="DoNotLogMe")
    payload = {**exporter.valid_fixtures()["StationStatusV1__minimal"], "debug": [secret_uri]}
    with pytest.raises(ValidationError) as excinfo:
        sp.validate("StationStatusV1", payload)
    assert "DoNotLogMe" not in str(excinfo.value)
    assert "DoNotLogMe" not in json.dumps(sp.safe_errors(excinfo.value))
    assert all(set(error) == {"loc", "type", "msg"} for error in sp.safe_errors(excinfo.value))

    token_payload = {"pc_id": "x", "auth_token": "Not A Valid Token DoNotLogMe"}
    with pytest.raises(ValidationError) as excinfo:
        sp.validate("RegisterV1", token_payload)
    assert "DoNotLogMe" not in str(excinfo.value)


def test_find_userinfo_reports_a_path_not_the_value():
    payload = {"wall": {"streams_failed": ["ok", _credential_uri(password="DoNotLogMe")]}}
    path = sp.find_userinfo(payload)
    assert path == "$.wall.streams_failed[1]"
    assert sp.find_userinfo({"a": [1, {"b": "rtsp://203.0.113.1/x"}]}) is None
    # Deep nesting does not exhaust the stack.
    deep = current = {}
    for _ in range(5000):
        current["n"] = {}
        current = current["n"]
    current["uri"] = _credential_uri()
    assert sp.find_userinfo(deep).endswith(".uri")


def test_known_features_and_runtime_hash_fit_registered(json_validators):
    payload = {
        "pc_id": exporter.PC_ID,
        "message": "Successfully registered",
        "features": list(sp.KNOWN_FEATURES),
        "protocol_hash": sp.protocol_hash(),
    }
    sp.validate("RegisteredV1", payload)
    assert json_validators["RegisteredV1"].is_valid(payload)


# --------------------------------------------------------------------------- #
# No credentials in any file; Client golden payloads
# --------------------------------------------------------------------------- #


def test_no_credentials_in_contract_files():
    paths = [p for p in CONTRACT_DIR.rglob("*") if p.is_file()]
    paths += [EXPORT_SCRIPT, Path(sp.__file__), Path(__file__)]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert not CREDENTIAL_URI_RE.search(text), path
        assert not JWT_RE.search(text), path
    for path in CONTRACT_DIR.rglob("*"):
        if path.is_file():
            for match in IPV4_RE.finditer(path.read_text(encoding="ascii")):
                assert tuple(int(part) for part in match.groups()[:3]) in TEST_NETS, path


GOLDEN = _files("golden")


def test_client_golden_payloads_exist():
    assert {p.stem for p in GOLDEN} >= {"RegisterV1__client_register"}


@pytest.mark.parametrize("path", GOLDEN, ids=lambda p: p.stem)
def test_client_golden_payload_validates(json_validators, path):
    payload = json.loads(path.read_text(encoding="ascii"))
    name = _schema_of(path.stem)
    sp.validate(name, payload)
    assert json_validators[name].is_valid(payload)
    if "auth_token" in payload:
        assert payload["auth_token"] == exporter.PLACEHOLDER_TOKEN
