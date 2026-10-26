"""Make the retained legacy ``hackathons_hackathon`` columns nullable so that
``Hackathon`` INSERTs from the current model succeed.

Context
-------
``0002_align_hackathon_schema`` deliberately kept the legacy columns of the
diverged dev DB (``name``, ``slug``, ``allow_multiple_teams``, ``created_by_id``)
intact: dropping or renaming them was never approved. Three of them are still
``NOT NULL`` with no default, while the rewritten ``0001_initial`` (and the
``Hackathon`` model) know nothing about them. Django therefore omits them from
every INSERT and Postgres rejects the row:

    null value in column "name" of relation "hackathons_hackathon"
    violates not-null constraint

Verified against the dev DB: ``name`` varchar(200), ``slug`` varchar(220)
(carrying ``hackathons_hackathon_slug_key UNIQUE``) and
``allow_multiple_teams`` boolean are all NOT NULL with no default, so fixing
``name`` alone would just move the failure to ``slug`` and then to
``allow_multiple_teams``. ``created_by_id`` is already nullable and harmless.

Strategy
--------
Relax those three NOT NULLs physically, nothing else:
  * No column is dropped, renamed or repopulated, and no row is touched.
  * Postgres exempts NULLs from UNIQUE, so a nullable ``slug`` stops blocking
    inserts without dropping the constraint.
  * Nothing is invented: new rows simply carry NULL in columns no code path
    reads (grep over the apps and templates finds no consumer of
    ``hackathon.name`` / ``hackathon.slug`` / ``allow_multiple_teams``).
  * The real cleanup stays a separately approved follow-up (populate legacy
    statuses/business values, then drop the dead columns).

State fidelity
--------------
These columns are not part of the migration state, so ``state_forwards`` is a
no-op and ``makemigrations --check --dry-run`` stays clean. The DDL is
physically conditional: a fresh DB (built from 0001 + 0002, e.g. the test
runner) has no legacy columns at all and is left completely untouched, which
also keeps the regression tests meaningful on both kinds of database.

Reversal
--------
Restoring NOT NULL is only safe while every row has a value. Rows created after
this migration have NULL in these columns, so ``database_backwards`` counts the
NULLs first and aborts with an explicit message *before* running any DDL; the
operator must populate (or remove) those rows in a reviewed step, never by an
automated guess from this migration.
"""

from django.db import migrations
from django.db.migrations.operations.base import Operation

TABLE = "hackathons_hackathon"

# Legacy columns that block the model's INSERT, in dependency order.
LEGACY_COLUMNS = ("name", "slug", "allow_multiple_teams")


def _physical_columns(connection):
    with connection.cursor() as c:
        c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            [TABLE],
        )
        return {r[0] for r in c.fetchall()}


def _is_nullable(connection, column):
    with connection.cursor() as c:
        c.execute(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = %s AND column_name = %s",
            [TABLE, column],
        )
        row = c.fetchone()
    return row is not None and row[0] == "YES"


def _null_counts(connection, columns):
    """{column: number of rows with NULL}, for columns that physically exist."""
    physical = _physical_columns(connection)
    quoted = connection.ops.quote_name(TABLE)
    counts = {}
    with connection.cursor() as c:
        for column in columns:
            if column not in physical:
                continue
            c.execute(f'SELECT count(*) FROM {quoted} WHERE "{column}" IS NULL')
            counts[column] = c.fetchone()[0]
    return counts


class ReversalUnsafe(RuntimeError):
    """The table holds rows that cannot satisfy the restored NOT NULLs."""


class LegacyHackathonColumnsNullable(Operation):
    """DROP NOT NULL on the legacy columns that the model no longer maps,
    where and only where they are physically present and still NOT NULL.

    Reverse is guarded: it refuses to run while any row has NULL in a column it
    would have to make NOT NULL again, because Postgres would either fail
    mid-transaction or force this migration to invent data.
    """

    def state_forwards(self, app_label, state):
        # No-op: these columns are absent from the migration state, so state
        # keeps matching the models and makemigrations detects no drift.
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        connection = schema_editor.connection
        quoted = connection.ops.quote_name(TABLE)
        with connection.cursor() as c:
            for column in LEGACY_COLUMNS:
                if column in _physical_columns(connection) and not _is_nullable(connection, column):
                    c.execute(f'ALTER TABLE {quoted} ALTER COLUMN "{column}" DROP NOT NULL')

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        connection = schema_editor.connection
        # Refuse before any DDL: a column we would make NOT NULL again must not
        # hold NULLs, and only this migration knows which columns are at stake.
        blockers = {column: count for column, count in
                    _null_counts(connection, LEGACY_COLUMNS).items()
                    if count and _is_nullable(connection, column)}
        if blockers:
            detail = ", ".join(f'"{c}"={n}' for c, n in sorted(blockers.items()))
            raise ReversalUnsafe(
                f"Cannot restore NOT NULL on {TABLE}: rows have NULL values ({detail}). "
                "Populate those columns from a verified source (or remove the rows "
                "created after this migration) in a separately reviewed step before "
                "reversing. This migration will not invent data."
            )
        quoted = connection.ops.quote_name(TABLE)
        with connection.cursor() as c:
            c.execute("SET CONSTRAINTS ALL IMMEDIATE")
            for column in LEGACY_COLUMNS:
                if column in _physical_columns(connection) and _is_nullable(connection, column):
                    c.execute(f'ALTER TABLE {quoted} ALTER COLUMN "{column}" SET NOT NULL')

    def describe(self):
        return (f"Make the legacy columns {', '.join(LEGACY_COLUMNS)} nullable "
                f"on {TABLE} (physical only, where present)")


class Migration(migrations.Migration):

    dependencies = [
        ("hackathons", "0002_align_hackathon_schema"),
    ]

    operations = [
        LegacyHackathonColumnsNullable(),
    ]
