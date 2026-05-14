"""
truelive-smtp-ingest — LMTP server that accepts mail from Postfix and enqueues
a Celery task for parsing.

Postfix accepts mail on :25 (alerts.usvg.ai MX) and hands it off via LMTP to the
unix socket configured in ALERT_LMTP_SOCKET. This service:

1. Validates the recipient (`<token>@alerts.usvg.ai`) against alert_addresses.
2. Enforces per-address rate limiting and the quarantine flag.
3. Streams the raw bytes to MinIO BEFORE acknowledging — never 250 without a
   durable persist (spec §4.4 "persist-before-ACK").
4. Writes the raw_messages row, ACKs LMTP 250.
5. Enqueues `process_inbound_alert` on the `alert_parse` queue.

Run as a separate process:

    python -m app.services.smtp_ingest

Horizontally scalable: multiple replicas can share a unix socket via Postfix
transport configured to round-robin between them (rare) — more commonly, run one
process per host and scale by adding hosts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import uuid
from datetime import datetime, timezone
from typing import Optional

from aiosmtpd.controller import UnixSocketController
from aiosmtpd.lmtp import LMTP
from aiosmtpd.smtp import Envelope, Session
from sqlalchemy.orm import Session as SASession

from app.core.config import settings
from app.database import SessionLocal
from app.models.alerting import AlertAddress, RawMessage
from app.services.minio_client import storage, MinioClientError
from app.utils.rate_limiter import rate_limiter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------- #
# Auth / persistence helpers
# ---------------------------------------------------------------------------- #

def _lookup_alert_address(db: SASession, local: str, domain: str) -> Optional[AlertAddress]:
    return (
        db.query(AlertAddress)
        .filter(
            AlertAddress.local_part == local,
            AlertAddress.domain == domain,
            AlertAddress.is_active == True,  # noqa: E712
        )
        .first()
    )


def _persist_raw_message(
    db: SASession,
    *,
    msg_id: str,
    received_at: datetime,
    envelope: Envelope,
    session: Session,
    storage_uri: str,
    alert_address: AlertAddress,
) -> RawMessage:
    auth = _parse_auth_results_headers(envelope.content or b"")
    sender_ip = None
    try:
        # session.peer = (host, port) for TCP; for unix socket peer is the path.
        peer = getattr(session, "peer", None)
        if isinstance(peer, tuple) and len(peer) >= 1:
            sender_ip = peer[0]
    except Exception:
        sender_ip = None

    row = RawMessage(
        id=msg_id,
        received_at=received_at,
        envelope_from=envelope.mail_from,
        envelope_to=(envelope.rcpt_tos[0] if envelope.rcpt_tos else None),
        camera_id=alert_address.camera_id,
        alert_address_id=alert_address.id,
        size_bytes=len(envelope.content or b""),
        storage_uri=storage_uri,
        sender_ip=sender_ip,
        helo=session.host_name,
        spf_result=auth.get("spf"),
        dkim_result=auth.get("dkim"),
        dmarc_result=auth.get("dmarc"),
        status="received",
    )
    db.add(row)
    db.commit()
    return row


def _parse_auth_results_headers(raw: bytes) -> dict[str, str]:
    """
    Extract spf/dkim/dmarc results from the `Authentication-Results` header line.

    Very lenient: just walks the top of the message text-mode and pulls the first
    `Authentication-Results:` block. If we miss it, we store NULLs — not the end
    of the world; spec §6.2 marks these as nullable.
    """
    out: dict[str, str] = {}
    if not raw:
        return out
    # Stop at the body; headers end at the first blank line.
    try:
        head = raw.split(b"\r\n\r\n", 1)[0]
        text = head.decode("ascii", errors="replace")
    except Exception:
        return out
    lower = text.lower()
    for key in ("spf", "dkim", "dmarc"):
        token = f"{key}="
        idx = lower.find(token)
        if idx == -1:
            continue
        rest = text[idx + len(token):]
        # Result token ends at whitespace, semicolon, or paren.
        end = 0
        for end, ch in enumerate(rest):
            if ch in " \t;(\n\r,":
                break
        else:
            end = len(rest)
        out[key] = rest[:end].strip().lower() or None
    return out


def _enqueue_parse(msg_id: str) -> None:
    """Enqueue the parse worker. Imported lazily to avoid Celery import at startup."""
    try:
        from app.tasks.process_inbound_alert import process_inbound_alert
        process_inbound_alert.apply_async(args=[msg_id], queue="alert_parse")
    except Exception:
        # Best-effort: the reconciliation loop will pick it up if Celery hiccups.
        logger.exception("failed to enqueue process_inbound_alert", extra={"msg_id": msg_id})


# ---------------------------------------------------------------------------- #
# LMTP handler
# ---------------------------------------------------------------------------- #

class AlertLMTPHandler:
    """aiosmtpd handler — see https://aiosmtpd.aio-libs.org/."""

    async def handle_RCPT(
        self,
        server,
        session: Session,
        envelope: Envelope,
        address: str,
        rcpt_options,
    ) -> str:
        local, _, domain = address.partition("@")
        local = local.lower()
        domain = domain.lower()

        with SessionLocal() as db:
            row = await asyncio.to_thread(_lookup_alert_address, db, local, domain)
            if row is None:
                return "550 5.1.1 No such recipient"
            if row.is_quarantined:
                return "550 5.7.1 Recipient quarantined"

            allowed = await asyncio.to_thread(rate_limiter.allow, str(row.id))
            if not allowed:
                logger.warning(
                    "rate-limited RCPT TO",
                    extra={"alert_address_id": str(row.id), "address": address},
                )
                return "451 4.7.1 Rate limit exceeded, try later"

            # Stash on the session for handle_DATA.
            envelope.rcpt_tos.append(address)
            session.alert_address_id = row.id
            session.alert_local = local
            session.alert_domain = domain
            session.camera_id = row.camera_id
        return "250 OK"

    async def handle_DATA(
        self, server, session: Session, envelope: Envelope,
    ) -> str:
        msg_id = str(uuid.uuid4())
        received_at = datetime.now(timezone.utc)
        content = envelope.content or b""

        if len(content) > settings.ALERT_MAX_MESSAGE_SIZE:
            return "552 5.3.4 Message too large"

        # 1. Stream to MinIO. If this fails, return 451 so Postfix queues + retries.
        try:
            storage_uri = await asyncio.to_thread(
                storage.put_raw_mail, msg_id, content, received_at,
            )
        except MinioClientError:
            logger.exception("MinIO put failed; returning 451", extra={"msg_id": msg_id})
            return "451 4.3.0 Temporary storage failure"

        # 2. Persist raw_messages row. Same failure semantics.
        try:
            with SessionLocal() as db:
                alert_address_id = getattr(session, "alert_address_id", None)
                if not alert_address_id:
                    return "451 4.7.0 Internal session state lost; retry"
                # Re-load the row in this session.
                row = db.get(AlertAddress, alert_address_id)
                if row is None:
                    return "550 5.1.1 Recipient revoked mid-transaction"
                await asyncio.to_thread(
                    _persist_raw_message,
                    db,
                    msg_id=msg_id,
                    received_at=received_at,
                    envelope=envelope,
                    session=session,
                    storage_uri=storage_uri,
                    alert_address=row,
                )
        except Exception:
            logger.exception(
                "raw_messages persist failed; returning 451",
                extra={"msg_id": msg_id},
            )
            return "451 4.3.0 Temporary persistence failure"

        # 3. ACK first — once we say 250 the sender forgets the message.
        # 4. Then enqueue. If enqueue fails, reconciliation sweep picks it up.
        await asyncio.to_thread(_enqueue_parse, msg_id)
        logger.info(
            "accepted message",
            extra={
                "msg_id": msg_id,
                "alert_address_id": getattr(session, "alert_address_id", None),
                "camera_id": getattr(session, "camera_id", None),
                "size_bytes": len(content),
            },
        )
        return f"250 OK <{msg_id}>"


