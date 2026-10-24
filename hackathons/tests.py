import importlib
import json
import os
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from config import settings as config_settings
from hackathons import ai
from hackathons.models import Hackathon

User = get_user_model()


def build_hackathon(organizer, title="Phase Test", **overrides):
    """Unsaved instance: NULL dates are impossible to INSERT on a fresh DB
    (NOT NULL in the new schema) but exist physically on the dev DB after
    0002, so in-memory objects are the faithful way to exercise the guard."""
    now = timezone.now()
    defaults = dict(
        organizer=organizer,
        title=title,
        description="d",
        category=Hackathon.Category.AI,
        event_format=Hackathon.Format.ONLINE,
        start_date=now + timedelta(days=2),
        end_date=now + timedelta(days=5),
        registration_deadline=now + timedelta(days=1),
        submission_deadline=now + timedelta(days=4),
        min_team_size=2,
        max_team_size=5,
        status=Hackathon.Status.PUBLISHED,
    )
    defaults.update(overrides)
    return Hackathon(**defaults)


class HackathonPhaseTests(TestCase):
    """phase must never compare a datetime with None (0002 left the date
    columns nullable on the dev DB) and must not touch the stored status."""

    @classmethod
    def setUpTestData(cls):
        cls.org = User.objects.create_user(
            username="phase_org", email="phase_org@ex.com",
            password="pass12345", role=User.Role.ORGANIZER,
        )

    def make(self, **overrides):
        return build_hackathon(self.org, **overrides)

    # --- existing semantics preserved for records with valid dates ---
    def test_upcoming_ongoing_completed_unchanged(self):
        now = timezone.now()
        upcoming = self.make(title="up", start_date=now + timedelta(days=2),
                             end_date=now + timedelta(days=5))
        ongoing = self.make(title="on", start_date=now - timedelta(days=2),
                            end_date=now + timedelta(days=5))
        completed = self.make(title="done", start_date=now - timedelta(days=5),
                              end_date=now - timedelta(days=2))
        self.assertEqual(upcoming.phase, "upcoming")
        self.assertEqual(ongoing.phase, "ongoing")
        self.assertEqual(completed.phase, "completed")

    def test_cancelled_and_draft_short_circuit(self):
        cancelled = self.make(title="c", status=Hackathon.Status.CANCELLED,
                              start_date=None, end_date=None)
        draft = self.make(title="dr", status=Hackathon.Status.DRAFT,
                          start_date=None, end_date=None)
        self.assertEqual(cancelled.phase, "cancelled")
        self.assertEqual(draft.phase, "draft")

    # --- NULL-date guards ---
    def test_null_start_date_returns_unscheduled(self):
        h = self.make(title="n1", start_date=None, end_date=None)
        self.assertEqual(h.phase, "unscheduled")

    def test_published_with_null_start_date_no_crash(self):
        h = self.make(title="n2", status=Hackathon.Status.PUBLISHED,
                      start_date=None, end_date=None)
        self.assertEqual(h.phase, "unscheduled")

    def test_legacy_status_with_null_dates_no_crash(self):
        # dev rows 14/15/16 carry statuses outside the model's choices.
        h = self.make(title="legacy", status="registration_open",
                      start_date=None, end_date=None)
        self.assertEqual(h.phase, "unscheduled")

    def test_null_end_date_with_past_start_is_ongoing(self):
        now = timezone.now()
        h = self.make(title="n3", start_date=now - timedelta(days=1),
                      end_date=None)
        self.assertEqual(h.phase, "ongoing")

    def test_null_end_date_with_future_start_is_upcoming(self):
        now = timezone.now()
        h = self.make(title="n4", start_date=now + timedelta(days=1),
                      end_date=None)
        self.assertEqual(h.phase, "upcoming")

    def test_persisted_row_phase_survives_refresh(self):
        h = build_hackathon(self.org, title="saved")
        h.save()
        h.refresh_from_db()
        self.assertEqual(h.phase, "upcoming")
        self.assertEqual(h.status, Hackathon.Status.PUBLISHED)

    def test_phase_is_read_only(self):
        h = self.make(title="n5", start_date=None, end_date=None)
        for _ in range(3):
            h.phase
        self.assertEqual(h.status, Hackathon.Status.PUBLISHED)


