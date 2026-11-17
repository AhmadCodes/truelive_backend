"""
PC token renewal and the dual-secret rotation window (plan 06 D30; OD-23,
NEW-77, NEW-42, NEW-72). Relay part (M2); the Client part ships in M6.

PC tokens are HS256 JWTs ``{pc_id, name, exp, type: "pc_auth"}`` signed with
JWT_SECRET; they last JWT_PC_TOKEN_EXPIRE_HOURS (8760 h). Until D30 a token
could not be renewed, and rotating JWT_SECRET would have dropped every station
off the relay at the same moment (OD-23).

Verification (``verify_pc_token``). A token verifies with JWT_SECRET, and while
the rotation window is open also with JWT_SECRET_PREVIOUS. The window is open
only when JWT_SECRET_PREVIOUS is set, differs from JWT_SECRET, and the current
time is before JWT_ROTATION_GRACE_UNTIL. Without a window end the previous
secret is never accepted (fail closed: a forgotten setting must not keep an
old secret valid for ever). After the window, previous-secret tokens are
rejected like any other bad signature.

Renewal (relay, ``issue_or_resume``). A registered PC gets a fresh pc_auth
token, minted with the current secret for the session's pc_id, when the token
it presented was signed with the previous secret or expires within
PC_TOKEN_RENEW_BEFORE_DAYS. Only when all of these hold:

* the session declared the capability ``pc_token.v1`` and ``registered``
  advertised the feature ``pc_token.v1`` to it (Client >= 1.4.0, M6). A Client
  without the handler (1.1.x .. 1.3.x) is never sent a token, so its stored
  token is left alone; the relay flags it ``token_drop_needed`` and the fleet
  report lists it (B22's token-only drop or a technician);
* the presented token is the PC's stored token (``pcs.auth_token``) or the
  replaced token of the PC's open renewal. **An outdated token is never
  renewed**: when the rotation happens because the previous secret leaked
  (gate G-1), anyone can mint a previous-secret pc_auth token, and renewing it
  would turn it into a valid current-secret token that survives the rotation;
* the session is not a duplicate takeover and was checked against the DB.

The new token is stored in ``pcs.auth_token`` / ``token_expiry`` (stamped as a
``system`` update) and the sha256 of the presented token goes into
``pc_token_renewals`` until the PC confirms. Until then both tokens register
as current (OD-23 (b) accepts either). A PC that reconnects with the replaced
token gets the same new token again (no second mint: "renewed exactly once").
The token is sent only to the registered sid as ``pc_token_renewed {token,
exp}`` with an ACK. The renewal is confirmed (``confirmed_at``) by an ACK
``{"status": "stored"}`` or by the PC registering with the new token; that
clears the session's token flags. ``{"status": "refused", "reason": ...}`` is
audited and leaves the renewal open. Every issue, confirmation, refusal and
cancellation is audited without any token or digest.

``generate-token`` (an admin replacing the token in the portal) cancels the
PC's open renewals, so the replaced token stops counting as current.

Fleet view (``fleet_report``; CLI ``python -m app.services.pc_tokens report``):
per PC, which secret its stored token verifies with, whether it expires soon,
whether it can take a renewal, and the status ok / renewal_unconfirmed /
awaiting_reconnect / needs_drop / no_token. Counts and PC ids only, never a
token. Rotation runbook: DOCKER_SETUP.md, "PC token renewal and JWT_SECRET
rotation (D30)".
"""

import argparse
import hmac
import json
import logging
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import jwt

from app.models.pc import PC
from app.models.pc_tracking import PcHealth, PcTokenRenewal
from app.schemas import station_protocol
from app.services import audit_service, relay_auth
from app.services.actor import SYSTEM_ACTOR, ActorTriple, stamp_updated
from app.services.audit_service import ResourceType

logger = logging.getLogger(__name__)

