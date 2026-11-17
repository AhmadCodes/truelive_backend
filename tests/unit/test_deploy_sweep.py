"""
The relay's deploy timeout sweep (plan 06 D15): "sweep table".

deploy_tracking.sweep_timeouts on SQLite (only teams, screen_layouts, pcs and
pc_deployments exist), with a fixed ``now`` and DEPLOY_ACK_TIMEOUT_SEC = 300:

* pending for more than 60 s: relay_failed;
* confirmable rows delivered or received for more than 300 s since delivery:
  unconfirmed;
* confirmable rows accepted for more than 600 s since acceptance: unconfirmed;
* non-confirmable rows (Client 1.1.x) stay received; one still delivered
  with no receipt more than 300 s after delivery (its ACK never came)
  becomes unconfirmed, and a late ACK still makes it received (M2 review);
* every other state is left alone, and late evidence still upgrades an
  unconfirmed row afterwards.

    python -m pytest tests/unit/test_deploy_sweep.py -o addopts='' -p no:cacheprovider
"""

import pytest
from sqlalchemy.orm import sessionmaker

from app.models.pc_tracking import DEPLOYMENT_STATUSES, PcDeployment
from app.services import deploy_tracking as dt
from tests.deploy_testkit import create_deploy_tables, seed_fleet, sqlite_engine
from tests.relay_testkit import import_relay

NOW = 1_800_000_000
T = 300


@pytest.fixture
def db():
    engine = sqlite_engine()
    create_deploy_tables(engine)
    session = sessionmaker(bind=engine, autoflush=False)()
    seed_fleet(session, ["PC_A"])
    yield session
    session.close()
    engine.dispose()


def _add(db, config_id, status, confirmable, *, changed_ago, delivered_ago=None, accepted_ago=None,
         received_ago=None):
    db.add(PcDeployment(
        id=config_id, pc_id="PC_A", trigger="pc_deploy", status=status, confirmable=confirmable,
        status_changed_at=NOW - changed_ago,
        delivered_at=None if delivered_ago is None else NOW - delivered_ago,
        accepted_at=None if accepted_ago is None else NOW - accepted_ago,
        received_at=None if received_ago is None else NOW - received_ago,
    ))


# (status, confirmable, changed_ago, delivered_ago, accepted_ago) -> expected
TABLE = [
    (("pending", False, 61, None, None), "relay_failed"),
    (("pending", True, 61, None, None), "relay_failed"),
    (("pending", False, 59, None, None), "pending"),
    (("delivered", True, 301, 301, None), "unconfirmed"),
    (("delivered", True, 299, 299, None), "delivered"),
    (("delivered", True, 301, None, None), "unconfirmed"),  # no delivered_at: time in state
    (("received", True, 10, 301, None), "unconfirmed"),  # measured from delivery, not receipt
    (("received", True, 10, 299, None), "received"),
    # Client 1.1.x: received is its end state; delivered with no ACK for more
    # than the timeout since delivery becomes unconfirmed (M2 review).
    (("delivered", False, 5000, 5000, None), "unconfirmed"),
    (("delivered", False, 301, 301, None), "unconfirmed"),
    (("delivered", False, 299, 299, None), "delivered"),
    (("delivered", False, 301, None, None), "unconfirmed"),  # no delivered_at: time in state
    (("delivered", False, 10, 299, None), "delivered"),  # measured from delivery
    (("received", False, 5000, 5000, None), "received"),
    (("accepted", True, 601, 900, 601), "unconfirmed"),
    (("accepted", True, 599, 900, 599), "accepted"),
    (("accepted", True, 10, 900, None), "accepted"),  # no accepted_at: time in state
    (("accepted", False, 5000, 5000, 5000), "accepted"),
    (("applied", True, 9000, 9000, 9000), "applied"),
    (("failed", True, 9000, 9000, None), "failed"),
    (("unconfirmed", True, 9000, 9000, None), "unconfirmed"),
    (("superseded", True, 9000, 9000, None), "superseded"),
    (("offline", False, 9000, None, None), "offline"),
    (("relay_failed", False, 9000, None, None), "relay_failed"),
]


@pytest.mark.parametrize("row,expected", TABLE, ids=[f"{r[0]}-{'c' if r[1] else 'nc'}-{r[2]}" for r, _ in TABLE])
def test_sweep_table(db, row, expected):
    status, confirmable, changed_ago, delivered_ago, accepted_ago = row
    _add(db, "cfg", status, confirmable, changed_ago=changed_ago, delivered_ago=delivered_ago,
         accepted_ago=accepted_ago)
    db.commit()
    dt.sweep_timeouts(db, now=NOW, ack_timeout=T)
    db.commit()
    db.expire_all()
    swept = db.get(PcDeployment, "cfg")
    assert swept.status == expected
    if expected != status:
        assert swept.status_changed_at == NOW and swept.status_detail
        if expected == "unconfirmed":
            assert (swept.unconfirmed_at, swept.source) == (NOW, "timeout")


def test_every_state_is_covered_by_the_table():
    assert {row[0] for row, _ in TABLE} == set(DEPLOYMENT_STATUSES)


