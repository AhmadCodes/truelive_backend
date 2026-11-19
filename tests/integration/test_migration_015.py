"""
Migration 015 (plan 06 D13) on scratch Postgres: "migration round trip".

* upgrade -> downgrade -> upgrade works (to 015: later migrations have their
  own tests); there is a single head and 015 is in its history.
* ``pcs`` (columns, defaults, comments, indexes, constraints and rows) and
  every other table are unchanged by the upgrade and by the downgrade.
* A restricted autogenerate (only the three new tables) shows an empty diff,
  and the same comparison does see a drifted model (so the check is live).
* Postgres behaviour: CHECK constraints, ON DELETE CASCADE from pcs,
  SET NULL from screen_layouts, json (not jsonb/array/uuid) columns, the
  latest-deployment order with a same-transaction created_at tie.
* Upgrade and downgrade are idempotent (inspector-guarded): re-running them
  over tables that already exist, or are already gone, does nothing.

Every database is created from template0 by alembic and dropped afterwards;
all rows are synthetic. Needs the local Postgres (TRUELIVE_TEST_PG_ADMIN_URL,
default postgresql://truelive@127.0.0.1:5433/postgres; the test fails, never
skips, without it).

    python -m pytest tests/integration/test_migration_015.py -o addopts='' -p no:cacheprovider
"""

import os
import secrets
import subprocess
import sys
from pathlib import Path

import psycopg2
import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.orm import sessionmaker

import app.models  # noqa: F401
from app.database import Base
from app.models.pc_tracking import (
    DEPLOYMENT_STATUSES,
    HEALTH_STATES,
    PcDeployment,
    PcHealth,
    latest_deployment,
    latest_deployments_subquery,
)
from tests.integration.test_env_wiring import _base_env

pytestmark = [pytest.mark.integration]

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PG_ADMIN_URL = os.environ.get("TRUELIVE_TEST_PG_ADMIN_URL", "postgresql://truelive@127.0.0.1:5433/postgres")
NEW_TABLES = ("pc_deployments", "pc_health", "pc_token_renewals")
REV_014 = "014_actor_audit_stamps"
REV_015 = "015_pc_deploy_tracking"


@pytest.fixture
def scratch():
    name = f"d13_mig_{os.getpid()}_{secrets.token_hex(3)}"
    try:
        admin = psycopg2.connect(PG_ADMIN_URL, connect_timeout=5)
    except psycopg2.OperationalError as exc:
        pytest.fail(f"local test Postgres not reachable at {PG_ADMIN_URL} ({type(exc).__name__}); see env_server.md")
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{name}" OWNER truelive TEMPLATE template0 ENCODING \'UTF8\' '
                    "LC_COLLATE 'C' LC_CTYPE 'C'")
    url = PG_ADMIN_URL.rsplit("/", 1)[0] + "/" + name
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    try:
        yield url, engine
    finally:
        engine.dispose()
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _alembic(url, *args):
    result = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=str(BACKEND_ROOT),
                            env=_base_env(DATABASE_URL=url), capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stderr[-3000:]}"
    return result.stdout + result.stderr


def _tables(engine):
    return set(sa.inspect(engine).get_table_names())


def _table_definition(conn, table):
    """Everything that defines a table: columns (type, length, nullability,
    default, comment), indexes and constraints."""
    columns = conn.exec_driver_sql(
        "SELECT column_name, data_type, character_maximum_length, is_nullable, column_default, "
        "col_description(%(t)s::regclass, ordinal_position) FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %(n)s ORDER BY ordinal_position",
        {"t": f"public.{table}", "n": table},
    ).fetchall()
    indexes = conn.exec_driver_sql(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' AND tablename = %(n)s "
        "ORDER BY indexname", {"n": table}).fetchall()
    constraints = conn.exec_driver_sql(
        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = %(t)s::regclass "
        "ORDER BY conname", {"t": f"public.{table}"}).fetchall()
    return {"columns": columns, "indexes": indexes, "constraints": constraints}


def _fingerprint(engine, exclude=("alembic_version",)):
    """Definition and row content of every base table (row content as an
    order-independent md5 over per-row md5s, so large tables fit in memory;
    nothing is printed)."""
    out = {}
    with engine.connect() as conn:
        tables = [r[0] for r in conn.exec_driver_sql(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
            "AND table_type = 'BASE TABLE' ORDER BY table_name")]
        for table in tables:
            if table in exclude:
                continue
            rows = conn.exec_driver_sql(
                "SELECT count(*), md5(coalesce(string_agg(h, '' ORDER BY h), '')) "
                f'FROM (SELECT md5(t::text) AS h FROM "{table}" t) rows'
            ).fetchone()
            out[table] = (tuple(rows), _table_definition(conn, table))
    return out


