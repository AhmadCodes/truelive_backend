"""
Deploy path: layouts from the portal to stations, tracked per deploy
(plan 06 D14; NEW-45, NEW-46, NEW-54).

``deploy_config_to_pcs`` replaces ``pcs._deploy_config_to_pcs``. For each PC:

1. The layout is generated (``config_generator``). A PC with no layout is
   ``skipped``; a generation error is ``failed``. Neither gets a row.
2. A uuid4 ``config_id`` is minted and, with ``DEPLOY_INJECT_CONFIG_ID``
   (default true; the M2 kill switch), put at the payload root. The row
   ``pc_deployments(id=config_id, status=pending)`` is inserted with the
   layout's content hash, screen count and trigger; the PC's older in-flight
   rows are superseded; one ``config.deployed`` audit entry per PC is added;
   and all of it is **committed before anything is sent**. The PC rows are
   locked (``FOR UPDATE``, in id order) while this happens, so two concurrent
   deploys to one PC are serialised and the later one supersedes the earlier.
3. Rows a concurrent deploy superseded in the meantime are not sent.
4. One authenticated sender session (D12) emits ``deploy_config`` per PC with
   an ACK callback (the ``sio.call`` contract, 5 s), in rounds of at most 8
   calls and 750 000 encoded bytes (the relay refuses a larger polling POST),
   so a silent relay costs one 5 s timeout per batch. The relay
   answers ``delivered`` (with ``confirmable``), ``offline`` or ``error``, and
   forwards the layout to the PC as the ``config`` event; the PC's ACK makes
   the relay mark the row ``received`` (deploy_tracking.record_receipt).
5. The answers become conditional transitions (deploy_tracking): delivered,
   offline or relay_failed (no answer or unreachable). A relay that ran ahead
   (received or accepted already) is never moved back.

``pcs.last_applied`` is no longer stamped here: the relay stamps it when a
Client 1.1.x row is received, and D15 when a confirmable row is applied.

The response never carries the generated layout (it holds camera URLs with
credentials, NEW-45): per PC it is ``{pc_id, status, delivery, config_id,
confirmable, message, screens, ack_timeout_sec}``. ``status`` keeps the old vocabulary
(deployed, skipped, failed): an offline PC is ``failed`` with
``delivery=offline`` (NEW-46).

Blocking: everything here is synchronous (psycopg2, python-socketio Client).
The two routes that call it are plain ``def``, so FastAPI runs them in its
threadpool and the event loop stays free (``/health`` keeps answering).
"""

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from app.core.config import settings
from app.models.pc import PC
from app.models.pc_tracking import PcDeployment
from app.models.screen_layout import ScreenLayout
from app.services import audit_service, deploy_tracking
from app.services.actor import SYSTEM_ACTOR, ActorTriple, stamp_created
from app.services.audit_service import AuditAction, ResourceType
from app.services.relay_auth import sender_auth

logger = logging.getLogger(__name__)

# Deploy triggers (pc_deployments.trigger).
TRIGGER_PC_DEPLOY = "pc_deploy"
TRIGGER_LAYOUT_DEPLOY = "layout_deploy"
TRIGGER_CATCH_UP = "catch_up"  # D17