PC_TOKEN_TYPE = "pc_auth"
FEATURE = station_protocol.FEATURE_PC_TOKEN  # "pc_token.v1": capability and feature

# Which secret verified a token.
SECRET_CURRENT = "current"
SECRET_PREVIOUS = "previous"

# Why a token is (re)issued.
REASON_PREVIOUS_SECRET = "previous_secret"
REASON_EXPIRING = "expiring"
REASON_RESEND = "resend"  # the open renewal's token, sent again (no new mint)
REASON_REISSUE = "reissue"  # the open renewal's token no longer qualifies (a second rotation)

# The push and its ACK (Client part, M6).
EVENT_RENEWED = "pc_token_renewed"
ACK_STORED = "stored"
ACK_REFUSED = "refused"
_ACK_REASON_RE = re.compile(r"^[a-z0-9_]{1,32}$")

# Rotation window states (startup line, fleet report): see relay_auth.
WINDOW_OFF = relay_auth.WINDOW_OFF
WINDOW_OPEN = relay_auth.WINDOW_OPEN
WINDOW_CLOSED = relay_auth.WINDOW_CLOSED
WINDOW_NO_END = relay_auth.WINDOW_NO_END
WINDOW_SAME = relay_auth.WINDOW_SAME

# Audit actions (resource pc, actor system unless an admin cancels).
AUDIT_ISSUED = "pc.token.renewed"
AUDIT_CONFIRMED = "pc.token.renewal.confirmed"
AUDIT_REFUSED = "pc.token.renewal.refused"
AUDIT_CANCELLED = "pc.token.renewal.cancelled"

# Fleet report statuses.
STATUS_OK = "ok"
STATUS_UNCONFIRMED = "renewal_unconfirmed"
STATUS_AWAITING = "awaiting_reconnect"
STATUS_NEEDS_DROP = "needs_drop"
STATUS_NO_TOKEN = "no_token"

DAY = 86400


def _settings(cfg: Any = None):
    if cfg is None:
        from app.core.config import settings

        return settings
    return cfg


def _now(now: Optional[float]) -> int:
    return int(time.time() if now is None else now)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# --------------------------------------------------------------------------- #
# The rotation window
# --------------------------------------------------------------------------- #


def grace_until(cfg: Any = None) -> Optional[int]:
    """JWT_ROTATION_GRACE_UNTIL as Unix seconds, or None."""
    return relay_auth.grace_until(_settings(cfg))


def window_state(cfg: Any = None, now: Optional[float] = None) -> str:
    return relay_auth.window_state(_settings(cfg), now)


def previous_secret_active(cfg: Any = None, now: Optional[float] = None) -> bool:
    """True while JWT_SECRET_PREVIOUS is accepted."""
    return window_state(cfg, now) == WINDOW_OPEN


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #


def verify_pc_token(token: str, cfg: Any = None, now: Optional[float] = None) -> Tuple[Dict[str, Any], str]:
    """Decode a PC token: ``(payload, SECRET_CURRENT | SECRET_PREVIOUS)``.

    Raises jwt.InvalidTokenError (ExpiredSignatureError, InvalidSignatureError,
    DecodeError, ...). The previous secret is tried only after a bad signature
    with the current one and only while the window is open, so an expired
    current-secret token stays expired.
    """
    cfg = _settings(cfg)
    try:
        return jwt.decode(token, cfg.JWT_SECRET, algorithms=[cfg.ALGORITHM]), SECRET_CURRENT
    except jwt.InvalidSignatureError:
        if not previous_secret_active(cfg, now):
            raise
    return jwt.decode(token, cfg.JWT_SECRET_PREVIOUS, algorithms=[cfg.ALGORITHM]), SECRET_PREVIOUS