class HackathonWindowPropertyTests(TestCase):
    """registration_open / submission_open gate every Teams join path, so a
    missing deadline must read as 'window closed' instead of raising."""

    @classmethod
    def setUpTestData(cls):
        cls.org = User.objects.create_user(
            username="window_org", email="window_org@ex.com",
            password="pass12345", role=User.Role.ORGANIZER,
        )

    def make(self, **overrides):
        return build_hackathon(self.org, **overrides)

    def test_registration_open_with_future_deadline(self):
        self.assertTrue(self.make().registration_open)

    def test_registration_closed_after_deadline(self):
        h = self.make(registration_deadline=timezone.now() - timedelta(days=1))
        self.assertFalse(h.registration_open)

    def test_null_registration_deadline_is_closed_not_a_typeerror(self):
        h = self.make(registration_deadline=None)
        self.assertFalse(h.registration_open)

    def test_non_published_status_never_opens_registration(self):
        for status in (Hackathon.Status.DRAFT, Hackathon.Status.CANCELLED):
            with self.subTest(status=status):
                self.assertFalse(self.make(status=status).registration_open)

    def test_submission_window(self):
        now = timezone.now()
        self.assertTrue(self.make(start_date=now - timedelta(hours=1)).submission_open)
        self.assertFalse(self.make(start_date=now + timedelta(days=1)).submission_open)
        self.assertFalse(self.make(start_date=None).submission_open)
        self.assertFalse(self.make(submission_deadline=None).submission_open)


class AIServiceTests(TestCase):
    """hackathons.ai must fail with clear, user-safe errors and never need
    the network in tests (genai client is mocked)."""

    def _mock_client(self, text):
        client = mock.Mock()
        client.models.generate_content.return_value = mock.Mock(text=text)
        return mock.patch("google.genai.Client", return_value=client), client

    @override_settings(GEMINI_API_KEY="")
    def test_missing_key_raises_config_error(self):
        with self.assertRaises(ai.AIServiceError) as ctx:
            ai.generate_description("Some Title")
        self.assertIn("not configured", str(ctx.exception))

    @override_settings(GEMINI_API_KEY="test-key", GEMINI_MODEL="gemini-test")
    def test_generate_description_uses_client(self):
        patcher, client = self._mock_client("Generated description.")
        with patcher:
            out = ai.generate_description("T", category="ai")
        self.assertEqual(out, "Generated description.")
        client.models.generate_content.assert_called_once()

    @override_settings(GEMINI_API_KEY="test-key", GEMINI_MODEL="gemini-configured")
    def test_client_is_called_with_configured_model(self):
        patcher, client = self._mock_client("Some text.")
        with patcher:
            ai.generate_description("T")
        _, kwargs = client.models.generate_content.call_args
        self.assertEqual(kwargs["model"], "gemini-configured")

    @override_settings(GEMINI_API_KEY="test-key", GEMINI_MODEL="gemini-test")
    def test_empty_answer_raises(self):
        patcher, _ = self._mock_client("   ")
        with patcher, self.assertRaises(ai.AIServiceError):
            ai.generate_description("T")

    @override_settings(GEMINI_API_KEY="test-key", GEMINI_MODEL="gemini-test")
    def test_client_failure_is_wrapped(self):
        with mock.patch("google.genai.Client", side_effect=RuntimeError("boom")), \
                self.assertRaises(ai.AIServiceError) as ctx:
            ai.generate_description("T")
        self.assertIn("temporarily unavailable", str(ctx.exception))

    @override_settings(GEMINI_API_KEY="test-key", GEMINI_MODEL="gemini-test")
    def test_title_and_description_parsed(self):
        patcher, _ = self._mock_client(
            "TITLE: Green Future Hackathon\nBuild sustainable tech in 48 hours.")
        with patcher:
            out = ai.generate_title_and_description(category="greentech")
        self.assertEqual(out["title"], "Green Future Hackathon")
        self.assertIn("sustainable", out["description"])

    @override_settings(GEMINI_API_KEY="test-key", GEMINI_MODEL="gemini-test")
    def test_title_and_description_rejects_unusable_answer(self):
        patcher, _ = self._mock_client("just some prose without markers")
        with patcher, self.assertRaises(ai.AIServiceError):
            ai.generate_title_and_description(category="ai")


