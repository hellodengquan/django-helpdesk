import os
import tempfile
import shutil
from unittest.mock import patch

from django.contrib.auth import get_user_model, authenticate
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command, CommandError
from django.test import TestCase, override_settings
from django.urls import reverse

from helpdesk.models import (
    Queue,
    Ticket,
    FollowUp,
    FollowUpAttachment,
    TicketChange,
    TicketCC,
    TicketDependency,
    TicketCustomFieldValue,
    KBCategory,
    KBItem,
    UserSettings,
)


User = get_user_model()

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEMO_FIXTURE = os.path.join(PROJECT_ROOT, "demodesk", "fixtures", "demo.json")


class ResetDemoDataTestCase(TestCase):
    """Test the reset_demo_data management command."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not os.path.exists(DEMO_FIXTURE):
            raise FileNotFoundError(
                f"Demo fixture not found at {DEMO_FIXTURE}. "
                f"Tests cannot run without it."
            )
        cls._temp_media_root = tempfile.mkdtemp()
        cls._media_patcher = override_settings(
            MEDIA_ROOT=cls._temp_media_root,
        )
        cls._media_patcher.enable()
        cls._demo_env_patcher = patch.dict(os.environ, {"HELPDESK_DEMO_MODE": "1"})
        cls._demo_env_patcher.start()

    @classmethod
    def tearDownClass(cls):
        cls._demo_env_patcher.stop()
        cls._media_patcher.disable()
        if os.path.exists(cls._temp_media_root):
            shutil.rmtree(cls._temp_media_root)
        super().tearDownClass()

    def setUp(self):
        self.fixture_arg = ["--fixture", DEMO_FIXTURE]

    def _get_demo_state_snapshot(self):
        return {
            "queues": set(Queue.objects.values_list("slug", "title")),
            "queue_count": Queue.objects.count(),
            "ticket_count": Ticket.objects.count(),
            "ticket_titles": set(Ticket.objects.values_list("title", flat=True)),
            "followup_count": FollowUp.objects.count(),
            "attachment_count": FollowUpAttachment.objects.count(),
            "kb_category_count": KBCategory.objects.count(),
            "kb_category_titles": set(
                KBCategory.objects.values_list("title", flat=True)
            ),
            "kb_item_count": KBItem.objects.count(),
            "kb_item_titles": set(KBItem.objects.values_list("title", flat=True)),
            "user_count": User.objects.count(),
            "admin_exists": User.objects.filter(username="admin").exists(),
        }

    def _assert_matches_demo_state(self, snapshot, msg_prefix=""):
        expected_queues = {("DH", "Django Helpdesk"), ("SP", "Some Product")}
        expected_ticket_count = 3
        expected_ticket_titles = {
            "Some django-helpdesk Problem",
            "Something else",
            "Something with an attachment",
        }
        expected_followup_count = 4
        expected_attachment_count = 2
        expected_kb_category_count = 2
        expected_kb_category_titles = {"KB Cat 1", "KB Cat 2"}
        expected_kb_item_count = 3
        expected_kb_item_titles = {
            "Django-Helpdesk",
            "Contributing to django-helpdesk",
            "Something Else",
        }
        expected_admin_exists = True

        self.assertEqual(
            snapshot["queue_count"],
            2,
            f"{msg_prefix}Expected 2 queues, got {snapshot['queue_count']}",
        )
        self.assertEqual(
            snapshot["queues"],
            expected_queues,
            f"{msg_prefix}Queues do not match expected demo state",
        )
        self.assertEqual(
            snapshot["ticket_count"],
            expected_ticket_count,
            f"{msg_prefix}Expected {expected_ticket_count} tickets, "
            f"got {snapshot['ticket_count']}",
        )
        self.assertEqual(
            snapshot["ticket_titles"],
            expected_ticket_titles,
            f"{msg_prefix}Ticket titles do not match expected demo state",
        )
        self.assertEqual(
            snapshot["followup_count"],
            expected_followup_count,
            f"{msg_prefix}Expected {expected_followup_count} followups, "
            f"got {snapshot['followup_count']}",
        )
        self.assertEqual(
            snapshot["attachment_count"],
            expected_attachment_count,
            f"{msg_prefix}Expected {expected_attachment_count} attachments, "
            f"got {snapshot['attachment_count']}",
        )
        self.assertEqual(
            snapshot["kb_category_count"],
            expected_kb_category_count,
            f"{msg_prefix}Expected {expected_kb_category_count} KB categories, "
            f"got {snapshot['kb_category_count']}",
        )
        self.assertEqual(
            snapshot["kb_category_titles"],
            expected_kb_category_titles,
            f"{msg_prefix}KB category titles do not match",
        )
        self.assertEqual(
            snapshot["kb_item_count"],
            expected_kb_item_count,
            f"{msg_prefix}Expected {expected_kb_item_count} KB items, "
            f"got {snapshot['kb_item_count']}",
        )
        self.assertEqual(
            snapshot["kb_item_titles"],
            expected_kb_item_titles,
            f"{msg_prefix}KB item titles do not match",
        )
        self.assertEqual(
            snapshot["admin_exists"],
            expected_admin_exists,
            f"{msg_prefix}Admin user should exist",
        )

    def _create_dirty_data(self):
        extra_user = User.objects.create_user(
            username="dirty_user",
            password="testpass123",
            email="dirty@example.com",
        )

        try:
            extra_queue = Queue.objects.create(
                title="Dirty Queue",
                slug="DQ",
                email_address="dirty@example.com",
            )
        except Exception:
            extra_queue = Queue.objects.create(
                title="Dirty Queue 2",
                slug="DQ2",
                email_address="dirty2@example.com",
            )

        base_queue = Queue.objects.first()
        extra_tickets = []
        for i in range(5):
            t = Ticket.objects.create(
                title=f"Dirty Ticket {i}",
                queue=base_queue,
                submitter_email=f"submitter{i}@example.com",
                description=f"This is dirty ticket {i}",
                priority=3,
                status=Ticket.OPEN_STATUS,
            )
            extra_tickets.append(t)

        for i, ticket in enumerate(extra_tickets):
            fu = FollowUp.objects.create(
                ticket=ticket,
                title=f"Dirty FollowUp {i}",
                comment=f"Dirty comment {i}",
                public=True,
                user=extra_user if i % 2 == 0 else None,
            )

            if i == 0:
                fake_file = SimpleUploadedFile(
                    f"dirty_file_{i}.txt",
                    b"This is dirty attachment content",
                    content_type="text/plain",
                )
                FollowUpAttachment.objects.create(
                    followup=fu,
                    file=fake_file,
                    filename=f"dirty_file_{i}.txt",
                    mime_type="text/plain",
                    size=len(b"This is dirty attachment content"),
                )

        if extra_tickets and len(extra_tickets) >= 2:
            TicketDependency.objects.create(
                ticket=extra_tickets[0],
                depends_on=extra_tickets[1],
            )

        if extra_tickets:
            TicketCC.objects.create(
                ticket=extra_tickets[0],
                email="cc@example.com",
            )

        if extra_tickets:
            fu = extra_tickets[0].followup_set.first()
            if fu:
                TicketChange.objects.create(
                    followup=fu,
                    field="Priority",
                    old_value="3",
                    new_value="1",
                )

        extra_cat = KBCategory.objects.create(
            title="Dirty KB Category",
            slug="dirty-cat",
            description="Dirty category",
        )
        extra_kb = KBItem.objects.create(
            category=extra_cat,
            title="Dirty KB Item",
            question="What is dirty?",
            answer="Dirty data!",
        )

        UserSettings.objects.get_or_create(user=extra_user)

        return {
            "user": extra_user,
            "queue": extra_queue,
            "tickets": extra_tickets,
            "kb_category": extra_cat,
            "kb_item": extra_kb,
        }

    def test_reset_with_dirty_data_restores_seed_state(self):
        """
        Scenario 1: Create dirty data (multiple tickets, followups, attachments),
        run reset command, and verify everything matches the expected seed state.
        """
        self._create_dirty_data()

        self.assertGreaterEqual(
            Ticket.objects.count(), 5, "Precondition: dirty data should be present"
        )

        call_command(
            "reset_demo_data",
            "--noinput",
            *self.fixture_arg,
            verbosity=0,
        )

        snapshot = self._get_demo_state_snapshot()
        self._assert_matches_demo_state(
            snapshot, msg_prefix="After reset from dirty state: "
        )

        self.assertFalse(
            User.objects.filter(username="dirty_user").exists(),
            "Dirty user should be removed",
        )
        self.assertFalse(
            Queue.objects.filter(slug__startswith="DQ").exists(),
            "Dirty queue should be removed",
        )
        self.assertFalse(
            Ticket.objects.filter(title__startswith="Dirty Ticket").exists(),
            "Dirty tickets should be removed",
        )
        self.assertFalse(
            KBCategory.objects.filter(title="Dirty KB Category").exists(),
            "Dirty KB category should be removed",
        )
        self.assertFalse(
            KBItem.objects.filter(title="Dirty KB Item").exists(),
            "Dirty KB item should be removed",
        )

    def test_reset_is_idempotent(self):
        """
        Scenario 2: Run reset twice consecutively without any changes in between.
        The state after first reset should be identical to state after second reset,
        proving the command's idempotency.
        """
        call_command(
            "reset_demo_data",
            "--noinput",
            *self.fixture_arg,
            verbosity=0,
        )
        snapshot1 = self._get_demo_state_snapshot()
        self._assert_matches_demo_state(snapshot1, msg_prefix="After first reset: ")

        call_command(
            "reset_demo_data",
            "--noinput",
            *self.fixture_arg,
            verbosity=0,
        )
        snapshot2 = self._get_demo_state_snapshot()
        self._assert_matches_demo_state(snapshot2, msg_prefix="After second reset: ")

        self.assertEqual(
            snapshot1,
            snapshot2,
            "State after first reset should be identical to state after second reset",
        )

        call_command(
            "reset_demo_data",
            "--noinput",
            *self.fixture_arg,
            verbosity=0,
        )
        snapshot3 = self._get_demo_state_snapshot()
        self.assertEqual(
            snapshot2,
            snapshot3,
            "State after second reset should be identical to state after third reset",
        )

    def test_reset_restores_admin_and_login_works(self):
        """
        Scenario 3: Delete the admin user and all users, run reset, then verify:
        - Admin user is restored from seed state
        - Admin can authenticate with expected password (Pa33w0rd)
        - Demo login actually works via client
        """
        UserSettings.objects.all().delete()
        User.objects.all().delete()
        self.assertEqual(
            User.objects.count(), 0, "Precondition: all users should be deleted"
        )

        call_command(
            "reset_demo_data",
            "--noinput",
            *self.fixture_arg,
            verbosity=0,
        )

        admin = User.objects.filter(username="admin").first()
        self.assertIsNotNone(admin, "Admin user should be restored")
        self.assertTrue(admin.is_staff, "Admin should be staff")
        self.assertTrue(admin.is_superuser, "Admin should be superuser")
        self.assertEqual(
            admin.email, "helpdesk@example.com", "Admin email should match seed"
        )

        authenticated_user = authenticate(
            username="admin",
            password="Pa33w0rd",
        )
        self.assertIsNotNone(
            authenticated_user,
            "Admin should authenticate successfully with password 'Pa33w0rd'",
        )
        self.assertEqual(
            authenticated_user.pk,
            admin.pk,
            "Authenticated user should be the admin user",
        )

        wrong_user = authenticate(
            username="admin",
            password="wrongpassword",
        )
        self.assertIsNone(wrong_user, "Wrong password should not authenticate")

        login_success = self.client.login(
            username="admin",
            password="Pa33w0rd",
        )
        self.assertTrue(
            login_success, "Client login with admin credentials should succeed"
        )

        response = self.client.get(reverse("helpdesk:home"), follow=True)
        self.assertEqual(
            response.status_code,
            200,
            "Admin should be able to access dashboard after login",
        )

    def test_reset_with_keep_users_flag(self):
        """
        Additional: Verify --keep-users flag preserves non-admin users.
        """
        User.objects.all().delete()
        call_command(
            "reset_demo_data",
            "--noinput",
            *self.fixture_arg,
            verbosity=0,
        )

        keeper = User.objects.create_user(
            username="keeper",
            password="password123",
            email="keeper@example.com",
            is_staff=True,
        )
        UserSettings.objects.get_or_create(user=keeper)

        call_command(
            "reset_demo_data",
            "--noinput",
            "--keep-users",
            *self.fixture_arg,
            verbosity=0,
        )

        self.assertTrue(
            User.objects.filter(username="keeper").exists(),
            "Keeper user should be preserved with --keep-users",
        )
        self.assertTrue(
            User.objects.filter(username="admin").exists(),
            "Admin user should still exist from fixture",
        )

    def test_reset_requires_valid_fixture(self):
        """
        Additional: Verify command fails gracefully with invalid fixture path.
        """
        with self.assertRaises((FileNotFoundError, CommandError)):
            call_command(
                "reset_demo_data",
                "--noinput",
                "--fixture",
                "/nonexistent/path/to/fixture.json",
                verbosity=0,
            )

    def test_reset_clears_all_related_objects(self):
        """
        Additional: Verify all helpdesk-related objects (TicketCC, TicketDependency,
        TicketChange, etc.) are properly cleared during reset.
        """
        self._create_dirty_data()

        self.assertGreater(
            TicketCC.objects.count(), 0, "Precondition: TicketCC should exist"
        )
        self.assertGreater(
            TicketDependency.objects.count(),
            0,
            "Precondition: TicketDependency should exist",
        )
        self.assertGreater(
            TicketChange.objects.count(), 0, "Precondition: TicketChange should exist"
        )

        call_command(
            "reset_demo_data",
            "--noinput",
            *self.fixture_arg,
            verbosity=0,
        )

        self.assertEqual(
            TicketCC.objects.count(),
            0,
            "All TicketCC objects should be cleared (demo fixture has none)",
        )
        self.assertEqual(
            TicketDependency.objects.count(),
            0,
            "All TicketDependency objects should be cleared (demo fixture has none)",
        )
        self.assertEqual(
            TicketChange.objects.count(),
            0,
            "All TicketChange objects should be cleared (demo fixture has none)",
        )
        self.assertEqual(
            TicketCustomFieldValue.objects.count(),
            0,
            "All TicketCustomFieldValue objects should be cleared",
        )
