from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.client import Client
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time

from helpdesk import settings as helpdesk_settings
from helpdesk.management.commands.escalate_tickets import Command
from helpdesk.models import FollowUp, Queue, Ticket


User = get_user_model()


class TicketEscalationSLATestCase(TestCase):
    fixtures = ["emailtemplate.json"]

    def setUp(self):
        self.queue = Queue.objects.create(
            title="Test Queue",
            slug="test",
            escalate_days=3,
        )
        self.ticket_data = {
            "queue": self.queue,
            "title": "Test Ticket SLA",
            "description": "Test description",
        }
        self.client = Client()
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = False
        helpdesk_settings.TICKET_ESCALATION_EXCLUDE_STATUSES = ()

    def _login_staff(self):
        self.user = User.objects.create(
            username="staff_user",
            is_staff=True,
        )
        self.user.set_password("pass")
        self.user.save()
        self.client.login(username="staff_user", password="pass")

    def test_place_on_hold_sets_start_time(self):
        ticket = Ticket.objects.create(**self.ticket_data)
        self.assertFalse(ticket.on_hold)
        self.assertIsNone(ticket.hold_start_time)

        ticket.place_on_hold()
        ticket.refresh_from_db()

        self.assertTrue(ticket.on_hold)
        self.assertIsNotNone(ticket.hold_start_time)
        self.assertIsNotNone(ticket.modified)

    def test_place_on_hold_idempotent(self):
        ticket = Ticket.objects.create(**self.ticket_data)
        ticket.place_on_hold()
        first_hold_time = ticket.hold_start_time

        with freeze_time(timezone.now() + timedelta(hours=1)):
            ticket.place_on_hold()
            ticket.refresh_from_db()

        self.assertEqual(ticket.hold_start_time, first_hold_time)

    def test_take_off_hold_accumulates_paused_time(self):
        ticket = Ticket.objects.create(**self.ticket_data)

        with freeze_time("2024-01-01 10:00:00"):
            ticket.place_on_hold()

        with freeze_time("2024-01-02 10:00:00"):
            ticket.take_off_hold()
            ticket.refresh_from_db()

        self.assertFalse(ticket.on_hold)
        self.assertIsNone(ticket.hold_start_time)
        self.assertIsNotNone(ticket.total_paused_time)
        self.assertGreaterEqual(ticket.total_paused_time, timedelta(days=1))
        self.assertLess(ticket.total_paused_time, timedelta(days=1, hours=1))

    def test_take_off_hold_multiple_times_accumulates(self):
        ticket = Ticket.objects.create(**self.ticket_data)

        with freeze_time("2024-01-01 10:00:00"):
            ticket.place_on_hold()
        with freeze_time("2024-01-02 10:00:00"):
            ticket.take_off_hold()

        with freeze_time("2024-01-03 10:00:00"):
            ticket.place_on_hold()
        with freeze_time("2024-01-04 10:00:00"):
            ticket.take_off_hold()
            ticket.refresh_from_db()

        self.assertGreaterEqual(ticket.total_paused_time, timedelta(days=2))
        self.assertLess(ticket.total_paused_time, timedelta(days=2, hours=1))

    def test_get_effective_last_escalation_on_hold(self):
        ticket = Ticket.objects.create(**self.ticket_data)
        ticket.place_on_hold()
        self.assertIsNone(ticket.get_effective_last_escalation())

    def test_get_effective_last_escalation_without_pause(self):
        with freeze_time("2024-01-01 10:00:00"):
            ticket = Ticket.objects.create(**self.ticket_data)

        effective = ticket.get_effective_last_escalation()
        self.assertEqual(effective, ticket.created)

    def test_get_effective_last_escalation_with_paused_time(self):
        with freeze_time("2024-01-01 10:00:00"):
            ticket = Ticket.objects.create(**self.ticket_data)

        with freeze_time("2024-01-02 10:00:00"):
            ticket.place_on_hold()
        with freeze_time("2024-01-04 10:00:00"):
            ticket.take_off_hold()

        effective = ticket.get_effective_last_escalation()
        expected = ticket.created + timedelta(days=2)
        self.assertEqual(effective, expected)

    def test_save_on_hold_change_via_attribute(self):
        ticket = Ticket.objects.create(**self.ticket_data)

        with freeze_time("2024-01-01 10:00:00"):
            ticket.on_hold = True
            ticket.save()
            ticket.refresh_from_db()

        self.assertTrue(ticket.on_hold)
        self.assertIsNotNone(ticket.hold_start_time)

        with freeze_time("2024-01-02 10:00:00"):
            ticket.on_hold = False
            ticket.save()
            ticket.refresh_from_db()

        self.assertFalse(ticket.on_hold)
        self.assertIsNone(ticket.hold_start_time)
        self.assertGreaterEqual(ticket.total_paused_time, timedelta(days=1))

    def test_hold_ticket_view(self):
        self._login_staff()
        ticket = Ticket.objects.create(**self.ticket_data)
        self.assertFalse(ticket.on_hold)

        with freeze_time("2024-01-01 10:00:00"):
            response = self.client.get(
                reverse("helpdesk:hold", kwargs={"ticket_id": ticket.id}),
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        ticket.refresh_from_db()
        self.assertTrue(ticket.on_hold)
        self.assertIsNotNone(ticket.hold_start_time)
        self.assertTrue(
            FollowUp.objects.filter(ticket=ticket, title="Ticket placed on hold").exists()
        )

    def test_unhold_ticket_view(self):
        self._login_staff()
        with freeze_time("2024-01-01 10:00:00"):
            ticket = Ticket.objects.create(**self.ticket_data)
            ticket.place_on_hold()

        with freeze_time("2024-01-03 10:00:00"):
            response = self.client.get(
                reverse("helpdesk:unhold", kwargs={"ticket_id": ticket.id}),
                follow=True,
            )

        self.assertEqual(response.status_code, 200)
        ticket.refresh_from_db()
        self.assertFalse(ticket.on_hold)
        self.assertIsNone(ticket.hold_start_time)
        self.assertGreaterEqual(ticket.total_paused_time, timedelta(days=2))
        self.assertTrue(
            FollowUp.objects.filter(ticket=ticket, title="Ticket taken off hold").exists()
        )


class TicketEscalationCommandTestCase(TestCase):
    fixtures = ["emailtemplate.json"]

    def setUp(self):
        self.queue = Queue.objects.create(
            title="Escalation Test Queue",
            slug="esc_test",
            escalate_days=3,
        )
        self.command = Command()
        helpdesk_settings.TICKET_ESCALATION_EXCLUDE_STATUSES = ()

    def test_on_hold_ticket_not_escalated(self):
        with freeze_time("2024-01-01 10:00:00"):
            ticket = Ticket.objects.create(
                queue=self.queue,
                title="On Hold Ticket",
                priority=3,
                status=Ticket.OPEN_STATUS,
            )
            ticket.place_on_hold()

        with freeze_time("2024-01-10 10:00:00"):
            with mock.patch.object(Ticket, "send") as mock_send:
                self.command.handle()

            ticket.refresh_from_db()
            self.assertEqual(ticket.priority, 3)
            mock_send.assert_not_called()

    def test_excluded_status_ticket_not_escalated(self):
        WAITING_STATUS = 6
        helpdesk_settings.TICKET_ESCALATION_EXCLUDE_STATUSES = (WAITING_STATUS,)

        with freeze_time("2024-01-01 10:00:00"):
            ticket = Ticket.objects.create(
                queue=self.queue,
                title="Waiting Customer Ticket",
                priority=3,
                status=WAITING_STATUS,
            )

        with freeze_time("2024-01-10 10:00:00"):
            with mock.patch.object(Ticket, "send") as mock_send:
                self.command.handle()

            ticket.refresh_from_db()
            self.assertEqual(ticket.priority, 3)
            mock_send.assert_not_called()

        helpdesk_settings.TICKET_ESCALATION_EXCLUDE_STATUSES = ()

    def test_paused_time_delays_escalation(self):
        with freeze_time("2024-01-01 10:00:00"):
            ticket = Ticket.objects.create(
                queue=self.queue,
                title="Paused Ticket",
                priority=3,
                status=Ticket.OPEN_STATUS,
            )

        with freeze_time("2024-01-02 10:00:00"):
            ticket.place_on_hold()

        with freeze_time("2024-01-05 10:00:00"):
            ticket.take_off_hold()

        with freeze_time("2024-01-06 10:00:00"):
            with mock.patch.object(Ticket, "send") as mock_send:
                self.command.handle()

            ticket.refresh_from_db()
            self.assertEqual(ticket.priority, 3)
            mock_send.assert_not_called()

        with freeze_time("2024-01-09 10:00:00"):
            with mock.patch.object(Ticket, "send") as mock_send:
                self.command.handle()

            ticket.refresh_from_db()
            self.assertEqual(ticket.priority, 2)
            mock_send.assert_called_once()

    def test_normal_ticket_escalates_on_time(self):
        with freeze_time("2024-01-01 10:00:00"):
            ticket = Ticket.objects.create(
                queue=self.queue,
                title="Normal Ticket",
                priority=3,
                status=Ticket.OPEN_STATUS,
            )

        with freeze_time("2024-01-03 10:00:00"):
            with mock.patch.object(Ticket, "send") as mock_send:
                self.command.handle()

            ticket.refresh_from_db()
            self.assertEqual(ticket.priority, 3)
            mock_send.assert_not_called()

        with freeze_time("2024-01-05 10:00:00"):
            with mock.patch.object(Ticket, "send") as mock_send:
                self.command.handle()

            ticket.refresh_from_db()
            self.assertEqual(ticket.priority, 2)
            mock_send.assert_called_once()

    def test_priority_1_not_escalated(self):
        with freeze_time("2024-01-01 10:00:00"):
            ticket = Ticket.objects.create(
                queue=self.queue,
                title="Priority 1 Ticket",
                priority=1,
                status=Ticket.OPEN_STATUS,
            )

        with freeze_time("2024-01-10 10:00:00"):
            with mock.patch.object(Ticket, "send") as mock_send:
                self.command.handle()

            ticket.refresh_from_db()
            self.assertEqual(ticket.priority, 1)
            mock_send.assert_not_called()

    def test_status_transition_open_to_waiting_to_open(self):
        WAITING_STATUS = 6
        helpdesk_settings.TICKET_ESCALATION_EXCLUDE_STATUSES = (WAITING_STATUS,)
        original_status_choices = helpdesk_settings.TICKET_STATUS_CHOICES
        helpdesk_settings.TICKET_STATUS_CHOICES = original_status_choices + (
            (WAITING_STATUS, "Waiting on Customer"),
        )

        with freeze_time("2024-01-01 10:00:00"):
            ticket = Ticket.objects.create(
                queue=self.queue,
                title="Status Transition Ticket",
                priority=3,
                status=Ticket.OPEN_STATUS,
            )

        with freeze_time("2024-01-02 10:00:00"):
            ticket.status = WAITING_STATUS
            ticket.save()

        with freeze_time("2024-01-06 10:00:00"):
            with mock.patch.object(Ticket, "send") as mock_send:
                self.command.handle()

            ticket.refresh_from_db()
            self.assertEqual(ticket.priority, 3)
            mock_send.assert_not_called()

        with freeze_time("2024-01-06 12:00:00"):
            ticket.status = Ticket.OPEN_STATUS
            ticket.save()

        with freeze_time("2024-01-08 10:00:00"):
            with mock.patch.object(Ticket, "send") as mock_send:
                self.command.handle()

            ticket.refresh_from_db()
            self.assertEqual(ticket.priority, 3)
            mock_send.assert_not_called()

        with freeze_time("2024-01-10 10:00:00"):
            with mock.patch.object(Ticket, "send") as mock_send:
                self.command.handle()

            ticket.refresh_from_db()
            self.assertEqual(ticket.priority, 2)
            mock_send.assert_called_once()

        helpdesk_settings.TICKET_ESCALATION_EXCLUDE_STATUSES = ()
        helpdesk_settings.TICKET_STATUS_CHOICES = original_status_choices

    def test_last_escalation_updated_on_escalation(self):
        with freeze_time("2024-01-01 10:00:00"):
            ticket = Ticket.objects.create(
                queue=self.queue,
                title="Escalation Timestamp Test",
                priority=3,
                status=Ticket.OPEN_STATUS,
            )
        self.assertIsNone(ticket.last_escalation)

        with freeze_time("2024-01-05 10:00:00"):
            with mock.patch.object(Ticket, "send"):
                self.command.handle()

            ticket.refresh_from_db()
            self.assertIsNotNone(ticket.last_escalation)
            self.assertEqual(ticket.last_escalation, timezone.now())


class TicketConcurrentEscalationTestCase(TestCase):
    fixtures = ["emailtemplate.json"]

    def setUp(self):
        self.queue = Queue.objects.create(
            title="Concurrent Test Queue",
            slug="concurrent_test",
            escalate_days=1,
        )
        self.command = Command()

    def test_concurrent_escalation_deduplication(self):
        with freeze_time("2024-01-01 10:00:00"):
            ticket = Ticket.objects.create(
                queue=self.queue,
                title="Concurrent Ticket",
                priority=3,
                status=Ticket.OPEN_STATUS,
            )

        with freeze_time("2024-01-03 10:00:00"):
            with mock.patch.object(Ticket, "send") as mock_send:
                self.command.handle()
                self.command.handle()

            ticket.refresh_from_db()
            self.assertEqual(ticket.priority, 2)
            self.assertEqual(mock_send.call_count, 1)

    def test_followup_created_only_once(self):
        with freeze_time("2024-01-01 10:00:00"):
            ticket = Ticket.objects.create(
                queue=self.queue,
                title="Followup Once Ticket",
                priority=3,
                status=Ticket.OPEN_STATUS,
            )

        with freeze_time("2024-01-03 10:00:00"):
            with mock.patch.object(Ticket, "send"):
                self.command.handle()
                self.command.handle()

            followups = FollowUp.objects.filter(
                ticket=ticket, title="Ticket Escalated"
            )
            self.assertEqual(followups.count(), 1)