class GeminiModelConfigTests(TestCase):
    """Google retired gemini-2.5-flash for new users (404 NOT_FOUND), so the
    project default must be the supported model while GEMINI_MODEL keeps
    overriding it."""

    RETIRED_MODEL = "gemini-2.5-flash"

    def setUp(self):
        self.addCleanup(importlib.reload, config_settings)

    def reload_without_dotenv(self):
        """Re-evaluate config/settings.py ignoring .env, so the code default
        and the process-environment precedence are both observable."""
        with mock.patch("dotenv.load_dotenv", lambda *a, **k: None):
            return importlib.reload(config_settings)

    def test_default_model_is_the_supported_one(self):
        self.assertEqual(config_settings.GEMINI_MODEL_DEFAULT, "gemini-3.8-flash")

    def test_default_applies_when_env_is_absent(self):
        saved = os.environ.pop("GEMINI_MODEL", None)
        try:
            self.assertEqual(self.reload_without_dotenv().GEMINI_MODEL, "gemini-3.8-flash")
        finally:
            if saved is not None:
                os.environ["GEMINI_MODEL"] = saved

    def test_env_var_takes_precedence_over_default(self):
        os.environ["GEMINI_MODEL"] = "gemini-from-env"
        try:
            self.assertEqual(self.reload_without_dotenv().GEMINI_MODEL, "gemini-from-env")
        finally:
            os.environ.pop("GEMINI_MODEL", None)

    def test_retired_model_is_not_configured(self):
        for relative_path in ("config/settings.py", ".env.example"):
            path = settings.BASE_DIR / relative_path
            self.assertNotIn(self.RETIRED_MODEL, path.read_text(encoding="utf-8"), relative_path)


class HackathonCreationTests(TestCase):
    """Creating a Hackathon through the model must not depend on the legacy
    columns the dev DB still carries (name / slug / allow_multiple_teams).
    A fresh test DB never has those columns, so these tests stay green on both
    kinds of database; 0003 is what makes the dev DB match."""

    url_name = "hackathons:create"

    @classmethod
    def setUpTestData(cls):
        cls.org = User.objects.create_user(
            username="create_org", email="create_org@ex.com",
            password="pass12345", role=User.Role.ORGANIZER)

    def form_payload(self, title="Autumn AI Sprint"):
        now = timezone.now()
        fmt = "%Y-%m-%d %H:%M:%S"
        return {
            "title": title,
            "category": Hackathon.Category.AI,
            "description": "Build something useful in 48 hours.",
            "event_format": Hackathon.Format.ONLINE,
            "location": "",
            "start_date": (now + timedelta(days=10)).strftime(fmt),
            "end_date": (now + timedelta(days=12)).strftime(fmt),
            "registration_deadline": (now + timedelta(days=8)).strftime(fmt),
            "submission_deadline": (now + timedelta(days=11)).strftime(fmt),
            "max_participants": 60,
            "min_team_size": 2,
            "max_team_size": 4,
        }

    def test_model_never_maps_the_legacy_columns(self):
        mapped = {f.name for f in Hackathon._meta.concrete_fields}
        self.assertEqual(mapped, {
            "id", "organizer", "title", "description", "category", "event_format",
            "location", "banner", "start_date", "end_date", "registration_deadline",
            "submission_deadline", "max_participants", "min_team_size",
            "max_team_size", "status", "results_published", "created_at", "updated_at",
        })
        self.assertFalse({"name", "slug", "allow_multiple_teams"} & mapped)

    def test_create_view_persists_a_draft_hackathon(self):
        self.client.force_login(self.org)
        resp = self.client.post(reverse(self.url_name), self.form_payload())
        self.assertEqual(resp.status_code, 302)
        hackathon = Hackathon.objects.get(title="Autumn AI Sprint")
        self.assertEqual(hackathon.status, Hackathon.Status.DRAFT)
        self.assertEqual(hackathon.phase, "draft")
        self.assertEqual(hackathon.organizer_id, self.org.pk)
        self.assertRedirects(resp, reverse("hackathons:detail", args=[hackathon.pk]))

    def test_created_row_round_trips_every_value(self):
        hackathon = build_hackathon(self.org, title="Round trip")
        hackathon.save()
        hackathon.refresh_from_db()
        self.assertIsNotNone(hackathon.pk)
        self.assertEqual(hackathon.description, "d")
        self.assertEqual(hackathon.category, Hackathon.Category.AI)
        self.assertEqual(hackathon.max_participants, 150)
        self.assertEqual(hackathon.max_team_size, 5)
        self.assertEqual(hackathon.phase, "upcoming")
        self.assertTrue(hackathon.created_at and hackathon.updated_at)

    def test_same_title_twice_is_allowed(self):
        # The dev DB keeps a UNIQUE index on the legacy slug column; NULL slugs
        # are exempt from it, so two identically-titled hackathons must both save.
        first = build_hackathon(self.org, title="Twin")
        second = build_hackathon(self.org, title="Twin")
        first.save()
        second.save()
        self.assertEqual(Hackathon.objects.filter(title="Twin").count(), 2)
        self.assertNotEqual(first.pk, second.pk)

    def test_status_flips_with_update_fields_persist(self):
        hackathon = build_hackathon(self.org, title="Publish me")
        hackathon.save()
        hackathon.status = Hackathon.Status.PUBLISHED
        hackathon.save(update_fields=["status", "updated_at"])
        hackathon.refresh_from_db()
        self.assertEqual(hackathon.status, Hackathon.Status.PUBLISHED)
        self.assertTrue(hackathon.registration_open)


