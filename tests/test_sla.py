from datetime import date, datetime
from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time
from helpdesk.lib import (
    calculate_working_days,
    calculate_sla_deadline,
    get_ticket_sla_status,
)
from helpdesk.models import EscalationExclusion, Queue, Ticket


class CalculateWorkingDaysTestCase(TestCase):
    """Test cases for calculate_working_days function."""

    def test_no_exclusions_same_day(self):
        """When start and end are the same date, 0 working days should be returned."""
        start = date(2026, 6, 11)
        end = date(2026, 6, 11)
        self.assertEqual(calculate_working_days(start, end), 0)

    def test_no_exclusions_single_day(self):
        """One calendar day with no exclusions should return 1 working day."""
        start = date(2026, 6, 11)
        end = date(2026, 6, 12)
        self.assertEqual(calculate_working_days(start, end), 1)

    def test_no_exclusions_multiple_days(self):
        """Multiple calendar days with no exclusions."""
        start = date(2026, 6, 8)
        end = date(2026, 6, 13)
        self.assertEqual(calculate_working_days(start, end), 5)

    def test_with_exclusion_date(self):
        """Days with EscalationExclusion should not be counted."""
        exclusion_date = date(2026, 6, 10)
        EscalationExclusion.objects.create(name="Holiday", date=exclusion_date)

        start = date(2026, 6, 8)
        end = date(2026, 6, 13)
        self.assertEqual(calculate_working_days(start, end), 4)

    def test_with_multiple_exclusion_dates(self):
        """Multiple exclusion dates should all be skipped."""
        for d in [date(2026, 6, 10), date(2026, 6, 11)]:
            EscalationExclusion.objects.create(name="Holiday", date=d)

        start = date(2026, 6, 8)
        end = date(2026, 6, 13)
        self.assertEqual(calculate_working_days(start, end), 3)

    def test_start_after_end(self):
        """When start date is after end date, should return 0."""
        start = date(2026, 6, 15)
        end = date(2026, 6, 11)
        self.assertEqual(calculate_working_days(start, end), 0)

    def test_weekend_days_not_excluded_by_default(self):
        """Weekend days are not automatically excluded (only EscalationExclusion dates)."""
        start = date(2026, 6, 13)
        end = date(2026, 6, 16)
        self.assertEqual(calculate_working_days(start, end), 3)


