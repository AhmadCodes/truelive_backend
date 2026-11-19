"""
Migration 016 (stream providers) on scratch Postgres: upgrade -> downgrade ->
upgrade; 016 is the single head; only ``stream_providers`` and
``pcs.hls_capable`` are added and every existing row is kept; the upgraded
schema equals the models. Helpers come from test_migration_015.py.

    python -m pytest tests/integration/test_migration_016.py -o addopts='' -p no:cacheprovider
"""

from app.database import Base
from tests.integration.test_migration_015 import (REV_014, REV_015, _alembic, _fingerprint, _seed_014, _tables,
                                                  scratch)  # noqa: F401  (scratch is a fixture)

REV_016 = "016_stream_providers"


def _pcs_columns(engine):
    with engine.connect() as conn:
        return [row[0] for row in conn.exec_driver_sql(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'pcs' ORDER BY ordinal_position")]


def test_single_head_is_016(scratch):  # noqa: F811
    url, _engine = scratch
    heads = _alembic(url, "heads")
    assert REV_016 in heads and heads.count("(head)") == 1


def test_migration_016_round_trip(scratch):  # noqa: F811
    url, engine = scratch
    _alembic(url, "upgrade", REV_014)
    _seed_014(engine)
    _alembic(url, "upgrade", REV_015)
    before = _fingerprint(engine)
    columns = _pcs_columns(engine)
    assert "stream_providers" not in _tables(engine) and "hls_capable" not in columns

    _alembic(url, "upgrade", "head")
    after = _fingerprint(engine)
    assert set(after) - set(before) == {"stream_providers"}
    assert {t: v for t, v in after.items() if t not in ("stream_providers", "pcs")} == \
        {t: v for t, v in before.items() if t != "pcs"}
    assert _pcs_columns(engine) == columns + ["hls_capable"]  # appended; existing columns untouched
    with engine.begin() as conn:
        assert conn.exec_driver_sql("SELECT version_num FROM alembic_version").scalar() == REV_016
        assert conn.exec_driver_sql("SELECT count(*), count(hls_capable) FROM pcs").fetchone() == (2, 0)
        assert conn.exec_driver_sql("SELECT count(*) FROM stream_providers").scalar() == 0
        conn.exec_driver_sql("UPDATE pcs SET hls_capable = true")
        conn.exec_driver_sql("INSERT INTO stream_providers (id, name, type, settings) "
                             "VALUES ('p-1', 'Fixture', 'lumana', '{}')")
    # The upgraded schema is what the models say (new table and column only).
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    def only_new(obj, name, type_, _reflected, _compare_to):
        if type_ == "table":
            return name in ("stream_providers", "pcs")
        if type_ == "column":
            return obj.table.name == "stream_providers" or name == "hls_capable"
        return obj.table.name == "stream_providers" if getattr(obj, "table", None) is not None else False

    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"include_object": only_new, "compare_type": True})
        assert compare_metadata(ctx, Base.metadata) == []

    # Running it again changes nothing (inspector-guarded).
    _alembic(url, "upgrade", "head")

    # downgrade: the table and the flag go; everything else is as before.
    _alembic(url, "downgrade", REV_015)
    assert "stream_providers" not in _tables(engine) and _pcs_columns(engine) == columns
    assert _fingerprint(engine) == before

    _alembic(url, "upgrade", "head")
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT count(*) FROM stream_providers").scalar() == 0
        assert conn.exec_driver_sql("SELECT count(hls_capable) FROM pcs").scalar() == 0