class LegacyColumnMigrationTests(TestCase):
    """0003 is physically conditional and its reverse is guarded."""

    @classmethod
    def setUpTestData(cls):
        cls.org = User.objects.create_user(
            username="mig_org", email="mig_org@ex.com",
            password="pass12345", role=User.Role.ORGANIZER)

    def _migration_module(self):
        return importlib.import_module(
            "hackathons.migrations.0003_legacy_hackathon_columns_nullable")

    def test_forward_is_a_noop_on_a_fresh_database(self):
        # The test DB is built from 0001 + 0002, which never create the legacy
        # columns; running the forward leg there must neither fail nor alter
        # the schema the model relies on.
        mod = self._migration_module()
        before = {f.name: f.null for f in Hackathon._meta.concrete_fields}
        mod.LegacyHackathonColumnsNullable().database_forwards(
            "hackathons", connection.schema_editor(), None, None)
        hackathon = build_hackathon(self.org, title="after forward")
        hackathon.save()
        hackathon.refresh_from_db()
        self.assertEqual(hackathon.title, "after forward")
        self.assertEqual({f.name: f.null for f in Hackathon._meta.concrete_fields}, before)

    def test_reverse_refuses_while_legacy_columns_hold_nulls(self):
        mod = self._migration_module()
        editor = mock.Mock()
        with mock.patch.object(mod, "_null_counts",
                               return_value={"name": 2, "slug": 2,
                                             "allow_multiple_teams": 0}), \
                mock.patch.object(mod, "_is_nullable", return_value=True), \
                self.assertRaises(mod.ReversalUnsafe) as ctx:
            mod.LegacyHackathonColumnsNullable().database_backwards(
                "hackathons", editor, None, None)
        message = str(ctx.exception)
        self.assertIn('"name"=2', message)
        self.assertIn('"slug"=2', message)
        self.assertNotIn("allow_multiple_teams", message)
        editor.connection.cursor.assert_not_called()


