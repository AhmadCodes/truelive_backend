"""
docker-compose.yml shared env block (plan 06 D28 test ``compose config``).

Resolves each service's environment the way ``docker compose config`` does
(env_file first, then ``environment``, which wins; ``${VAR:-default}`` from the
shell, then the project .env) and checks that backend, websocket, celery_worker
and celery_beat see every D28 variable with the same values. Values are never
printed: assertions compare names, booleans and fingerprints only.

    python -m pytest tests/unit/test_compose_env.py -o addopts='' -p no:cacheprovider
"""

import hashlib
import re
from pathlib import Path

import pytest
import yaml

from app.core.config import Settings

BACKEND_ROOT = Path(__file__).resolve().parents[2]
COMPOSE = BACKEND_ROOT / "docker-compose.yml"
ENV_EXAMPLE = BACKEND_ROOT / ".env.example"

FOUR = ("backend", "websocket", "celery_worker", "celery_beat")
FLAGS = {
    "DEPLOY_INJECT_CONFIG_ID": "true",
    "RELAY_STATION_REPORTS": "on",
    "DEPLOY_CATCHUP_ENABLED": "false",
    "DEPLOY_ACK_TIMEOUT_SEC": "300",
    "RELAY_DEBUG_LOGGING": "false",
}
FROM_DOTENV = ("SECRET_KEY", "JWT_SECRET", "SETTINGS_ENCRYPTION_KEY", "RELAY_SENDER_SECRET")
SHARED_PLAIN = ("DATABASE_URL", "REDIS_URL", "CELERY_BROKER_URL", "CELERY_RESULT_BACKEND")
_INTERPOLATION = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


@pytest.fixture(scope="module")
def compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _fp(value):
    return None if value is None else hashlib.sha256(value.encode()).hexdigest()[:12]


def _environment_items(service):
    items = service.get("environment") or []
    if isinstance(items, dict):
        return {key: ("" if value is None else str(value)) for key, value in items.items()}
    return dict(item.split("=", 1) for item in items)


def _interpolate(value, shell, dotenv):
    def repl(match):
        name, default = match.group(1), match.group(2)
        found = shell.get(name, dotenv.get(name))
        return found if found not in (None, "") else (default or "")

    return _INTERPOLATION.sub(repl, value)


def resolve(compose_doc, service_name, env_files, shell=None):
    """Effective container environment of one service. ``env_files`` maps an
    env_file path (as written in compose) to its parsed content; the project
    .env doubles as the interpolation source, as in docker compose."""
    shell = shell or {}
    service = compose_doc["services"][service_name]
    files = service.get("env_file") or []
    files = [files] if isinstance(files, str) else files
    effective = {}
    for path in files:
        effective.update(env_files[path])
    for key, value in _environment_items(service).items():
        effective[key] = _interpolate(value, shell, env_files.get(".env", {}))
    return effective


def _dotenv():
    return {
        ".env": {
            "SECRET_KEY": "fixture-secret-key-" + "k" * 32,
            "JWT_SECRET": "fixture-jwt-secret-" + "j" * 16,
            "SETTINGS_ENCRYPTION_KEY": "fixture-fernet-key-placeholder",
            "RELAY_SENDER_SECRET": "fixture-sender-secret-" + "s" * 32,
        },
        "./secrets/alerting.env": {"MINIO_ACCESS_KEY": "fixture-minio"},
    }


# --------------------------------------------------------------------------- #


def test_four_services_share_one_environment_block(compose):
    shared = compose["x-backend-env"]
    for name in FOUR:
        assert compose["services"][name]["environment"] == shared, name


def test_four_services_read_dotenv(compose):
    for name in FOUR:
        files = compose["services"][name].get("env_file")
        assert ".env" in files, name
    # The relay reads only .env (least privilege); backend and Celery keep the alerting secrets.
    assert compose["services"]["websocket"]["env_file"] == [".env"]


def test_no_secret_is_inlined_in_the_shared_block(compose):
    names = set(_environment_items({"environment": compose["x-backend-env"]}))
    assert not names & set(FROM_DOTENV)


def test_every_variable_reaches_all_four_services_with_the_same_value(compose):
    env_files = _dotenv()
    resolved = {name: resolve(compose, name, env_files) for name in FOUR}
    required = set(FLAGS) | set(FROM_DOTENV) | set(SHARED_PLAIN)
    for name in FOUR:
        assert required <= set(resolved[name]), (name, sorted(required - set(resolved[name])))
    for variable in required:
        assert len({_fp(resolved[name].get(variable)) for name in FOUR}) == 1, variable
    for variable, default in FLAGS.items():
        assert resolved["websocket"][variable] == default, variable
    # D28: the relay also gets the broker.
    assert _fp(resolved["websocket"]["CELERY_BROKER_URL"]) == _fp(resolved["backend"]["CELERY_BROKER_URL"])


def test_flags_follow_dotenv_and_shell_in_every_service(compose):
    env_files = _dotenv()
    env_files[".env"].update({"RELAY_STATION_REPORTS": "off", "DEPLOY_ACK_TIMEOUT_SEC": "120"})
    for name in FOUR:
        effective = resolve(compose, name, env_files, shell={"DEPLOY_INJECT_CONFIG_ID": "false"})
        assert effective["RELAY_STATION_REPORTS"] == "off"
        assert effective["DEPLOY_ACK_TIMEOUT_SEC"] == "120"
        assert effective["DEPLOY_INJECT_CONFIG_ID"] == "false"
        assert effective["DEPLOY_CATCHUP_ENABLED"] == "false"


def test_relay_and_api_agree_on_the_encryption_key(compose):
    env_files = _dotenv()
    api = resolve(compose, "backend", env_files)
    relay = resolve(compose, "websocket", env_files)
    assert _fp(api["SETTINGS_ENCRYPTION_KEY"]) == _fp(relay["SETTINGS_ENCRYPTION_KEY"]) is not None
    assert _fp(api["JWT_SECRET"]) == _fp(relay["JWT_SECRET"])
    # Without its env_file (the compose file before D28), the relay had no key (NEW-47).
    legacy = {"services": {"websocket": dict(compose["services"]["websocket"], env_file=[])}}
    assert "SETTINGS_ENCRYPTION_KEY" not in resolve(legacy, "websocket", env_files)


def test_other_services_keep_their_own_environment(compose):
    for name in ("migration", "smtp_ingest"):
        assert compose["services"][name]["environment"] != compose["x-backend-env"]


def test_settings_reads_every_shared_variable():
    fields = set(Settings.model_fields)
    for variable in set(FLAGS) | set(FROM_DOTENV) | set(SHARED_PLAIN) | {"WEBSOCKET_URL", "WEBSOCKET_PORT"}:
        assert variable in fields, variable


def test_flag_defaults_match_settings_defaults():
    defaults = {name: Settings.model_fields[name].default for name in FLAGS}
    assert defaults == {
        "DEPLOY_INJECT_CONFIG_ID": True,
        "RELAY_STATION_REPORTS": True,  # "on"
        "DEPLOY_CATCHUP_ENABLED": False,
        "DEPLOY_ACK_TIMEOUT_SEC": 300,
        "RELAY_DEBUG_LOGGING": False,
    }


def test_env_example_has_placeholders_and_no_values_for_new_secrets():
    entries = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            entries[key.strip()] = value.strip()
    assert entries["SETTINGS_ENCRYPTION_KEY"] == ""
    assert entries["RELAY_SENDER_SECRET"] == ""
    for variable, default in FLAGS.items():
        assert entries[variable] == default, variable