# The sender event the relay serves (websocket_server.deploy_config) and the
# ``sio.call`` timeout of the card.
RELAY_EVENT = "deploy_config"
# Stream providers: the same layout with renewed addresses. The relay forwards
# it as the config event and writes no deploy row (websocket_server.refresh_config).
REFRESH_EVENT = "refresh_config"
REFRESH_TASK = "app.tasks.stream_provider_tasks.refresh_pc"
REFRESH_FOLLOW_UP_SEC = 10
# engine.io logs two ERROR lines on every healthy send ("websocket-client package not installed,
# only polling transport is available", and "packet queue is empty, aborting" at the disconnect).
# The sender polls on purpose (SENDER_WINDOW) and reports its own failures, so its transport is silent.
_SENDER_TRANSPORT_LOG = logging.getLogger("truelive.sender.transport")
_SENDER_TRANSPORT_LOG.setLevel(logging.CRITICAL)
RELAY_CALL_TIMEOUT = 5.0
# python-engineio 4.x refuses a polling POST that carries more than 16 packets
# ("Too many packets in payload"), and the API's client polls (websocket-client
# is not in requirements.txt), sending everything queued in one POST. So at
# most this many deploy_config calls are in flight at once; found by D17 with
# 50 PCs (every one ended relay_failed).
SENDER_WINDOW = 8
# The window also has a byte budget (M2 review): engine.io packs every queued
# packet into one polling POST, and the relay refuses a POST larger than its
# max_http_buffer_size with HTTP 400 and closes the session, so a round of 5
# production-sized layouts (about 216 KB each) was lost whole. A round holds
# as many requests as fit in SENDER_ROUND_BYTES (always at least one), which
# stays under python-engineio's default limit of 1 000 000 bytes, so it holds
# for a relay that still runs with the default too.
SENDER_ROUND_BYTES = 750_000
# The relay's max_http_buffer_size (websocket_server, also the websocket
# frame limit). One request larger than this is never sent: the relay would
# drop the whole session. It is answered locally as an error instead.
RELAY_MAX_HTTP_BUFFER_BYTES = 4_000_000
# Room for the engine.io / Socket.IO framing of one packet ("42<ack id>" and
# the record separator).
PACKET_OVERHEAD_BYTES = 32
REASON_TOO_LARGE = "layout too large for the relay"

# Relay answers (``delivery`` in the response).
DELIVERY_DELIVERED = "delivered"
DELIVERY_OFFLINE = "offline"
DELIVERY_ERROR = "error"
RELAY_ANSWERS = (DELIVERY_DELIVERED, DELIVERY_OFFLINE, DELIVERY_ERROR)

# Response statuses (unchanged vocabulary).
STATUS_DEPLOYED = "deployed"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

MSG_NO_LAYOUT = "No screen layout is assigned to this deployment target"
MSG_GENERATION_FAILED = "Failed to generate the display configuration"
MSG_SUPERSEDED = "A newer deploy to this PC replaced this one before it was sent"
MSG_UNREACHABLE = "Could not reach the display client service to deploy the configuration"
MSG_CONTENT_CHANGED = "The layout changed since the deploy being re-pushed; nothing was sent"  # D17

DETAIL_OFFLINE = "PC not connected to the relay"
DETAIL_NO_ANSWER = "relay did not answer"
DETAIL_UNREACHABLE = "relay unreachable"


class RelayUnreachable(Exception):
    """The sender session could not be opened."""


# A sender takes the list of deploy_config requests and returns, per
# config_id, the relay's answer (a dict) or None when none arrived in time.
Sender = Callable[[List[Dict[str, Any]]], Dict[str, Optional[Any]]]


def relay_url() -> str:
    """URL of the relay as seen from the API (the compose service name when
    the relay binds every interface)."""
    host = "websocket" if settings.WEBSOCKET_HOST == "0.0.0.0" else settings.WEBSOCKET_HOST
    return f"http://{host}:{settings.WEBSOCKET_PORT}"


def encoded_size(request: Mapping[str, Any]) -> int:
    """Bytes one ``deploy_config`` request takes in a polling POST: the
    Socket.IO JSON encoding (compact separators, ASCII escapes) plus the
    framing allowance."""
    body = json.dumps([RELAY_EVENT, request], separators=(",", ":"), ensure_ascii=True, default=str)
    return len(body) + PACKET_OVERHEAD_BYTES


def plan_rounds(requests: Sequence[Mapping[str, Any]], *, window: int = SENDER_WINDOW,
                round_bytes: int = SENDER_ROUND_BYTES) -> List[List[int]]:
    """Indexes of ``requests`` per round: at most ``window`` requests and
    ``round_bytes`` encoded bytes each, in order, and at least one request
    per round (a single large request goes alone)."""
    window = max(1, int(window))
    rounds: List[List[int]] = []
    current: List[int] = []
    used = 0
    for index, request in enumerate(requests):
        size = encoded_size(request)
        if current and (len(current) >= window or used + size > round_bytes):
            rounds.append(current)
            current, used = [], 0
        current.append(index)
        used += size
    if current:
        rounds.append(current)
    return rounds