def _seed_014(engine):
    """Synthetic team, layout and PCs on the 014 schema."""
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO teams (id, name) VALUES ('TEAM_m', 'Migration team')")
        conn.exec_driver_sql("INSERT INTO screen_layouts (id, name, team_id) VALUES ('LAYOUT_m', 'Wall', 'TEAM_m')")
        for pc_id in ("PC_M1", "PC_M2"):
            conn.exec_driver_sql(
                "INSERT INTO pcs (id, name, role, team_id, screen_layout_id, auth_token, token_expiry, last_connected, "
                "last_seen, last_applied) VALUES (%(id)s, %(id)s, 'controller', 'TEAM_m', 'LAYOUT_m', %(tok)s, "
                "1800000000, 1700000000, 1700000001, 1700000002)",
                {"id": pc_id, "tok": "synthetic-token-" + pc_id},
            )


def _include_new(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        return name in NEW_TABLES
    table = getattr(obj, "table", None)
    return getattr(table, "name", None) in NEW_TABLES


def _restricted_diff(engine, metadata):
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"include_object": _include_new, "compare_type": True})
        return compare_metadata(ctx, metadata)


# --------------------------------------------------------------------------- #


def test_single_head_follows_015(scratch):
    """One head; 015 is in its history (016_stream_providers came after it)."""
    url, _engine = scratch
    assert _alembic(url, "heads").count("(head)") == 1
    assert REV_015 in _alembic(url, "history")


def test_migration_round_trip(scratch):
    url, engine = scratch
    _alembic(url, "upgrade", REV_014)
    _seed_014(engine)
    assert not _tables(engine) & set(NEW_TABLES)
    before = _fingerprint(engine)
    assert "pcs" in before and before["pcs"][0][0] == 2

    # upgrade: the three tables appear; nothing else changes.
    _alembic(url, "upgrade", REV_015)
    assert set(NEW_TABLES) <= _tables(engine)
    after_up = _fingerprint(engine)
    assert {t: v for t, v in after_up.items() if t not in NEW_TABLES} == before
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == REV_015

    # Postgres behaviour of the new tables.
    _check_postgres_behaviour(engine)

    # downgrade: the tables (and their rows) go; pcs and every other table are unchanged.
    _alembic(url, "downgrade", REV_014)
    assert not _tables(engine) & set(NEW_TABLES)
    assert _fingerprint(engine) == before
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == REV_014

    # upgrade again: back, empty, and identical to the models.
    _alembic(url, "upgrade", REV_015)
    assert set(NEW_TABLES) <= _tables(engine)
    with engine.connect() as conn:
        for table in NEW_TABLES:
            assert conn.exec_driver_sql(f"SELECT count(*) FROM {table}").scalar() == 0
    assert _restricted_diff(engine, Base.metadata) == []
    assert {t: v for t, v in _fingerprint(engine).items() if t not in NEW_TABLES} == before


