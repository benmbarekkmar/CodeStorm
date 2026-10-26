"""Bring a diverged ``hackathons_hackathon`` table in line with the current
``Hackathon`` model and create the missing ``Challenge`` table — data-safely,
and correctly on BOTH kinds of databases.

Context
-------
``0001_initial`` was rewritten in place (commit 920c306) from an older
Hackathon schema (name/slug/created_by/allow_multiple_teams) to the current one
(title/organizer/category/dates/...). The dev DB only ever applied the *old*
0001, so the new columns and ``hackathons_challenge`` were never created even
though ``0001_initial`` is marked applied. Fresh databases (test runners, new
installs) apply the *new* 0001 and already have the full schema — an
unconditional ADD COLUMN here would crash on them ("column already exists").

Strategy
--------
The legacy columns (``name``/``created_by_id``) are the marker: they exist
only on a diverged DB.
  * ``AlignHackathonSchema`` adds only physically-missing columns, creates
    ``hackathons_challenge`` only if absent, and copies ONLY verified legacy
    data (``title = name``; ``organizer_id = created_by_id`` where that user
    exists) via raw SQL — independently per field, never overwriting existing
    non-null values. On a fresh DB it is a complete no-op (schema and data).
  * ``NotNullFlip`` turns ``title``/``organizer_id`` NOT NULL only when this
    DB was aligned by the forward run (legacy columns still present); the
    matching state change always makes state == model.
  * Reverse drops ONLY what the forward added (Challenge if we created it,
    our columns if physically present, NOT NULL only on diverged-style DBs
    where 0001 cannot have created them). Legacy columns/data untouched, and
    it refuses outright (``ReversalUnsafe``) as soon as the census finds data
    that post-dates the forward run: any ``hackathons_challenge`` row, any
    non-NULL value in a column that has no legacy counterpart, and any
    ``title``/``organizer`` edited away from ``name``/``created_by_id``.
  * Legacy columns (name, slug, created_by_id, allow_multiple_teams) are NOT
    dropped or renamed. Legacy status values are NOT converted. Missing
    business values (category/event_format/dates/...) are NOT invented; those
    fields are added nullable and stay nullable. ``0003`` only relaxed the
    legacy NOT NULLs so the rewritten model can INSERT at all; populating real
    business values for the pre-existing dev rows is a separate, explicitly
    approved data step (never an automated guess from this migration).

State fidelity
--------------
The rewritten 0001 already declares the FULL new schema in migration STATE
(it is only the dev DB's physical tables that diverged). So this migration is
state-neutral: after it, state == models exactly, and
``makemigrations --check --dry-run`` reports NO drift, before or after apply.
The deliberate gap is physical only: on the dev DB, ``category``,
``event_format``, ``location``, the four datetimes, ``max_participants`` and
``min_team_size`` are added NULLABLE (real values unknown — not invented)
while the model/state say NOT NULL. Extra permissiveness in the DB cannot
break Django writes; it only means raw-SQL inserts could bypass the not-NULL
invariant. Closing that gap means populating verified values, which is a
separately approved data step (``0003`` does not do it — see above).
"""

from django.db import migrations, models
from django.db.migrations.operations.base import Operation
import django.db.models.deletion

TABLE = "hackathons_hackathon"

# Physical column DDL for the missing fields (all NULLABLE — the model's
# NOT NULL is not enforced here because the table already has rows and the
# real values are unknown; title/organizer are flipped by NotNullFlip).
_SIMPLE_DDL = {
    "title": 'varchar(150) NULL',
    "category": 'varchar(20) NULL',
    "event_format": 'varchar(20) NULL',
    "location": 'varchar(200) NULL',
    "banner": 'varchar(100) NULL',
    "start_date": 'timestamp with time zone NULL',
    "end_date": 'timestamp with time zone NULL',
    "registration_deadline": 'timestamp with time zone NULL',
    "submission_deadline": 'timestamp with time zone NULL',
    "max_participants": 'integer NULL CHECK ("max_participants" >= 0)',
    "min_team_size": 'smallint NULL CHECK ("min_team_size" >= 0)',
}


def _physical_columns(connection, table=TABLE):
    with connection.cursor() as c:
        c.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
            [table],
        )
        return {r[0] for r in c.fetchall()}


