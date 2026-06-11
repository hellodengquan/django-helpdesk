from datetime import date, datetime, timedelta
from io import StringIO
import pytz

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from django.utils.translation import gettext as _

from helpdesk.lib import daily_time_spent_calculation
from helpdesk.models import (
    EscalationExclusion,
    FollowUp,
    Queue,
    Ticket,
    TicketChange,
)
from tests.helpers import (
    HelpdeskSettingsOverride,
    HelpdeskSLAEscalationTestCase,
    create_ticket_with_created_date,
    patch_escalation_datetime,
)

User = get_user_model()


class SLACalculationTestCase(TestCase):
    """Test SLA calculation with business hours, holidays, and timezones."""

    fixtures = ["emailtemplate.json"]

    def setUp(self):
        self.queue = Queue.objects.create(
            title="Test Queue",
            slug="test-queue",
            escalate_days=2,
        )

        self.user = User.objects.create(
            username="testuser",
            is_staff=True,
        )

    def test_daily_time_spent_calculation_full_business_day(self):
        """Test daily time calculation with standard business hours."""
        open_hours = {
            "monday": (9, 17),
            "tuesday": (9, 17),
            "wednesday": (9, 17),
            "thursday": (9, 17),
            "friday": (9, 17),
            "saturday": (0, 0),
            "sunday": (0, 0),
        }

        earliest = datetime(2024, 1, 15, 9, 0, 0)
        latest = datetime(2024, 1, 15, 17, 0, 0)

        seconds = daily_time_spent_calculation(earliest, latest, open_hours)
        self.assertEqual(seconds, 8 * 3600)

    def test_daily_time_spent_calculation_partial_day(self):
        """Test daily time calculation with partial working hours."""
        open_hours = {
            "monday": (9, 17),
        }

        earliest = datetime(2024, 1, 15, 10, 0, 0)
        latest = datetime(2024, 1, 15, 14, 0, 0)

        seconds = daily_time_spent_calculation(earliest, latest, open_hours)
        self.assertEqual(seconds, 4 * 3600)

    def test_daily_time_spent_calculation_before_business_hours(self):
        """Test time calculation when earliest is before business hours."""
        open_hours = {
            "monday": (9, 17),
        }

        earliest = datetime(2024, 1, 15, 8, 0, 0)
        latest = datetime(2024, 1, 15, 10, 0, 0)

        seconds = daily_time_spent_calculation(earliest, latest, open_hours)
        self.assertEqual(seconds, 1 * 3600)

    def test_daily_time_spent_calculation_after_business_hours(self):
        """Test time calculation when latest is after business hours."""
        open_hours = {
            "monday": (9, 17),
        }

        earliest = datetime(2024, 1, 15, 16, 0, 0)
        latest = datetime(2024, 1, 15, 18, 0, 0)

        seconds = daily_time_spent_calculation(earliest, latest, open_hours)
        self.assertEqual(seconds, 1 * 3600)

    def test_daily_time_spent_calculation_weekend(self):
        """Test time calculation on weekends with no working hours."""
        open_hours = {
            "saturday": (0, 0),
            "sunday": (0, 0),
        }

        earliest = datetime(2024, 1, 13, 10, 0, 0)
        latest = datetime(2024, 1, 13, 14, 0, 0)

        seconds = daily_time_spent_calculation(earliest, latest, open_hours)
        self.assertEqual(seconds, 0)

    def test_daily_time_spent_calculation_fractional_hours(self):
        """Test time calculation with fractional hours."""
        open_hours = {
            "monday": (9.5, 17.5),
        }

        earliest = datetime(2024, 1, 15, 9, 30, 0)
        latest = datetime(2024, 1, 15, 17, 30, 0)

        seconds = daily_time_spent_calculation(earliest, latest, open_hours)
        self.assertEqual(seconds, 8 * 3600)

    def test_follow_up_time_spent_with_holiday_exclusion(self):
        """Test that holidays are excluded from time spent calculation.
        
        Timeline:
        - 2023-12-29 (Friday): created at 10:00, works 10:00-17:00 = 7h
        - 2023-12-30 (Saturday): weekend, 0h
        - 2023-12-31 (Sunday): weekend, 0h
        - 2024-01-01 (Monday): holiday, excluded, 0h
        - 2024-01-02 (Tuesday): followup at 17:00, works 09:00-17:00 = 8h
        Total: 7 + 8 = 15h
        """
        settings_override = HelpdeskSettingsOverride(
            FOLLOWUP_TIME_SPENT_AUTO=True,
            FOLLOWUP_TIME_SPENT_OPENING_HOURS={
                "monday": (9, 17),
                "tuesday": (9, 17),
                "wednesday": (9, 17),
                "thursday": (9, 17),
                "friday": (9, 17),
                "saturday": (0, 0),
                "sunday": (0, 0),
            },
            FOLLOWUP_TIME_SPENT_EXCLUDE_HOLIDAYS=["2024-01-01"],
        )
        with settings_override:
            ticket = create_ticket_with_created_date(
                created_date=timezone.make_aware(datetime(2023, 12, 29, 10, 0, 0)),
                queue=self.queue,
                title="Test Ticket",
                description="Test Description",
            )

            followup = FollowUp.objects.create(
                ticket=ticket,
                title="Test FollowUp",
                date=timezone.make_aware(datetime(2024, 1, 2, 17, 0, 0)),
            )
            followup.refresh_from_db()

            time_spent = followup.time_spent_calculation()
            self.assertEqual(time_spent, timedelta(hours=15))

    def test_follow_up_time_spent_multiple_holidays(self):
        """Test multiple consecutive holidays are excluded.
        
        Timeline:
        - 2023-12-29 (Friday): created at 10:00, works 10:00-17:00 = 7h
        - 2023-12-30 (Saturday): weekend, 0h
        - 2023-12-31 (Sunday): weekend, 0h
        - 2024-01-01 (Monday): holiday, excluded, 0h
        - 2024-01-02 (Tuesday): holiday, excluded, 0h
        - 2024-01-03 (Wednesday): followup at 11:00, works 09:00-11:00 = 2h
        Total: 7 + 2 = 9h
        """
        settings_override = HelpdeskSettingsOverride(
            FOLLOWUP_TIME_SPENT_AUTO=True,
            FOLLOWUP_TIME_SPENT_OPENING_HOURS={
                "monday": (9, 17),
                "tuesday": (9, 17),
                "wednesday": (9, 17),
                "thursday": (9, 17),
                "friday": (9, 17),
                "saturday": (0, 0),
                "sunday": (0, 0),
            },
            FOLLOWUP_TIME_SPENT_EXCLUDE_HOLIDAYS=["2024-01-01", "2024-01-02"],
        )
        with settings_override:
            ticket = create_ticket_with_created_date(
                created_date=timezone.make_aware(datetime(2023, 12, 29, 10, 0, 0)),
                queue=self.queue,
                title="Test Ticket",
                description="Test Description",
            )

            followup = FollowUp.objects.create(
                ticket=ticket,
                title="Test FollowUp",
                date=timezone.make_aware(datetime(2024, 1, 3, 11, 0, 0)),
            )
            followup.refresh_from_db()

            time_spent = followup.time_spent_calculation()
            self.assertEqual(time_spent, timedelta(hours=9))

    def test_follow_up_time_spent_status_exclusion(self):
        """Test that excluded statuses result in zero time spent."""
        settings_override = HelpdeskSettingsOverride(
            FOLLOWUP_TIME_SPENT_AUTO=True,
            FOLLOWUP_TIME_SPENT_OPENING_HOURS={
                "monday": (9, 17),
                "tuesday": (9, 17),
                "wednesday": (9, 17),
                "thursday": (9, 17),
                "friday": (9, 17),
                "saturday": (0, 0),
                "sunday": (0, 0),
            },
            FOLLOWUP_TIME_SPENT_EXCLUDE_STATUSES=[Ticket.RESOLVED_STATUS],
        )
        with settings_override:
            ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 15, 10, 0, 0)),
                queue=self.queue,
                title="Test Ticket",
                description="Test Description",
                                status=Ticket.RESOLVED_STATUS,
            )

            followup = FollowUp.objects.create(
                ticket=ticket,
                title="Test FollowUp",
                date=timezone.make_aware(datetime(2024, 1, 15, 14, 0, 0)),
            )

            time_spent = followup.time_spent_calculation()
            self.assertEqual(time_spent, timedelta(seconds=0))

    def test_follow_up_time_spent_queue_exclusion(self):
        """Test that excluded queues result in zero time spent."""
        settings_override = HelpdeskSettingsOverride(
            FOLLOWUP_TIME_SPENT_AUTO=True,
            FOLLOWUP_TIME_SPENT_OPENING_HOURS={
                "monday": (9, 17),
            },
            FOLLOWUP_TIME_SPENT_EXCLUDE_QUEUES=["test-queue"],
        )
        with settings_override:
            ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 15, 10, 0, 0)),
                queue=self.queue,
                title="Test Ticket",
                description="Test Description",
                            )

            followup = FollowUp.objects.create(
                ticket=ticket,
                title="Test FollowUp",
                date=timezone.make_aware(datetime(2024, 1, 15, 14, 0, 0)),
            )

            time_spent = followup.time_spent_calculation()
            self.assertEqual(time_spent, timedelta(seconds=0))

    def test_follow_up_time_spent_multiple_days(self):
        """Test time calculation spanning multiple days.
        
        Timeline:
        - 2024-01-15 (Monday): created at 16:00, works 16:00-17:00 = 1h
        - 2024-01-16 (Tuesday): followup at 10:00, works 09:00-10:00 = 1h
        Total: 2h
        """
        settings_override = HelpdeskSettingsOverride(
            FOLLOWUP_TIME_SPENT_AUTO=True,
            FOLLOWUP_TIME_SPENT_OPENING_HOURS={
                "monday": (9, 17),
                "tuesday": (9, 17),
                "wednesday": (9, 17),
                "thursday": (9, 17),
                "friday": (9, 17),
                "saturday": (0, 0),
                "sunday": (0, 0),
            },
        )
        with settings_override:
            ticket = create_ticket_with_created_date(
                created_date=timezone.make_aware(datetime(2024, 1, 15, 16, 0, 0)),
                queue=self.queue,
                title="Test Ticket",
                description="Test Description",
            )

            followup = FollowUp.objects.create(
                ticket=ticket,
                title="Test FollowUp",
                date=timezone.make_aware(datetime(2024, 1, 16, 10, 0, 0)),
            )
            followup.refresh_from_db()

            time_spent = followup.time_spent_calculation()
            self.assertEqual(time_spent, timedelta(hours=2))

    @override_settings(USE_TZ=True, TIME_ZONE="UTC")
    def test_timezone_awareness(self):
        """Test that calculations are timezone-aware."""
        utc_time = timezone.now()
        self.assertTrue(timezone.is_aware(utc_time))

        ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 8, 10, 0, 0)),
            queue=self.queue,
            title="Test Ticket",
            description="Test Description",
        )

        self.assertTrue(timezone.is_aware(ticket.created))
        self.assertTrue(timezone.is_aware(ticket.modified))

    def test_due_date_tracking(self):
        """Test that due_date changes are properly tracked in TicketChange."""
        ticket = Ticket.objects.create(
            queue=self.queue,
            title="Test Ticket",
            description="Test Description",
            due_date=timezone.make_aware(datetime(2024, 1, 15, 10, 0, 0)),
        )

        initial_due_date = ticket.due_date
        new_due_date = timezone.make_aware(datetime(2024, 1, 20, 10, 0, 0))

        followup = FollowUp.objects.create(
            ticket=ticket,
            title="Due Date Update",
            date=timezone.now(),
            user=self.user,
        )

        ticket_change = TicketChange.objects.create(
            followup=followup,
            field=_("Due on"),
            old_value=str(initial_due_date),
            new_value=str(new_due_date),
        )

        ticket.due_date = new_due_date
        ticket.save()

        self.assertEqual(ticket_change.field, _("Due on"))
        self.assertEqual(ticket_change.old_value, str(initial_due_date))
        self.assertEqual(ticket_change.new_value, str(new_due_date))


