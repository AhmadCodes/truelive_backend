"""
Startup self-check of the shared backend environment (plan 06, D28 + C18).

The API, the Socket.IO relay, the Celery worker and Celery beat run from one
shared env block (docker-compose.yml, X-18). Each calls
:func:`run_startup_check` once at start and logs one line:

    startup_check service=websocket deploy_inject_config_id=true
      relay_station_reports=on deploy_catchup_enabled=false
      deploy_ack_timeout_sec=300 relay_debug_logging=false
      relay_sender_secret=fp:1a2b3c4d5e6f settings_encryption_key=ok fp:abcdef012345
      jwt_secret_is_client_constant=false broker=reachable

Secrets are never logged: a secret is shown as ``fp:`` plus the first 12 hex
characters of its sha256 (``printf %s "$VALUE" | sha256sum | cut -c1-12``
reproduces it), or as ``unset``. Equal fingerprints across services mean the
services share the value.

Problems are logged, never fatal: a missing or invalid SETTINGS_ENCRYPTION_KEY
is an ERROR (encrypted core.* settings then refuse writes, C18); a JWT_SECRET
equal to the signing constant compiled into TrueLive Client is an ERROR (gate
G-1, NEW-72; rotate it only through D30's dual-secret window, never at once,
OD-23); an unset RELAY_SENDER_SECRET is a WARNING (legacy-open relay, D12); an
unreachable broker is a WARNING (the relay keeps serving).

CLI (prints the report as JSON, never a value):
    python -m app.core.startup_check --service api [--check-broker] [--strict]
"""

import argparse
import hashlib
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger("app.startup_check")

FINGERPRINT_LENGTH = 12
BROKER_TIMEOUT_SEC = 2.0

KEY_OK = "ok"
KEY_MISSING = "no_key"
KEY_INVALID = "invalid_key"

# sha256(b"truelive-g1-client-jwt-constant-v1\0" + constant) of the JWT signing
# constant compiled into TrueLive Client up to at least 1.1.1
# (shomer_client/src/config.py:50, NEW-72). The constant itself ships in every
# Client binary; only this domain-separated digest is kept here.
_G1_DOMAIN = b"truelive-g1-client-jwt-constant-v1\x00"
CLIENT_JWT_CONSTANT_DIGESTS: Tuple[str, ...] = (
    "4d97a3cd6c423f338058e9dd77871bc976b087407aab37a439afe7ad612af8c5",
)

SERVICES = ("api", "websocket", "celery_worker", "celery_beat")


def fingerprint(value: Optional[str]) -> str:
    """``fp:<12 hex>`` of a secret, or ``unset``. Never the value."""
    if not value:
        return "unset"
    return "fp:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def resolve_settings_encryption_key(cfg: Any = None) -> Optional[str]:
    """SETTINGS_ENCRYPTION_KEY as every process resolves it: the process
    environment first (docker env_file), then Settings (a local .env file).
    An empty or blank value counts as missing."""
    value = os.environ.get("SETTINGS_ENCRYPTION_KEY")
    if not (value and value.strip()) and cfg is not None:
        value = getattr(cfg, "SETTINGS_ENCRYPTION_KEY", None)
    if value and value.strip():
        return value.strip()
    return None


def settings_key_status(key: Optional[str]) -> str:
    """``ok``, ``no_key`` or ``invalid_key`` (not a Fernet key)."""
    if not key:
        return KEY_MISSING
    try:
        from cryptography.fernet import Fernet

        Fernet(key.encode("utf-8"))
    except Exception:
        return KEY_INVALID
    return KEY_OK


def jwt_secret_is_client_constant(secret: Optional[str]) -> bool:
    """Gate G-1: True when JWT_SECRET is the Client's compiled-in constant."""
    if not secret:
        return False
    digest = hashlib.sha256(_G1_DOMAIN + secret.encode("utf-8")).hexdigest()
    return digest in CLIENT_JWT_CONSTANT_DIGESTS