def socketio_sender(requests: List[Dict[str, Any]], *, url: Optional[str] = None,
                    timeout: float = RELAY_CALL_TIMEOUT, window: int = SENDER_WINDOW,
                    round_bytes: int = SENDER_ROUND_BYTES,
                    max_request_bytes: int = RELAY_MAX_HTTP_BUFFER_BYTES,
                    event: str = RELAY_EVENT, id_key: str = "config_id") -> Dict[str, Optional[Any]]:
    """Send every request over one authenticated sender session.

    ``event`` and ``id_key`` name the relay event and the request field that
    identifies a request (``refresh_sender`` sends ``refresh_config`` keyed by
    ``refresh_id``); the text below describes the default, ``deploy_config``.

    Each ``deploy_config`` is emitted with an ACK callback, in rounds of at
    most ``window`` calls (SENDER_WINDOW) and ``round_bytes`` encoded bytes
    (SENDER_ROUND_BYTES); each round gets ``timeout`` seconds to answer. A
    round the relay leaves unanswered ends the batch: the requests not sent
    yet get no answer (relay_failed) instead of one timeout per round. A
    request larger than ``max_request_bytes`` is not sent (the relay would
    close the session) and is answered locally with ``error`` /
    REASON_TOO_LARGE. Raises RelayUnreachable when the session cannot be
    opened (also a wrong RELAY_SENDER_SECRET: the relay refuses the CONNECT).
    """
    import socketio

    answers: Dict[str, Optional[Any]] = {}
    sendable: List[Dict[str, Any]] = []
    for request in requests:
        size = encoded_size(request)
        if size > max_request_bytes:
            config_id = request[id_key]
            logger.error(f"Deploy {config_id} to PC {request.get('targetId')} not sent: {size} bytes is more than "
                         f"the relay accepts ({max_request_bytes})")
            answers[config_id] = {"status": DELIVERY_ERROR, "config_id": config_id, "confirmable": False,
                                  "reason": REASON_TOO_LARGE}
        else:
            sendable.append(request)
    if not sendable:
        return {request[id_key]: answers.get(request[id_key]) for request in requests}

    client = socketio.Client(reconnection=False, logger=False, engineio_logger=_SENDER_TRANSPORT_LOG)
    target = url or relay_url()
    try:
        logger.info(f"Connecting to display client service at {target}")
        # D12: the sender secret goes in the CONNECT auth payload, never the URL.
        client.connect(target, auth=sender_auth(settings.RELAY_SENDER_SECRET), wait_timeout=timeout)
    except Exception as exc:
        logger.error(f"Could not reach display client service: {type(exc).__name__}")
        raise RelayUnreachable(type(exc).__name__) from exc

    waits: Dict[str, threading.Event] = {}
    rounds = plan_rounds(sendable, window=window, round_bytes=round_bytes)
    try:
        for number, indexes in enumerate(rounds):
            round_waits = []
            for request in (sendable[i] for i in indexes):
                config_id = request[id_key]
                done = threading.Event()
                waits[config_id] = done
                round_waits.append(done)

                def _on_answer(*args, _config_id=config_id, _done=done):
                    answers[_config_id] = args[0] if args else None
                    _done.set()

                try:
                    client.emit(event, request, callback=_on_answer)
                except Exception as exc:
                    logger.error(f"Could not send deploy {config_id}: {type(exc).__name__}")
                    done.set()
            deadline = time.monotonic() + timeout
            for done in round_waits:
                done.wait(max(0.0, deadline - time.monotonic()))
            if not all(done.is_set() for done in round_waits):
                rest = [sendable[i] for later in rounds[number + 1:] for i in later]
                if rest:
                    logger.error(f"The relay left deploys unanswered; {len(rest)} more not sent")
                for request in rest:
                    waits.setdefault(request[id_key], threading.Event())
                break
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
    return {request[id_key]: answers.get(request[id_key]) for request in requests}


