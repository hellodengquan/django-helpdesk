from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, TransactionTestCase, override_settings
from django.test.client import Client
from django.urls import reverse
from unittest.mock import MagicMock, patch
from io import StringIO
from helpdesk import settings as helpdesk_settings
from helpdesk.forms import EditTicketForm
from helpdesk.models import KBCategory, KBItem, Queue, Ticket
from helpdesk.user import HelpdeskUser, huser_from_request
from helpdesk.views.staff import get_user_queues


User = get_user_model()


class HelpdeskUserCoreMethodTestCase(TestCase):
    """
    Unit tests for HelpdeskUser core permission methods.
    Ensures is_staff, is_superuser, is_team_member branches
    behave identically before and after refactoring.
    """

    def setUp(self):
        self.user_anon = MagicMock()
        self.user_anon.is_authenticated = False
        self.user_anon.is_active = False
        self.user_anon.is_staff = False
        self.user_anon.is_superuser = False

        self.user_inactive = User.objects.create_user(
            username="inactive", password="pass", is_active=False
        )

        self.user_non_staff = User.objects.create_user(
            username="non_staff", password="pass", is_staff=False, is_superuser=False
        )

        self.user_staff = User.objects.create_user(
            username="staff", password="pass", is_staff=True, is_superuser=False
        )

        self.user_superuser = User.objects.create_user(
            username="superuser", password="pass", is_staff=True, is_superuser=True
        )

    def test_is_authenticated(self):
        """is_authenticated() correctly reflects user authentication state."""
        huser = HelpdeskUser(self.user_anon)
        self.assertFalse(huser.is_authenticated())

        huser = HelpdeskUser(self.user_staff)
        self.assertTrue(huser.is_authenticated())

    def test_is_active(self):
        """is_active() correctly reflects user active state."""
        huser = HelpdeskUser(self.user_inactive)
        self.assertFalse(huser.is_active())

        huser = HelpdeskUser(self.user_staff)
        self.assertTrue(huser.is_active())

    def test_is_staff_default_config(self):
        """is_staff() with default config requires is_staff=True."""
        original = helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE
        try:
            helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE = False
            huser = HelpdeskUser(self.user_non_staff)
            self.assertFalse(huser.is_staff())

            huser = HelpdeskUser(self.user_staff)
            self.assertTrue(huser.is_staff())

            huser = HelpdeskUser(self.user_superuser)
            self.assertTrue(huser.is_staff())
        finally:
            helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE = original

    def test_is_staff_allow_non_staff(self):
        """is_staff() allows all authenticated active users when config is True."""
        original = helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE
        try:
            helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE = True
            huser = HelpdeskUser(self.user_non_staff)
            self.assertTrue(huser.is_staff())

            huser = HelpdeskUser(self.user_inactive)
            self.assertFalse(huser.is_staff())

            huser = HelpdeskUser(self.user_anon)
            self.assertFalse(huser.is_staff())
        finally:
            helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE = original

    def test_is_staff_callable_config(self):
        """is_staff() respects callable HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE."""
        def custom_check(user):
            return user.username == "special"

        special_user = User.objects.create_user(
            username="special", password="pass", is_staff=False
        )

        original = helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE
        try:
            helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE = custom_check
            huser = HelpdeskUser(self.user_non_staff)
            self.assertFalse(huser.is_staff())

            huser = HelpdeskUser(special_user)
            self.assertTrue(huser.is_staff())
        finally:
            helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE = original

    def test_is_superuser(self):
        """is_superuser() requires authenticated, active, and is_superuser=True."""
        huser = HelpdeskUser(self.user_anon)
        self.assertFalse(huser.is_superuser())

        huser = HelpdeskUser(self.user_staff)
        self.assertFalse(huser.is_superuser())

        huser = HelpdeskUser(self.user_superuser)
        self.assertTrue(huser.is_superuser())

    def test_is_superuser_inactive(self):
        """Inactive superuser should not pass is_superuser() check."""
        inactive_superuser = User.objects.create_user(
            username="inactive_super", password="pass",
            is_active=False, is_superuser=True
        )
        huser = HelpdeskUser(inactive_superuser)
        self.assertFalse(huser.is_superuser())

    def test_is_team_member(self):
        """is_team_member() correctly identifies team members via KBItem."""
        category = KBCategory.objects.create(title="Cat1", slug="cat1")
        kbitem = KBItem.objects.create(
            title="KB1", category=category,
            answer="test", question="test"
        )

        mock_team = MagicMock()
        mock_team.is_member.side_effect = lambda u: u.username == "team_user"

        team_user = User.objects.create_user(
            username="team_user", password="pass"
        )

        with patch.object(KBItem, 'get_team', return_value=mock_team):
            huser = HelpdeskUser(team_user)
            self.assertTrue(huser.is_team_member(kbitem))

            huser = HelpdeskUser(self.user_staff)
            self.assertFalse(huser.is_team_member(kbitem))

    def test_is_team_member_no_team(self):
        """is_team_member() returns False when KBItem has no team."""
        category = KBCategory.objects.create(title="Cat1", slug="cat1")
        kbitem = KBItem.objects.create(
            title="KB1", category=category,
            answer="test", question="test"
        )

        with patch.object(KBItem, 'get_team', return_value=None):
            huser = HelpdeskUser(self.user_staff)
            self.assertFalse(huser.is_team_member(kbitem))