class CalculateSlaDeadlineTestCase(TestCase):
    """Test cases for calculate_sla_deadline function."""

    def setUp(self):
        self.queue = Queue.objects.create(
            title="Test Queue",
            slug="test-queue",
            escalate_days=3,
        )

    def test_queue_without_escalate_days(self):
        """If queue has no escalation days, should return None."""
        queue_no_sla = Queue.objects.create(title="No SLA", slug="no-sla")
        ticket = Ticket.objects.create(
            title="Test", queue=queue_no_sla, created=timezone.now()
        )
        self.assertIsNone(calculate_sla_deadline(ticket))

    def test_queue_with_escalate_days_zero(self):
        """If queue escalation days is 0, should return None."""
        queue_zero = Queue.objects.create(
            title="Zero SLA", slug="zero-sla", escalate_days=0
        )
        ticket = Ticket.objects.create(
            title="Test", queue=queue_zero, created=timezone.now()
        )
        self.assertIsNone(calculate_sla_deadline(ticket))

    @freeze_time("2026-06-11 10:00:00")
    def test_deadline_without_exclusions(self):
        """Deadline calculation without any exclusion dates.

        Simulating the escalate_tickets management command at T=June 11 10:00:
        - today=June 11, last=June 11-3=June 8
        - working_days in [June 8, June 11) = 3 (Jun8, Jun9, Jun10)
        - req_last_escl_date = June 11 10:00 - 3d = June 8 10:00
        - created=June 8 10:00 <= June 8 10:00 → needs escalation
        => deadline = June 11 10:00
        """
        created = timezone.make_aware(datetime(2026, 6, 8, 10, 0, 0))
        ticket = Ticket.objects.create(
            title="Test", queue=self.queue, created=created
        )
        deadline = calculate_sla_deadline(ticket)
        expected = timezone.make_aware(datetime(2026, 6, 11, 10, 0, 0))
        self.assertEqual(deadline, expected)

    @freeze_time("2026-06-11 10:00:00")
    def test_deadline_with_exclusion_date(self):
        """Deadline calculation should skip EscalationExclusion dates.

        With Jun 9 excluded and origin=June 8 10:00, escalate_days=3:
        At T=June 8: last=Jun5, wd[Jun5,Jun8)=3, req=Jun5, Jun8<=Jun5? No
        At T=June 9: last=Jun6, wd[Jun6,Jun9)=3, req=Jun6, Jun8<=Jun6? No
        At T=June 10: last=Jun7, wd[Jun7,Jun10)=2 (Jun9 excluded!), req=Jun8
        Jun8 <= Jun8? YES → escalation needed
        => deadline = June 10 10:00
        """
        EscalationExclusion.objects.create(
            name="Holiday", date=date(2026, 6, 9)
        )
        created = timezone.make_aware(datetime(2026, 6, 8, 10, 0, 0))
        ticket = Ticket.objects.create(
            title="Test", queue=self.queue, created=created
        )
        deadline = calculate_sla_deadline(ticket)
        expected = timezone.make_aware(datetime(2026, 6, 10, 10, 0, 0))
        self.assertEqual(deadline, expected)

    @freeze_time("2026-06-11 10:00:00")
    def test_deadline_uses_last_escalation(self):
        """If ticket has last_escalation, use that instead of created date.

        With last_escalation=June 8 14:30, escalate_days=3:
        At T=June 11 14:30 (management command runs):
        - today=June 11, last=June 8
        - working_days in [Jun8, Jun11) = 3
        - req = Jun 11 14:30 - 3d = Jun 8 14:30
        - last_escalation=Jun 8 14:30 <= req → escalation needed
        => deadline = June 11 14:30
        """
        created = timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0))
        last_escalation = timezone.make_aware(datetime(2026, 6, 8, 14, 30, 0))
        ticket = Ticket.objects.create(
            title="Test",
            queue=self.queue,
            created=created,
            last_escalation=last_escalation,
        )
        deadline = calculate_sla_deadline(ticket)
        expected = timezone.make_aware(datetime(2026, 6, 11, 14, 30, 0))
        self.assertEqual(deadline, expected)

    @freeze_time("2026-06-11 10:00:00")
    def test_deadline_preserves_time_component(self):
        """Deadline should preserve the time component from the origin datetime."""
        created = timezone.make_aware(datetime(2026, 6, 8, 15, 45, 30))
        ticket = Ticket.objects.create(
            title="Test", queue=self.queue, created=created
        )
        deadline = calculate_sla_deadline(ticket)
        self.assertEqual(deadline.hour, 15)
        self.assertEqual(deadline.minute, 45)
        self.assertEqual(deadline.second, 30)