def refresh_sender(requests: List[Dict[str, Any]]) -> Dict[str, Optional[Any]]:
    """Send ``refresh_config`` requests ``{refresh_id, targetId, content}``;
    answers per refresh_id."""
    return socketio_sender(requests, event=REFRESH_EVENT, id_key="refresh_id")


@dataclass
class _Target:
    pc_id: str
    layout_id: Optional[str]
    config_id: Optional[str] = None
    payload: Optional[Dict[str, Any]] = None
    config_hash: Optional[str] = None
    screens: int = 0
    result: Optional[Dict[str, Any]] = None
    answer: Optional[Dict[str, Any]] = field(default=None)
    # Stream providers: set only when the layout holds provider cameras.
    resolved_payload: Optional[Dict[str, Any]] = None
    provider_fingerprint: Optional[str] = None
    provider_streams: Optional[Dict[str, Any]] = None


def _result(target: _Target, status: str, message: str, *, delivery: Optional[str] = None,
            confirmable: bool = False) -> Dict[str, Any]:
    result = {
        "pc_id": target.pc_id,
        "status": status,
        "delivery": delivery,
        "config_id": target.config_id,
        "confirmable": bool(confirmable),
        "message": message,
        "screens": target.screens,
        "ack_timeout_sec": int(settings.DEPLOY_ACK_TIMEOUT_SEC),
    }
    if target.provider_streams is not None:
        result["provider_streams"] = target.provider_streams
        unresolved = target.provider_streams["unresolved"]
        if unresolved and delivery == DELIVERY_DELIVERED:
            result["message"] = (f"{result['message'].rstrip('.')}. {len(unresolved)} provider camera(s) are not "
                                 f"available: {unresolved[0]['message']}")
    return result


def _resolve_provider_streams(db, pcs: Sequence[PC], targets: List[_Target]) -> None:
    """Replace provider camera references in the layouts about to be sent
    with playable addresses (stream_providers.engine). One bounded pass for
    all targets, so a slow provider costs the deploy at most
    ``deploy_resolve_budget_sec``; what is not ready is sent as a cell that
    fails alone and is delivered later by ``refresh_pc``. A layout without a
    reference is not touched and costs no Redis or provider call. The content
    hash stays the one of the unresolved layout."""
    from app.services.stream_providers import engine

    with_refs = [t for t in targets if engine.layout_refs(t.payload)]
    if not with_refs:
        return
    # Only the cells of a wall get an address; a camera that is merely listed in LocationUris does not.
    refs = {t.pc_id: engine.wall_refs(t.payload) for t in with_refs}
    by_id = {pc.id: pc for pc in pcs}
    core_ok: Dict[str, bool] = {}
    resolved: Dict[str, Any] = {}
    try:
        from app.models.pc_tracking import PcHealth

        healths = {h.pc_id: h for h in db.query(PcHealth).filter(
            PcHealth.pc_id.in_([t.pc_id for t in with_refs])).all()}
        for target in with_refs:
            core_ok[target.pc_id] = engine.core_supports_hls(by_id.get(target.pc_id), healths.get(target.pc_id))
        cfg = engine.config(db)
        wanted = [ref for t in with_refs if core_ok[t.pc_id] for ref in refs[t.pc_id]]
        resolved = engine.resolve_refs(db, wanted, budget_sec=cfg["deploy_resolve_budget_sec"], cfg=cfg)
        db.commit()  # end the read transaction before the relay call
    except Exception as exc:
        # The deploy goes on: unresolved cells fail alone and the tick delivers later.
        logger.error(f"Provider cameras could not be resolved for this deploy: {type(exc).__name__}")
        try:
            db.rollback()
        except Exception:
            pass
    for target in with_refs:
        ok = core_ok.get(target.pc_id, False)
        target.resolved_payload, _cells, unresolved = engine.resolve_layout(target.payload, resolved, ok)
        target.provider_fingerprint = engine.fingerprint(refs[target.pc_id], resolved, ok)
        target.provider_streams = {
            "total": len(refs[target.pc_id]),
            "unresolved": [{"camera_id": u.get("camera_id"), "code": u["code"], "message": u["message"]}
                           for u in unresolved],
        }