def _check_postgres_behaviour(engine):
    with engine.connect() as conn:
        types = conn.exec_driver_sql(
            "SELECT table_name, column_name, data_type, udt_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name IN ('pc_deployments', 'pc_health', 'pc_token_renewals')"
        ).fetchall()
        assert types
        for table, column, data_type, udt in types:
            assert data_type not in ("jsonb", "ARRAY", "uuid") and not udt.startswith("_"), (table, column)
        json_cols = {(t, c) for t, c, d, _u in types if d == "json"}
        assert ("pc_health", "flags") in json_cols and ("pc_health", "readiness") in json_cols
        assert ("pc_health", "relay_flags") in json_cols and ("pc_health", "streams_failed") in json_cols

        checks = dict(conn.exec_driver_sql(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint WHERE contype = 'c' "
            "AND conrelid IN ('pc_deployments'::regclass, 'pc_health'::regclass)").fetchall())
        for status in DEPLOYMENT_STATUSES:
            assert f"'{status}'" in checks["ck_pc_deployments_status"], status
        for health in HEALTH_STATES:
            assert f"'{health}'" in checks["ck_pc_health_health"], health
        fks = dict(conn.exec_driver_sql(
            "SELECT conname, confdeltype FROM pg_constraint WHERE contype = 'f' AND conrelid IN "
            "('pc_deployments'::regclass, 'pc_health'::regclass, 'pc_token_renewals'::regclass)").fetchall())
        assert fks == {"fk_pc_deployments_pc": "c", "fk_pc_deployments_layout": "n", "fk_pc_health_pc": "c",
                       "fk_pc_token_renewals_pc": "c"}
        indexes = {r[0] for r in conn.exec_driver_sql(
            "SELECT indexname FROM pg_indexes WHERE tablename IN ('pc_deployments', 'pc_token_renewals')")}
        assert {"ix_pc_deployments_pc_id_created_at", "ix_pc_deployments_status_created_at",
                "ix_pc_token_renewals_pc_id"} <= indexes

    Session = sessionmaker(bind=engine, autoflush=False)
    db = Session()
    try:
        # Two rows in one transaction share now(): the greater id is the latest.
        db.add_all([
            PcDeployment(id="cfg-m1-a", pc_id="PC_M1", trigger="pc_deploy", layout_id="LAYOUT_m"),
            PcDeployment(id="cfg-m1-b", pc_id="PC_M1", trigger="pc_deploy", layout_id="LAYOUT_m"),
            PcDeployment(id="cfg-m2-a", pc_id="PC_M2", trigger="layout_deploy", status="received"),
            PcHealth(pc_id="PC_M1", health="ok", flags={"x": True}, readiness=[{"code": "AUTOLOGON_OFF"}]),
        ])
        db.commit()
        rows = db.query(PcDeployment).filter(PcDeployment.pc_id == "PC_M1").all()
        assert rows[0].created_at == rows[1].created_at and rows[0].created_at.tzinfo is not None
        assert rows[0].confirmable is False and rows[0].status == "pending"
        assert latest_deployment(db, "PC_M1").id == "cfg-m1-b"
        latest = latest_deployments_subquery()
        assert sorted(db.execute(sa.select(latest.c.pc_id, latest.c.id)).all()) == [
            ("PC_M1", "cfg-m1-b"), ("PC_M2", "cfg-m2-a")]
        assert db.get(PcHealth, "PC_M1").readiness == [{"code": "AUTOLOGON_OFF"}]

        db.add(PcDeployment(id="cfg-bad", pc_id="PC_M1", trigger="pc_deploy", status="deployed"))
        with pytest.raises(sa.exc.IntegrityError):
            db.commit()
        db.rollback()
    finally:
        db.close()

    # Cascade and SET NULL, inside a transaction that is rolled back (pcs stays as seeded).
    with engine.connect() as conn:
        trans = conn.begin()
        conn.exec_driver_sql("INSERT INTO pc_token_renewals (pc_id, previous_token_sha256, issued_at) "
                             "VALUES ('PC_M1', repeat('a', 64), 1)")
        conn.exec_driver_sql("DELETE FROM pcs WHERE id = 'PC_M1'")
        for table in NEW_TABLES:
            assert conn.exec_driver_sql(f"SELECT count(*) FROM {table} WHERE pc_id = 'PC_M1'").scalar() == 0
        conn.exec_driver_sql("UPDATE pcs SET screen_layout_id = NULL")
        conn.exec_driver_sql("DELETE FROM screen_layouts WHERE id = 'LAYOUT_m'")
        assert conn.exec_driver_sql("SELECT layout_id FROM pc_deployments WHERE id = 'cfg-m2-a'").scalar() is None
        trans.rollback()


def test_restricted_autogenerate_sees_drift(scratch):
    """The empty diff above is meaningful: a drifted model is reported."""
    url, engine = scratch
    _alembic(url, "upgrade", REV_015)
    assert _restricted_diff(engine, Base.metadata) == []
    drifted = sa.MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(drifted)
    drifted.tables["pc_health"].append_column(sa.Column("unexpected", sa.Integer()))
    drifted.tables["pc_deployments"].c.status_detail.comment = "changed"
    diff = _restricted_diff(engine, drifted)
    kinds = sorted(d[0] if isinstance(d, tuple) else d[0][0] for d in diff)
    assert kinds == ["add_column", "modify_comment"], diff
    # Tables outside the three are ignored by the restricted comparison (pre-existing drift, env_server 5.1).
    assert all(NEW_TABLES[0] in str(d) or "pc_health" in str(d) for d in diff)


def test_migration_is_idempotent(scratch):
    url, engine = scratch
    _alembic(url, "upgrade", REV_015)
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO teams (id, name) VALUES ('TEAM_i', 'Idempotent')")
        conn.exec_driver_sql("INSERT INTO pcs (id, name, role, team_id) VALUES ('PC_I', 'PC_I', 'controller', 'TEAM_i')")
        conn.exec_driver_sql("INSERT INTO pc_health (pc_id, health) VALUES ('PC_I', 'ok')")

    # Re-running the upgrade over existing tables keeps them and their rows.
    _alembic(url, "stamp", REV_014)
    _alembic(url, "upgrade", REV_015)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM pc_health").scalar() == 1

    # Re-running the downgrade over dropped tables does nothing.
    _alembic(url, "downgrade", REV_014)
    _alembic(url, "stamp", REV_015)
    _alembic(url, "downgrade", REV_014)
    assert not _tables(engine) & set(NEW_TABLES)

    # A partial state (one table present) is completed by the upgrade.
    PcHealth.__table__.create(engine)
    _alembic(url, "upgrade", REV_015)
    assert set(NEW_TABLES) <= _tables(engine)
    assert _restricted_diff(engine, Base.metadata) == []
