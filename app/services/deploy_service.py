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
   an ACK callback (the ``sio.call`` contract, 5 s), all calls in flight at
   once, so a batch waits at most about 5 s for a slow relay. The relay
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
confirmable, message, screens}``. ``status`` keeps the old vocabulary
(deployed, skipped, failed): an offline PC is ``failed`` with
``delivery=offline`` (NEW-46).

Blocking: everything here is synchronous (psycopg2, python-socketio Client).
The two routes that call it are plain ``def``, so FastAPI runs them in its
threadpool and the event loop stays free (``/health`` keeps answering).
"""

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
RELAY_CALL_TIMEOUT = 5.0

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


def socketio_sender(requests: List[Dict[str, Any]], *, url: Optional[str] = None,
                    timeout: float = RELAY_CALL_TIMEOUT) -> Dict[str, Optional[Any]]:
    """Send every request over one authenticated sender session.

    Each ``deploy_config`` is emitted with an ACK callback, so all calls are
    in flight together; each gets at least ``timeout`` seconds to answer.
    Raises RelayUnreachable when the session cannot be opened (also a wrong
    RELAY_SENDER_SECRET: the relay refuses the CONNECT).
    """
    import socketio

    client = socketio.Client(reconnection=False, logger=False, engineio_logger=False)
    target = url or relay_url()
    try:
        logger.info(f"Connecting to display client service at {target}")
        # D12: the sender secret goes in the CONNECT auth payload, never the URL.
        client.connect(target, auth=sender_auth(settings.RELAY_SENDER_SECRET), wait_timeout=timeout)
    except Exception as exc:
        logger.error(f"Could not reach display client service: {type(exc).__name__}")
        raise RelayUnreachable(type(exc).__name__) from exc

    answers: Dict[str, Optional[Any]] = {}
    waits: Dict[str, threading.Event] = {}
    try:
        for request in requests:
            config_id = request["config_id"]
            done = threading.Event()
            waits[config_id] = done

            def _on_answer(*args, _config_id=config_id, _done=done):
                answers[_config_id] = args[0] if args else None
                _done.set()

            try:
                client.emit(RELAY_EVENT, request, callback=_on_answer)
            except Exception as exc:
                logger.error(f"Could not send deploy {config_id}: {type(exc).__name__}")
                done.set()
        deadline = time.monotonic() + timeout
        for done in waits.values():
            done.wait(max(0.0, deadline - time.monotonic()))
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
    return {config_id: answers.get(config_id) for config_id in waits}


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


def _result(target: _Target, status: str, message: str, *, delivery: Optional[str] = None,
            confirmable: bool = False) -> Dict[str, Any]:
    return {
        "pc_id": target.pc_id,
        "status": status,
        "delivery": delivery,
        "config_id": target.config_id,
        "confirmable": bool(confirmable),
        "message": message,
        "screens": target.screens,
    }


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
                         sender: Optional[Sender] = None) -> Dict[str, Any]:
    """Deploy each PC's layout and report what really happened, per PC.

    Args:
        pcs: PCs to deploy to (loaded from ``db``); duplicates are ignored.
        db: the caller's session. It is committed twice: once with the
            pending rows and audit entries (before any send), once with the
            relay's answers.
        trigger: pc_deployments.trigger (TRIGGER_*).
        actor: who deploys (created_by_* of the rows, audit actor).
        sender: how requests reach the relay (default socketio_sender).

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
        target.config_id = str(uuid.uuid4())
        target.config_hash = deploy_tracking.config_hash(config)
        target.screens = deploy_tracking.screen_count(config)
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
        requests = [{"config_id": t.config_id, "targetId": t.pc_id, "content": t.payload} for t in to_send]
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
                elif answer is not None:  # the relay refused the request
                    detail = f"relay error: {str(answer.get('reason') or 'unknown')[:200]}"
                    message = f"The display client service refused the deploy to PC {target.pc_id}"
                else:
                    detail = DETAIL_NO_ANSWER
                    message = f"The display client service did not confirm delivery to PC {target.pc_id}"
                transitions.append((target.config_id, deploy_tracking.RELAY_FAILED, detail, None))
                target.result = _result(target, STATUS_FAILED, message, delivery=DELIVERY_ERROR)
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