# ---------------------------------------------------------------------------- #
# Reconciliation
# ---------------------------------------------------------------------------- #

def reconcile_stuck_messages(*, older_than_seconds: int = 60) -> int:
    """
    Sweep raw_messages where status='received' and received_at < (now - threshold).
    These got persisted but the Celery enqueue didn't take. Re-enqueue them.

    Returns the count re-enqueued.
    """
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)
    count = 0
    with SessionLocal() as db:
        stuck = (
            db.query(RawMessage)
            .filter(RawMessage.status == "received", RawMessage.received_at < cutoff)
            .all()
        )
        for row in stuck:
            _enqueue_parse(row.id)
            count += 1
    if count:
        logger.info("reconciled stuck messages", extra={"count": count})
    return count


# ---------------------------------------------------------------------------- #
# Entrypoint
# ---------------------------------------------------------------------------- #

def _make_controller() -> UnixSocketController:
    socket_path = settings.ALERT_LMTP_SOCKET
    # Ensure parent dir exists. Postfix usually sets /var/run/truelive ownership.
    parent = os.path.dirname(socket_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # Remove stale socket from a previous crash.
    if os.path.exists(socket_path):
        try:
            os.unlink(socket_path)
        except OSError:
            pass

    handler = AlertLMTPHandler()
    controller = UnixSocketController(
        handler=handler,
        unix_socket=socket_path,
        server_kwargs={"factory": LMTP},
    )
    return controller


def main() -> int:  # pragma: no cover — process entrypoint
    logging.basicConfig(
        level=settings.LOG_LEVEL,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    controller = _make_controller()

    stop_event = asyncio.Event()
    loop = asyncio.new_event_loop()

    def _shutdown(*_args):
        logger.info("shutdown signal received")
        loop.call_soon_threadsafe(stop_event.set)

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _shutdown)

    async def _run():
        # Reconcile on startup — handles the rare ACK-but-no-enqueue edge case
        # from a previous crashed run.
        try:
            await asyncio.to_thread(reconcile_stuck_messages)
        except Exception:
            logger.exception("startup reconciliation failed")
        controller.start()
        logger.info(
            "LMTP listening on %s", settings.ALERT_LMTP_SOCKET,
        )
        try:
            await stop_event.wait()
        finally:
            controller.stop()
            try:
                if os.path.exists(settings.ALERT_LMTP_SOCKET):
                    os.unlink(settings.ALERT_LMTP_SOCKET)
            except OSError:
                pass

    try:
        loop.run_until_complete(_run())
    finally:
        loop.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