def _lock_provider_sends(db, to_send: List[_Target]) -> Dict[str, str]:
    """Hold each target's send lock (stream_providers.engine) from before the
    send until the answers are recorded, so a renewal that is being sent
    right now never arrives after this deploy and none starts before the
    deploy is on record. Only when provider cameras exist: a system without
    them makes no Redis call. A failure here never stops the deploy."""
    from app.models.camera import Camera
    from app.services.stream_providers import engine

    try:
        if not settings.STREAM_PROVIDERS_ENABLED:
            return {}
        if not any(t.resolved_payload is not None for t in to_send):
            known = db.query(Camera.id).filter(Camera.rtsp_url.like("provider://%")).first() is not None
            db.commit()  # end the read transaction before the relay call
            if not known:
                return {}
        return engine.lock_pcs([t.pc_id for t in to_send])
    except Exception as exc:
        logger.warning(f"Stream address send locks not taken: {type(exc).__name__}")
        try:
            db.rollback()
        except Exception:
            pass
        return {}


def _unlock_provider_sends(held: Dict[str, str]) -> None:
    if held:
        from app.services.stream_providers import engine

        engine.unlock_pcs(held)  # after an exception above, the locks expire by themselves (30 s)


def _note_provider_deploys(delivered: List[_Target]) -> None:
    """Remember what was delivered, and follow up on cells left pending."""
    from app.services import catchup
    from app.services.stream_providers import engine

    for target in delivered:
        try:
            engine.note_deployed(target.pc_id, target.config_id, target.config_hash, target.payload,
                                 target.provider_fingerprint)
            if any(u["code"] == engine.CODE_PENDING for u in target.provider_streams["unresolved"]):
                catchup.send_task(REFRESH_TASK, [target.pc_id, "deploy"], countdown=REFRESH_FOLLOW_UP_SEC)
        except Exception as exc:
            # The tick rebuilds the record from the database and delivers.
            logger.error(f"Provider cameras of deploy {target.config_id} not recorded: {type(exc).__name__}")


def _valid_answer(answer: Any, config_id: str) -> Optional[Dict[str, Any]]:
    if not isinstance(answer, dict) or answer.get("status") not in RELAY_ANSWERS:
        return None
    if answer.get("config_id") not in (None, config_id):
        return None
    return answer


def _audit(db, target: _Target, actor: ActorTriple, trigger: str, layout_name: Optional[str],
           outcome: str) -> None:
    """One config.deployed entry per PC (NEW-54): ids, counts and the content
    hash only, never the layout."""
    audit_service.record_event(
        db,
        action=AuditAction.CONFIG_DEPLOYED,
        resource_type=ResourceType.PC,
        resource_id=target.pc_id,
        actor=actor,
        changes={
            "config_id": target.config_id,
            "trigger": trigger,
            "outcome": outcome,
            "layout_id": target.layout_id,
            "layout_name": layout_name,
            "screens": target.screens,
            "config_hash": target.config_hash,
            "config_id_injected": bool(target.config_id and target.payload and "config_id" in target.payload),
        },
    )