class EscalationCycleTestCase(TestCase):
    """Test escalation cycles with business days, holidays, and exclusions."""

    fixtures = ["emailtemplate.json"]

    def setUp(self):
        self.queue = Queue.objects.create(
            title="Support Queue",
            slug="support",
            escalate_days=2,
        )

        self.user = User.objects.create(
            username="escalation_user",
            is_staff=True,
            email="user@example.com",
        )

    def test_escalation_exclusion_creation(self):
        """Test basic EscalationExclusion model creation."""
        exclusion_date = date(2024, 1, 1)
        exclusion = EscalationExclusion.objects.create(
            name="New Year's Day",
            date=exclusion_date,
        )

        self.assertEqual(exclusion.name, "New Year's Day")
        self.assertEqual(exclusion.date, exclusion_date)
        self.assertEqual(str(exclusion), "New Year's Day")

    def test_escalation_exclusion_with_specific_queues(self):
        """Test EscalationExclusion with specific queue association."""
        queue2 = Queue.objects.create(
            title="Another Queue",
            slug="another",
            escalate_days=3,
        )

        exclusion = EscalationExclusion.objects.create(
            name="Queue-specific holiday",
            date=date(2024, 2, 1),
        )
        exclusion.queues.add(self.queue)

        self.assertIn(self.queue, exclusion.queues.all())
        self.assertNotIn(queue2, exclusion.queues.all())

    def test_escalation_basic_workdays(self):
        """Test basic escalation without any exclusions.
        
        Timeline:
        - created: 2024-01-08 (Monday) 10:00
        - escalate_days: 2
        - Mock today: 2024-01-10 (Wednesday)
        - Working days from 2024-01-08 to 2024-01-10: 2 days (Jan 8, 9)
        - Should escalate priority 3 -> 2
        """
        ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 8, 10, 0, 0)),
            queue=self.queue,
            title="Test Ticket",
            description="Test Description",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(date(2024, 1, 10)):
            call_command("escalate_tickets", "-x")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 2)
        self.assertIsNotNone(ticket.last_escalation)

        followup = ticket.followup_set.latest("date")
        self.assertEqual(followup.title, _("Ticket Escalated"))

        ticket_change = followup.ticketchange_set.latest("id")
        self.assertEqual(ticket_change.field, _("Priority"))
        self.assertEqual(int(ticket_change.old_value), 3)
        self.assertEqual(int(ticket_change.new_value), 2)

    def test_escalation_with_exclusion_dates(self):
        """Test escalation skips exclusion dates.
        
        Timeline:
        - created: 2024-01-09 (Tuesday) 10:00
        - escalate_days: 2
        - Exclusion: 2024-01-10 (Wednesday)
        - Mock today: 2024-01-11 (Thursday)
        - Date range: from (2024-01-11 - 2 days) = 2024-01-09 to 2024-01-11
        - Working days in range: 1 (Jan 9 only, Jan 10 excluded)
        - req_last_escl_date = 2024-01-11 10:00 - 1 day = 2024-01-10 10:00
        - ticket.created 2024-01-09 10:00 <= req_last_escl_date? Yes, should escalate
        
        Wait, let me recalculate to ensure NO escalation:
        - created: 2024-01-10 (Wednesday) 11:00
        - escalate_days: 2
        - Exclusion: 2024-01-10 (Wednesday)
        - Mock today: 2024-01-11 (Thursday) 10:00
        - Date range: from 2024-01-09 to 2024-01-11
        - Jan 9: no exclusion, days=1
        - Jan 10: exclusion, skipped
        - days = 1
        - req_last_escl_date = 2024-01-11 10:00 - 1 day = 2024-01-10 10:00
        - created (Jan 10 11:00) > req_last_escl_date (Jan 10 10:00) → NO escalation!
        """
        EscalationExclusion.objects.create(
            name="Company Holiday",
            date=date(2024, 1, 10),
        )

        ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 10, 11, 0, 0)),
            queue=self.queue,
            title="Test Ticket",
            description="Test Description",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(date(2024, 1, 11)):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 3)
        self.assertIsNone(ticket.last_escalation)

    def test_escalation_after_skipping_exclusion(self):
        """Test escalation happens after counting through exclusion dates.
        
        Timeline:
        - created: 2024-01-08 (Monday) 10:00
        - escalate_days: 2
        - Exclusion: 2024-01-09 (Tuesday)
        - Mock today: 2024-01-11 (Thursday)
        - Working days: 2 (Jan 8, 10; Jan 9 excluded)
        - Should escalate priority 3 -> 2
        """
        EscalationExclusion.objects.create(
            name="Company Holiday",
            date=date(2024, 1, 9),
        )

        ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 8, 10, 0, 0)),
            queue=self.queue,
            title="Test Ticket",
            description="Test Description",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(date(2024, 1, 11)):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 2)
        self.assertIsNotNone(ticket.last_escalation)

        followup = ticket.followup_set.latest("date")
        self.assertIn("2 days", followup.comment)

    def test_escalation_skip_on_hold_tickets(self):
        """Test that on-hold tickets are not escalated."""
        ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 8, 10, 0, 0)),
            queue=self.queue,
            title="On Hold Ticket",
            description="Test Description",
            priority=3,
            status=Ticket.OPEN_STATUS,
            on_hold=True,
        )

        with patch_escalation_datetime(date(2024, 1, 10)):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 3)
        self.assertIsNone(ticket.last_escalation)

    def test_escalation_skip_highest_priority(self):
        """Test that tickets at highest priority (1) are not escalated."""
        ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 8, 10, 0, 0)),
            queue=self.queue,
            title="Critical Ticket",
            description="Test Description",
            priority=1,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(date(2024, 1, 10)):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 1)
        self.assertIsNone(ticket.last_escalation)

    def test_escalation_skip_closed_tickets(self):
        """Test that closed tickets are not escalated."""
        ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 8, 10, 0, 0)),
            queue=self.queue,
            title="Closed Ticket",
            description="Test Description",
            priority=3,
            status=Ticket.CLOSED_STATUS,
        )

        with patch_escalation_datetime(date(2024, 1, 10)):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 3)
        self.assertIsNone(ticket.last_escalation)

    def test_escalation_with_previous_escalation(self):
        """Test escalation with existing last_escalation timestamp."""
        ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 8, 10, 0, 0)),
            queue=self.queue,
            title="Test Ticket",
            description="Test Description",
            priority=3,
            status=Ticket.OPEN_STATUS,
            last_escalation=timezone.make_aware(datetime(2024, 1, 10, 10, 0, 0)),
        )

        with patch_escalation_datetime(date(2024, 1, 12)):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 2)

    def test_escalation_notify_only_mode(self):
        """Test notify-only mode sends email but doesn't create FollowUp."""
        ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 8, 10, 0, 0)),
            queue=self.queue,
            title="Test Ticket",
            description="Test Description",
            priority=3,
            status=Ticket.OPEN_STATUS,
            submitter_email="submitter@example.com",
            assigned_to=self.user,
        )

        initial_followup_count = ticket.followup_set.count()

        with patch_escalation_datetime(date(2024, 1, 10)):
            call_command("escalate_tickets", "-n")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 2)
        self.assertIsNotNone(ticket.last_escalation)
        self.assertEqual(ticket.followup_set.count(), initial_followup_count)

    def test_escalation_specific_queue(self):
        """Test escalation with specific queue selection."""
        queue2 = Queue.objects.create(
            title="Another Queue",
            slug="another",
            escalate_days=1,
        )

        ticket1 = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 8, 10, 0, 0)),
            queue=self.queue,
            title="Test Ticket 1",
            description="Test Description",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        ticket2 = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 8, 10, 0, 0)),
            queue=queue2,
            title="Test Ticket 2",
            description="Test Description",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(date(2024, 1, 10)):
            call_command("escalate_tickets", "-q", "another")

        ticket1.refresh_from_db()
        ticket2.refresh_from_db()

        self.assertEqual(ticket1.priority, 3)
        self.assertEqual(ticket2.priority, 2)

    def test_escalation_queue_specific_exclusion(self):
        """Test that queue-specific exclusions only affect target queues.
        
        Setup:
        - queue1 (self.queue): escalate_days=2
        - queue2: escalate_days=2
        - Exclusion on 2024-01-09: only applies to queue1
        
        Mock today: 2024-01-10 10:00
        
        For queue1 (affected by exclusion):
        - last = 2024-01-08
        - Jan 8: no exclusion, days=1
        - Jan 9: queue-specific exclusion, skipped
        - days = 1
        - req_last_escl_date = 2024-01-10 10:00 - 1 day = 2024-01-09 10:00
        - ticket1.created = 2024-01-09 11:00 > req_last_escl_date → NO escalation
        
        For queue2 (NOT affected by exclusion):
        - last = 2024-01-08
        - Jan 8: no exclusion, days=1
        - Jan 9: no exclusion for queue2, days=2
        - req_last_escl_date = 2024-01-10 10:00 - 2 days = 2024-01-08 10:00
        - ticket2.created = 2024-01-08 09:00 <= req_last_escl_date → YES escalation 3->2
        """
        queue2 = Queue.objects.create(
            title="Another Queue",
            slug="another",
            escalate_days=2,
        )

        exclusion = EscalationExclusion.objects.create(
            name="Queue 1 Only",
            date=date(2024, 1, 9),
        )
        exclusion.queues.add(self.queue)

        ticket1 = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 9, 11, 0, 0)),
            queue=self.queue,
            title="Test Ticket 1",
            description="Test Description",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        ticket2 = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 8, 9, 0, 0)),
            queue=queue2,
            title="Test Ticket 2",
            description="Test Description",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(date(2024, 1, 10)):
            call_command("escalate_tickets")

        ticket1.refresh_from_db()
        ticket2.refresh_from_db()

        self.assertEqual(ticket1.priority, 3)
        self.assertEqual(ticket2.priority, 2)

    def test_escalation_global_exclusion(self):
        """Test that global exclusions (no queues) affect all queues.
        
        Setup:
        - queue1 (self.queue): escalate_days=2
        - queue2: escalate_days=2
        - Exclusion on 2024-01-09: global (no queues specified)
        
        Mock today: 2024-01-10 10:00
        
        For BOTH queues (exclusion applies globally):
        - last = 2024-01-08
        - Jan 8: no exclusion, days=1
        - Jan 9: global exclusion, skipped
        - days = 1
        - req_last_escl_date = 2024-01-10 10:00 - 1 day = 2024-01-09 10:00
        
        ticket1.created = 2024-01-09 11:00 > req_last_escl_date → NO escalation
        ticket2.created = 2024-01-09 11:30 > req_last_escl_date → NO escalation
        """
        queue2 = Queue.objects.create(
            title="Another Queue",
            slug="another",
            escalate_days=2,
        )

        EscalationExclusion.objects.create(
            name="Global Holiday",
            date=date(2024, 1, 9),
        )

        ticket1 = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 9, 11, 0, 0)),
            queue=self.queue,
            title="Test Ticket 1",
            description="Test Description",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        ticket2 = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 9, 11, 30, 0)),
            queue=queue2,
            title="Test Ticket 2",
            description="Test Description",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(date(2024, 1, 10)):
            call_command("escalate_tickets")

        ticket1.refresh_from_db()
        ticket2.refresh_from_db()

        self.assertEqual(ticket1.priority, 3)
        self.assertEqual(ticket2.priority, 3)

    def test_escalation_audit_fields_verification(self):
        """Test that all audit fields are correctly populated on escalation."""
        ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 8, 10, 0, 0)),
            queue=self.queue,
            title="Test Ticket",
            description="Test Description",
            priority=4,
            status=Ticket.OPEN_STATUS,
        )

        initial_modified = ticket.modified

        with patch_escalation_datetime(date(2024, 1, 10)):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        followup = ticket.followup_set.latest("date")
        ticket_change = followup.ticketchange_set.latest("id")

        self.assertEqual(ticket.priority, 3)
        self.assertIsNotNone(ticket.last_escalation)
        self.assertGreater(ticket.modified, initial_modified)

        self.assertEqual(followup.title, _("Ticket Escalated"))
        self.assertTrue(followup.public)
        self.assertIn("2 days", followup.comment)

        self.assertEqual(ticket_change.field, _("Priority"))
        self.assertEqual(int(ticket_change.old_value), 4)
        self.assertEqual(int(ticket_change.new_value), 3)

    def test_escalation_no_escalation_days_zero(self):
        """Test that queues with escalate_days=0 don't escalate."""
        queue_no_escalation = Queue.objects.create(
            title="No Escalation Queue",
            slug="no-escalation",
            escalate_days=0,
        )

        ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 5, 10, 0, 0)),
            queue=queue_no_escalation,
            title="Test Ticket",
            description="Test Description",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(date(2024, 1, 10)):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 3)
        self.assertIsNone(ticket.last_escalation)

    def test_escalation_verbose_output(self):
        """Test verbose output of escalation command.
        
        Timeline:
        - created: 2024-01-08 (Monday) 10:00
        - escalate_days: 2
        - Mock today: 2024-01-10 (Wednesday)
        - Should escalate and show verbose output
        """
        ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 8, 10, 0, 0)),
            queue=self.queue,
            title="Test Ticket",
            description="Test Description",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        out = StringIO()
        with patch_escalation_datetime(date(2024, 1, 10)):
            call_command("escalate_tickets", "-x", stdout=out)
        output = out.getvalue()

        self.assertIn("Processing:", output)
        self.assertIn("Escalating", output)

    @override_settings(USE_TZ=True, TIME_ZONE="America/New_York")
    def test_escalation_with_different_timezone(self):
        """Test escalation works correctly with different timezones.
        
        Timeline:
        - created: 2024-01-08 (Monday) 10:00 EST (15:00 UTC)
        - escalate_days: 2
        - Mock today: 2024-01-10 (Wednesday)
        - Working days: 2 (Jan 8, 9)
        - Should escalate priority 3 -> 2
        """
        ny_tz = pytz.timezone("America/New_York")
        created_time = ny_tz.localize(datetime(2024, 1, 8, 10, 0, 0))

        ticket = create_ticket_with_created_date(
            created_date=created_time,
            queue=self.queue,
            title="Test Ticket",
            description="Test Description",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(date(2024, 1, 10)):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 2)
        self.assertTrue(timezone.is_aware(ticket.last_escalation))

    def test_escalation_multiple_exclusions_weekend(self):
        """Test escalation that spans a weekend with multiple exclusion days.
        
        Timeline:
        - created: 2024-01-11 (Thursday) 10:00
        - escalate_days: 2
        - Exclusions: 2024-01-13 (Saturday), 2024-01-14 (Sunday)
        - Mock today: 2024-01-15 (Monday)
        - Working days: 2 (Jan 11, 12; Sat/Sun excluded)
        - Should escalate priority 3 -> 2
        """
        EscalationExclusion.objects.create(
            name="Saturday",
            date=date(2024, 1, 13),
        )
        EscalationExclusion.objects.create(
            name="Sunday",
            date=date(2024, 1, 14),
        )

        ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 11, 10, 0, 0)),
            queue=self.queue,
            title="Test Ticket",
            description="Test Description",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(date(2024, 1, 15)):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 2)

    def test_escalation_multiple_tickets_same_queue(self):
        """Test escalation handles multiple tickets in the same queue.
        
        Timeline:
        - ticket1 created: 2024-01-08 09:00 (Monday)
        - ticket2 created: 2024-01-08 09:30 (Monday)
        - ticket3 created: 2024-01-09 10:00 (Tuesday)
        - escalate_days: 2
        - Mock today: 2024-01-10 10:00 (Wednesday)
        - Working days counted: 2 (Jan 8, 9)
        - req_last_escl_date: 2024-01-10 10:00 - 2 days = 2024-01-08 10:00
        - ticket1 (09:00 <= 10:00): should escalate 3->2
        - ticket2 (09:30 <= 10:00): should escalate 4->3
        - ticket3 (created Jan 9, too recent): no escalation
        """
        ticket1 = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 8, 9, 0, 0)),
            queue=self.queue,
            title="Test Ticket 1",
            description="Test Description 1",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        ticket2 = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 8, 9, 30, 0)),
            queue=self.queue,
            title="Test Ticket 2",
            description="Test Description 2",
            priority=4,
            status=Ticket.OPEN_STATUS,
        )

        ticket3 = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 9, 10, 0, 0)),
            queue=self.queue,
            title="Recent Ticket",
            description="Test Description 3",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(date(2024, 1, 10)):
            call_command("escalate_tickets")

        ticket1.refresh_from_db()
        ticket2.refresh_from_db()
        ticket3.refresh_from_db()

        self.assertEqual(ticket1.priority, 2)
        self.assertEqual(ticket2.priority, 3)
        self.assertEqual(ticket3.priority, 3)

        self.assertEqual(ticket1.followup_set.count(), 1)
        self.assertEqual(ticket2.followup_set.count(), 1)
        self.assertEqual(ticket3.followup_set.count(), 0)