class GenerateDescriptionEndpointTests(TestCase):
    url_name = "hackathons:generate_description"

    def setUp(self):
        self.org = User.objects.create_user(
            username="ai_org", email="ai_org@ex.com",
            password="pass12345", role=User.Role.ORGANIZER)
        self.participant = User.objects.create_user(
            username="ai_part", email="ai_part@ex.com",
            password="pass12345", role=User.Role.PARTICIPANT)

    def _post(self, user, payload):
        self.client.force_login(user)
        return self.client.post(
            reverse(self.url_name),
            data=json.dumps(payload), content_type="application/json")

    def test_with_title_returns_description(self):
        with mock.patch("hackathons.views.ai.generate_description",
                        return_value="Desc here") as gen:
            resp = self._post(self.org, {"title": "My Hack", "category": "AI"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["description"], "Desc here")
        self.assertIn("elapsed_ms", resp.json())
        gen.assert_called_once()
        self.assertNotIn("title", resp.json())

    def test_category_only_generates_title_and_description(self):
        with mock.patch("hackathons.views.ai.generate_title_and_description",
                        return_value={"title": "T", "description": "D"}):
            resp = self._post(self.org, {"category": "ai"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["title"], "T")
        self.assertEqual(resp.json()["description"], "D")

    def test_empty_payload_rejected(self):
        resp = self._post(self.org, {})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("error", resp.json())

    def test_invalid_json_rejected(self):
        self.client.force_login(self.org)
        resp = self.client.post(reverse(self.url_name),
                                data="{nope", content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_service_error_maps_to_502_with_message(self):
        with mock.patch("hackathons.views.ai.generate_description",
                        side_effect=ai.AIServiceError("The AI service is not configured.")):
            resp = self._post(self.org, {"title": "My Hack"})
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.json()["error"], "The AI service is not configured.")

    def test_participant_forbidden(self):
        resp = self._post(self.participant, {"title": "My Hack"})
        self.assertEqual(resp.status_code, 403)

    def test_get_not_allowed(self):
        self.client.force_login(self.org)
        resp = self.client.get(reverse(self.url_name))
        self.assertEqual(resp.status_code, 405)


class AlignSchemaReversalGuardTests(TestCase):
    """0002 must never destroy data that post-dates its forward run.

    The test DB is built from 0001 + 0002, so it is a *fresh* database: the
    legacy columns do not exist there. A synthetic diverged shape is created
    with raw DDL inside the test transaction, which Postgres rolls back.
    """

    @classmethod
    def setUpTestData(cls):
        cls.org = User.objects.create_user(
            username="guard_org", email="guard_org@ex.com",
            password="pass12345", role=User.Role.ORGANIZER)

    def _module(self):
        return importlib.import_module(
            "hackathons.migrations.0002_align_hackathon_schema")

    def _add_legacy_columns(self):
        quoted = connection.ops.quote_name("hackathons_hackathon")
        with connection.cursor() as c:
            # Deferred FK inserts (Challenge) queue RI trigger events, and
            # Postgres refuses ALTER TABLE until they are flushed.
            c.execute("SET CONSTRAINTS ALL IMMEDIATE")
            c.execute(f'ALTER TABLE {quoted} ADD COLUMN IF NOT EXISTS "name" varchar(200) NULL')
            c.execute(f'ALTER TABLE {quoted} ADD COLUMN IF NOT EXISTS "created_by_id" bigint NULL')
            c.execute(f'UPDATE {quoted} SET name = title, created_by_id = organizer_id')

    def test_reverse_is_a_noop_on_a_fresh_database(self):
        # 0001 legitimately owns this schema on a fresh DB: reversing 0002
        # there must not drop the model's columns nor its Challenge table.
        mod = self._module()
        hackathon = build_hackathon(self.org, title="Keep me")
        hackathon.save()
        hackathon.challenges.create(title="c", description="d")

        self.assertFalse(mod._is_diverged_db(connection))
        editor = connection.schema_editor()
        mod.NotNullFlip().database_backwards("hackathons", editor, None, None)
        mod.AlignHackathonSchema().database_backwards("hackathons", editor, None, None)

        hackathon.refresh_from_db()
        self.assertEqual(hackathon.title, "Keep me")
        self.assertEqual(hackathon.challenges.count(), 1)

    def test_census_flags_every_value_the_reverse_would_destroy(self):
        mod = self._module()
        hackathon = build_hackathon(self.org, title="Legacy row")
        hackathon.save()
        hackathon.challenges.create(title="c", description="d")
        # The legacy columns mirror the aligned ones, as they do right after
        # the forward run.
        self._add_legacy_columns()

        blockers = mod._reversal_blockers(connection)
        self.assertEqual(blockers["hackathons_challenge rows"], 1)
        # Every value with no legacy counterpart was written after the forward.
        self.assertEqual(blockers['"category" values'], 1)
        self.assertEqual(blockers['"start_date" values'], 1)
        self.assertEqual(blockers['"max_participants" values'], 1)
        # title/organizer still re-derivable from name/created_by_id.
        self.assertNotIn('"title" values edited away from "name"', blockers)
        self.assertNotIn('"organizer_id" values edited away from "created_by_id"', blockers)

        with connection.cursor() as c:
            c.execute('UPDATE hackathons_hackathon SET title = %s', ["Renamed later"])
        blockers = mod._reversal_blockers(connection)
        self.assertEqual(blockers['"title" values edited away from "name"'], 1)

    def test_reverse_refuses_before_any_ddl(self):
        mod = self._module()
        editor = mock.Mock()
        with mock.patch.object(mod, "_is_diverged_db", return_value=True), \
                mock.patch.object(mod, "_reversal_blockers",
                                  return_value={'"category" values': 3,
                                                "hackathons_challenge rows": 1}), \
                self.assertRaises(mod.ReversalUnsafe) as ctx:
            # Django asks the last operation first, so NotNullFlip is the one
            # that has to abort the whole reversal.
            mod.NotNullFlip().database_backwards("hackathons", editor, None, None)
        message = str(ctx.exception)
        self.assertIn('"category" values=3', message)
        self.assertIn("hackathons_challenge rows=1", message)
        self.assertEqual(editor.method_calls, [])