def deploy_config_to_pcs(pcs: Sequence[PC], db, *, trigger: str, actor: ActorTriple = SYSTEM_ACTOR,
                         sender: Optional[Sender] = None,
                         expected_hashes: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Deploy each PC's layout and report what really happened, per PC.

    Args:
        pcs: PCs to deploy to (loaded from ``db``); duplicates are ignored.
        db: the caller's session. It is committed twice: once with the
            pending rows and audit entries (before any send), once with the
            relay's answers.
        trigger: pc_deployments.trigger (TRIGGER_*).
        actor: who deploys (created_by_* of the rows, audit actor).
        sender: how requests reach the relay (default socketio_sender).
        expected_hashes: per PC, the content hash the generated layout must
            have (D17 catch-up re-pushes only an unchanged layout); a PC whose
            layout differs is ``skipped`` and gets no row.

    Returns ``{total, deployed, skipped, failed, results}``, results in input
    order, each ``{pc_id, status, delivery, config_id, confirmable, message,
    screens}``.
    """
    from app.services.config_generator import generate_config
    from app.services.config_loader import load_pc_config

    send = sender or socketio_sender
    inject = bool(settings.DEPLOY_INJECT_CONFIG_ID)

    targets: List[_Target] = []
    seen = set()
    for pc in pcs:
        if pc.id in seen:
            continue
        seen.add(pc.id)
        targets.append(_Target(pc_id=pc.id, layout_id=pc.screen_layout_id))

    # 1. Generate every layout up front.
    planned: List[_Target] = []
    for target in targets:
        if target.layout_id is None:
            target.result = _result(target, STATUS_SKIPPED, MSG_NO_LAYOUT)
            continue
        try:
            logger.info(f"Generating device config for PC {target.pc_id}")
            config = generate_config(load_pc_config(target.pc_id, db), db)
        except Exception as exc:
            logger.error(f"Error generating config for PC {target.pc_id}: {type(exc).__name__}")
            target.result = _result(target, STATUS_FAILED, MSG_GENERATION_FAILED)
            continue
        content_hash = deploy_tracking.config_hash(config)
        target.screens = deploy_tracking.screen_count(config)
        if expected_hashes and target.pc_id in expected_hashes and expected_hashes[target.pc_id] != content_hash:
            logger.info(f"Deploy to PC {target.pc_id} not sent: its layout changed since the expected content")
            target.result = _result(target, STATUS_SKIPPED, MSG_CONTENT_CHANGED)
            continue
        target.config_id = str(uuid.uuid4())
        target.config_hash = content_hash
        payload = dict(config)
        if inject:
            payload["config_id"] = target.config_id
        else:
            payload.pop("config_id", None)
        target.payload = payload
        planned.append(target)

    layout_ids = sorted({t.layout_id for t in targets if t.layout_id})
    layout_names: Dict[str, str] = {}
    if layout_ids:
        layout_names = dict(db.query(ScreenLayout.id, ScreenLayout.name).filter(ScreenLayout.id.in_(layout_ids)).all())

    # 2. Commit the pending rows (and the audit) before anything is sent.
    ts = deploy_tracking.now_ts()
    if planned:
        # Serialise concurrent deploys to the same PC (no-op on SQLite).
        db.query(PC.id).filter(PC.id.in_(sorted(t.pc_id for t in planned))).order_by(PC.id).with_for_update().all()
        for target in planned:
            row = PcDeployment(
                id=target.config_id,
                pc_id=target.pc_id,
                layout_id=target.layout_id,
                layout_name=layout_names.get(target.layout_id),
                config_hash=target.config_hash,
                screens=target.screens,
                trigger=trigger,
                status=deploy_tracking.PENDING,
                confirmable=False,
                status_changed_at=ts,
            )
            stamp_created(row, actor)
            db.add(row)
        db.flush()
        for target in planned:
            superseded = deploy_tracking.supersede_older(db, target.pc_id, target.config_id, ts=ts)
            if superseded:
                logger.info(f"Deploy {target.config_id} supersedes {superseded} older deploy(s) of PC {target.pc_id}")
    for target in targets:
        outcome = "pending" if target.result is None else (
            "skipped" if target.result["status"] == STATUS_SKIPPED else "generation_failed")
        _audit(db, target, actor, trigger, layout_names.get(target.layout_id), outcome)
    db.commit()

    # 3. Do not send a row a concurrent deploy has superseded meanwhile.
    if planned:
        current = deploy_tracking.statuses(db, [t.config_id for t in planned])
        db.commit()  # end the read transaction before the network call
        for target in planned:
            if current.get(target.config_id) == deploy_tracking.SUPERSEDED:
                target.result = _result(target, STATUS_SKIPPED, MSG_SUPERSEDED)
        to_send = [t for t in planned if t.result is None]
    else:
        to_send = []

    # 4. Send.
    if to_send:
        _resolve_provider_streams(db, pcs, to_send)
        requests = [
            {"config_id": t.config_id, "targetId": t.pc_id, "content": t.resolved_payload, "track_ack": True}
            if t.resolved_payload is not None else
            {"config_id": t.config_id, "targetId": t.pc_id, "content": t.payload}
            for t in to_send
        ]
        provider_locks = _lock_provider_sends(db, to_send)
        try:
            answers = send(requests)
            unreachable = False
        except RelayUnreachable:
            answers = {}
            unreachable = True

        # 5. Record the answers (one transaction).
        ts = deploy_tracking.now_ts()
        transitions = []
        for target in to_send:
            answer = None if unreachable else _valid_answer(answers.get(target.config_id), target.config_id)
            target.answer = answer
            if answer is not None and answer["status"] == DELIVERY_DELIVERED:
                confirmable = bool(answer.get("confirmable")) and inject
                transitions.append((target.config_id, deploy_tracking.DELIVERED, None, confirmable))
                message = (
                    f"Delivered to PC {target.pc_id}; waiting for the station to confirm it applied the layout"
                    if confirmable else
                    f"Delivered to PC {target.pc_id}; this station cannot confirm whether it applied the layout"
                )
                target.result = _result(target, STATUS_DEPLOYED, message, delivery=DELIVERY_DELIVERED,
                                        confirmable=confirmable)
            elif answer is not None and answer["status"] == DELIVERY_OFFLINE:
                transitions.append((target.config_id, deploy_tracking.OFFLINE, DETAIL_OFFLINE, None))
                target.result = _result(
                    target, STATUS_FAILED,
                    f"PC {target.pc_id} is not connected to the display client service; nothing was delivered",
                    delivery=DELIVERY_OFFLINE,
                )
            else:
                if unreachable:
                    detail, message = DETAIL_UNREACHABLE, MSG_UNREACHABLE
                elif answer is not None and answer.get("reason") == REASON_TOO_LARGE:  # never sent
                    detail = f"relay error: {REASON_TOO_LARGE}"
                    message = (f"The layout for PC {target.pc_id} is larger than the display client service "
                               f"accepts; nothing was sent")
                elif answer is not None:  # the relay refused the request
                    detail = f"relay error: {str(answer.get('reason') or 'unknown')[:200]}"
                    message = f"The display client service refused the deploy to PC {target.pc_id}"
                else:
                    detail = DETAIL_NO_ANSWER
                    message = f"The display client service did not confirm delivery to PC {target.pc_id}"
                transitions.append((target.config_id, deploy_tracking.RELAY_FAILED, detail, None))
                target.result = _result(target, STATUS_FAILED, message, delivery=DELIVERY_ERROR)
        _note_provider_deploys([t for t in to_send if t.resolved_payload is not None
                                and t.result["delivery"] == DELIVERY_DELIVERED])
        try:
            for config_id, to, detail, confirmable in transitions:
                if confirmable is not None:
                    deploy_tracking.set_confirmable(db, config_id, confirmable)
                deploy_tracking.transition(db, config_id, to, ts=ts, detail=detail)
            db.commit()
        except Exception as exc:
            # The sends happened; report them truthfully. Rows left pending are
            # moved by the relay's receipt or by D15's sweep (relay_failed).
            logger.error(f"Could not record deploy answers: {type(exc).__name__}")
            try:
                db.rollback()
            except Exception:
                pass
        _unlock_provider_sends(provider_locks)

    ordered = [t.result for t in targets]
    summary = {
        "total": len(ordered),
        "deployed": sum(1 for r in ordered if r["status"] == STATUS_DEPLOYED),
        "skipped": sum(1 for r in ordered if r["status"] == STATUS_SKIPPED),
        "failed": sum(1 for r in ordered if r["status"] == STATUS_FAILED),
        "results": ordered,
    }
    logger.info(
        f"Deploy ({trigger}): {summary['deployed']} delivered, {summary['failed']} failed, "
        f"{summary['skipped']} skipped of {summary['total']}"
    )
    return summary