def broker_endpoint(url: str) -> str:
    """``host:port`` of a broker URL, without userinfo (safe to log)."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or "?"
        port = parts.port or (6379 if parts.scheme.startswith("redis") else 0)
        return f"{host}:{port}"
    except Exception:
        return "?"


def check_broker(url: str, timeout: float = BROKER_TIMEOUT_SEC) -> Tuple[str, str]:
    """``("reachable" | "unreachable", detail)`` for a Redis broker URL.

    One PING with ``timeout`` seconds for connect and reply. The detail holds
    ``host:port`` and the exception class only, never the URL or password.
    """
    endpoint = broker_endpoint(url)
    try:
        import redis

        client = redis.Redis.from_url(
            url, socket_connect_timeout=timeout, socket_timeout=timeout, retry_on_timeout=False
        )
        try:
            client.ping()
        finally:
            client.close()
        return "reachable", endpoint
    except Exception as exc:  # any failure is "unreachable"; the cause class is enough
        return "unreachable", f"{endpoint} {type(exc).__name__}"


@dataclass
class StartupReport:
    service: str
    app_version: str
    flags: Dict[str, Any] = field(default_factory=dict)
    relay_sender_secret: str = "unset"
    # D30: the rotation window (off, open, closed, no_end, same_as_current),
    # its end (ISO 8601 UTC or ""), and the previous secret's fingerprint.
    jwt_rotation_window: str = "off"
    jwt_rotation_grace_until: str = ""
    jwt_secret_previous: str = "unset"
    pc_token_renew_before_days: int = 30
    settings_encryption_key: str = KEY_MISSING
    settings_encryption_key_fp: str = "unset"
    jwt_secret_is_client_constant: bool = False
    broker: str = "not_checked"
    broker_detail: str = ""
    problems: Dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def log_line(self) -> str:
        parts = [f"startup_check service={self.service}", f"version={self.app_version}"]
        parts += [f"{name}={_flag_text(value)}" for name, value in self.flags.items()]
        parts.append(f"relay_sender_secret={self.relay_sender_secret}")
        key = self.settings_encryption_key
        if self.settings_encryption_key_fp != "unset":
            key += " " + self.settings_encryption_key_fp
        parts.append(f"settings_encryption_key={key}")
        parts.append(f"jwt_secret_is_client_constant={_flag_text(self.jwt_secret_is_client_constant)}")
        # D30, after D28's fields (their order is parsed by deploy checks).
        window = self.jwt_rotation_window
        if self.jwt_rotation_grace_until:
            window += f" until {self.jwt_rotation_grace_until}"
        parts.append(f"jwt_rotation_window={window}")
        parts.append(f"jwt_secret_previous={self.jwt_secret_previous}")
        parts.append(f"pc_token_renew_before_days={self.pc_token_renew_before_days}")
        broker = self.broker + (f" ({self.broker_detail})" if self.broker_detail else "")
        parts.append(f"broker={broker}")
        return " ".join(parts)


def _flag_text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def collect(service: str, cfg: Any = None, check_broker_reachable: bool = False) -> StartupReport:
    """Build the report without logging."""
    if cfg is None:
        from app.core.config import settings as cfg  # imported late: tests pass their own

    report = StartupReport(service=service, app_version=str(getattr(cfg, "APP_VERSION", "?")))
    report.flags = {
        "deploy_inject_config_id": bool(cfg.DEPLOY_INJECT_CONFIG_ID),
        "relay_station_reports": "on" if cfg.RELAY_STATION_REPORTS else "off",
        "deploy_catchup_enabled": bool(cfg.DEPLOY_CATCHUP_ENABLED),
        "deploy_ack_timeout_sec": int(cfg.DEPLOY_ACK_TIMEOUT_SEC),
        "relay_debug_logging": bool(cfg.RELAY_DEBUG_LOGGING),
        "relay_require_current_token": bool(getattr(cfg, "RELAY_REQUIRE_CURRENT_TOKEN", False)),
    }
    report.relay_sender_secret = fingerprint(cfg.RELAY_SENDER_SECRET)
    if not cfg.RELAY_SENDER_SECRET:
        report.problems["relay_sender_secret"] = (
            "RELAY_SENDER_SECRET is unset: the relay accepts unauthenticated senders (legacy-open, D12)"
        )
    elif len(cfg.RELAY_SENDER_SECRET) < 32:
        report.problems["relay_sender_secret"] = (
            "RELAY_SENDER_SECRET is shorter than 32 characters; generate one with "
            "python -c \"import secrets; print(secrets.token_urlsafe(32))\""
        )

    key = resolve_settings_encryption_key(cfg)
    report.settings_encryption_key = settings_key_status(key)
    report.settings_encryption_key_fp = fingerprint(key)
    if report.settings_encryption_key == KEY_MISSING:
        report.problems["settings_encryption_key"] = (
            "SETTINGS_ENCRYPTION_KEY is missing: encrypted settings cannot be shared between processes "
            "and encrypted core.* settings refuse writes (C18, NEW-47)"
        )
    elif report.settings_encryption_key == KEY_INVALID:
        report.problems["settings_encryption_key"] = (
            "SETTINGS_ENCRYPTION_KEY is not a valid Fernet key (32 url-safe base64 bytes)"
        )

    _rotation_window(report, cfg)

    report.jwt_secret_is_client_constant = jwt_secret_is_client_constant(cfg.JWT_SECRET)
    if report.jwt_secret_is_client_constant:
        report.problems["jwt_secret"] = (
            "JWT_SECRET equals the signing constant compiled into TrueLive Client (gate G-1, NEW-72): "
            "any station can mint PC tokens. Do not rotate it at once; follow OD-23 and D30"
        )

    if check_broker_reachable:
        report.broker, report.broker_detail = check_broker(cfg.CELERY_BROKER_URL)
        if report.broker != "reachable":
            report.problems["broker"] = (
                f"Celery broker unreachable ({report.broker_detail}); the relay keeps serving, "
                "and broker-backed work (catch-up, D17) waits"
            )
    return report


def _rotation_window(report: StartupReport, cfg: Any) -> None:
    """D30: the dual-secret window as this process sees it (never a value)."""
    from app.services import relay_auth as window

    previous = getattr(cfg, "JWT_SECRET_PREVIOUS", None)
    report.jwt_secret_previous = fingerprint(previous)
    report.pc_token_renew_before_days = int(getattr(cfg, "PC_TOKEN_RENEW_BEFORE_DAYS", 30))
    report.jwt_rotation_window = window.window_state(cfg)
    until = window.grace_until(cfg)
    if until is not None:
        from datetime import datetime, timezone

        report.jwt_rotation_grace_until = datetime.fromtimestamp(until, tz=timezone.utc).isoformat()
    state = report.jwt_rotation_window
    if state == window.WINDOW_NO_END:
        report.problems["jwt_rotation_window_end"] = (
            "JWT_SECRET_PREVIOUS is set without JWT_ROTATION_GRACE_UNTIL: the previous secret is NOT accepted, "
            "so stations still on it cannot register. Set the window end (D30 runbook)"
        )
    elif state == window.WINDOW_SAME:
        report.problems["jwt_rotation_window"] = "JWT_SECRET_PREVIOUS equals JWT_SECRET: nothing to rotate; remove it"
    elif state == window.WINDOW_CLOSED:
        report.problems["jwt_rotation_window"] = (
            "The JWT rotation window has ended: tokens signed with JWT_SECRET_PREVIOUS are refused. "
            "Remove JWT_SECRET_PREVIOUS and JWT_ROTATION_GRACE_UNTIL"
        )
    if previous and jwt_secret_is_client_constant(previous) and state == window.WINDOW_OPEN:
        report.problems["jwt_secret_previous"] = (
            "JWT_SECRET_PREVIOUS is the signing constant compiled into TrueLive Client (G-1): until the window "
            "ends, tokens minted with it verify; they register only as outdated and are never renewed. "
            "Keep the window as short as the fleet allows"
        )


_SEVERITY = {
    "settings_encryption_key": logging.ERROR,
    "jwt_secret": logging.ERROR,
    "jwt_rotation_window": logging.WARNING,
    "jwt_rotation_window_end": logging.ERROR,
    "jwt_secret_previous": logging.WARNING,
    "relay_sender_secret": logging.WARNING,
    "broker": logging.WARNING,
}


def run_startup_check(
    service: str,
    check_broker_reachable: bool = False,
    cfg: Any = None,
    log: Optional[logging.Logger] = None,
) -> StartupReport:
    """Collect and log the report. Never raises: a failing check is logged."""
    log = log or logger
    try:
        report = collect(service, cfg=cfg, check_broker_reachable=check_broker_reachable)
    except Exception as exc:  # the self-check must never stop a service
        log.error("startup_check service=%s failed: %s", service, type(exc).__name__)
        report = StartupReport(service=service, app_version="?")
        report.problems["startup_check"] = type(exc).__name__
        return report
    log.info(report.log_line())
    for name, text in report.problems.items():
        log.log(_SEVERITY.get(name, logging.WARNING), "startup_check service=%s: %s", service, text)
    return report


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description="Print the startup self-check as JSON (no secret values).")
    parser.add_argument("--service", default="cli", help="service name to report (api, websocket, ...)")
    parser.add_argument("--check-broker", action="store_true", help="also PING the Celery broker")
    parser.add_argument("--strict", action="store_true", help="exit 1 when the key is missing or invalid, or G-1 fails")
    args = parser.parse_args(argv)
    report = collect(args.service, check_broker_reachable=args.check_broker)
    print(json.dumps(report.as_dict(), sort_keys=True))
    if args.strict and (report.settings_encryption_key != KEY_OK or report.jwt_secret_is_client_constant):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