def _table_exists(connection, table):
    with connection.cursor() as c:
        c.execute("SELECT to_regclass(%s)", [table])
        return c.fetchone()[0] is not None


def _is_diverged_db(connection):
    """True only on a DB whose hackathon table still carries the legacy
    columns; such a DB can never have received the new 0001 schema."""
    return {"name", "created_by_id"} <= _physical_columns(connection)


# Columns this migration adds that have NO legacy counterpart. Any non-NULL
# value in one of them was therefore written *after* the forward run, so the
# reverse would destroy data that nothing else can restore.
_NO_LEGACY_SOURCE = tuple(column for column in _SIMPLE_DDL if column != "title")


class ReversalUnsafe(RuntimeError):
    """Reversing would delete rows or column values created after the forward."""


def _reversal_blockers(connection):
    """Census, read-only, of the data the reverse would irrecoverably destroy.

    ``{label: count}``; empty means the reverse can only remove what the
    forward added. title/organizer_id are judged against their legacy source
    columns: a value that still equals ``name``/``created_by_id`` can be
    re-derived, an edited one cannot.
    """
    blockers = {}
    physical = _physical_columns(connection)
    quoted = connection.ops.quote_name(TABLE)
    with connection.cursor() as c:
        if _table_exists(connection, "hackathons_challenge"):
            # The forward only creates this table when absent, so on a diverged
            # DB every row inside it post-dates the migration.
            c.execute("SELECT count(*) FROM hackathons_challenge")
            count = c.fetchone()[0]
            if count:
                blockers["hackathons_challenge rows"] = count
        checks = [(column, f'"{column}" values',
                   f'WHERE "{column}" IS NOT NULL') for column in _NO_LEGACY_SOURCE]
        checks += [
            ("title", '"title" values edited away from "name"',
              'WHERE title IS NOT NULL AND (name IS NULL OR title <> name)'),
            ("organizer_id", '"organizer_id" values edited away from "created_by_id"',
              'WHERE organizer_id IS NOT NULL '
              'AND (created_by_id IS NULL OR organizer_id <> created_by_id)'),
        ]
        for column, label, where in checks:
            if column not in physical:
                continue
            c.execute(f'SELECT count(*) FROM {quoted} {where}')
            count = c.fetchone()[0]
            if count:
                blockers[label] = count
    return blockers


def _refuse_reversal_if_blocked(connection):
    """Abort before any DDL when the reverse would delete real data.

    Called by both backwards operations: Django reverses a migration by running
    its operations in reverse order inside one transaction, so the first raise
    rolls back whatever the earlier one had already done.
    """
    blockers = _reversal_blockers(connection)
    if not blockers:
        return
    detail = ", ".join(f"{label}={count}" for label, count in sorted(blockers.items()))
    raise ReversalUnsafe(
        f"Cannot reverse 0002_align_hackathon_schema on this database: it holds "
        f"data created after the migration ({detail}). The reverse drops the "
        f"hackathons_challenge table and the {_NO_LEGACY_SOURCE} / organizer_id "
        "columns of hackathons_hackathon, and none of those values exist in the "
        "legacy columns any more. Export the rows first and replay them on a "
        "0001-built database, or delete them in a separately reviewed step. "
        "This migration will not invent or silently discard data."
    )


def _copy_verified_data(connection):
    """Raw SQL: the legacy columns exist physically but NOT in migration
    state, so the historical model cannot expose them. Each field is copied
    independently, only where its target is still NULL; existing non-null
    values are never overwritten. organizer requires the user to exist."""
    quoted = connection.ops.quote_name(TABLE)
    with connection.cursor() as c:
        c.execute(
            f"UPDATE {quoted} SET title = name "
            "WHERE title IS NULL AND name IS NOT NULL AND name <> ''"
        )
        c.execute(
            f"UPDATE {quoted} SET organizer_id = created_by_id "
            "WHERE organizer_id IS NULL AND created_by_id IS NOT NULL "
            "AND created_by_id IN (SELECT id FROM accounts_user)"
        )