class HelpdeskUserQueueAccessTestCase(TestCase):
    """
    Tests for queue access methods: get_queues, get_queue_choices,
    has_full_access, can_access_queue, can_access_ticket.
    Verifies staff/superuser/per-queue permission branches.
    """

    def setUp(self):
        self.old_per_queue_setting = helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = True

        self.queue_1 = Queue.objects.create(title="Queue 1", slug="q1")
        self.queue_2 = Queue.objects.create(title="Queue 2", slug="q2")
        self.queue_public = Queue.objects.create(
            title="Queue Public", slug="qpub", allow_public_submission=True
        )

        self.user_staff_q1 = User.objects.create_user(
            username="staff_q1", password="pass", is_staff=True
        )
        p1 = Permission.objects.get(codename=self.queue_1.permission_name[9:])
        self.user_staff_q1.user_permissions.add(p1)

        self.user_staff_q2 = User.objects.create_user(
            username="staff_q2", password="pass", is_staff=True
        )
        p2 = Permission.objects.get(codename=self.queue_2.permission_name[9:])
        self.user_staff_q2.user_permissions.add(p2)

        self.user_superuser = User.objects.create_user(
            username="superuser", password="pass", is_staff=True, is_superuser=True
        )

        self.ticket_q1 = Ticket.objects.create(title="T in Q1", queue=self.queue_1)
        self.ticket_q2 = Ticket.objects.create(title="T in Q2", queue=self.queue_2)
        self.ticket_q1_assigned = Ticket.objects.create(
            title="T in Q1 assigned to Q2 user",
            queue=self.queue_1, assigned_to=self.user_staff_q2
        )

    def tearDown(self):
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = self.old_per_queue_setting

    def test_get_queues_superuser(self):
        """Superuser should see ALL queues including private ones."""
        huser = HelpdeskUser(self.user_superuser)
        queues = huser.get_queues()
        self.assertEqual(queues.count(), 3)
        self.assertIn(self.queue_1, queues)
        self.assertIn(self.queue_2, queues)
        self.assertIn(self.queue_public, queues)

    def test_get_queues_staff_with_permission(self):
        """Staff with queue permission sees their queue + public queues."""
        huser = HelpdeskUser(self.user_staff_q1)
        queues = huser.get_queues()
        queue_ids = list(queues.values_list('id', flat=True))
        self.assertIn(self.queue_1.id, queue_ids)
        self.assertIn(self.queue_public.id, queue_ids)
        self.assertNotIn(self.queue_2.id, queue_ids)

    def test_get_queues_staff_without_permission(self):
        """Staff without any queue permission still sees public queues."""
        user_no_perm = User.objects.create_user(
            username="staff_no_perm", password="pass", is_staff=True
        )
        huser = HelpdeskUser(user_no_perm)
        queues = huser.get_queues()
        self.assertEqual(queues.count(), 1)
        self.assertEqual(queues.first(), self.queue_public)

    def test_get_queues_per_queue_disabled(self):
        """When per-queue permission is disabled, staff sees all queues."""
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = False
        huser = HelpdeskUser(self.user_staff_q1)
        queues = huser.get_queues()
        self.assertEqual(queues.count(), 3)

    def test_get_queue_choices_multiple_queues(self):
        """get_queue_choices with include_empty=True adds empty option for >1 queues."""
        huser = HelpdeskUser(self.user_superuser)
        choices = huser.get_queue_choices(include_empty=True)
        self.assertEqual(len(choices), 4)
        self.assertEqual(choices[0], ("", "--------"))

    def test_get_queue_choices_no_empty(self):
        """get_queue_choices with include_empty=False excludes empty option."""
        huser = HelpdeskUser(self.user_superuser)
        choices = huser.get_queue_choices(include_empty=False)
        self.assertEqual(len(choices), 3)
        for choice in choices:
            self.assertNotEqual(choice[0], "")

    def test_get_queue_choices_single_queue(self):
        """Staff with 1 private queue permission also sees public queue (total 2)."""
        huser = HelpdeskUser(self.user_staff_q1)
        choices = huser.get_queue_choices(include_empty=True)
        # user_staff_q1 has access to queue_1 (permission) + queue_public (public)
        self.assertEqual(len(choices), 3)
        self.assertEqual(choices[0], ("", "--------"))
        queue_titles = [c[1] for c in choices if c[0]]
        self.assertIn("Queue 1", queue_titles)
        self.assertIn("Queue Public", queue_titles)
        self.assertNotIn("Queue 2", queue_titles)

    def test_has_full_access_superuser(self):
        """Superuser always has full access regardless of per-queue setting."""
        huser = HelpdeskUser(self.user_superuser)
        self.assertTrue(huser.has_full_access())

    def test_has_full_access_staff_per_queue_disabled(self):
        """Staff has full access when per-queue permission is disabled."""
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = False
        huser = HelpdeskUser(self.user_staff_q1)
        self.assertTrue(huser.has_full_access())

    def test_has_full_access_staff_per_queue_enabled(self):
        """Staff does NOT have full access when per-queue permission is enabled."""
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = True
        huser = HelpdeskUser(self.user_staff_q1)
        self.assertFalse(huser.has_full_access())

    def test_can_access_queue_superuser(self):
        """Superuser can access any queue."""
        huser = HelpdeskUser(self.user_superuser)
        self.assertTrue(huser.can_access_queue(self.queue_1))
        self.assertTrue(huser.can_access_queue(self.queue_2))

    def test_can_access_queue_staff_with_permission(self):
        """Staff can only access queues they have explicit permission for."""
        huser = HelpdeskUser(self.user_staff_q1)
        self.assertTrue(huser.can_access_queue(self.queue_1))
        self.assertFalse(huser.can_access_queue(self.queue_2))

    def test_can_access_queue_public(self):
        """Public queues are accessible to staff with per-queue enabled."""
        huser = HelpdeskUser(self.user_staff_q1)
        self.assertTrue(huser.can_access_queue(self.queue_public))

    def test_can_access_ticket_superuser(self):
        """Superuser can access any ticket regardless of queue."""
        huser = HelpdeskUser(self.user_superuser)
        self.assertTrue(huser.can_access_ticket(self.ticket_q1))
        self.assertTrue(huser.can_access_ticket(self.ticket_q2))

    def test_can_access_ticket_queue_permission(self):
        """Staff can access tickets in queues they have permission for."""
        huser = HelpdeskUser(self.user_staff_q1)
        self.assertTrue(huser.can_access_ticket(self.ticket_q1))
        self.assertFalse(huser.can_access_ticket(self.ticket_q2))

    def test_can_access_ticket_assigned_to(self):
        """Staff can access tickets assigned to them even in other queues."""
        huser = HelpdeskUser(self.user_staff_q2)
        self.assertTrue(huser.can_access_ticket(self.ticket_q1_assigned))


