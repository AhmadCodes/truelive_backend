"""
Deploy state machine (plan 06 D14; used by D15): the transition table.

Every (from, to) pair is driven through ``deploy_tracking.transition`` on
SQLite (only teams, screen_layouts, pcs and pc_deployments exist) and checked
against ALLOWED_FROM: the row moves exactly when the table allows it, stamps
its state's timestamp once, and otherwise keeps its state (with evidence
timestamps for delivered/received/accepted). Also: supersede rules, receipt
stamping of pcs.last_applied, the content hash, and PendingTransitions
(events kept until their row exists).

    python -m pytest tests/unit/test_deploy_tracking.py -o addopts='' -p no:cacheprovider
"""

import itertools

import pytest
from sqlalchemy.orm import sessionmaker

from app.models.pc import PC
from app.models.pc_tracking import DEPLOYMENT_STATUSES, PcDeployment
from app.services import deploy_tracking as dt
from tests.deploy_testkit import create_deploy_tables, seed_fleet, sqlite_engine, synthetic_layout

T0 = 1_800_000_000


@pytest.fixture
def db():
    engine = sqlite_engine()
    create_deploy_tables(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    seed_fleet(session, ["PC_A", "PC_B"])
    yield session
    session.close()
    engine.dispose()


def _row(db, config_id, status="pending", pc_id="PC_A", **fields):
    db.add(PcDeployment(id=config_id, pc_id=pc_id, trigger="pc_deploy", status=status, **fields))
    db.commit()


def _get(db, config_id):
    db.expire_all()
    return db.get(PcDeployment, config_id)


# --------------------------------------------------------------------------- #
# The table itself
# --------------------------------------------------------------------------- #


def test_table_covers_every_state_and_names_only_known_states():
    known = set(DEPLOYMENT_STATUSES)
    assert set(dt.ALLOWED_FROM) == known - {dt.PENDING}  # nothing transitions *to* pending
    for to, sources in dt.ALLOWED_FROM.items():
        assert sources <= known, to
        assert to not in sources, f"{to} -> {to} must be a no-op"
    assert dt.TERMINAL_STATES == {dt.FAILED, dt.OFFLINE, dt.SUPERSEDED}


def test_card_rules_hold():
    # monotonic: nothing goes back to delivered/received once accepted or later
    for later in (dt.ACCEPTED, dt.APPLIED, dt.FAILED, dt.SUPERSEDED, dt.OFFLINE):
        assert not dt.can_transition(later, dt.DELIVERED)
        assert not dt.can_transition(later, dt.RECEIVED)
    # applied can move to failed (late Failed, OD-18); failed never moves
    assert dt.can_transition(dt.APPLIED, dt.FAILED)
    assert not any(dt.can_transition(dt.FAILED, to) for to in dt.ALLOWED_FROM)
    # late Core evidence upgrades unconfirmed; a late ACK does not
    for to in (dt.ACCEPTED, dt.APPLIED, dt.FAILED):
        assert dt.can_transition(dt.UNCONFIRMED, to)
    assert not dt.can_transition(dt.UNCONFIRMED, dt.RECEIVED)
    # relay_failed is not evidence of non-delivery: later evidence upgrades it
    for to in (dt.DELIVERED, dt.RECEIVED, dt.ACCEPTED, dt.APPLIED, dt.FAILED):
        assert dt.can_transition(dt.RELAY_FAILED, to)
    # offline is terminal; applied rows are history (never superseded)
    assert not dt.can_transition(dt.APPLIED, dt.SUPERSEDED)


@pytest.mark.parametrize("current,to", list(itertools.product(DEPLOYMENT_STATUSES, sorted(dt.ALLOWED_FROM))))
def test_every_transition_pair(db, current, to):
    _row(db, "cfg", status=current, status_changed_at=T0)
    result = dt.transition(db, "cfg", to, ts=T0 + 10, detail="why")
    db.commit()
    row = _get(db, "cfg")
    column = dt.STATE_TIMESTAMP.get(to)
    if to in dt.ALLOWED_FROM and current in dt.ALLOWED_FROM[to]:
        assert result.changed and result.previous == current
        assert (row.status, row.status_changed_at, row.status_detail) == (to, T0 + 10, "why")
        if column:
            assert getattr(row, column) == T0 + 10
    else:
        assert result.outcome == "stale" and result.status == current
        assert (row.status, row.status_changed_at, row.status_detail) == (current, T0, None)
        if column:
            expected = T0 + 10 if (to in dt.EVIDENCE_STATES and current != dt.SUPERSEDED) else None
            assert getattr(row, column) == expected


def test_timestamps_are_stamped_once(db):
    _row(db, "cfg", status="delivered", delivered_at=T0)
    dt.transition(db, "cfg", dt.DELIVERED, ts=T0 + 50)  # stale, evidence keeps the first stamp
    dt.transition(db, "cfg", dt.RECEIVED, ts=T0 + 60, stamps={"delivered_at": T0 + 99})
    db.commit()
    row = _get(db, "cfg")
    assert (row.status, row.delivered_at, row.received_at) == ("received", T0, T0 + 60)


def test_missing_and_mismatched_rows(db):
    assert dt.transition(db, "nope", dt.RECEIVED).outcome == "missing"
    _row(db, "cfg", pc_id="PC_B")
    result = dt.transition(db, "cfg", dt.RECEIVED, pc_id="PC_A")
    assert result.outcome == "mismatch"
    db.commit()
    assert _get(db, "cfg").status == "pending"
    assert dt.transition(db, "cfg", dt.RECEIVED, pc_id="PC_B").changed


def test_unknown_target_and_fields_are_refused(db):
    _row(db, "cfg")
    with pytest.raises(ValueError):
        dt.transition(db, "cfg", "deployed")
    with pytest.raises(ValueError):
        dt.transition(db, "cfg", dt.PENDING)
    with pytest.raises(ValueError):
        dt.transition(db, "cfg", dt.DELIVERED, fields={"pc_id": "PC_B"})


def test_detail_is_truncated(db):
    _row(db, "cfg")
    dt.transition(db, "cfg", dt.RELAY_FAILED, detail="x" * 900)
    db.commit()
    assert len(_get(db, "cfg").status_detail) == 500


def test_relay_that_runs_ahead_is_never_moved_back(db):
    """A relay/Client that advanced the row before the API writes
    ``delivered`` keeps it there (D14 acceptance: ends in accepted)."""
    _row(db, "cfg")
    assert dt.transition(db, "cfg", dt.ACCEPTED, ts=T0 + 2).changed
    assert dt.transition(db, "cfg", dt.DELIVERED, ts=T0 + 3).outcome == "stale"
    assert dt.transition(db, "cfg", dt.RECEIVED, ts=T0 + 4).outcome == "stale"
    db.commit()
    row = _get(db, "cfg")
    assert (row.status, row.accepted_at, row.delivered_at, row.received_at) == ("accepted", T0 + 2, T0 + 3, T0 + 4)


# --------------------------------------------------------------------------- #
# Supersede, receipt, last_applied
# --------------------------------------------------------------------------- #


def test_supersede_older_moves_only_in_flight_rows(db):
    for status in DEPLOYMENT_STATUSES:
        _row(db, f"old-{status}", status=status, confirmable=True)
    _row(db, "old-received-111", status="received", confirmable=False)  # Client 1.1.x end state
    _row(db, "other-pc", status="pending", pc_id="PC_B")
    _row(db, "new", status="pending")
    moved = dt.supersede_older(db, "PC_A", "new", ts=T0)
    db.commit()
    expected = {"old-pending", "old-delivered", "old-received", "old-accepted"}
    assert moved == len(expected)
    for status in DEPLOYMENT_STATUSES:
        row = _get(db, f"old-{status}")
        if f"old-{status}" in expected:
            assert (row.status, row.superseded_at, row.status_detail) == ("superseded", T0, "superseded by new")
        else:
            assert row.status == status
    assert _get(db, "old-received-111").status == "received"
    assert _get(db, "other-pc").status == "pending"
    assert _get(db, "new").status == "pending"


def test_receipt_stamps_last_applied_only_for_non_confirmable_rows(db):
    _row(db, "cfg-111")
    _row(db, "cfg-140", pc_id="PC_B")
    assert dt.record_receipt(db, "cfg-111", "PC_A", confirmable=False, ts=T0 + 1, forwarded_at=T0).changed
    assert dt.record_receipt(db, "cfg-140", "PC_B", confirmable=True, ts=T0 + 1, forwarded_at=T0).changed
    db.commit()
    db.expire_all()
    assert db.get(PC, "PC_A").last_applied == T0 + 1
    assert db.get(PC, "PC_B").last_applied is None  # D15 stamps it on applied
    row = _get(db, "cfg-111")
    assert (row.status, row.received_at, row.delivered_at, row.confirmable) == ("received", T0 + 1, T0, False)
    assert _get(db, "cfg-140").confirmable is True


def test_receipt_of_a_superseded_or_finished_row_stamps_nothing(db):
    _row(db, "cfg-s", status="superseded")
    _row(db, "cfg-a", status="applied", confirmable=True)
    assert dt.record_receipt(db, "cfg-s", "PC_A", confirmable=False, ts=T0).outcome == "stale"
    assert dt.record_receipt(db, "cfg-a", "PC_A", confirmable=False, ts=T0).outcome == "stale"
    db.commit()
    db.expire_all()
    assert db.get(PC, "PC_A").last_applied is None
    assert _get(db, "cfg-s").received_at is None  # nothing is stamped on superseded rows
    assert _get(db, "cfg-a").received_at == T0  # evidence on history rows


def test_set_confirmable_is_a_fact_whatever_the_status(db):
    _row(db, "cfg", status="accepted")
    dt.set_confirmable(db, "cfg", True)
    db.commit()
    assert _get(db, "cfg").confirmable is True


def test_statuses(db):
    _row(db, "a", status="delivered")
    _row(db, "b", status="offline")
    assert dt.statuses(db, ["a", "b", "zz"]) == {"a": "delivered", "b": "offline"}
    assert dt.statuses(db, []) == {}


# --------------------------------------------------------------------------- #
# Content hash
# --------------------------------------------------------------------------- #


def test_config_hash_ignores_config_id_and_key_order():
    layout = synthetic_layout("PC_A")
    reordered = dict(reversed(list(layout.items())))
    with_id = dict(layout, config_id="0f0e8d9c-0000-4000-8000-000000000001")
    assert dt.config_hash(layout) == dt.config_hash(reordered) == dt.config_hash(with_id)
    assert len(dt.config_hash(layout)) == 64
    assert dt.config_hash(layout) != dt.config_hash(synthetic_layout("PC_B"))
    assert dt.screen_count(layout) == 2 and dt.screen_count({}) == 0 and dt.screen_count({"screens": None}) == 0


# --------------------------------------------------------------------------- #
# Events that arrive early
# --------------------------------------------------------------------------- #


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _opener(db):
    class _NoClose:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def close(self):
            pass

    return lambda: _NoClose(db)


def test_pending_event_is_applied_once_its_row_exists(db):
    pending = dt.PendingTransitions(ttl_seconds=60, clock=_Clock())

    def apply(session):
        return dt.record_receipt(session, "early", "PC_A", confirmable=False, ts=T0)

    assert apply(db).outcome == "missing"
    pending.add("early", apply, label="received")
    assert pending.retry(_opener(db)) == (0, 1)  # still no row
    _row(db, "early")
    assert pending.retry(_opener(db)) == (1, 0)
    assert _get(db, "early").status == "received"
    db.expire_all()
    assert db.get(PC, "PC_A").last_applied == T0


def test_pending_events_expire_and_are_bounded(db):
    clock = _Clock()
    pending = dt.PendingTransitions(ttl_seconds=10, max_events=3, clock=clock)
    for i in range(5):
        pending.add(f"x{i}", lambda session, i=i: dt.transition(session, f"x{i}", dt.RECEIVED))
    assert len(pending) == 3 and pending.dropped == 2
    clock.t += 11
    assert pending.retry(_opener(db)) == (0, 0)
    assert pending.expired == 3 and len(pending) == 0


def test_a_database_error_keeps_every_event(db):
    pending = dt.PendingTransitions(ttl_seconds=60, clock=_Clock())
    _row(db, "cfg")
    calls = []

    def apply(session):
        calls.append(1)
        return dt.transition(session, "cfg", dt.RECEIVED, ts=T0)

    pending.add("cfg", apply)

    def broken():
        raise RuntimeError("database unavailable (simulated)")

    assert pending.retry(broken) == (0, 1)
    assert pending.retry(_opener(db)) == (1, 0)
    assert _get(db, "cfg").status == "received" and calls == [1]