def _undo_verified_copy(connection):
    """Null the copied values only where they STILL equal their legacy
    sources; post-apply edits are never destroyed."""
    quoted = connection.ops.quote_name(TABLE)
    with connection.cursor() as c:
        c.execute(f"UPDATE {quoted} SET title = NULL "
                  "WHERE title IS NOT NULL AND title = name")
        c.execute(f"UPDATE {quoted} SET organizer_id = NULL "
                  "WHERE organizer_id IS NOT NULL AND organizer_id = created_by_id")


def _set_not_null(connection, column):
    # A DEFERRED FK added in this same transaction leaves pending RI trigger
    # events, and Postgres refuses ALTER TABLE on such a relation; flush
    # constraints first. No-op when the column is already NOT NULL.
    with connection.cursor() as c:
        c.execute("SET CONSTRAINTS ALL IMMEDIATE")
        c.execute(
            f'ALTER TABLE {connection.ops.quote_name(TABLE)} '
            f'ALTER COLUMN "{column}" SET NOT NULL'
        )


def _drop_not_null(connection, column):
    with connection.cursor() as c:
        c.execute(
            f'ALTER TABLE {connection.ops.quote_name(TABLE)} '
            f'ALTER COLUMN "{column}" DROP NOT NULL'
        )


class _StateAlterTitle(migrations.AlterField):
    def __init__(self):
        super().__init__(
            model_name="hackathon", name="title",
            field=models.CharField(max_length=150, verbose_name="Title"),
        )


class _StateAlterOrganizer(migrations.AlterField):
    def __init__(self):
        super().__init__(
            model_name="hackathon", name="organizer",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="organized_hackathons", to="accounts.user",
            ),
        )


class AlignHackathonSchema(Operation):
    """Add missing Hackathon columns (raw DDL, all nullable), create
    ``hackathons_challenge`` if absent, and copy verified legacy data —
    each conditional on the PHYSICAL database. Migration STATE already
    matches the models (the rewritten 0001 declares the full new schema;
    only the dev DB's tables diverged), so state_forwards is a no-op and
    ``makemigrations --check`` stays clean before and after.

    Raw DDL is deliberate: schema_editor.add_field/create_model choke on
    ForeignKey instances whose targets cannot be resolved inside this
    diverged migration graph. The statements mirror exactly what
    ``sqlmigrate`` renders for the equivalent AddField/CreateModel ops.

    Reverse runs in a fresh process (instance attrs from the forward run are
    gone), so it derives everything from the DB itself: legacy columns
    present == this DB was aligned by this migration == safe to remove.
    """

    def state_forwards(self, app_label, state):
        # No-op: state == models already. (Kept as an operation class so
        # forwards plans/describe still show the alignment step.)
        pass

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        connection = schema_editor.connection
        quoted = connection.ops.quote_name(TABLE)
        physical = _physical_columns(connection)
        self.diverged = _is_diverged_db(connection)
        with connection.cursor() as c:
            for name, ddl in _SIMPLE_DDL.items():
                if name not in physical:
                    c.execute(f'ALTER TABLE {quoted} ADD COLUMN "{name}" {ddl}')
            if "organizer_id" not in physical:
                c.execute(
                    f'ALTER TABLE {quoted} ADD COLUMN "organizer_id" bigint NULL '
                    'CONSTRAINT "hackathons_hackathon_organizer_id_562f708b_fk_accounts_user_id" '
                    'REFERENCES "accounts_user"("id") DEFERRABLE INITIALLY DEFERRED'
                )
                c.execute(
                    f'CREATE INDEX "hackathons_hackathon_organizer_id_562f708b" '
                    f'ON {quoted} ("organizer_id")'
                )
        self.challenge_created = not _table_exists(connection, "hackathons_challenge")
        if self.challenge_created:
            with connection.cursor() as c:
                c.execute(
                    'CREATE TABLE "hackathons_challenge" ('
                    '"id" bigint NOT NULL PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY, '
                    '"title" varchar(150) NOT NULL, '
                    '"description" text NOT NULL, '
                    '"difficulty" varchar(10) NOT NULL, '
                    '"deadline" timestamp with time zone NULL, '
                    '"is_published" boolean NOT NULL, '
                    '"published_at" timestamp with time zone NULL, '
                    '"created_at" timestamp with time zone NOT NULL, '
                    '"hackathon_id" bigint NOT NULL)'
                )
                c.execute(
                    'ALTER TABLE "hackathons_challenge" ADD CONSTRAINT '
                    '"hackathons_challenge_hackathon_id_ef7bdb7d_fk_hackathon" '
                    'FOREIGN KEY ("hackathon_id") REFERENCES "hackathons_hackathon" ("id") '
                    'DEFERRABLE INITIALLY DEFERRED'
                )
                c.execute(
                    'CREATE INDEX "hackathons_challenge_hackathon_id_ef7bdb7d" '
                    'ON "hackathons_challenge" ("hackathon_id")'
                )
        if self.diverged:
            _copy_verified_data(connection)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        connection = schema_editor.connection
        # On a fresh DB (no legacy columns) 0001 legitimately owns this
        # schema; reversing 0002 there must not destroy 0001's tables.
        if not _is_diverged_db(connection):
            return
        _refuse_reversal_if_blocked(connection)
        quoted = connection.ops.quote_name(TABLE)
        with connection.cursor() as c:
            # challenge_id FK columns only exist if we created the table.
            if _table_exists(connection, "hackathons_challenge"):
                c.execute("DROP TABLE IF EXISTS hackathons_challenge")
            _undo_verified_copy(connection)
            # Dropping hackathons_challenge (or its deferred FK) can leave
            # pending RI trigger events queued against hackathons_hackathon,
            # and Postgres refuses ALTER TABLE on such a relation; flush the
            # queue first. Harmless when no deferred constraint remains.
            c.execute("SET CONSTRAINTS ALL IMMEDIATE")
            for column in list(_SIMPLE_DDL) + ["organizer_id"]:
                if column in _physical_columns(connection):
                    c.execute(f'ALTER TABLE {quoted} DROP COLUMN "{column}"')

    def describe(self):
        return ("Align hackathons_hackathon with the model (missing columns "
                "only), create hackathons_challenge if absent, copy verified "
                "legacy data")


