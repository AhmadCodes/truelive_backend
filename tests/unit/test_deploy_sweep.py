"""
The relay's deploy timeout sweep (plan 06 D15): "sweep table".

deploy_tracking.sweep_timeouts on SQLite (only teams, screen_layouts, pcs and
pc_deployments exist), with a fixed ``now`` and DEPLOY_ACK_TIMEOUT_SEC = 300:

* pending for more than 60 s: relay_failed;
* confirmable rows delivered or received for more than 300 s since delivery:
  unconfirmed;
* confirmable rows accepted for more than 600 s since acceptance: unconfirmed;
* non-confirmable rows (Client 1.1.x) stay received (or delivered);
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


def _add(db, config_id, status, confirmable, *, changed_ago, delivered_ago=None, accepted_ago=None):
    db.add(PcDeployment(
        id=config_id, pc_id="PC_A", trigger="pc_deploy", status=status, confirmable=confirmable,
        status_changed_at=NOW - changed_ago,
        delivered_at=None if delivered_ago is None else NOW - delivered_ago,
        accepted_at=None if accepted_ago is None else NOW - accepted_ago,
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
    (("delivered", False, 5000, 5000, None), "delivered"),  # Client 1.1.x: never unconfirmed
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
    counts = dt.sweep_timeouts(db, now=NOW, ack_timeout=T)
    db.commit()
    assert counts == {"pending_to_relay_failed": 1, "delivered_to_unconfirmed": 1, "accepted_to_unconfirmed": 1}
    assert dt.sweep_timeouts(db, now=NOW, ack_timeout=T) == {
        "pending_to_relay_failed": 0, "delivered_to_unconfirmed": 0, "accepted_to_unconfirmed": 0}
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