def mint_pc_token(pc_id: str, name: Optional[str], cfg: Any = None, now: Optional[float] = None,
                  min_exp: Optional[int] = None) -> Tuple[str, int]:
    """A fresh pc_auth token signed with the current secret: ``(token, exp)``.

    ``exp`` is now + JWT_PC_TOKEN_EXPIRE_HOURS, and at least ``min_exp`` (the
    Client refuses a renewed token that expires before the one it holds).
    """
    cfg = _settings(cfg)
    exp = _now(now) + int(cfg.JWT_PC_TOKEN_EXPIRE_HOURS) * 3600
    if min_exp is not None and exp < min_exp:
        exp = int(min_exp)
    claims = {"pc_id": pc_id, "name": name or "", "exp": exp, "type": PC_TOKEN_TYPE}
    return jwt.encode(claims, cfg.JWT_SECRET, algorithm=cfg.ALGORITHM), exp


def renewal_reason(payload: Dict[str, Any], secret_used: str, cfg: Any = None,
                   now: Optional[float] = None) -> Optional[str]:
    """Why a verified token needs a new one, or None."""
    cfg = _settings(cfg)
    if secret_used == SECRET_PREVIOUS:
        return REASON_PREVIOUS_SECRET
    days = int(getattr(cfg, "PC_TOKEN_RENEW_BEFORE_DAYS", 30))
    exp = payload.get("exp") if isinstance(payload, dict) else None
    if days > 0 and _is_number(exp) and exp - _now(now) <= days * DAY:
        return REASON_EXPIRING
    return None


def can_take_renewal(capabilities: Sequence[str], features: Sequence[str]) -> bool:
    """The session declared ``pc_token.v1`` and ``registered`` advertised it."""
    return FEATURE in (capabilities or ()) and FEATURE in (features or ())


def parse_renewal_ack(ack: Any) -> Tuple[Optional[str], Optional[str]]:
    """``(ACK_STORED, None)``, ``(ACK_REFUSED, reason)`` or ``(None, None)``
    for anything else (no ACK payload is not a confirmation)."""
    if isinstance(ack, dict):
        status = ack.get("status")
        if status == ACK_STORED:
            return ACK_STORED, None
        if status == ACK_REFUSED:
            reason = ack.get("reason")
            return ACK_REFUSED, reason if isinstance(reason, str) and _ACK_REASON_RE.match(reason) else "unspecified"
    return None, None


def _usable_stored_token(pc: PC, cfg: Any, now: int) -> Optional[int]:
    """``exp`` of the PC's stored token when it is a current-secret pc_auth
    token for this PC that needs no renewal itself, else None."""
    if not pc.auth_token:
        return None
    try:
        payload = jwt.decode(pc.auth_token, cfg.JWT_SECRET, algorithms=[cfg.ALGORITHM])
    except jwt.InvalidTokenError:
        return None
    if payload.get("type") != PC_TOKEN_TYPE or payload.get("pc_id") != pc.id:
        return None
    if renewal_reason(payload, SECRET_CURRENT, cfg, now) is not None:
        return None
    exp = payload.get("exp")
    return int(exp) if _is_number(exp) else None


# --------------------------------------------------------------------------- #
# Renewal rows (run in the caller's transaction; the caller commits)
# --------------------------------------------------------------------------- #


@dataclass
class Renewal:
    """A token to send to the PC. ``token`` never appears in repr or logs."""

    renewal_id: int
    exp: int
    minted: bool
    reason: str
    token: str = field(repr=False)


def _audit(db, pc_id: str, action: str, changes: Dict[str, Any], actor: ActorTriple = SYSTEM_ACTOR) -> None:
    audit_service.record_event(db, action=action, resource_type=ResourceType.PC, resource_id=pc_id, actor=actor,
                               changes=changes)


def open_renewals(db, pc_id: str) -> List[PcTokenRenewal]:
    """The PC's unconfirmed renewals, newest first."""
    return (
        db.query(PcTokenRenewal)
        .filter(PcTokenRenewal.pc_id == pc_id, PcTokenRenewal.confirmed_at.is_(None))
        .order_by(PcTokenRenewal.issued_at.desc(), PcTokenRenewal.id.desc())
        .all()
    )