class NotNullFlip(Operation):
    """title/organizer are NOT NULL on the model and fully populated after
    AlignHackathonSchema (verified: every diverged row has a non-empty name
    and a created_by pointing at an existing user; fresh rows were always
    NOT NULL). Flip physically, and always fix the state to match the model.
    If a row were somehow still NULL, Postgres aborts and the whole migration
    rolls back atomically."""

    FLIPS = (("title", "title", _StateAlterTitle),
             ("organizer_id", "organizer", _StateAlterOrganizer))

    def _is_nonnull(self, connection, column, table=TABLE):
        with connection.cursor() as c:
            c.execute(
                "SELECT is_nullable FROM information_schema.columns "
                "WHERE table_name = %s AND column_name = %s",
                [table, column],
            )
            row = c.fetchone()
        return row is not None and row[0] == "NO"

    def state_forwards(self, app_label, state):
        for _col, _name, alter_cls in self.FLIPS:
            alter_cls().state_forwards(app_label, state)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        self.flipped = []
        for column, _name, _cls in self.FLIPS:
            if not self._is_nonnull(schema_editor.connection, column):
                _set_not_null(schema_editor.connection, column)
                self.flipped.append(column)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        # On a fresh DB these NOT NULLs belong to 0001 — never relax them.
        if not _is_diverged_db(schema_editor.connection):
            return
        # Django reverses the operations of this migration in order, so this
        # runs first: refuse the whole reversal before any DDL is emitted.
        _refuse_reversal_if_blocked(schema_editor.connection)
        for column, _name, _cls in self.FLIPS:
            if self._is_nonnull(schema_editor.connection, column):
                _drop_not_null(schema_editor.connection, column)

    def describe(self):
        return "Set title/organizer NOT NULL (physical only where needed)"


class Migration(migrations.Migration):

    # 0001_initial is left untouched (its history is unfaithful on the dev DB
    # but it is recorded as applied; rewriting it would corrupt rollback).
    dependencies = [
        migrations.swappable_dependency("accounts.User"),
        ("hackathons", "0001_initial"),
    ]

    operations = [
        AlignHackathonSchema(),
        NotNullFlip(),
    ]