def test_sweep_counts_and_late_upgrade(db):
    _add(db, "p", "pending", False, changed_ago=120)
    _add(db, "d", "delivered", True, changed_ago=400, delivered_ago=400)
    _add(db, "a", "accepted", True, changed_ago=700, accepted_ago=700)
    _add(db, "r", "received", False, changed_ago=9000, delivered_ago=9000)
    db.commit()
    _add(db, "n", "delivered", False, changed_ago=400, delivered_ago=400)
    db.commit()
    counts = dt.sweep_timeouts(db, now=NOW, ack_timeout=T)
    db.commit()
    assert counts == {"pending_to_relay_failed": 1, "delivered_to_unconfirmed": 1, "accepted_to_unconfirmed": 1,
                      "unacknowledged_to_unconfirmed": 1}
    assert dt.sweep_timeouts(db, now=NOW, ack_timeout=T) == {
        "pending_to_relay_failed": 0, "delivered_to_unconfirmed": 0, "accepted_to_unconfirmed": 0,
        "unacknowledged_to_unconfirmed": 0}
    db.expire_all()
    n = db.get(PcDeployment, "n")
    assert (n.status, n.source, n.unconfirmed_at) == ("unconfirmed", "timeout", NOW)
    assert "did not acknowledge" in n.status_detail
    # Late Core evidence still upgrades an unconfirmed row; a late receipt only stamps it.
    assert dt.transition(db, "a", dt.APPLIED, ts=NOW + 5).changed
    assert dt.record_receipt(db, "d", "PC_A", confirmable=True, ts=NOW + 6).outcome == "stale"
    db.commit()
    db.expire_all()
    assert db.get(PcDeployment, "a").status == "applied"
    assert (db.get(PcDeployment, "d").status, db.get(PcDeployment, "d").received_at) == ("unconfirmed", NOW + 6)
    # A relay_failed row (pending too long) is upgraded by a late receipt.
    assert dt.record_receipt(db, "p", "PC_A", confirmable=False, ts=NOW + 7).changed


def test_the_relay_sweep_uses_the_ack_timeout_setting(monkeypatch):
    relay = import_relay()
    seen = {}

    def fake_sweep(db, *, now=None, ack_timeout=300):
        seen["ack_timeout"] = ack_timeout
        return {"pending_to_relay_failed": 0}

    monkeypatch.setattr(dt, "sweep_timeouts", fake_sweep)
    monkeypatch.setattr(relay.settings, "DEPLOY_ACK_TIMEOUT_SEC", 450)

    class _Db:
        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(relay, "_open_db", lambda: _Db())
    assert relay.sweep_deploys_once() == {"pending_to_relay_failed": 0}
    assert seen["ack_timeout"] == 450


def test_unacknowledged_row_with_a_receipt_stamp_is_left_alone(db):
    """A non-confirmable delivered row that already has received_at (the ACK
    was recorded as evidence) is not swept."""
    _add(db, "cfg", "delivered", False, changed_ago=5000, delivered_ago=5000, received_ago=4990)
    db.commit()
    dt.sweep_timeouts(db, now=NOW, ack_timeout=T)
    db.commit()
    db.expire_all()
    assert db.get(PcDeployment, "cfg").status == "delivered"


def test_late_ack_upgrades_an_unacknowledged_1_1_x_row(db):
    """M2 review: a Client 1.1.x deploy whose ACK never came is unconfirmed
    after the timeout; its ACK is the only evidence such a station gives, so a
    late one still makes the row received and stamps pcs.last_applied. A
    confirmable unconfirmed row is only stamped (Core evidence is needed)."""
    from app.models.pc import PC

    _add(db, "old", "delivered", False, changed_ago=400, delivered_ago=400)
    _add(db, "cur", "delivered", True, changed_ago=400, delivered_ago=400)
    db.commit()
    dt.sweep_timeouts(db, now=NOW, ack_timeout=T)
    db.commit()
    assert dt.statuses(db, ["old", "cur"]) == {"old": "unconfirmed", "cur": "unconfirmed"}

    late = dt.record_receipt(db, "old", "PC_A", confirmable=False, ts=NOW + 30)
    assert (late.outcome, late.status, late.previous) == ("changed", "received", "unconfirmed")
    assert dt.record_receipt(db, "cur", "PC_A", confirmable=True, ts=NOW + 31).outcome == "stale"
    # The row decides, not the session: a confirmable row the sweep gave up on
    # is not made received by an ACK, whatever that session declared.
    assert dt.record_receipt(db, "cur", "PC_A", confirmable=False, ts=NOW + 33).outcome == "stale"
    # Another PC's ACK never moves the row.
    assert dt.record_receipt(db, "old", "PC_OTHER", confirmable=False, ts=NOW + 32).outcome == "mismatch"
    db.commit()
    db.expire_all()
    old, cur = db.get(PcDeployment, "old"), db.get(PcDeployment, "cur")
    assert (old.status, old.received_at, old.status_changed_at) == ("received", NOW + 30, NOW + 30)
    assert old.status_detail == "acknowledged after the timeout"
    assert (cur.status, cur.received_at) == ("unconfirmed", NOW + 31)
    assert db.get(PC, "PC_A").last_applied == NOW + 30
    # A second ACK is a no-op.
    assert dt.record_receipt(db, "old", "PC_A", confirmable=False, ts=NOW + 40).outcome == "stale"