class TicketListPermissionTestCase(TestCase):
    """
    Integration tests for ticket list scene.
    Verifies staff/superuser/team member see correct queue subsets in list views.
    """

    def setUp(self):
        self.old_per_queue_setting = helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = True

        self.queue_1 = Queue.objects.create(title="Queue 1", slug="q1")
        self.queue_2 = Queue.objects.create(title="Queue 2", slug="q2")
        self.queue_3 = Queue.objects.create(title="Queue 3", slug="q3")

        self.user_staff_q1 = User.objects.create_user(
            username="staff_q1", password="pass", is_staff=True
        )
        p1 = Permission.objects.get(codename=self.queue_1.permission_name[9:])
        self.user_staff_q1.user_permissions.add(p1)

        self.user_staff_q1q2 = User.objects.create_user(
            username="staff_q1q2", password="pass", is_staff=True
        )
        p1 = Permission.objects.get(codename=self.queue_1.permission_name[9:])
        p2 = Permission.objects.get(codename=self.queue_2.permission_name[9:])
        self.user_staff_q1q2.user_permissions.add(p1)
        self.user_staff_q1q2.user_permissions.add(p2)

        self.user_superuser = User.objects.create_user(
            username="superuser", password="pass", is_staff=True, is_superuser=True
        )

        Ticket.objects.create(title="T1 Q1", queue=self.queue_1)
        Ticket.objects.create(title="T2 Q1", queue=self.queue_1)
        Ticket.objects.create(title="T1 Q2", queue=self.queue_2)
        Ticket.objects.create(title="T2 Q2", queue=self.queue_2)
        Ticket.objects.create(title="T3 Q2", queue=self.queue_2)
        Ticket.objects.create(title="T1 Q3", queue=self.queue_3)

        self.client = Client()

    def tearDown(self):
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = self.old_per_queue_setting

    def test_dashboard_staff_single_queue(self):
        """Staff with single queue permission sees only that queue on dashboard."""
        self.client.login(username="staff_q1", password="pass")
        response = self.client.get(reverse("helpdesk:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["unassigned_tickets"]), 2)
        for ticket in response.context["unassigned_tickets"]:
            self.assertEqual(ticket.queue, self.queue_1)

    def test_dashboard_staff_multiple_queues(self):
        """Staff with multiple queue permissions sees all accessible queues."""
        self.client.login(username="staff_q1q2", password="pass")
        response = self.client.get(reverse("helpdesk:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["unassigned_tickets"]), 5)
        queues_seen = set(t.queue.id for t in response.context["unassigned_tickets"])
        self.assertIn(self.queue_1.id, queues_seen)
        self.assertIn(self.queue_2.id, queues_seen)
        self.assertNotIn(self.queue_3.id, queues_seen)

    def test_dashboard_superuser(self):
        """Superuser sees all queues on dashboard."""
        self.client.login(username="superuser", password="pass")
        response = self.client.get(reverse("helpdesk:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["unassigned_tickets"]), 6)
        queues_seen = set(t.queue.id for t in response.context["unassigned_tickets"])
        self.assertIn(self.queue_1.id, queues_seen)
        self.assertIn(self.queue_2.id, queues_seen)
        self.assertIn(self.queue_3.id, queues_seen)

    def test_get_user_queues_staff(self):
        """get_user_queues helper correctly filters by permission."""
        choices = get_user_queues(self.user_staff_q1)
        choice_ids = [c[0] for c in choices if c[0]]
        self.assertEqual(len(choice_ids), 1)
        self.assertIn(self.queue_1.id, choice_ids)

    def test_get_user_queues_superuser(self):
        """get_user_queues returns all queues for superuser."""
        choices = get_user_queues(self.user_superuser)
        choice_ids = [c[0] for c in choices if c[0]]
        self.assertEqual(len(choice_ids), 3)

    def test_huser_from_request(self):
        """huser_from_request helper correctly creates HelpdeskUser from request."""
        self.client.login(username="staff_q1", password="pass")
        request = MagicMock()
        request.user = self.user_staff_q1
        huser = huser_from_request(request)
        self.assertIsInstance(huser, HelpdeskUser)
        self.assertEqual(huser.user, self.user_staff_q1)
        queues = huser.get_queues()
        self.assertIn(self.queue_1, queues)
        self.assertNotIn(self.queue_2, queues)


class TicketFormPermissionTestCase(TestCase):
    """
    Integration tests for ticket form scene.
    Verifies staff/superuser see correct queue options in create/edit forms.
    """

    def setUp(self):
        self.old_per_queue_setting = helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = True

        self.queue_1 = Queue.objects.create(title="Queue 1", slug="q1")
        self.queue_2 = Queue.objects.create(title="Queue 2", slug="q2")
        self.queue_3 = Queue.objects.create(title="Queue 3", slug="q3")

        self.user_staff_q1 = User.objects.create_user(
            username="staff_q1", password="pass", is_staff=True
        )
        p1 = Permission.objects.get(codename=self.queue_1.permission_name[9:])
        self.user_staff_q1.user_permissions.add(p1)

        self.user_superuser = User.objects.create_user(
            username="superuser", password="pass", is_staff=True, is_superuser=True
        )

        self.client = Client()

    def tearDown(self):
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = self.old_per_queue_setting

    def test_create_ticket_form_staff_queue_options(self):
        """Create ticket form shows only queues staff has permission for."""
        from helpdesk.forms import TicketForm
        queue_choices = HelpdeskUser(self.user_staff_q1).get_queue_choices()
        form = TicketForm(queue_choices=queue_choices)
        queue_ids = [c[0] for c in form.fields["queue"].choices if c[0]]
        self.assertEqual(len(queue_ids), 1)
        self.assertIn(self.queue_1.id, queue_ids)
        self.assertNotIn(self.queue_2.id, queue_ids)
        self.assertNotIn(self.queue_3.id, queue_ids)

    def test_create_ticket_form_superuser_queue_options(self):
        """Create ticket form shows all queues for superuser."""
        from helpdesk.forms import TicketForm
        queue_choices = HelpdeskUser(self.user_superuser).get_queue_choices()
        form = TicketForm(queue_choices=queue_choices)
        queue_ids = [c[0] for c in form.fields["queue"].choices if c[0]]
        self.assertEqual(len(queue_ids), 3)
        self.assertIn(self.queue_1.id, queue_ids)
        self.assertIn(self.queue_2.id, queue_ids)
        self.assertIn(self.queue_3.id, queue_ids)

    def test_edit_ticket_form_staff_queue_options(self):
        """Edit ticket form shows only queues staff has permission for."""
        ticket = Ticket.objects.create(title="Test Ticket", queue=self.queue_1)
        self.client.login(username="staff_q1", password="pass")
        response = self.client.get(
            reverse("helpdesk:edit", kwargs={"ticket_id": ticket.id})
        )
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        queue_choices = form.fields["queue"].choices
        queue_ids = [c[0] for c in queue_choices if c[0]]
        self.assertEqual(len(queue_ids), 1)
        self.assertIn(self.queue_1.id, queue_ids)

    def test_edit_ticket_form_superuser_queue_options(self):
        """Edit ticket form shows all queues for superuser."""
        ticket = Ticket.objects.create(title="Test Ticket", queue=self.queue_1)
        self.client.login(username="superuser", password="pass")
        response = self.client.get(
            reverse("helpdesk:edit", kwargs={"ticket_id": ticket.id})
        )
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        queue_choices = form.fields["queue"].choices
        queue_ids = [c[0] for c in queue_choices if c[0]]
        self.assertEqual(len(queue_ids), 3)

    def test_edit_ticket_form_inherits_queue_choices(self):
        """EditTicketForm properly accepts and applies queue_choices parameter."""
        ticket = Ticket.objects.create(title="Test Ticket", queue=self.queue_1)

        custom_choices = [("", "---"), (self.queue_1.id, "Custom Q1")]
        form = EditTicketForm(
            instance=ticket,
            queue_choices=custom_choices
        )
        self.assertEqual(form.fields["queue"].choices, custom_choices)

    def test_edit_ticket_form_without_queue_field(self):
        """EditTicketForm handles missing queue field gracefully."""
        from helpdesk.forms import EditTicketCustomFieldForm
        ticket = Ticket.objects.create(title="Test Ticket", queue=self.queue_1)

        custom_choices = [("", "---"), (self.queue_1.id, "Custom Q1")]
        form = EditTicketCustomFieldForm(
            instance=ticket,
            queue_choices=custom_choices
        )
        self.assertNotIn("queue", form.fields)

    def test_update_ticket_view_staff_queue_options(self):
        """Update ticket view (UpdateTicketView) filters queues by permission."""
        ticket = Ticket.objects.create(title="Test Ticket", queue=self.queue_1)
        self.client.login(username="staff_q1", password="pass")
        response = self.client.get(
            reverse("helpdesk:update", kwargs={"ticket_id": ticket.id})
        )
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        queue_choices = form.fields["queue"].choices
        queue_ids = [c[0] for c in queue_choices if c[0]]
        self.assertEqual(len(queue_ids), 1)
        self.assertIn(self.queue_1.id, queue_ids)

    def test_get_queue_choices_integration(self):
        """get_queue_choices integrates correctly with form views."""
        huser = HelpdeskUser(self.user_staff_q1)
        choices = huser.get_queue_choices()
        # Only 1 queue accessible, so no empty option
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0][0], self.queue_1.id)

        huser_super = HelpdeskUser(self.user_superuser)
        choices_super = huser_super.get_queue_choices()
        self.assertEqual(len(choices_super), 4)
        self.assertEqual(choices_super[0][0], "")
        queue_ids_super = [c[0] for c in choices_super if c[0]]
        self.assertEqual(len(queue_ids_super), 3)

    def test_update_ticket_form_does_not_leak_queues(self):
        """Staff cannot change ticket to a queue they don't have permission for."""
        ticket = Ticket.objects.create(title="Test Ticket", queue=self.queue_1)
        self.client.login(username="staff_q1", password="pass")

        post_data = {
            "title": "Updated Title",
            "queue": str(self.queue_2.id),
            "priority": "3",
        }
        response = self.client.post(
            reverse("helpdesk:edit", kwargs={"ticket_id": ticket.id}),
            post_data
        )

        ticket.refresh_from_db()
        self.assertEqual(ticket.queue.id, self.queue_1.id)
        self.assertNotEqual(ticket.queue.id, self.queue_2.id)


class ManagementCommandPermissionTestCase(TransactionTestCase):
    """
    Integration tests for management command scene.
    Verifies that --user parameter correctly filters queues by permission,
    and that default behavior (no --user) remains unchanged.
    """

    def setUp(self):
        self.old_per_queue_setting = helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = True

        self.queue_1 = Queue.objects.create(
            title="Queue 1", slug="q1", escalate_days=5
        )
        self.queue_2 = Queue.objects.create(
            title="Queue 2", slug="q2", escalate_days=3
        )
        self.queue_3 = Queue.objects.create(
            title="Queue 3", slug="q3", escalate_days=7
        )

        # Create permissions first
        call_command("create_queue_permissions", verbosity=0)

        self.user_staff_q1 = User.objects.create_user(
            username="staff_q1", password="pass", is_staff=True
        )
        p1 = Permission.objects.get(codename=self.queue_1.permission_name[9:])
        self.user_staff_q1.user_permissions.add(p1)

        self.user_staff_q1q2 = User.objects.create_user(
            username="staff_q1q2", password="pass", is_staff=True
        )
        p1 = Permission.objects.get(codename=self.queue_1.permission_name[9:])
        p2 = Permission.objects.get(codename=self.queue_2.permission_name[9:])
        self.user_staff_q1q2.user_permissions.add(p1)
        self.user_staff_q1q2.user_permissions.add(p2)

        self.user_superuser = User.objects.create_user(
            username="superuser", password="pass", is_staff=True, is_superuser=True
        )

    def tearDown(self):
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = self.old_per_queue_setting

    def test_escalate_tickets_with_user_filter(self):
        """escalate_tickets --user only processes queues the user can access."""
        out = StringIO()
        call_command(
            "escalate_tickets",
            user="staff_q1",
            escalate_verbosely=True,
            stdout=out
        )
        output = out.getvalue()
        self.assertIn("Queue 1", output)
        self.assertNotIn("Queue 2", output)
        self.assertNotIn("Queue 3", output)

    def test_escalate_tickets_with_user_multiple_queues(self):
        """escalate_tickets --user processes all queues user can access."""
        out = StringIO()
        call_command(
            "escalate_tickets",
            user="staff_q1q2",
            escalate_verbosely=True,
            stdout=out
        )
        output = out.getvalue()
        self.assertIn("Queue 1", output)
        self.assertIn("Queue 2", output)
        self.assertNotIn("Queue 3", output)

    def test_escalate_tickets_default_behavior(self):
        """escalate_tickets without --user processes all queues (backward compatible)."""
        out = StringIO()
        call_command(
            "escalate_tickets",
            escalate_verbosely=True,
            stdout=out
        )
        output = out.getvalue()
        self.assertIn("Queue 1", output)
        self.assertIn("Queue 2", output)
        self.assertIn("Queue 3", output)

    def test_escalate_tickets_with_queue_filter_and_user(self):
        """escalate_tickets with both --user and --queues applies both filters."""
        out = StringIO()
        call_command(
            "escalate_tickets",
            user="staff_q1q2",
            queues=["q2"],
            escalate_verbosely=True,
            stdout=out
        )
        output = out.getvalue()
        self.assertNotIn("Queue 1", output)
        self.assertIn("Queue 2", output)
        self.assertNotIn("Queue 3", output)

    def test_escalate_tickets_nonexistent_user(self):
        """escalate_tickets raises CommandError for nonexistent --user."""
        with self.assertRaises(CommandError) as context:
            call_command("escalate_tickets", user="nonexistent")
        self.assertIn("does not exist", str(context.exception))

    def test_create_queue_permissions_with_user_filter(self):
        """create_queue_permissions --user only creates perms for accessible queues."""
        out = StringIO()
        call_command(
            "create_queue_permissions",
            user="staff_q1",
            escalate_verbosely=True,
            stdout=out
        )
        output = out.getvalue()
        self.assertIn("Queue 1", output)
        self.assertNotIn("Queue 2", output)
        self.assertNotIn("Queue 3", output)

    def test_create_queue_permissions_default_behavior(self):
        """create_queue_permissions without --user processes all queues."""
        out = StringIO()
        call_command(
            "create_queue_permissions",
            escalate_verbosely=True,
            stdout=out
        )
        output = out.getvalue()
        self.assertIn("Queue 1", output)
        self.assertIn("Queue 2", output)
        self.assertIn("Queue 3", output)

    def test_create_queue_permissions_nonexistent_user(self):
        """create_queue_permissions raises CommandError for nonexistent --user."""
        with self.assertRaises(CommandError) as context:
            call_command("create_queue_permissions", user="nonexistent")
        self.assertIn("does not exist", str(context.exception))

    def test_create_escalation_exclusions_with_user_filter(self):
        """create_escalation_exclusions --user only excludes accessible queues."""
        out = StringIO()
        call_command(
            "create_escalation_exclusions",
            user="staff_q1",
            days=["monday"],
            occurrences=1,
            queues=["q1"],
            exclude_verbosely=True,
            stdout=out
        )
        output = out.getvalue()
        self.assertIn("Queue 1", output)
        self.assertNotIn("Queue 2", output)

    def test_create_escalation_exclusions_nonexistent_user(self):
        """create_escalation_exclusions raises CommandError for nonexistent --user."""
        with self.assertRaises(CommandError) as context:
            call_command(
                "create_escalation_exclusions",
                user="nonexistent",
                days=["monday"],
                occurrences=1
            )
        self.assertIn("does not exist", str(context.exception))

    def test_escalate_tickets_user_has_no_queue_permission(self):
        """escalate_tickets --user processes zero queues when user has no permissions."""
        user_no_perm = User.objects.create_user(
            username="no_perm", password="pass", is_staff=True
        )
        out = StringIO()
        call_command(
            "escalate_tickets",
            user="no_perm",
            escalate_verbosely=True,
            stdout=out
        )
        output = out.getvalue()
        self.assertNotIn("Queue 1", output)
        self.assertNotIn("Queue 2", output)
        self.assertNotIn("Queue 3", output)

    def test_command_help_shows_user_option(self):
        """Management commands include --user option in help text."""
        from django.core.management import get_commands, load_command_class

        commands_to_check = [
            "escalate_tickets",
            "create_queue_permissions",
            "create_escalation_exclusions",
        ]

        for cmd_name in commands_to_check:
            app = get_commands()[cmd_name]
            cmd = load_command_class(app, cmd_name)

            import argparse
            parser = argparse.ArgumentParser()
            cmd.add_arguments(parser)

            # Check if --user/-u is in the parser actions
            has_user_option = any(
                "--user" in action.option_strings or "-u" in action.option_strings
                for action in parser._actions
            )
            self.assertTrue(
                has_user_option,
                f"Command '{cmd_name}' should have --user option"
            )


class AnonymousUserPermissionTestCase(TestCase):
    """
    Boundary tests for anonymous (unauthenticated) user access.
    Ensures anonymous users get empty or public-only queue sets
    across all permission service entry points.
    """

    def setUp(self):
        self.old_per_queue_setting = helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = True

        self.queue_private = Queue.objects.create(title="Private Q", slug="priv")
        self.queue_public = Queue.objects.create(
            title="Public Q", slug="pub", allow_public_submission=True
        )

        from django.contrib.auth.models import AnonymousUser
        self.user_anon = AnonymousUser()

    def tearDown(self):
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = self.old_per_queue_setting

    def test_anonymous_is_authenticated(self):
        """Anonymous user is not authenticated."""
        huser = HelpdeskUser(self.user_anon)
        self.assertFalse(huser.is_authenticated())

    def test_anonymous_is_active(self):
        """Anonymous user is not active."""
        huser = HelpdeskUser(self.user_anon)
        self.assertFalse(huser.is_active())

    def test_anonymous_is_staff_default_config(self):
        """Anonymous user is not staff under default config."""
        original = helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE
        try:
            helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE = False
            huser = HelpdeskUser(self.user_anon)
            self.assertFalse(huser.is_staff())
        finally:
            helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE = original

    def test_anonymous_is_staff_allow_non_staff(self):
        """Anonymous user is not staff even when HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE=True."""
        original = helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE
        try:
            helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE = True
            huser = HelpdeskUser(self.user_anon)
            self.assertFalse(huser.is_staff())
        finally:
            helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE = original

    def test_anonymous_is_superuser(self):
        """Anonymous user is never superuser."""
        huser = HelpdeskUser(self.user_anon)
        self.assertFalse(huser.is_superuser())

    def test_anonymous_has_full_access_per_queue_enabled(self):
        """Anonymous user has no full access when per-queue permission is enabled."""
        huser = HelpdeskUser(self.user_anon)
        self.assertFalse(huser.has_full_access())

    def test_anonymous_get_queues_per_queue_enabled(self):
        """Anonymous user sees only public queues when per-queue permission is enabled."""
        huser = HelpdeskUser(self.user_anon)
        queues = huser.get_queues()
        self.assertEqual(queues.count(), 1)
        self.assertIn(self.queue_public, queues)
        self.assertNotIn(self.queue_private, queues)

    def test_anonymous_get_queues_per_queue_disabled(self):
        """Anonymous user sees all queues when per-queue permission is disabled (no filter applied)."""
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = False
        huser = HelpdeskUser(self.user_anon)
        queues = huser.get_queues()
        self.assertEqual(queues.count(), 2)

    def test_anonymous_get_queue_choices(self):
        """Anonymous user gets only public queue choices."""
        huser = HelpdeskUser(self.user_anon)
        choices = huser.get_queue_choices()
        queue_ids = [c[0] for c in choices if c[0]]
        self.assertEqual(len(queue_ids), 1)
        self.assertIn(self.queue_public.id, queue_ids)
        self.assertNotIn(self.queue_private.id, queue_ids)

    def test_anonymous_can_access_queue_private(self):
        """Anonymous user cannot access private queues."""
        huser = HelpdeskUser(self.user_anon)
        self.assertFalse(huser.can_access_queue(self.queue_private))

    def test_anonymous_can_access_queue_public(self):
        """Anonymous user can access public queues."""
        huser = HelpdeskUser(self.user_anon)
        self.assertTrue(huser.can_access_queue(self.queue_public))

    def test_anonymous_can_access_ticket_in_private_queue(self):
        """Anonymous user cannot access tickets in private queues."""
        ticket = Ticket.objects.create(title="Private Ticket", queue=self.queue_private)
        huser = HelpdeskUser(self.user_anon)
        self.assertFalse(huser.can_access_ticket(ticket))

    def test_anonymous_can_access_ticket_in_public_queue(self):
        """Anonymous user can access tickets in public queues."""
        ticket = Ticket.objects.create(title="Public Ticket", queue=self.queue_public)
        huser = HelpdeskUser(self.user_anon)
        self.assertTrue(huser.can_access_ticket(ticket))

    def test_anonymous_get_tickets_in_queues(self):
        """Anonymous user only sees tickets in public queues."""
        Ticket.objects.create(title="Private Ticket", queue=self.queue_private)
        Ticket.objects.create(title="Public Ticket", queue=self.queue_public)
        huser = HelpdeskUser(self.user_anon)
        tickets = huser.get_tickets_in_queues()
        self.assertEqual(tickets.count(), 1)
        self.assertEqual(tickets.first().queue, self.queue_public)

    def test_anonymous_dashboard_redirects(self):
        """Anonymous user is redirected from dashboard (not staff)."""
        client = Client()
        response = client.get(reverse("helpdesk:dashboard"))
        self.assertIn(response.status_code, [302, 403])

    def test_anonymous_ticket_list_redirects(self):
        """Anonymous user is redirected from ticket list."""
        client = Client()
        response = client.get(reverse("helpdesk:list"))
        self.assertIn(response.status_code, [302, 403])

    def test_anonymous_edit_ticket_redirects(self):
        """Anonymous user is redirected from edit ticket view."""
        ticket = Ticket.objects.create(title="Test", queue=self.queue_private)
        client = Client()
        response = client.get(
            reverse("helpdesk:edit", kwargs={"ticket_id": ticket.id})
        )
        self.assertIn(response.status_code, [302, 403])

    def test_anonymous_get_user_queues_returns_public_only(self):
        """get_user_queues for anonymous user returns only public queues."""
        choices = get_user_queues(self.user_anon)
        queue_ids = [c[0] for c in choices if c[0]]
        self.assertEqual(len(queue_ids), 1)
        self.assertIn(self.queue_public.id, queue_ids)

    def test_anonymous_huser_from_request(self):
        """huser_from_request with anonymous user yields empty queue access."""
        request = MagicMock()
        request.user = self.user_anon
        huser = huser_from_request(request)
        self.assertFalse(huser.is_staff())
        queues = huser.get_queues()
        self.assertEqual(queues.count(), 1)
        self.assertIn(self.queue_public, queues)


class UnauthorizedUserPermissionTestCase(TestCase):
    """
    Boundary tests for authenticated but unauthorized users.
    These are users who are logged in, active, but have no staff flag,
    no superuser flag, and no queue permissions.
    Ensures such users get empty or public-only queue sets.
    """

    def setUp(self):
        self.old_per_queue_setting = helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = True

        self.queue_private = Queue.objects.create(title="Private Q", slug="priv")
        self.queue_public = Queue.objects.create(
            title="Public Q", slug="pub", allow_public_submission=True
        )

        self.user_unauthorized = User.objects.create_user(
            username="unauthorized", password="pass",
            is_staff=False, is_superuser=False, is_active=True
        )
        self.user_inactive = User.objects.create_user(
            username="inactive", password="pass",
            is_staff=False, is_superuser=False, is_active=False
        )

        self.client = Client()

    def tearDown(self):
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = self.old_per_queue_setting

    def test_unauthorized_is_authenticated(self):
        """Unauthorized user is authenticated."""
        huser = HelpdeskUser(self.user_unauthorized)
        self.assertTrue(huser.is_authenticated())

    def test_unauthorized_is_active(self):
        """Unauthorized user is active."""
        huser = HelpdeskUser(self.user_unauthorized)
        self.assertTrue(huser.is_active())

    def test_unauthorized_is_staff_default_config(self):
        """Unauthorized user is not staff under default config."""
        original = helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE
        try:
            helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE = False
            huser = HelpdeskUser(self.user_unauthorized)
            self.assertFalse(huser.is_staff())
        finally:
            helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE = original

    def test_unauthorized_is_staff_allow_non_staff(self):
        """Unauthorized user IS staff when HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE=True."""
        original = helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE
        try:
            helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE = True
            huser = HelpdeskUser(self.user_unauthorized)
            self.assertTrue(huser.is_staff())
        finally:
            helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE = original

    def test_unauthorized_is_superuser(self):
        """Unauthorized user is never superuser."""
        huser = HelpdeskUser(self.user_unauthorized)
        self.assertFalse(huser.is_superuser())

    def test_unauthorized_has_full_access(self):
        """Unauthorized user has no full access when per-queue permission is enabled."""
        huser = HelpdeskUser(self.user_unauthorized)
        self.assertFalse(huser.has_full_access())

    def test_unauthorized_get_queues_per_queue_enabled(self):
        """Unauthorized user sees only public queues when per-queue permission is enabled."""
        huser = HelpdeskUser(self.user_unauthorized)
        queues = huser.get_queues()
        self.assertEqual(queues.count(), 1)
        self.assertIn(self.queue_public, queues)
        self.assertNotIn(self.queue_private, queues)

    def test_unauthorized_get_queues_no_public_queues(self):
        """Unauthorized user sees zero queues when no public queues exist."""
        Queue.objects.filter(allow_public_submission=True).update(
            allow_public_submission=False
        )
        huser = HelpdeskUser(self.user_unauthorized)
        queues = huser.get_queues()
        self.assertEqual(queues.count(), 0)

    def test_unauthorized_get_queue_choices(self):
        """Unauthorized user gets only public queue choices."""
        huser = HelpdeskUser(self.user_unauthorized)
        choices = huser.get_queue_choices()
        queue_ids = [c[0] for c in choices if c[0]]
        self.assertEqual(len(queue_ids), 1)
        self.assertIn(self.queue_public.id, queue_ids)
        self.assertNotIn(self.queue_private.id, queue_ids)

    def test_unauthorized_get_queue_choices_no_public_queues(self):
        """Unauthorized user gets empty choices when no public queues exist."""
        Queue.objects.filter(allow_public_submission=True).update(
            allow_public_submission=False
        )
        huser = HelpdeskUser(self.user_unauthorized)
        choices = huser.get_queue_choices()
        queue_ids = [c[0] for c in choices if c[0]]
        self.assertEqual(len(queue_ids), 0)

    def test_unauthorized_can_access_queue_private(self):
        """Unauthorized user cannot access private queues."""
        huser = HelpdeskUser(self.user_unauthorized)
        self.assertFalse(huser.can_access_queue(self.queue_private))

    def test_unauthorized_can_access_queue_public(self):
        """Unauthorized user can access public queues."""
        huser = HelpdeskUser(self.user_unauthorized)
        self.assertTrue(huser.can_access_queue(self.queue_public))

    def test_unauthorized_can_access_ticket_in_private_queue(self):
        """Unauthorized user cannot access tickets in private queues."""
        ticket = Ticket.objects.create(title="Private Ticket", queue=self.queue_private)
        huser = HelpdeskUser(self.user_unauthorized)
        self.assertFalse(huser.can_access_ticket(ticket))

    def test_unauthorized_can_access_ticket_in_public_queue(self):
        """Unauthorized user can access tickets in public queues."""
        ticket = Ticket.objects.create(title="Public Ticket", queue=self.queue_public)
        huser = HelpdeskUser(self.user_unauthorized)
        self.assertTrue(huser.can_access_ticket(ticket))

    def test_unauthorized_get_tickets_in_queues(self):
        """Unauthorized user only sees tickets in public queues."""
        Ticket.objects.create(title="Private Ticket", queue=self.queue_private)
        Ticket.objects.create(title="Public Ticket", queue=self.queue_public)
        huser = HelpdeskUser(self.user_unauthorized)
        tickets = huser.get_tickets_in_queues()
        self.assertEqual(tickets.count(), 1)
        self.assertEqual(tickets.first().queue, self.queue_public)

    def test_unauthorized_dashboard_redirects(self):
        """Unauthorized user is redirected/forbidden from dashboard."""
        self.client.login(username="unauthorized", password="pass")
        response = self.client.get(reverse("helpdesk:dashboard"))
        self.assertIn(response.status_code, [302, 403])

    def test_unauthorized_ticket_list_redirects(self):
        """Unauthorized user is redirected/forbidden from ticket list."""
        self.client.login(username="unauthorized", password="pass")
        response = self.client.get(reverse("helpdesk:list"))
        self.assertIn(response.status_code, [302, 403])

    def test_unauthorized_edit_ticket_redirects(self):
        """Unauthorized user is redirected/forbidden from edit ticket view."""
        ticket = Ticket.objects.create(title="Test", queue=self.queue_private)
        self.client.login(username="unauthorized", password="pass")
        response = self.client.get(
            reverse("helpdesk:edit", kwargs={"ticket_id": ticket.id})
        )
        self.assertIn(response.status_code, [302, 403])

    def test_unauthorized_get_user_queues_returns_public_only(self):
        """get_user_queues for unauthorized user returns only public queues."""
        choices = get_user_queues(self.user_unauthorized)
        queue_ids = [c[0] for c in choices if c[0]]
        self.assertEqual(len(queue_ids), 1)
        self.assertIn(self.queue_public.id, queue_ids)

    def test_inactive_user_get_queues(self):
        """Inactive user sees only public queues (not private)."""
        huser = HelpdeskUser(self.user_inactive)
        queues = huser.get_queues()
        self.assertEqual(queues.count(), 1)
        self.assertIn(self.queue_public, queues)
        self.assertNotIn(self.queue_private, queues)

    def test_inactive_user_is_staff(self):
        """Inactive user is not staff regardless of config."""
        original = helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE
        try:
            helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE = True
            huser = HelpdeskUser(self.user_inactive)
            self.assertFalse(huser.is_staff())

            helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE = False
            huser = HelpdeskUser(self.user_inactive)
            self.assertFalse(huser.is_staff())
        finally:
            helpdesk_settings.HELPDESK_ALLOW_NON_STAFF_TICKET_UPDATE = original

    def test_inactive_user_is_superuser(self):
        """Inactive user is never superuser."""
        huser = HelpdeskUser(self.user_inactive)
        self.assertFalse(huser.is_superuser())

    def test_inactive_user_dashboard_redirects(self):
        """Inactive user is redirected/forbidden from dashboard."""
        self.client.login(username="inactive", password="pass")
        response = self.client.get(reverse("helpdesk:dashboard"))
        self.assertIn(response.status_code, [302, 403])


class UnauthorizedManagementCommandTestCase(TransactionTestCase):
    """
    Tests for management commands with unauthorized/anonymous users.
    Verifies that --user with an unauthorized user returns empty queue results,
    and that --user with a nonexistent user raises CommandError.
    """

    def setUp(self):
        self.old_per_queue_setting = helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = True

        self.queue_private = Queue.objects.create(
            title="Private Q", slug="priv", escalate_days=5
        )
        self.queue_public = Queue.objects.create(
            title="Public Q", slug="pub", allow_public_submission=True, escalate_days=3
        )

        call_command("create_queue_permissions", verbosity=0)

        self.user_unauthorized = User.objects.create_user(
            username="unauthorized", password="pass",
            is_staff=False, is_superuser=False, is_active=True
        )

    def tearDown(self):
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = self.old_per_queue_setting

    def test_escalate_tickets_unauthorized_user_no_private_queues(self):
        """escalate_tickets --user with unauthorized user skips private queues."""
        out = StringIO()
        call_command(
            "escalate_tickets",
            user="unauthorized",
            escalate_verbosely=True,
            stdout=out
        )
        output = out.getvalue()
        self.assertNotIn("Private Q", output)

    def test_escalate_tickets_unauthorized_user_sees_public_queue(self):
        """escalate_tickets --user with unauthorized user includes public queues."""
        out = StringIO()
        call_command(
            "escalate_tickets",
            user="unauthorized",
            escalate_verbosely=True,
            stdout=out
        )
        output = out.getvalue()
        self.assertIn("Public Q", output)

    def test_create_queue_permissions_unauthorized_user(self):
        """create_queue_permissions --user with unauthorized user skips private queues."""
        out = StringIO()
        call_command(
            "create_queue_permissions",
            user="unauthorized",
            escalate_verbosely=True,
            stdout=out
        )
        output = out.getvalue()
        self.assertNotIn("Private Q", output)
        self.assertIn("Public Q", output)

    def test_unauthorized_user_get_queues_no_public(self):
        """Unauthorized user gets empty queues when no public queues exist."""
        Queue.objects.filter(allow_public_submission=True).update(
            allow_public_submission=False
        )
        huser = HelpdeskUser(self.user_unauthorized)
        queues = huser.get_queues()
        self.assertEqual(queues.count(), 0)