class GetTicketSlaStatusTestCase(TestCase):
    """Test cases for get_ticket_sla_status function."""

    def setUp(self):
        self.queue = Queue.objects.create(
            title="Test Queue",
            slug="test-queue",
            escalate_days=3,
        )
        self.queue_no_sla = Queue.objects.create(
            title="No SLA Queue", slug="no-sla-queue"
        )

    @freeze_time("2026-06-11 10:00:00")
    def test_status_overdue(self):
        """Ticket past the deadline should return 'overdue' status.

        With escalate_days=3, created=June 1: day1=Jun1, day2=Jun2, day3=Jun3
        => deadline = June 3 10:00. Now is June 11, so overdue.
        """
        created = timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0))
        ticket = Ticket.objects.create(
            title="Overdue Ticket",
            queue=self.queue,
            created=created,
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "overdue")
        self.assertIsNotNone(deadline)
        self.assertLess(time_remaining.total_seconds(), 0)

    @freeze_time("2026-06-12 10:00:00")
    def test_status_warning_within_24h(self):
        """Ticket within 24 hours of deadline should return 'warning' status.

        With escalate_days=3, created=June 9 15:00:
        At T=June 12 15:00: needs escalation (deadline)
        Now is June 12 10:00, remaining=5h => warning.
        """
        created = timezone.make_aware(datetime(2026, 6, 9, 15, 0, 0))
        ticket = Ticket.objects.create(
            title="Warning Ticket",
            queue=self.queue,
            created=created,
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "warning")
        self.assertGreaterEqual(time_remaining.total_seconds(), 0)
        self.assertLess(time_remaining.total_seconds(), 86400)

    @freeze_time("2026-06-11 10:00:00")
    def test_status_ok(self):
        """Ticket with plenty of time remaining should return 'ok' status.

        With escalate_days=3, created=June 11 09:00:
        day1=Jun11, day2=Jun12, day3=Jun13 => deadline=Jun13 09:00.
        Now is Jun11 10:00, remaining ~47h (>24h) => ok.
        """
        created = timezone.make_aware(datetime(2026, 6, 11, 9, 0, 0))
        ticket = Ticket.objects.create(
            title="OK Ticket",
            queue=self.queue,
            created=created,
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "ok")
        self.assertGreaterEqual(time_remaining.total_seconds(), 86400)

    def test_status_excluded_priority_1(self):
        """Ticket with priority 1 (Critical) should be excluded from SLA."""
        ticket = Ticket.objects.create(
            title="Priority 1 Ticket",
            queue=self.queue,
            created=timezone.now(),
            status=Ticket.OPEN_STATUS,
            priority=1,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "excluded")
        self.assertIsNone(deadline)
        self.assertIsNone(time_remaining)

    def test_status_excluded_on_hold(self):
        """Ticket that is on hold should be excluded from SLA."""
        ticket = Ticket.objects.create(
            title="On Hold Ticket",
            queue=self.queue,
            created=timezone.now(),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=True,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "excluded")
        self.assertIsNone(deadline)
        self.assertIsNone(time_remaining)

    def test_status_excluded_resolved(self):
        """Ticket with Resolved status should be excluded from SLA."""
        ticket = Ticket.objects.create(
            title="Resolved Ticket",
            queue=self.queue,
            created=timezone.now(),
            status=Ticket.RESOLVED_STATUS,
            priority=3,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "excluded")
        self.assertIsNone(deadline)
        self.assertIsNone(time_remaining)

    def test_status_excluded_closed(self):
        """Ticket with Closed status should be excluded from SLA."""
        ticket = Ticket.objects.create(
            title="Closed Ticket",
            queue=self.queue,
            created=timezone.now(),
            status=Ticket.CLOSED_STATUS,
            priority=3,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "excluded")
        self.assertIsNone(deadline)
        self.assertIsNone(time_remaining)

    def test_status_excluded_duplicate(self):
        """Ticket with Duplicate status should be excluded from SLA."""
        ticket = Ticket.objects.create(
            title="Duplicate Ticket",
            queue=self.queue,
            created=timezone.now(),
            status=Ticket.DUPLICATE_STATUS,
            priority=3,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "excluded")
        self.assertIsNone(deadline)
        self.assertIsNone(time_remaining)

    def test_status_no_sla_queue(self):
        """Ticket in a queue without escalation days should return 'no_sla'."""
        ticket = Ticket.objects.create(
            title="No SLA Ticket",
            queue=self.queue_no_sla,
            created=timezone.now(),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "no_sla")
        self.assertIsNone(deadline)
        self.assertIsNone(time_remaining)

    @freeze_time("2026-06-12 10:00:00")
    def test_warning_boundary_at_exactly_24h(self):
        """At exactly 86400 seconds (24h), should still be 'ok', not 'warning'.

        With escalate_days=3, created=June 10 10:00: deadline=June 13 10:00.
        Now is June 12 10:00, remaining=86400s exactly => ok (not warning).
        """
        created = timezone.make_aware(datetime(2026, 6, 10, 10, 0, 0))
        ticket = Ticket.objects.create(
            title="Boundary Ticket",
            queue=self.queue,
            created=created,
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "ok")
        self.assertEqual(int(time_remaining.total_seconds()), 86400)

    @freeze_time("2026-06-12 10:00:01")
    def test_warning_boundary_just_under_24h(self):
        """At 86399 seconds, should be 'warning'.

        With escalate_days=3, created=June 10 10:00:00: deadline=June 13 10:00:00.
        Now is June 12 10:00:01, remaining=86399s => warning.
        """
        created = timezone.make_aware(datetime(2026, 6, 10, 10, 0, 0))
        ticket = Ticket.objects.create(
            title="Boundary Ticket 2",
            queue=self.queue,
            created=created,
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "warning")
        self.assertLess(time_remaining.total_seconds(), 86400)
        self.assertGreaterEqual(time_remaining.total_seconds(), 0)

    @freeze_time("2026-06-13 10:00:02")
    def test_overdue_boundary_just_over_zero(self):
        """When just past the deadline (negative remaining time), should be 'overdue'.

        With escalate_days=3, created=June 10 10:00:01: deadline=June 13 10:00:01.
        Now is June 13 10:00:02 => remaining=-1s => overdue.
        """
        created = timezone.make_aware(datetime(2026, 6, 10, 10, 0, 1))
        ticket = Ticket.objects.create(
            title="Overdue Boundary",
            queue=self.queue,
            created=created,
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "overdue")
        self.assertLess(time_remaining.total_seconds(), 0)

    @freeze_time("2026-06-13 10:00:01")
    def test_overdue_boundary_at_exactly_zero(self):
        """At exactly 0 seconds remaining, should be 'warning' (>= 0 and < 86400).

        With escalate_days=3, created=June 10 10:00:01: deadline=June 13 10:00:01.
        Now is June 13 10:00:01 => remaining=0s => warning (since 0 >= 0 and 0 < 86400).
        """
        created = timezone.make_aware(datetime(2026, 6, 10, 10, 0, 1))
        ticket = Ticket.objects.create(
            title="Zero Remaining Boundary",
            queue=self.queue,
            created=created,
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "warning")
        self.assertEqual(int(time_remaining.total_seconds()), 0)