def _matching(rows: Sequence[PcTokenRenewal], token: Optional[str]) -> Optional[PcTokenRenewal]:
    digest = relay_auth.token_digest(token)
    if digest is None:
        return None
    for row in rows:
        if hmac.compare_digest(str(row.previous_token_sha256), digest):
            return row
    return None


def match_open_renewal(db, pc_id: str, token: Optional[str]) -> Optional[int]:
    """Id of the open renewal that replaced ``token``, or None (OD-23 (b)
    accepts the replaced token until the PC confirms the new one)."""
    row = _matching(open_renewals(db, pc_id), token)
    return row.id if row is not None else None


def _confirm_rows(db, pc_id: str, rows: Sequence[PcTokenRenewal], via: str, now: Optional[float]) -> List[int]:
    if not rows:
        return []
    ts = _now(now)
    for row in rows:
        row.confirmed_at = ts
    ids = [row.id for row in rows]
    _audit(db, pc_id, AUDIT_CONFIRMED, {"renewal_ids": ids, "via": via})
    return ids


def close_open_renewals(db, pc_id: str, *, via: str, now: Optional[float] = None) -> List[int]:
    """The PC registered with its stored token, which is the renewed one: it
    holds the new token, so its open renewals are confirmed."""
    return _confirm_rows(db, pc_id, open_renewals(db, pc_id), via, now)


@dataclass
class PresentedToken:
    """The database half of a register's token check (OD-23 + D30)."""

    current: bool  # the stored token, or the replaced token of an open renewal
    via_renewal: Optional[int] = None  # the open renewal whose replaced token was presented
    confirmed: List[int] = field(default_factory=list)  # renewals the stored (renewed) token confirmed


def check_presented_token(db, pc_id: str, stored_token: Optional[str], presented: Optional[str],
                          now: Optional[float] = None) -> PresentedToken:
    """Compare the presented token with the stored one and the PC's open
    renewals (one query, usually empty). Presenting the stored token while a
    renewal is open confirms it: the stored token is the renewed one."""
    rows = open_renewals(db, pc_id)
    if relay_auth.same_token(presented, stored_token):
        return PresentedToken(current=True, confirmed=_confirm_rows(db, pc_id, rows, "register", now))
    match = _matching(rows, presented)
    return PresentedToken(current=match is not None, via_renewal=match.id if match is not None else None)


def issue_or_resume(db, pc_id: str, presented_token: str, reason: Optional[str], *, cfg: Any = None,
                    now: Optional[float] = None) -> Optional[Renewal]:
    """The token to send to a PC that registered with ``presented_token``.

    * The presented token is the replaced token of an open renewal: that
      renewal's token (the stored one) again, without a new mint, while it is
      still usable; else a fresh token (``reissue``).
    * The presented token is the stored token and ``reason`` is set: a fresh
      token, stored in pcs, a renewal row with the presented token's digest,
      and an audit entry. Older open renewals of the PC are replaced.
    * Anything else (no reason, an outdated token, an unknown PC): None.

    The PC row is locked (FOR UPDATE) so concurrent registers of one PC
    serialise. The caller commits.
    """
    cfg = _settings(cfg)
    ts = _now(now)
    pc = db.query(PC).filter(PC.id == pc_id).with_for_update().first()
    if pc is None or not presented_token:
        return None
    rows = open_renewals(db, pc_id)
    match = _matching(rows, presented_token)
    if match is not None:
        stored_exp = _usable_stored_token(pc, cfg, ts)
        if stored_exp is not None:
            return Renewal(renewal_id=match.id, exp=stored_exp, minted=False, reason=REASON_RESEND,
                           token=pc.auth_token)
        reason = REASON_REISSUE
    elif not relay_auth.same_token(presented_token, pc.auth_token):
        return None  # outdated: never renewed (module docstring)
    if reason is None:
        return None

    presented_exp = None
    try:
        claims = jwt.decode(presented_token, options={"verify_signature": False})
        if _is_number(claims.get("exp")):
            presented_exp = int(claims["exp"]) + 1
    except jwt.InvalidTokenError:
        pass
    token, exp = mint_pc_token(pc_id, pc.name, cfg, ts, min_exp=presented_exp)
    replaced = [row.id for row in rows]
    for row in rows:
        db.delete(row)
    renewal = PcTokenRenewal(pc_id=pc_id, previous_token_sha256=relay_auth.token_digest(presented_token),
                             issued_at=ts)
    db.add(renewal)
    pc.auth_token = token
    pc.token_expiry = exp
    stamp_updated(pc, SYSTEM_ACTOR)  # a credential change by the system, not by the last human editor
    db.flush()
    changes: Dict[str, Any] = {"renewal_id": renewal.id, "reason": reason, "token_expiry": exp}
    if replaced:
        changes["replaced_renewal_ids"] = replaced
    _audit(db, pc_id, AUDIT_ISSUED, changes)
    return Renewal(renewal_id=renewal.id, exp=exp, minted=True, reason=reason, token=token)


