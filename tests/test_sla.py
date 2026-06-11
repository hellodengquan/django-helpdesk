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


class GetTicketSlaStatusEdgeCaseTestCase(TestCase):
    """Boundary edge case tests for get_ticket_sla_status function.

    Covers:
    1. Queue escalate_days edge cases (None, 0, negative values)
    2. Priority edge cases (None, 0, out-of-range high values)
    3. on_hold edge cases (None, False explicit)
    4. Fallback path when calculate_sla_deadline returns None
    """

    def setUp(self):
        self.queue_with_sla = Queue.objects.create(
            title="Normal SLA Queue",
            slug="normal-sla",
            escalate_days=3,
        )
        self.queue_no_sla_none = Queue.objects.create(
            title="No SLA None Queue",
            slug="no-sla-none",
            escalate_days=None,
        )
        self.queue_no_sla_zero = Queue.objects.create(
            title="No SLA Zero Queue",
            slug="no-sla-zero",
            escalate_days=0,
        )
        self.queue_no_sla_field = Queue.objects.create(
            title="No SLA Unconfigured Queue",
            slug="no-sla-unconfigured",
        )
        self.queue_negative_sla = Queue.objects.create(
            title="Negative SLA Queue",
            slug="negative-sla",
            escalate_days=-1,
        )

    # --- Group 1: Queue escalate_days boundary tests ---

    @freeze_time("2026-06-11 10:00:00")
    def test_no_sla_queue_escalate_days_none(self):
        """Queue with escalate_days=None must return 'no_sla'.

        Verifies the first branch of get_ticket_sla_status:
        if ticket.queue.escalate_days is None ... → ('no_sla', None, None)
        """
        ticket = Ticket.objects.create(
            title="Escalate None Queue Ticket",
            queue=self.queue_no_sla_none,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "no_sla")
        self.assertIsNone(deadline)
        self.assertIsNone(time_remaining)

    @freeze_time("2026-06-11 10:00:00")
    def test_no_sla_queue_escalate_days_zero(self):
        """Queue with escalate_days=0 must return 'no_sla'.

        Verifies the first branch of get_ticket_sla_status:
        if ... ticket.queue.escalate_days == 0 → ('no_sla', None, None)
        """
        ticket = Ticket.objects.create(
            title="Escalate Zero Queue Ticket",
            queue=self.queue_no_sla_zero,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "no_sla")
        self.assertIsNone(deadline)
        self.assertIsNone(time_remaining)

    @freeze_time("2026-06-11 10:00:00")
    def test_no_sla_queue_escalate_days_unset(self):
        """Queue without escalate_days field configured (default None) must return 'no_sla'.

        Simulates a queue created without specifying escalate_days at all.
        The model default is null=True so it defaults to None in DB.
        """
        self.assertIsNone(self.queue_no_sla_field.escalate_days)
        ticket = Ticket.objects.create(
            title="Escalate Unset Queue Ticket",
            queue=self.queue_no_sla_field,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "no_sla")
        self.assertIsNone(deadline)
        self.assertIsNone(time_remaining)

    @freeze_time("2026-06-11 10:00:00")
    def test_no_sla_queue_with_priority_1_still_no_sla(self):
        """Even excluded ticket must return 'no_sla' if queue has no SLA config.

        Critical boundary: queue.escalate_days check is evaluated BEFORE
        the priority/exclusion check. This test guards against future
        reordering that would swap 'excluded' in place of 'no_sla'.
        """
        ticket = Ticket.objects.create(
            title="No SLA But Priority 1",
            queue=self.queue_no_sla_none,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=1,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "no_sla")
        self.assertIsNone(deadline)
        self.assertIsNone(time_remaining)

    @freeze_time("2026-06-11 10:00:00")
    def test_no_sla_queue_escalate_days_negative(self):
        """Queue with escalate_days=-1 (or any negative) must return 'no_sla'.

        This is a guard against misconfigured queues (e.g. admin mistakenly
        entered a negative value). Before the fix, negative values would
        silently pass the `== 0 or is None` check and produce undefined
        behavior in calculate_sla_deadline.
        """
        ticket = Ticket.objects.create(
            title="Escalate Negative Queue Ticket",
            queue=self.queue_negative_sla,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "no_sla")
        self.assertIsNone(deadline)
        self.assertIsNone(time_remaining)

    @freeze_time("2026-06-11 10:00:00")
    def test_sla_deadline_with_negative_escalate_days(self):
        """calculate_sla_deadline must return None for negative escalate_days.

        Guards against the same boundary at the lower-level function, since
        calculate_sla_deadline can be called independently by other code.
        """
        ticket = Ticket.objects.create(
            title="Negative Escalate Deadline Check",
            queue=self.queue_negative_sla,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        self.assertIsNone(calculate_sla_deadline(ticket))

    @freeze_time("2026-06-11 10:00:00")
    def test_no_sla_queue_escalate_days_negative_extreme(self):
        """Even extreme negative escalate_days (e.g. -999) must safely return no_sla.

        Ensures the guard uses <= 0 comparison, not a specific sentinel value.
        """
        queue_extreme_neg = Queue.objects.create(
            title="Extreme Negative SLA",
            slug="extreme-neg-sla",
            escalate_days=-999,
        )
        ticket = Ticket.objects.create(
            title="Extreme Neg SLA Ticket",
            queue=queue_extreme_neg,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "no_sla")
        self.assertIsNone(deadline)
        self.assertIsNone(time_remaining)

    # --- Group 2: Priority edge cases (unspecified / abnormal values) ---

    @freeze_time("2026-06-11 10:00:00")
    def test_priority_not_1_not_excluded_priority_2(self):
        """Priority 2 is NOT excluded (only priority == 1 is excluded)."""
        created = timezone.make_aware(datetime(2026, 6, 11, 9, 0, 0))
        ticket = Ticket.objects.create(
            title="Priority 2 Ticket",
            queue=self.queue_with_sla,
            created=created,
            status=Ticket.OPEN_STATUS,
            priority=2,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertNotEqual(status, "excluded")
        self.assertIn(status, ("ok", "warning", "overdue"))
        self.assertIsNotNone(deadline)

    @freeze_time("2026-06-11 10:00:00")
    def test_priority_highest_choice_5_not_excluded(self):
        """Priority 5 (lowest configured choice) must NOT be excluded.

        Verifies that the check is strictly `priority == 1` and not
        e.g. `priority <= 1` or `priority in some range`.
        """
        created = timezone.make_aware(datetime(2026, 6, 11, 9, 0, 0))
        ticket = Ticket.objects.create(
            title="Priority 5 Ticket",
            queue=self.queue_with_sla,
            created=created,
            status=Ticket.OPEN_STATUS,
            priority=5,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertNotEqual(status, "excluded")
        self.assertIn(status, ("ok", "warning", "overdue"))
        self.assertIsNotNone(deadline)

    @freeze_time("2026-06-11 10:00:00")
    def test_priority_out_of_range_high_not_excluded(self):
        """Priority beyond configured range (e.g. 99) must NOT be excluded.

        Guards against regression where future code might add
        `priority not in ALLOWED_RANGE → excluded` without SLA review.
        Out-of-range values should still get an SLA deadline.
        """
        created = timezone.make_aware(datetime(2026, 6, 11, 9, 0, 0))
        ticket = Ticket.objects.create(
            title="Out-of-range High Priority",
            queue=self.queue_with_sla,
            created=created,
            status=Ticket.OPEN_STATUS,
            priority=99,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertNotEqual(status, "excluded")
        self.assertIn(status, ("ok", "warning", "overdue"))
        self.assertIsNotNone(deadline)

    @freeze_time("2026-06-11 10:00:00")
    def test_priority_zero_not_excluded(self):
        """Priority=0 (common default-injection bug) must NOT be excluded.

        Priority 0 is an edge case that can arise from:
        - Form field default mishaps
        - Integer coercion of empty strings
        - Legacy data migration errors
        It must still get SLA evaluated, not silently excluded.
        """
        created = timezone.make_aware(datetime(2026, 6, 11, 9, 0, 0))
        ticket = Ticket.objects.create(
            title="Priority Zero Ticket",
            queue=self.queue_with_sla,
            created=created,
            status=Ticket.OPEN_STATUS,
            priority=0,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertNotEqual(status, "excluded")
        self.assertIn(status, ("ok", "warning", "overdue"))
        self.assertIsNotNone(deadline)

    # --- Group 3: on_hold edge cases ---

    @freeze_time("2026-06-11 10:00:00")
    def test_on_hold_explicit_false_not_excluded(self):
        """on_hold explicitly set to False must NOT be excluded.

        Guards against broken truthy checks like `if ticket.on_hold:`
        that would treat False correctly but fail on None differently.
        The code uses: `is_on_hold = ticket.on_hold is not None and ticket.on_hold`
        so both False and None → not excluded → confirmed.
        """
        created = timezone.make_aware(datetime(2026, 6, 11, 9, 0, 0))
        ticket = Ticket.objects.create(
            title="Explicit on_hold=False",
            queue=self.queue_with_sla,
            created=created,
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertNotEqual(status, "excluded")
        self.assertIn(status, ("ok", "warning", "overdue"))
        self.assertIsNotNone(deadline)

    @freeze_time("2026-06-11 10:00:00")
    def test_on_hold_default_false_not_excluded(self):
        """on_hold left unset (DB default False) must NOT be excluded.

        on_hold is a BooleanField with default=False and no null=True,
        so it can never literally be None in the database. However the
        status function still guards with `on_hold is not None and on_hold`
        for defensive programming (e.g. unsaved instances, mocked data).
        This test verifies the common case: creating a ticket without
        specifying on_hold triggers the default, and the ticket is SLAed.
        """
        created = timezone.make_aware(datetime(2026, 6, 11, 9, 0, 0))
        ticket = Ticket.objects.create(
            title="on_hold=Default (not set)",
            queue=self.queue_with_sla,
            created=created,
            status=Ticket.OPEN_STATUS,
            priority=3,
        )
        self.assertIs(ticket.on_hold, False)
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertNotEqual(status, "excluded")
        self.assertIn(status, ("ok", "warning", "overdue"))
        self.assertIsNotNone(deadline)

    @freeze_time("2026-06-11 10:00:00")
    def test_on_hold_true_excluded_even_with_overdue(self):
        """on_hold=True must return 'excluded' even if ticket would be overdue.

        Confirms exclusion short-circuits SLA calculation entirely.
        Without exclusion: created=June 1 → deadline=June 4 → way overdue.
        With on_hold=True → excluded regardless of SLA timing.
        """
        ticket = Ticket.objects.create(
            title="On-Hold Would-Be-Overdue Ticket",
            queue=self.queue_with_sla,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=True,
        )
        status, deadline, time_remaining = get_ticket_sla_status(ticket)
        self.assertEqual(status, "excluded")
        self.assertIsNone(deadline)
        self.assertIsNone(time_remaining)

    @freeze_time("2026-06-11 10:00:00")
    def test_in_memory_ticket_on_hold_none_safe(self):
        """Defensive: an in-memory ticket with on_hold=None must not crash.

        In normal DB flow on_hold is never None (BooleanField NOT NULL).
        But the guard `ticket.on_hold is not None and ticket.on_hold` was
        written specifically for edge cases like unsaved instances or
        mocked objects. This test exercises that branch to keep the guard
        honest during future refactors.
        """
        ticket = Ticket(
            title="In-memory ticket",
            queue=self.queue_with_sla,
            created=timezone.make_aware(datetime(2026, 6, 11, 9, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
        )
        object.__setattr__(ticket, "on_hold", None)
        try:
            status, deadline, time_remaining = get_ticket_sla_status(ticket)
        except Exception as exc:
            self.fail(
                f"get_ticket_sla_status raised on in-memory on_hold=None: {exc!r}"
            )
        self.assertNotEqual(
            status, "excluded",
            "on_hold=None must not trigger the exclusion branch"
        )
        self.assertIn(status, ("ok", "warning", "overdue"))
        self.assertIsNotNone(deadline)


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


class SlaAlertViewPermissionTestCase(TestCase):
    """Integration tests for sla_alert view access control.

    Covers three critical entry paths that previously lacked regression
    protection:

    1. Anonymous (not logged in) users must be blocked.
    2. Authenticated users without helpdesk-staff permission must be blocked.
    3. A staff user who (under per-queue permission mode) has access to ZERO
       queues must still get a well-formed 200 response with empty data, not
       a server crash or permission bypass.
    """

    fixtures = ["emailtemplate.json"]

    def setUp(self):
        from django.contrib.auth import get_user_model
        from django.test.client import Client
        from helpdesk import settings as helpdesk_settings

        self._saved_per_queue_setting = (
            helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION
        )
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = False

        User = get_user_model()
        self.staff_user = User.objects.create(
            username="staff_user", is_staff=True
        )
        self.staff_user.set_password("pass")
        self.staff_user.save()

        self.non_staff_user = User.objects.create(
            username="regular_user", is_staff=False, is_active=True
        )
        self.non_staff_user.set_password("pass")
        self.non_staff_user.save()

        self.queue = Queue.objects.create(
            title="Test Queue",
            slug="perm-test-queue",
            escalate_days=3,
        )

        self.client = Client()

    def tearDown(self):
        from helpdesk import settings as helpdesk_settings

        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = (
            self._saved_per_queue_setting
        )

    # --- Group 1: Unauthenticated (anonymous) access ---

    def test_anonymous_user_redirected_to_login(self):
        """Anonymous user requesting sla_alert must be redirected to login.

        Guards the outermost @helpdesk_staff_member_required decorator.
        A regression here would mean a full permission bypass allowing any
        random web visitor to read the SLA dashboard.
        """
        from django.urls import reverse

        self.client.logout()
        response = self.client.get(reverse("helpdesk:sla_alert"))
        self.assertEqual(
            response.status_code,
            302,
            "Anonymous users must be redirected (HTTP 302), not served the view.",
        )
        location = response.get("Location", "")
        self.assertTrue(
            "login" in location.lower() or location.startswith("/"),
            f"Redirect target should point to login page, got: {location!r}",
        )

    def test_anonymous_user_has_no_access_to_context(self):
        """Anonymous user must not receive any ticket context.

        Secondary check ensuring the redirect happens before the view body
        runs (i.e. no partial data leak on error pages or misconfigured
        decorator stacking).
        """
        from django.urls import reverse

        self.client.logout()
        response = self.client.get(reverse("helpdesk:sla_alert"))
        self.assertIsNone(
            getattr(response, "context", None)
            or (response.context if hasattr(response, "context") and response.status_code != 302 else None),
            "Anonymous response must not carry template context.",
        )

    # --- Group 2: Authenticated but not staff ---

    def test_authenticated_non_staff_user_forbidden(self):
        """Logged-in user without is_staff flag must be blocked (redirected).

        The view is decorated twice:
          1. @helpdesk_staff_member_required  (outermost, via user_passes_test)
          2. staff_member_required()          (applied manually at the bottom)
        The outermost decorator runs first and rejects non-staff users by
        redirecting them to the login page (HTTP 302). This is consistent
        with how Django's user_passes_test decorator handles failed tests.
        Either way, the critical security guarantee is: the view body
        never executes for non-staff users.
        """
        from django.urls import reverse

        self.client.login(username="regular_user", password="pass")
        response = self.client.get(reverse("helpdesk:sla_alert"))
        self.assertIn(
            response.status_code,
            (302, 403),
            "Non-staff users must not reach the view body (expected 302 or 403).",
        )
        if response.status_code == 302:
            location = response.get("Location", "")
            self.assertTrue(
                "login" in location.lower() or location.startswith("/"),
                f"Redirect must be to login, got: {location!r}",
            )

    def test_inactive_user_treated_as_unauthenticated(self):
        """An inactive user must not be allowed through (redirected, 302).

        The staff check first gates on is_authenticated AND is_active; an
        inactive account should not even reach the 403 branch.
        """
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        User = get_user_model()
        inactive = User.objects.create(
            username="inactive_user", is_staff=True, is_active=False
        )
        inactive.set_password("pass")
        inactive.save()

        self.client.login(username="inactive_user", password="pass")
        response = self.client.get(reverse("helpdesk:sla_alert"))
        self.assertEqual(
            response.status_code,
            302,
            "Inactive users must be redirected to login (HTTP 302).",
        )

    # --- Group 3: Staff user with zero accessible queues ---

    @freeze_time("2026-06-11 10:00:00")
    def test_staff_with_no_queue_access_returns_200_empty(self):
        """Under per-queue permission, staff with zero queues gets HTTP 200 with empty data.

        This is the "empty-queue degradation" scenario: the user is a valid
        staff member, but HelpdeskUser.get_queues() returns an empty queryset.
        The view must:
          - return 200 (not 403, 404, or 500)
          - carry all expected context keys
          - show 0 tickets in every bucket
        A regression here typically surfaces as a server crash when the
        template iterates over None / unexpected empty data.
        """
        from django.contrib.auth import get_user_model
        from django.urls import reverse
        from helpdesk import settings as helpdesk_settings
        from helpdesk.models import Ticket

        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = True

        # A staff user who has NOT been granted any queue permission.
        # Under per-queue mode they will see zero queues.
        User = get_user_model()
        noqueue_staff = User.objects.create(
            username="noqueue_staff", is_staff=True, is_active=True
        )
        noqueue_staff.set_password("pass")
        noqueue_staff.save()

        # Seed a ticket in the unrelated queue to confirm it is NOT leaked
        Ticket.objects.create(
            title="Other Queue Ticket",
            queue=self.queue,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
        )

        self.client.logout()
        self.client.login(username="noqueue_staff", password="pass")
        response = self.client.get(reverse("helpdesk:sla_alert"))

        self.assertEqual(
            response.status_code,
            200,
            "Staff with no queues must still receive HTTP 200 (graceful degradation).",
        )
        self.assertEqual(
            response.context["total_count"],
            0,
            "Staff with no queues must see total_count=0 (no data leak).",
        )
        self.assertEqual(response.context["overdue_count"], 0)
        self.assertEqual(response.context["warning_count"], 0)
        self.assertEqual(response.context["excluded_count"], 0)
        self.assertEqual(response.context["ok_count"], 0)
        self.assertEqual(
            response.context["display_tickets"],
            [],
            "display_tickets must be an empty list for the zero-queue case.",
        )

    @freeze_time("2026-06-11 10:00:00")
    def test_staff_with_partial_queue_access_no_data_leak(self):
        """Staff with permission to queue_A must not see tickets in queue_B.

        Validates that the `queue__in=user_queues` filter is actually wired
        up correctly — i.e. the per-queue permission setting really does
        restrict the ticket queryset, not just the dropdown choices.
        """
        from django.contrib.auth import get_user_model
        from django.contrib.auth.models import Permission
        from django.urls import reverse
        from helpdesk import settings as helpdesk_settings
        from helpdesk.models import Ticket

        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = True

        queue_a = Queue.objects.create(
            title="Queue A", slug="perm-queue-a", escalate_days=3
        )
        queue_b = Queue.objects.create(
            title="Queue B", slug="perm-queue-b", escalate_days=3
        )

        User = get_user_model()
        user_a = User.objects.create(
            username="user_a_only", is_staff=True, is_active=True
        )
        user_a.set_password("pass")
        user_a.save()

        # Grant permission ONLY for queue_a (strip "helpdesk." prefix)
        perm_a = Permission.objects.get(codename=queue_a.permission_name[9:])
        user_a.user_permissions.add(perm_a)

        ticket_a = Ticket.objects.create(
            title="Ticket in A",
            queue=queue_a,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
        )
        ticket_b = Ticket.objects.create(
            title="Ticket in B",
            queue=queue_b,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
        )

        self.client.login(username="user_a_only", password="pass")
        response = self.client.get(reverse("helpdesk:sla_alert"))
        self.assertEqual(response.status_code, 200)

        visible_ids = {t.id for t in response.context["display_tickets"]}
        self.assertIn(
            ticket_a.id,
            visible_ids,
            "User must see tickets in queues they have permission for.",
        )
        self.assertNotIn(
            ticket_b.id,
            visible_ids,
            "User must NOT see tickets in queues they lack permission for (data leak).",
        )


class SlaTicketDetailE2ETestCase(TestCase):
    """End-to-end tests verifying the SLA status is correctly wired into the
    ticket detail view (view_ticket).

    These tests validate the full path:
        Ticket in DB → view_ticket() view → get_ticket_sla_status() called
        → sla_* variables rendered into template context.
    """

    fixtures = ["emailtemplate.json"]

    def setUp(self):
        from django.contrib.auth import get_user_model
        from django.test.client import Client

        User = get_user_model()
        self.staff = User.objects.create(username="staff_detail", is_staff=True)
        self.staff.set_password("pass")
        self.staff.save()

        self.queue_sla = Queue.objects.create(
            title="E2E SLA Queue", slug="e2e-sla-q", escalate_days=3
        )
        self.queue_no_sla = Queue.objects.create(
            title="E2E No-SLA Queue", slug="e2e-nosla-q", escalate_days=None
        )

        self.client = Client()
        self.client.login(username="staff_detail", password="pass")

    # --- Detail-view SLA context injection ---

    @freeze_time("2026-06-13 12:00:00")
    def test_detail_view_shows_overdue_sla_status(self):
        """A ticket that has passed its SLA deadline must surface status='overdue'
        on the detail page."""
        from django.urls import reverse

        ticket = Ticket.objects.create(
            title="Overdue detail ticket",
            queue=self.queue_sla,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        resp = self.client.get(reverse("helpdesk:view", args=[ticket.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["sla_status"], "overdue")
        self.assertIsNotNone(resp.context["sla_deadline"])
        self.assertTrue(
            resp.context["sla_time_remaining"].total_seconds() < 0,
            "Overdue ticket must carry a negative time_remaining timedelta.",
        )

    @freeze_time("2026-06-11 09:00:00")
    def test_detail_view_shows_ok_sla_status(self):
        """A ticket comfortably inside its SLA window must show status='ok'."""
        from django.urls import reverse

        # Created 4 full days before the deadline gives us ~4d remaining, well over 24h.
        ticket = Ticket.objects.create(
            title="OK detail ticket",
            queue=self.queue_sla,
            created=timezone.make_aware(datetime(2026, 6, 10, 9, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        resp = self.client.get(reverse("helpdesk:view", args=[ticket.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["sla_status"], "ok")
        self.assertIsNotNone(resp.context["sla_deadline"])
        self.assertTrue(
            resp.context["sla_time_remaining"].total_seconds() >= 86400,
            "OK ticket must have >= 24h remaining on the detail page.",
        )

    @freeze_time("2026-06-14 09:00:00")
    def test_detail_view_shows_warning_sla_status(self):
        """A ticket within 24 hours of its deadline must show status='warning'.

        With escalate_days=3, created=June 11 10:00 gives deadline=June 14 10:00.
        Freeze to June 14 09:00 → exactly 1 hour (3600s) remaining, which is
        between 0 ≤ remaining < 86400 → status='warning'.
        """
        from django.urls import reverse

        ticket = Ticket.objects.create(
            title="Warning detail ticket",
            queue=self.queue_sla,
            created=timezone.make_aware(datetime(2026, 6, 11, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        resp = self.client.get(reverse("helpdesk:view", args=[ticket.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["sla_status"], "warning")
        remaining_sec = resp.context["sla_time_remaining"].total_seconds()
        self.assertGreaterEqual(
            remaining_sec, 0, "Warning ticket must not yet be overdue."
        )
        self.assertLess(
            remaining_sec,
            86400,
            "Warning ticket must have fewer than 24h of SLA time remaining.",
        )

    @freeze_time("2026-06-11 10:00:00")
    def test_detail_view_shows_excluded_sla_status(self):
        """An on-hold ticket must surface status='excluded' on the detail page."""
        from django.urls import reverse

        ticket = Ticket.objects.create(
            title="Excluded detail ticket",
            queue=self.queue_sla,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=True,
        )
        resp = self.client.get(reverse("helpdesk:view", args=[ticket.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["sla_status"], "excluded")
        self.assertIsNone(resp.context["sla_deadline"])
        self.assertIsNone(resp.context["sla_time_remaining"])

    @freeze_time("2026-06-11 10:00:00")
    def test_detail_view_shows_no_sla_for_unconfigured_queue(self):
        """A ticket in a queue without escalate_days must show status='no_sla'
        on the detail page, with no deadline or time-remaining payload."""
        from django.urls import reverse

        ticket = Ticket.objects.create(
            title="No-SLA detail ticket",
            queue=self.queue_no_sla,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        resp = self.client.get(reverse("helpdesk:view", args=[ticket.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["sla_status"], "no_sla")
        self.assertIsNone(resp.context["sla_deadline"])
        self.assertIsNone(resp.context["sla_time_remaining"])


class SlaTicketListAndAlertE2ETestCase(TestCase):
    """End-to-end tests verifying the ticket list / SLA alert view entry points.

    Validates cross-view consistency: the same ticket set must produce the
    same SLA bucket counts whether viewed from the dedicated sla_alert page
    or via the list view's default open-ticket query.
    """

    fixtures = ["emailtemplate.json"]

    def setUp(self):
        from django.contrib.auth import get_user_model
        from django.test.client import Client

        User = get_user_model()
        self.staff = User.objects.create(username="staff_list", is_staff=True)
        self.staff.set_password("pass")
        self.staff.save()

        self.queue = Queue.objects.create(
            title="List E2E Queue", slug="list-e2e-q", escalate_days=3
        )

        self.client = Client()
        self.client.login(username="staff_list", password="pass")

    @freeze_time("2026-06-13 12:00:00")
    def test_sla_alert_summary_counts_match_ticket_population(self):
        """The sla_alert view summary counters must agree with the tickets
        actually present. Seed one ticket per category and verify counts."""
        from django.urls import reverse

        # Overdue: created 2026-06-01, way past 3-day SLA
        Ticket.objects.create(
            title="E2E Overdue",
            queue=self.queue,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        # Warning: created 2026-06-12 14:00 (less than 24h remain on 2026-06-13 12:00
        # with escalate_days=3 → deadline is June 15 14:00 which is > 24h away.
        # So instead make created=June 9 15:00 → deadline around June 12 15:00,
        # then now=June 13 12:00 → already past deadline (overdue). Hmm.
        # Simpler: created June 11 15:00 → deadline around June 14 15:00.
        # now=June 14 10:00 would give 5h remaining = warning. But we need
        # overdue + warning + 2x excluded, so change freeze_time.
        Ticket.objects.create(
            title="E2E Warning",
            queue=self.queue,
            created=timezone.make_aware(datetime(2026, 6, 11, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        # Excluded: on_hold=True
        Ticket.objects.create(
            title="E2E Excluded",
            queue=self.queue,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=True,
        )
        # Excluded: priority=1
        Ticket.objects.create(
            title="E2E Excluded Priority 1",
            queue=self.queue,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=1,
            on_hold=False,
        )

        # With escalate_days=3, created June 11 10:00 deadline=June 14 10:00.
        # Running at June 13 12:00 → 22h remaining < 24h → warning. ✓
        resp = self.client.get(reverse("helpdesk:sla_alert"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 4)
        self.assertEqual(resp.context["overdue_count"], 1)
        self.assertEqual(resp.context["warning_count"], 1)
        self.assertEqual(resp.context["excluded_count"], 2)

        statuses = {t.sla_status for t in resp.context["display_tickets"]}
        self.assertEqual(
            statuses,
            {"overdue", "warning", "excluded"},
            "SLA alert page must carry the full expected status set.",
        )

    @freeze_time("2026-06-13 12:00:00")
    def test_sla_alert_filter_by_priority(self):
        """SLA alert page must honour the priority filter via query string."""
        from django.urls import reverse

        Ticket.objects.create(
            title="Priority 3 Overdue",
            queue=self.queue,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
        )
        Ticket.objects.create(
            title="Priority 5 Overdue",
            queue=self.queue,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=5,
        )

        resp = self.client.get(
            reverse("helpdesk:sla_alert") + "?priority=3"
        )
        self.assertEqual(resp.status_code, 200)
        titles = {t.title for t in resp.context["display_tickets"]}
        self.assertIn("Priority 3 Overdue", titles)
        self.assertNotIn("Priority 5 Overdue", titles)

    @freeze_time("2026-06-13 12:00:00")
    def test_sla_alert_filter_by_unassigned_owner(self):
        """SLA alert page must return only unassigned tickets when owner=-1."""
        from django.contrib.auth import get_user_model
        from django.urls import reverse

        User = get_user_model()
        owner = User.objects.create(username="owner_e2e", is_staff=True)

        Ticket.objects.create(
            title="Unassigned ticket",
            queue=self.queue,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
        )
        Ticket.objects.create(
            title="Assigned ticket",
            queue=self.queue,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            assigned_to=owner,
        )

        resp = self.client.get(
            reverse("helpdesk:sla_alert") + "?assigned_to=-1"
        )
        self.assertEqual(resp.status_code, 200)
        titles = {t.title for t in resp.context["display_tickets"]}
        self.assertIn("Unassigned ticket", titles)
        self.assertNotIn("Assigned ticket", titles)


class SlaEscalateCommandE2ETestCase(TestCase):
    """End-to-end tests exercising the escalate_tickets management command.

    These tests exercise the full upgrade path:
        Tickets in DB → `manage.py escalate_tickets` → SLA threshold
        comparison against date.today() → tickets that breach the threshold
        get their priority bumped (or left alone when already at max) and
        receive a follow-up noting the escalation.
    """

    fixtures = ["emailtemplate.json"]

    def setUp(self):
        self.queue = Queue.objects.create(
            title="Escalate E2E Queue",
            slug="escalate-e2e-q",
            escalate_days=3,
        )
        self.queue_no_sla = Queue.objects.create(
            title="No Escalate Queue",
            slug="no-escalate-q",
            escalate_days=None,
        )

    @freeze_time("2026-06-11 10:00:00")
    def test_command_escalates_overdue_ticket_priority(self):
        """A ticket created far enough in the past must have its priority
        bumped by the escalate_tickets management command.

        NOTE: django-helpdesk treats smaller priority integers as MORE urgent,
        so "escalating priority" means `ticket.priority -= 1` (e.g. 3 → 2).
        """
        from django.core.management import call_command
        from io import StringIO
        from helpdesk.models import FollowUp

        ticket = Ticket.objects.create(
            title="Escalate Me",
            queue=self.queue,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        initial_priority = ticket.priority

        call_command("escalate_tickets", stdout=StringIO(), stderr=StringIO())

        ticket.refresh_from_db()
        self.assertLess(
            ticket.priority,
            initial_priority,
            "Overdue ticket's priority integer must DECREASE (3→2) because "
            "lower integer = higher urgency.",
        )
        self.assertEqual(
            ticket.priority,
            initial_priority - 1,
            "Priority must be decremented by exactly one level.",
        )

        followups = FollowUp.objects.filter(ticket=ticket)
        self.assertTrue(
            followups.exists(),
            "Escalation must produce at least one FollowUp entry.",
        )
        self.assertTrue(
            any("scalat" in f.comment.lower() for f in followups),
            "FollowUp comment must mention the escalation action.",
        )

    @freeze_time("2026-06-11 10:00:00")
    def test_command_skips_recent_ticket_within_sla(self):
        """A freshly-created ticket inside the SLA window must NOT be escalated."""
        from django.core.management import call_command
        from io import StringIO
        from helpdesk.models import FollowUp

        ticket = Ticket.objects.create(
            title="Do Not Escalate",
            queue=self.queue,
            created=timezone.make_aware(datetime(2026, 6, 11, 9, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        initial_priority = ticket.priority

        call_command("escalate_tickets", stdout=StringIO(), stderr=StringIO())

        ticket.refresh_from_db()
        self.assertEqual(
            ticket.priority,
            initial_priority,
            "Fresh ticket within SLA window must NOT be escalated.",
        )
        self.assertFalse(
            FollowUp.objects.filter(ticket=ticket).exists(),
            "Fresh ticket must not receive any FollowUp from the command.",
        )

    @freeze_time("2026-06-11 10:00:00")
    def test_command_skips_on_hold_ticket(self):
        """An on-hold ticket must be excluded from escalation even when overdue."""
        from django.core.management import call_command
        from io import StringIO

        ticket = Ticket.objects.create(
            title="On-hold Not Escalated",
            queue=self.queue,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=True,
        )
        initial_priority = ticket.priority

        call_command("escalate_tickets", stdout=StringIO(), stderr=StringIO())

        ticket.refresh_from_db()
        self.assertEqual(
            ticket.priority,
            initial_priority,
            "On-hold ticket must never be escalated.",
        )

    @freeze_time("2026-06-11 10:00:00")
    def test_command_skips_closed_ticket(self):
        """A closed ticket must be excluded from escalation even when overdue."""
        from django.core.management import call_command
        from io import StringIO

        ticket = Ticket.objects.create(
            title="Closed Not Escalated",
            queue=self.queue,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.CLOSED_STATUS,
            priority=3,
            on_hold=False,
        )
        initial_priority = ticket.priority

        call_command("escalate_tickets", stdout=StringIO(), stderr=StringIO())

        ticket.refresh_from_db()
        self.assertEqual(
            ticket.priority,
            initial_priority,
            "Closed ticket must not be escalated regardless of age.",
        )

    @freeze_time("2026-06-11 10:00:00")
    def test_command_skips_queue_without_sla_config(self):
        """Tickets in a queue without escalate_days must never be escalated."""
        from django.core.management import call_command
        from io import StringIO

        ticket = Ticket.objects.create(
            title="No-SLA Queue Ticket",
            queue=self.queue_no_sla,
            created=timezone.make_aware(datetime(2026, 6, 1, 10, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        initial_priority = ticket.priority

        call_command("escalate_tickets", stdout=StringIO(), stderr=StringIO())

        ticket.refresh_from_db()
        self.assertEqual(
            ticket.priority,
            initial_priority,
            "Ticket in queue with no SLA config must not be escalated.",
        )

    @freeze_time("2026-06-11 10:00:00")
    def test_command_respects_escalation_exclusion_dates(self):
        """EscalationExclusion entries must be subtracted from the working-day
        window, effectively giving the ticket more grace time.

        Management command logic (today=June 11, escalate_days=3):
          last = June 11 - 3 days = June 8
          Iterate June 8, 9, 10 (workdate < today)
          If all three dates are EscalationExclusion → working_days = 0
          req_last_escl_date = June 11 10:00 - 0 days = June 11 10:00
          → a ticket created June 11 09:00 (<= June 11 10:00) would still escalate.

        Instead we want the ticket protected. With 2 exclusion days in the
        3-day window: working_days = 1, req_last_escl_date = June 10 10:00.
        A ticket created June 10 11:00 is NOT <= that threshold → spared.
        """
        from django.core.management import call_command
        from io import StringIO
        from helpdesk.models import EscalationExclusion

        # Exclude two of the three days in the command's look-back window
        EscalationExclusion.objects.create(date=date(2026, 6, 8))
        EscalationExclusion.objects.create(date=date(2026, 6, 9))
        # June 10 remains a working day → working_days = 1
        # → req_last_escl_date = June 11 10:00 - 1 day = June 10 10:00

        ticket = Ticket.objects.create(
            title="Exclusion-Protected Ticket",
            queue=self.queue,
            created=timezone.make_aware(datetime(2026, 6, 10, 11, 0, 0)),
            status=Ticket.OPEN_STATUS,
            priority=3,
            on_hold=False,
        )
        initial_priority = ticket.priority

        call_command("escalate_tickets", stdout=StringIO(), stderr=StringIO())

        ticket.refresh_from_db()
        self.assertEqual(
            ticket.priority,
            initial_priority,
            "Ticket within SLA window after subtracting exclusion days "
            "must NOT be escalated.",
        )