class SlaAlertViewFilterTestCase(TestCase):
    """Test cases for SLA alert view filtering."""

    fixtures = ["emailtemplate.json"]

    def setUp(self):
        from django.contrib.auth import get_user_model
        from django.test.client import Client
        from helpdesk import settings as helpdesk_settings

        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = False

        User = get_user_model()
        self.user = User.objects.create(username="staff_user", is_staff=True)
        self.user.set_password("pass")
        self.user.save()

        self.queue = Queue.objects.create(
            title="Test Queue",
            slug="test-queue",
            escalate_days=3,
        )

        self.client = Client()
        self.client.login(username="staff_user", password="pass")

    @freeze_time("2026-06-11 10:00:00")
    def test_view_excludes_closed_tickets(self):
        """SLA alert view should not include closed tickets."""
        Ticket.objects.create(
            title="Closed Ticket",
            queue=self.queue,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.CLOSED_STATUS,
            priority=3,
        )
        Ticket.objects.create(
            title="Resolved Ticket",
            queue=self.queue,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.RESOLVED_STATUS,
            priority=3,
        )
        Ticket.objects.create(
            title="Duplicate Ticket",
            queue=self.queue,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.DUPLICATE_STATUS,
            priority=3,
        )
        open_ticket = Ticket.objects.create(
            title="Open Ticket",
            queue=self.queue,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
        )

        from django.urls import reverse

        response = self.client.get(reverse("helpdesk:sla_alert"))
        self.assertEqual(response.status_code, 200)
        display_tickets = response.context["display_tickets"]
        ticket_ids = [t.id for t in display_tickets]

        self.assertIn(open_ticket.id, ticket_ids)
        for t in Ticket.objects.exclude(status__in=Ticket.OPEN_STATUSES):
            self.assertNotIn(t.id, ticket_ids)

    @freeze_time("2026-06-11 10:00:00")
    def test_view_total_count_only_open_statuses(self):
        """Total count should only reflect tickets with open statuses."""
        for _ in range(2):
            Ticket.objects.create(
                title="Closed",
                queue=self.queue,
                created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
                status=Ticket.CLOSED_STATUS,
                priority=3,
            )
        for _ in range(3):
            Ticket.objects.create(
                title="Open",
                queue=self.queue,
                created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
                status=Ticket.OPEN_STATUS,
                priority=3,
            )

        from django.urls import reverse

        response = self.client.get(reverse("helpdesk:sla_alert"))
        self.assertEqual(response.context["total_count"], 3)