def confirm_renewal(db, pc_id: str, renewal_id: int, *, via: str = "ack", now: Optional[float] = None) -> bool:
    """Close renewal ``renewal_id`` of ``pc_id``; False when it is gone
    (cancelled by an admin) or already confirmed."""
    row = (
        db.query(PcTokenRenewal)
        .filter(PcTokenRenewal.id == renewal_id, PcTokenRenewal.pc_id == pc_id)
        .with_for_update()
        .first()
    )
    if row is None or row.confirmed_at is not None:
        return False
    row.confirmed_at = _now(now)
    _audit(db, pc_id, AUDIT_CONFIRMED, {"renewal_ids": [renewal_id], "via": via})
    return True


def record_refusal(db, pc_id: str, renewal_id: int, reason: str) -> None:
    """The Client refused the token (another pc_id, an older expiry, a wrong
    type). The renewal stays open; the old token keeps working."""
    _audit(db, pc_id, AUDIT_REFUSED, {"renewal_id": renewal_id, "reason": reason})


def cancel_open_renewals(db, pc_id: str, actor: ActorTriple, via: str = "generate_token") -> List[int]:
    """An admin replaced the PC's token: the open renewals' replaced tokens
    stop counting as current. Audited with the admin as actor."""
    rows = open_renewals(db, pc_id)
    if not rows:
        return []
    ids = [row.id for row in rows]
    for row in rows:
        db.delete(row)
    _audit(db, pc_id, AUDIT_CANCELLED, {"renewal_ids": ids, "via": via}, actor=actor)
    return ids


# --------------------------------------------------------------------------- #
# Fleet report (rotation runbook: "watch the count of PCs on the old secret")
# --------------------------------------------------------------------------- #