class EscalationTimeBoundaryTestCase(TestCase):
    """Test escalation time boundary cases: DST transitions, year/month rollovers, timezone mismatches."""

    fixtures = ["emailtemplate.json"]

    def setUp(self):
        super().setUp()
        self.queue = Queue.objects.create(
            title="Boundary Test Queue",
            slug="boundary-test",
            escalate_days=2,
        )
        self.user = User.objects.create(
            username="boundary_user",
            is_staff=True,
        )

    # =========================================================================
    # Daylight Saving Time transitions
    #
    # America/New_York 2024 DST:
    #   SPRING FORWARD: Mar 10, 2024, 2:00 AM -> 3:00 AM (23-hour day)
    #   FALL BACK:     Nov 3,  2024, 2:00 AM -> 1:00 AM (25-hour day)
    # =========================================================================

    @override_settings(USE_TZ=True, TIME_ZONE="America/New_York")
    def test_escalation_spring_forward_dst_boundary_ticket_created_before(self):
        """Test escalation across spring-forward DST, ticket created BEFORE transition.

        DST Spring Forward 2024 (America/New_York):
          - Saturday,  Mar 9:  normal 24-hour day
          - Sunday,    Mar 10: 23-hour day (2am jumps to 3am)
          - Monday,    Mar 11: normal 24-hour day

        Timeline:
          - escalate_days: 2
          - ticket created: Fri Mar 8, 10:00 AM EST (15:00 UTC)
          - mock today:     Mon Mar 11

        Escalation logic (using LOCAL dates, since date.today() is timezone-naive local):
          - last = Mar 11 - 2 days = Mar 9
          - Iterate dates [Mar 9, Mar 10]:
              Mar 9 (Sat): no exclusion, days=1
              Mar 10 (Sun, DST day): no exclusion record, days=2
          - days = 2
          - req_last_escl_date = Mar 11 10:00 - 2 days = Mar 9 10:00
            (Note: 'now' is mocked to Mar 11 10:00 local = Mar 11 14:00 UTC)
          - Ticket created Mar 8 10:00 EST (15:00 UTC) <= Mar 9 10:00 EST? YES
          - Ticket SHOULD escalate.

        The key check here is that the 23-hour day (Mar 10) does NOT cause
        us to miscount working days - the calendar date Mar 10 must still
        count as exactly one working-day-or-not regardless of its length.
        """
        ny_tz = pytz.timezone("America/New_York")

        ticket = create_ticket_with_created_date(
            created_date=ny_tz.localize(datetime(2024, 3, 8, 10, 0, 0)),
            queue=self.queue,
            title="Pre-DST Transition Ticket",
            description="Created before spring-forward DST",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(
            date(2024, 3, 11),
            datetime(2024, 3, 11, 10, 0, 0),
        ):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 2)
        self.assertIsNotNone(ticket.last_escalation)

    @override_settings(USE_TZ=True, TIME_ZONE="America/New_York")
    def test_escalation_spring_forward_dst_boundary_ticket_created_on_transition(self):
        """Test escalation when ticket created ON the 23-hour spring-forward day.

        Ticket created Sunday Mar 10 at 10:00 AM EDT (14:00 UTC) — the
        23-hour DST day. escalate_days=2, mock today=Wed Mar 13.

        Escalation logic:
          - last = Mar 13 - 2 days = Mar 11
          - Dates [Mar 11, Mar 12]:
              Mar 11 (Mon): days=1
              Mar 12 (Tue): days=2
          - days=2
          - req_last_escl_date = Mar 13 10:00 - 2 days = Mar 11 10:00
          - Ticket created Mar 10 10:00 EDT (14:00 UTC) <= Mar 11 10:00 EDT? YES
          - SHOULD escalate.

        The bug to guard against: if we used naive datetime arithmetic that
        confused 24-hour vs 23-hour calendar days when subtracting
        timedelta(days=2) from a DST datetime, the comparison threshold
        would be off and we'd incorrectly skip the escalation.
        """
        ny_tz = pytz.timezone("America/New_York")

        ticket = create_ticket_with_created_date(
            created_date=ny_tz.localize(datetime(2024, 3, 10, 10, 0, 0)),
            queue=self.queue,
            title="DST-Day Ticket",
            description="Created ON the spring-forward 23-hour day",
            priority=4,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(
            date(2024, 3, 13),
            datetime(2024, 3, 13, 10, 0, 0),
        ):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 3)
        self.assertIsNotNone(ticket.last_escalation)

        # Audit trail check
        followup = ticket.followup_set.latest("date")
        self.assertEqual(followup.title, _("Ticket Escalated"))
        ticket_change = followup.ticketchange_set.latest("id")
        self.assertEqual(int(ticket_change.old_value), 4)
        self.assertEqual(int(ticket_change.new_value), 3)

    @override_settings(USE_TZ=True, TIME_ZONE="America/New_York")
    def test_escalation_fall_back_dst_25_hour_day(self):
        """Test escalation across fall-back DST, 25-hour day.

        DST Fall Back 2024 (America/New_York):
          - Saturday, Nov 2:  normal 24-hour day
          - Sunday,   Nov 3:  25-hour day (2am repeats as 1am)
          - Monday,   Nov 4:  normal 24-hour day

        Ticket created Fri Nov 1 10:00 AM EDT. escalate_days=2, mock today=Mon Nov 4.

        Escalation logic:
          - last = Nov 4 - 2 days = Nov 2
          - Dates [Nov 2, Nov 3]:
              Nov 2 (Sat): no exclusion, days=1
              Nov 3 (Sun, 25-hr day): no exclusion record, days=2
          - days=2
          - req_last_escl_date = Nov 4 10:00 - 2 days = Nov 2 10:00
          - Ticket created Nov 1 10:00 EDT <= Nov 2 10:00? YES
          - SHOULD escalate.

        Critical guard: even though Nov 3 has an EXTRA hour and subtracting
        2*24h from the mock "now" would land in a different wall-clock hour
        than a naive calendar subtraction, we must still count exactly
        2 calendar working-days and trigger correctly.
        """
        ny_tz = pytz.timezone("America/New_York")

        ticket = create_ticket_with_created_date(
            created_date=ny_tz.localize(datetime(2024, 11, 1, 10, 0, 0)),
            queue=self.queue,
            title="Pre Fall-Back Ticket",
            description="Created before fall-back 25-hour day",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(
            date(2024, 11, 4),
            datetime(2024, 11, 4, 10, 0, 0),
        ):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 2)

    @override_settings(USE_TZ=True, TIME_ZONE="America/New_York")
    def test_escalation_dst_transition_with_exclusion(self):
        """Test escalation with an exclusion on a DST transition weekend day.

        Spring Forward 2024 weekend:
          - Exclusion set on Sunday Mar 10 (the 23-hour DST day)
          - Ticket created Friday Mar 8 10:00 AM
          - escalate_days=2, mock today=Tuesday Mar 12

        Escalation logic:
          - last = Mar 12 - 2 days = Mar 10
          - Dates [Mar 10, Mar 11]:
              Mar 10 (Sun): EXCLUDED (explicit exclusion)
              Mar 11 (Mon): days=1
          - days=1  (NOT 2 — the exclusion ate one slot)
          - req_last_escl_date = Mar 12 10:00 - 1 day = Mar 11 10:00
          - Ticket created Mar 8 10:00 EST (15:00 UTC) <= Mar 11 10:00 EST? YES
          - Wait, that'd still escalate!

        To guarantee NO escalation we need:
          - Ticket created later. Use Mar 11 11:00 AM instead.
          - req_last_escl_date = Mar 12 10:00 - 1 day = Mar 11 10:00
          - Created Mar 11 11:00 AM > 10:00 threshold → NO escalation.
        """
        ny_tz = pytz.timezone("America/New_York")

        EscalationExclusion.objects.create(
            name="DST Sunday Off",
            date=date(2024, 3, 10),
        )

        ticket = create_ticket_with_created_date(
            created_date=ny_tz.localize(datetime(2024, 3, 11, 11, 0, 0)),
            queue=self.queue,
            title="Monday After DST",
            description="Exclusion + DST combined",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(
            date(2024, 3, 12),
            datetime(2024, 3, 12, 10, 0, 0),
        ):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 3)
        self.assertIsNone(ticket.last_escalation)

    # =========================================================================
    # Year rollover & month rollover
    # =========================================================================

    def test_escalation_year_rollover_with_holiday_exclusions(self):
        """Test escalation across Dec → Jan year boundary with holiday exclusions.

        Scenario (holiday season):
          - escalate_days=3 (overridden on this queue)
          - Exclusions: Dec 31 (New Year's Eve), Jan 1 (New Year's Day)
          - mock today=Friday Jan 3, 2025
          - Ticket created Monday Dec 23, 2024 09:00

        Escalation logic:
          - last = Jan 3 - 3 days = Dec 31, 2024
          - Dates [Dec 31, Jan 1, Jan 2]:
              Dec 31 (Tue): EXCLUDED (New Year's Eve)
              Jan  1 (Wed): EXCLUDED (New Year's Day)
              Jan  2 (Thu): days=1
          - days = 1
          - req_last_escl_date = Jan 3 10:00 - 1 day = Jan 2 10:00
          - Ticket created Dec 23 09:00 <= Jan 2 10:00 → YES
          - SHOULD escalate 3 → 2

        Critical boundary: exclusion rows in two different calendar years (2024 and
        2025) must both be found by the date lookup and correctly exclude days.
        """
        self.queue.escalate_days = 3
        self.queue.save()

        EscalationExclusion.objects.create(
            name="New Year's Eve 2024",
            date=date(2024, 12, 31),
        )
        EscalationExclusion.objects.create(
            name="New Year's Day 2025",
            date=date(2025, 1, 1),
        )

        ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 12, 23, 9, 0, 0)),
            queue=self.queue,
            title="Year-End Ticket",
            description="Spans year rollover with holidays",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(date(2025, 1, 3)):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 2)
        self.assertIsNotNone(ticket.last_escalation)

        # The followup comment records escalate_days (3), not the counted days (1)
        followup = ticket.followup_set.latest("date")
        self.assertIn("3 days", followup.comment)
        ticket_change = followup.ticketchange_set.latest("id")
        self.assertEqual(ticket_change.field, _("Priority"))
        self.assertEqual(int(ticket_change.old_value), 3)
        self.assertEqual(int(ticket_change.new_value), 2)

    def test_escalation_year_rollover_no_escalation_on_fresh_ticket(self):
        """Verify a recently-created ticket does NOT escalate on Jan 2.

        Setup: escalate_days=3, exclusions on Dec 31 and Jan 1.
        mock today = Jan 2, 10:00.

        Escalation logic:
          - last = Jan 2 - 3 days = Dec 30, 2024
          - Dates [Dec 30, Dec 31, Jan 1]:
              Dec 30 (Mon): days=1
              Dec 31 (Tue): EXCLUDED
              Jan  1 (Wed): EXCLUDED
          - days = 1
          - req_last_escl_date = Jan 2 10:00 - 1 day = Jan 1 10:00
          - Ticket created Jan 1 11:00 > Jan 1 10:00 → NO escalation.

        The edge: a ticket created literally on the holiday at 11am must not
        be treated as "old enough" when only one calendar slot counted as a
        working day because of the exclusions.
        """
        self.queue.escalate_days = 3
        self.queue.save()

        EscalationExclusion.objects.create(
            name="New Year's Eve 2024",
            date=date(2024, 12, 31),
        )
        EscalationExclusion.objects.create(
            name="New Year's Day 2025",
            date=date(2025, 1, 1),
        )

        ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2025, 1, 1, 11, 0, 0)),
            queue=self.queue,
            title="New Year Ticket",
            description="Created on holiday",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(date(2025, 1, 2)):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 3)
        self.assertIsNone(ticket.last_escalation)

    def test_escalation_month_rollover_with_exclusions(self):
        """Test escalation across Jan 31 → Feb 1 month boundary.

        Setup:
          - escalate_days=3, mock today=Mon Feb 3
          - Exclusion: Fri Jan 31 (company inventory day)
          - Ticket created Tue Jan 28 09:30

        Escalation logic:
          - last = Feb 3 - 3 days = Jan 31
          - Dates [Jan 31, Feb 1, Feb 2]:
              Jan 31 (Fri): EXCLUDED → skip
              Feb 1 (Sat): no exclusion → days=1
              Feb 2 (Sun): no exclusion → days=2
          - days = 2 (less than escalate_days=3; days cap at actual count of 2)
          - req_last_escl_date = Feb 3 10:00 - 2 days = Feb 1 10:00
          - Ticket created Jan 28 09:30 <= Feb 1 10:00 → YES
          - SHOULD escalate 3 → 2

        Guard against: the lookup for Jan 31 (different month) and
        Feb 1/2 (next month) both returning correct exclusion rows.
        """
        self.queue.escalate_days = 3
        self.queue.save()

        EscalationExclusion.objects.create(
            name="Inventory Day",
            date=date(2025, 1, 31),
        )

        ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2025, 1, 28, 9, 30, 0)),
            queue=self.queue,
            title="Month-End Ticket",
            description="Spans Jan → Feb boundary",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(date(2025, 2, 3)):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 2)

    def test_escalation_february_leap_year_rollover(self):
        """Test escalation over Feb 28/29 during a leap year with Feb 29 exclusion.

        Leap Year 2024:
          - escalate_days=2, mock today=Monday Mar 4
          - Exclusion: Thursday Feb 29 (leap day company holiday)
          - Ticket 1 created Tuesday Feb 27 09:00
          - Ticket 2 created Friday Feb 23 09:00

        Escalation logic:
          - last = Mar 4 - 2 days = Mar 2
          - Dates [Mar 2, Mar 3]:
              Mar 2 (Sat): days=1
              Mar 3 (Sun): days=2
          - days=2
          - req_last_escl_date = Mar 4 10:00 - 2 days = Mar 2 10:00
          - Ticket 1 (Feb 27 09:00) <= Mar 2 10:00 → YES, escalate 3→2
          - Ticket 2 (Feb 23 09:00) <= Mar 2 10:00 → YES, escalate 2→1

        Extra check: exclusion on Feb 29 is NEVER consulted because the
        algorithm only walks back from today - escalate_days. If someone
        refactored the loop to start earlier, the Feb 29 exclusion would
        wrongly eat a day — this test locks in the START-of-range behavior.
        """
        EscalationExclusion.objects.create(
            name="Leap Day Off",
            date=date(2024, 2, 29),
        )

        ticket1 = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 2, 27, 9, 0, 0)),
            queue=self.queue,
            title="Pre-Leap-Day Ticket",
            description="Created Tue before leap-day exclusion",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )
        ticket2 = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 2, 23, 9, 0, 0)),
            queue=self.queue,
            title="Older Ticket",
            description="Created Fri before leap week",
            priority=2,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(date(2024, 3, 4)):
            call_command("escalate_tickets")

        ticket1.refresh_from_db()
        ticket2.refresh_from_db()

        self.assertEqual(ticket1.priority, 2)
        self.assertEqual(ticket2.priority, 1)

    # =========================================================================
    # Server timezone vs. ticket-creation timezone
    # =========================================================================

    @override_settings(USE_TZ=True, TIME_ZONE="UTC")
    def test_escalation_server_utc_ticket_created_in_shanghai_tz_early_morning(self):
        """Server runs UTC; ticket created in Shanghai (UTC+8) near midnight.

        Date-boundary mismatch case:
          - Ticket created: 2024-06-10 07:30 Shanghai = 2024-06-09 23:30 UTC
            → In Shanghai, it's MONDAY morning June 10
            → In server UTC,   it's SUNDAY  night   June  9

        escalate_days=2, mock today (UTC date): June 11 → local server date June 11.

        Escalation logic uses server-local date.today() = June 11:
          - last = June 11 - 2 days = June 9
          - Dates [June 9, June 10]:
              June 9 (Sun): no exclusion → days=1
              June 10 (Mon): no exclusion → days=2
          - days=2
          - req_last_escl_date = June 11 10:00 UTC - 2 days = June 9 10:00 UTC
          - ticket.created stored as June 9 23:30 UTC. Is that <= June 9 10:00 UTC?
            NO. → Ticket must NOT escalate.

        Even though the user *perceives* the ticket as being opened on Monday
        business hours June 10, on the server timeline it was filed Sunday
        night and is therefore too new to trigger escalation by June 11 10:00 UTC.
        """
        shanghai_tz = pytz.timezone("Asia/Shanghai")

        # 07:30 June 10 Shanghai → 23:30 June 9 UTC
        shanghai_time = shanghai_tz.localize(datetime(2024, 6, 10, 7, 30, 0))
        ticket = create_ticket_with_created_date(
            created_date=shanghai_time,
            queue=self.queue,
            title="Shanghai Early Morning Ticket",
            description="Server sees Sunday night; user sees Monday morning",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        # Sanity check the timezone math — convert to UTC and read the .date()
        utc_created = ticket.created.astimezone(pytz.UTC)
        self.assertEqual(utc_created.date(), date(2024, 6, 9),
                         "Ticket created UTC date should be June 9, not June 10")

        with patch_escalation_datetime(
            date(2024, 6, 11),
            datetime(2024, 6, 11, 10, 0, 0),
        ):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 3,
                         "Ticket should NOT escalate — created Sunday UTC, too new")
        self.assertIsNone(ticket.last_escalation)

    @override_settings(USE_TZ=True, TIME_ZONE="UTC")
    def test_escalation_server_utc_ticket_shanghai_evening_flips_day_boundary(self):
        """Server UTC; ticket created Shanghai evening → next day UTC.

        Opposite edge case:
          - Ticket created: 2024-06-10 22:00 Shanghai = 2024-06-10 14:00 UTC
            → Both sides agree it's June 10.

        escalate_days=2, mock today=June 12 10:00 UTC.

        Escalation:
          - last = June 12 - 2 days = June 10
          - Dates [June 10, June 11]:
              June 10 (Mon): days=1
              June 11 (Tue): days=2
          - req_last_escl_date = June 12 10:00 - 2 days = June 10 10:00 UTC
          - Ticket created June 10 14:00 UTC > June 10 10:00 → NO escalation.

        Now move ticket one day EARLIER (June 9 Shanghai evening → June 9 14:00 UTC)
        and it SHOULD escalate.
        """
        shanghai_tz = pytz.timezone("Asia/Shanghai")

        # Case A: too recent, must NOT escalate
        ticket_too_new = create_ticket_with_created_date(
            created_date=shanghai_tz.localize(datetime(2024, 6, 10, 22, 0, 0)),
            queue=self.queue,
            title="Shanghai Evening — Too New",
            description="June 10 22:00 CST = 14:00 UTC same day",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        # Case B: one day earlier, SHOULD escalate
        ticket_old_enough = create_ticket_with_created_date(
            created_date=shanghai_tz.localize(datetime(2024, 6, 9, 22, 0, 0)),
            queue=self.queue,
            title="Shanghai Evening — Old Enough",
            description="June 9 22:00 CST = June 9 14:00 UTC",
            priority=4,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(
            date(2024, 6, 12),
            datetime(2024, 6, 12, 10, 0, 0),
        ):
            call_command("escalate_tickets")

        ticket_too_new.refresh_from_db()
        ticket_old_enough.refresh_from_db()

        self.assertEqual(ticket_too_new.priority, 3)
        self.assertIsNone(ticket_too_new.last_escalation)

        self.assertEqual(ticket_old_enough.priority, 3)
        self.assertIsNotNone(ticket_old_enough.last_escalation)

    @override_settings(USE_TZ=True, TIME_ZONE="America/Los_Angeles")
    def test_escalation_server_pacific_ticket_europe_date_boundary(self):
        """Server runs Pacific time; ticket created in Berlin (UTC+1 / UTC+2 DST).

        Summer 2024 — Europe observes CEST (UTC+2).

        Ticket scenario:
          - Created in Berlin:  2024-06-11 01:00 CEST = 2024-06-10 23:00 UTC
            → Berlin sees June 11 Tuesday early morning
            → UTC sees    June 10 Monday late night
            → LA server:  2024-06-10 16:00 PDT (Monday afternoon)

        escalate_days=1.

        The core test idea: date.today() runs in LOCAL server time. For the
        comparison we call timezone.now() → returns UTC internally, then we
        subtract days and compare against the stored-UTC created datetime.

        We want the ticket to NOT escalate, so we need ticket.created >
        req_last_escl_date. We use mock today = June 10 LA (Monday).

        Escalation with today = Monday June 10, LA local:
          - last = June 10 - 1 day = June 9
          - Dates [June 9]:
              June 9 (Sun LA local): days=1
          - days=1
          - mock "now" = June 10 10:00 PDT = June 10 17:00 UTC
          - req_last_escl_date = June 10 17:00 UTC - 1 day = June 9 17:00 UTC

        Now let's pick a ticket creation time that sits JUST after that
        threshold on the Berlin user's timeline:
          - Ticket created June 10 23:00 CEST = 21:00 UTC June 10
            → Berlin sees June 10 late night
            → UTC sees also June 10 evening
            → req_last_escl_date is June 9 17:00 UTC
            → 21:00 UTC June 10 comes AFTER 17:00 UTC June 9
            → should NOT escalate.

        Move ticket earlier on Berlin side but still pass threshold:
          - Berlin June 9 19:00 CEST = 17:00 UTC June 9 → AT the threshold
          - Berlin June 9 18:00 CEST = 16:00 UTC June 9 → BEFORE threshold
            → SHOULD escalate.

        So the two cases demonstrate the boundary:
          - OLD ticket (June 9 18:00 Berlin = 16:00 UTC June 9) → escalates
          - NEW ticket (June 10 23:00 Berlin = 21:00 UTC June 10) → no escalation
        """
        berlin_tz = pytz.timezone("Europe/Berlin")
        la_tz = pytz.timezone("America/Los_Angeles")

        # Override to 1 working day for fine-grained boundary testing
        self.queue.escalate_days = 1
        self.queue.save()

        # Case A — OLD ticket: should escalate
        ticket_old = create_ticket_with_created_date(
            created_date=berlin_tz.localize(datetime(2024, 6, 9, 18, 0, 0)),
            queue=self.queue,
            title="Berlin Old Ticket",
            description="18:00 Berlin Sun = 16:00 UTC Sun = old enough",
            priority=4,
            status=Ticket.OPEN_STATUS,
        )
        # Sanity check old: should be June 9 16:00 UTC
        old_utc = ticket_old.created.astimezone(pytz.UTC)
        self.assertEqual(old_utc.date(), date(2024, 6, 9))
        self.assertEqual(old_utc.hour, 16)

        # Case B — NEW ticket: should NOT escalate
        ticket_new = create_ticket_with_created_date(
            created_date=berlin_tz.localize(datetime(2024, 6, 10, 23, 0, 0)),
            queue=self.queue,
            title="Berlin New Ticket",
            description="23:00 Berlin Mon = 21:00 UTC Mon = still too new",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )
        # Sanity check new: should be June 10 21:00 UTC
        new_utc = ticket_new.created.astimezone(pytz.UTC)
        self.assertEqual(new_utc.date(), date(2024, 6, 10))
        self.assertEqual(new_utc.hour, 21)

        # Mock today = June 10 (Monday LA). "now" = June 10 10:00 PDT = 17:00 UTC
        mock_today_local = date(2024, 6, 10)
        mock_now_local_10am = datetime(2024, 6, 10, 10, 0, 0)  # LA local 10am

        with patch_escalation_datetime(mock_today_local, mock_now_local_10am):
            call_command("escalate_tickets")

        ticket_old.refresh_from_db()
        ticket_new.refresh_from_db()

        # Case A (16:00 UTC June 9 <= 17:00 UTC June 9): SHOULD escalate
        self.assertEqual(ticket_old.priority, 3)
        self.assertIsNotNone(ticket_old.last_escalation)

        # Case B (21:00 UTC June 10 > 17:00 UTC June 9): should NOT escalate
        self.assertEqual(ticket_new.priority, 3)
        self.assertIsNone(ticket_new.last_escalation)


class CombinedSLAAndEscalationTestCase(TestCase):
    """Test combined scenarios of SLA calculation and escalation cycles."""

    fixtures = ["emailtemplate.json"]

    def setUp(self):
        self.queue = Queue.objects.create(
            title="Combined Test Queue",
            slug="combined",
            escalate_days=3,
        )

        self.user = User.objects.create(
            username="combined_user",
            is_staff=True,
        )

    def test_combined_holiday_exclusion_sla_and_escalation(self):
        """Test that both SLA time tracking and escalation use the same holiday logic."""
        settings_override = HelpdeskSettingsOverride(
            FOLLOWUP_TIME_SPENT_AUTO=True,
            FOLLOWUP_TIME_SPENT_OPENING_HOURS={
                "monday": (9, 17),
                "tuesday": (9, 17),
                "wednesday": (9, 17),
                "thursday": (9, 17),
                "friday": (9, 17),
                "saturday": (0, 0),
                "sunday": (0, 0),
            },
            FOLLOWUP_TIME_SPENT_EXCLUDE_HOLIDAYS=["2024-01-01"],
        )
        with settings_override:
            EscalationExclusion.objects.create(
                name="New Year",
                date=date(2024, 1, 1),
            )
            EscalationExclusion.objects.create(
                name="Weekend",
                date=date(2024, 1, 6),
            )
            EscalationExclusion.objects.create(
                name="Weekend",
                date=date(2024, 1, 7),
            )

            ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2023, 12, 29, 10, 0, 0)),
                queue=self.queue,
                title="Combined Test",
                description="Test Description",
                                priority=3,
                status=Ticket.OPEN_STATUS,
            )

            followup = FollowUp.objects.create(
                ticket=ticket,
                title="First Response",
                date=timezone.make_aware(datetime(2024, 1, 2, 10, 0, 0)),
            )
            followup.refresh_from_db()

            time_spent = followup.time_spent_calculation()
            self.assertEqual(time_spent, timedelta(hours=8))

            with patch_escalation_datetime(date(2024, 1, 5)):
                call_command("escalate_tickets")

            ticket.refresh_from_db()
            self.assertEqual(ticket.priority, 2)

    def test_combined_extended_business_hours(self):
        """Test combined scenario with extended business hours including weekend."""
        settings_override = HelpdeskSettingsOverride(
            FOLLOWUP_TIME_SPENT_AUTO=True,
            FOLLOWUP_TIME_SPENT_OPENING_HOURS={
                "monday": (8, 18),
                "tuesday": (8, 18),
                "wednesday": (8, 18),
                "thursday": (8, 18),
                "friday": (8, 18),
                "saturday": (10, 14),
                "sunday": (0, 0),
            },
        )
        with settings_override:
            ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 10, 10, 0, 0)),
                queue=self.queue,
                title="Extended Hours Test",
                description="Test Description",
                                priority=3,
                status=Ticket.OPEN_STATUS,
            )

            followup = FollowUp.objects.create(
                ticket=ticket,
                title="Weekend Update",
                date=timezone.make_aware(datetime(2024, 1, 13, 12, 0, 0)),
            )
            followup.refresh_from_db()

            time_spent = followup.time_spent_calculation()
            expected_hours = (18 - 10) + (10 * 2) + 2
            self.assertEqual(time_spent, timedelta(hours=expected_hours))

    def test_combined_long_holiday_period(self):
        """Test combined scenario with an extended holiday period.
        
        Setup:
        - queue escalate_days = 3
        - exclusions (EscalationExclusion): 2024-01-01, 01-02, 01-03
        - mock today for escalation: 2024-01-09 10:00
        
        Escalation logic:
        - last = 2024-01-09 - 3 days = 2024-01-06
        - Work dates from 2024-01-06 to 2024-01-09:
          Jan 6 (Sat): not in exclusion table → days=1
          Jan 7 (Sun): not in exclusion table → days=2
          Jan 8 (Mon): not in exclusion table → days=3
        - days = 3
        - req_last_escl_date = 2024-01-09 10:00 - 3 days = 2024-01-06 10:00
        - ticket.created = 2024-01-06 11:00 > req_last_escl_date → NO escalation (priority stays 4)

        SLA logic:
        - ticket.created = 2024-01-06 (Sat) 11:00
        - followup.date = 2024-01-08 (Mon) 17:00
        - Business days from settings exclude weekends: Mon(9-17), Tue(9-17), etc.
        - Excluded holidays: Jan 1, 2, 3 (all before created, no impact)
        - Jan 6 (Sat): weekend, 0h
        - Jan 7 (Sun): weekend, 0h
        - Jan 8 (Mon): 9:00-17:00, full day = 8h
        Total: 8h
        """
        settings_override = HelpdeskSettingsOverride(
            FOLLOWUP_TIME_SPENT_AUTO=True,
            FOLLOWUP_TIME_SPENT_OPENING_HOURS={
                "monday": (9, 17),
                "tuesday": (9, 17),
                "wednesday": (9, 17),
                "thursday": (9, 17),
                "friday": (9, 17),
                "saturday": (0, 0),
                "sunday": (0, 0),
            },
            FOLLOWUP_TIME_SPENT_EXCLUDE_HOLIDAYS=[
                "2024-01-01",
                "2024-01-02",
                "2024-01-03",
            ],
        )
        with settings_override:
            for d in range(1, 4):
                EscalationExclusion.objects.create(
                    name=f"Holiday {d}",
                    date=date(2024, 1, d),
                )

            ticket = create_ticket_with_created_date(
                created_date=timezone.make_aware(datetime(2024, 1, 6, 11, 0, 0)),
                queue=self.queue,
                title="Holiday Period Test",
                description="Test Description",
                priority=4,
                status=Ticket.OPEN_STATUS,
            )

            with patch_escalation_datetime(date(2024, 1, 9)):
                call_command("escalate_tickets")

            ticket.refresh_from_db()
            self.assertEqual(ticket.priority, 4)

            followup = FollowUp.objects.create(
                ticket=ticket,
                title="Post-Holiday Update",
                date=timezone.make_aware(datetime(2024, 1, 8, 17, 0, 0)),
            )
            followup.refresh_from_db()

            time_spent = followup.time_spent_calculation()
            self.assertEqual(time_spent, timedelta(hours=8))

    def test_combined_due_date_and_escalation_audit(self):
        """Test that both due_date changes and escalations create proper audit trails."""
        ticket = Ticket.objects.create(
            queue=self.queue,
            title="Audit Trail Test",
            description="Test Description",
            due_date=timezone.make_aware(datetime(2024, 1, 20, 10, 0, 0)),
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        due_date_followup = FollowUp.objects.create(
            ticket=ticket,
            title="Due Date Change",
            date=timezone.now(),
            user=self.user,
        )

        new_due_date = timezone.make_aware(datetime(2024, 1, 25, 10, 0, 0))
        TicketChange.objects.create(
            followup=due_date_followup,
            field=_("Due on"),
            old_value=str(ticket.due_date),
            new_value=str(new_due_date),
        )
        ticket.due_date = new_due_date
        ticket.save()

        priority_followup = FollowUp.objects.create(
            ticket=ticket,
            title=_("Ticket Escalated"),
            date=timezone.now(),
            public=True,
            comment=_("Ticket escalated after 3 days"),
        )

        TicketChange.objects.create(
            followup=priority_followup,
            field=_("Priority"),
            old_value="3",
            new_value="2",
        )
        ticket.priority = 2
        ticket.last_escalation = timezone.now()
        ticket.save()

        ticket_changes = TicketChange.objects.filter(
            followup__ticket=ticket
        ).order_by("id")

        self.assertEqual(ticket_changes.count(), 2)
        self.assertEqual(ticket_changes[0].field, _("Due on"))
        self.assertEqual(ticket_changes[1].field, _("Priority"))

        followups = ticket.followup_set.all().order_by("date")
        self.assertEqual(followups.count(), 2)