def classify_token(token: Optional[str], pc_id: str, cfg: Any = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Which secret a stored token verifies with (whatever the window says),
    and whether it is usable: ``{secret, state, exp}``.

    secret: none, current, previous, unknown. state: ok, expired, wrong_type,
    wrong_pc, invalid.
    """
    cfg = _settings(cfg)
    if not token:
        return {"secret": "none", "state": "invalid", "exp": None}
    payload = None
    secret = "unknown"
    keys = [(SECRET_CURRENT, cfg.JWT_SECRET)]
    previous = getattr(cfg, "JWT_SECRET_PREVIOUS", None)
    if previous and previous != cfg.JWT_SECRET:
        keys.append((SECRET_PREVIOUS, previous))
    for name, key in keys:
        try:
            payload = jwt.decode(token, key, algorithms=[cfg.ALGORITHM], options={"verify_exp": False})
            secret = name
            break
        except jwt.InvalidSignatureError:
            continue
        except jwt.InvalidTokenError:
            return {"secret": "unknown", "state": "invalid", "exp": None}
    if payload is None:
        return {"secret": secret, "state": "invalid", "exp": None}
    exp = int(payload["exp"]) if _is_number(payload.get("exp")) else None
    if payload.get("type") != PC_TOKEN_TYPE:
        state = "wrong_type"
    elif payload.get("pc_id") != pc_id:
        state = "wrong_pc"
    elif exp is not None and exp <= _now(now):
        state = "expired"
    else:
        state = "ok"
    return {"secret": secret, "state": state, "exp": exp}


def fleet_report(db, cfg: Any = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Per-PC token status for the rotation runbook. Never holds a token."""
    cfg = _settings(cfg)
    ts = _now(now)
    days = int(getattr(cfg, "PC_TOKEN_RENEW_BEFORE_DAYS", 30))
    rows = (
        db.query(PC.id, PC.name, PC.auth_token, PcHealth.capabilities, PcHealth.relay_flags)
        .outerjoin(PcHealth, PcHealth.pc_id == PC.id)
        .order_by(PC.id)
        .all()
    )
    open_pcs = {pc_id for (pc_id,) in db.query(PcTokenRenewal.pc_id).filter(PcTokenRenewal.confirmed_at.is_(None))}
    counts = {s: 0 for s in (STATUS_OK, STATUS_UNCONFIRMED, STATUS_AWAITING, STATUS_NEEDS_DROP, STATUS_NO_TOKEN)}
    secrets_count = {"current": 0, "previous": 0, "unknown": 0, "none": 0}
    listed: List[Dict[str, Any]] = []
    for pc_id, name, token, capabilities, relay_flags in rows:
        info = classify_token(token, pc_id, cfg, ts)
        secrets_count[info["secret"]] += 1
        flags = relay_flags if isinstance(relay_flags, dict) else {}
        can_renew = isinstance(capabilities, list) and FEATURE in capabilities
        needs: List[str] = []
        if info["secret"] == SECRET_PREVIOUS:
            needs.append("previous_secret")
        if info["secret"] in ("unknown",) or info["state"] in ("invalid", "wrong_type", "wrong_pc"):
            if info["secret"] != "none":
                needs.append("invalid_token")
        if info["state"] == "expired":
            needs.append("expired")
        elif info["exp"] is not None and days > 0 and info["exp"] - ts <= days * DAY:
            needs.append("expiring")
        if flags.get("token_outdated"):
            needs.append("outdated")
        if flags.get("token_drop_needed"):
            needs.append("relay_flagged")
        if info["secret"] == "none":
            status = STATUS_NO_TOKEN
        elif pc_id in open_pcs:
            status = STATUS_UNCONFIRMED
        elif not needs:
            status = STATUS_OK
        elif can_renew and set(needs) <= {"previous_secret", "expiring"}:
            status = STATUS_AWAITING
        else:
            status = STATUS_NEEDS_DROP
        counts[status] += 1
        if status != STATUS_OK:
            listed.append({"pc_id": pc_id, "name": name, "status": status, "needs": needs, "can_renew": can_renew,
                           "token_expiry": info["exp"]})
    until = grace_until(cfg)
    return {
        "generated_at": ts,
        "window": window_state(cfg, ts),
        "grace_until": until,
        "renew_before_days": days,
        "total": len(rows),
        "counts": counts,
        "secrets": secrets_count,
        "pcs": listed,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="PC token renewal status (D30). Prints JSON; never a token.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("report", help="per-PC token status for the JWT_SECRET rotation runbook")
    args = parser.parse_args(argv)
    if args.command == "report":
        from app.database import SessionLocal

        db = SessionLocal()
        try:
            print(json.dumps(fleet_report(db), sort_keys=True, indent=2))
        finally:
            db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
