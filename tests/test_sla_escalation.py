from datetime import date, datetime, timedelta
from io import StringIO
from unittest.mock import patch, MagicMock
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
from helpdesk import settings as helpdesk_settings

User = get_user_model()


def create_ticket_with_created_date(created_date, **kwargs):
    """Create a ticket with a specific created date, bypassing auto-now-add in save()."""
    ticket = Ticket.objects.create(**kwargs)
    ticket.created = created_date
    ticket.save()
    return ticket


class HelpdeskSettingsOverride:
    """Context manager to override helpdesk_settings module attributes."""
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.original_values = {}

    def __enter__(self):
        for key, value in self.kwargs.items():
            self.original_values[key] = getattr(helpdesk_settings, key, None)
            setattr(helpdesk_settings, key, value)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for key, value in self.original_values.items():
            if value is not None:
                setattr(helpdesk_settings, key, value)
            else:
                delattr(helpdesk_settings, key)
        return False


def patch_escalation_datetime(test_date, test_time=None):
    """
    Context manager to patch both date.today() and timezone.now() in escalation command.
    This ensures consistent date/time handling in tests.
    """
    if test_time is None:
        test_time = datetime(test_date.year, test_date.month, test_date.day, 10, 0, 0)
    
    mock_date = MagicMock()
    mock_date.today.return_value = test_date
    
    mock_tz = MagicMock()
    mock_tz.now.return_value = timezone.make_aware(test_time)
    
    return patch.multiple(
        "helpdesk.management.commands.escalate_tickets",
        date=mock_date,
        timezone=mock_tz,
    )


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
        """Test basic escalation without any exclusions."""
        ticket = Ticket.objects.create(
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
        """Test escalation skips exclusion dates."""
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

        with patch_escalation_datetime(date(2024, 1, 10)):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 3)
        self.assertIsNone(ticket.last_escalation)

    def test_escalation_after_skipping_exclusion(self):
        """Test escalation happens after counting through exclusion dates."""
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
        """Test that queue-specific exclusions only affect target queues."""
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
            call_command("escalate_tickets")

        ticket1.refresh_from_db()
        ticket2.refresh_from_db()

        self.assertEqual(ticket1.priority, 3)
        self.assertEqual(ticket2.priority, 2)

    def test_escalation_global_exclusion(self):
        """Test that global exclusions (no queues) affect all queues."""
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
        """Test verbose output of escalation command."""
        Ticket.objects.create(
            queue=self.queue,
            title="Test Ticket",
            description="Test Description",
            created=timezone.make_aware(datetime(2024, 1, 8, 10, 0, 0)),
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
        """Test escalation works correctly with different timezones."""
        ny_tz = pytz.timezone("America/New_York")
        created_time = ny_tz.localize(datetime(2024, 1, 8, 10, 0, 0))

        ticket = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 11, 10, 0, 0)),
            queue=self.queue,
            title="Test Ticket",
            description="Test Description",
            created=created_time,
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        with patch_escalation_datetime(date(2024, 1, 10)):
            call_command("escalate_tickets")

        ticket.refresh_from_db()
        self.assertEqual(ticket.priority, 2)
        self.assertTrue(timezone.is_aware(ticket.last_escalation))

    def test_escalation_multiple_exclusions_weekend(self):
        """Test escalation that spans a weekend with multiple exclusion days."""
        EscalationExclusion.objects.create(
            name="Saturday",
            date=date(2024, 1, 13),
        )
        EscalationExclusion.objects.create(
            name="Sunday",
            date=date(2024, 1, 14),
        )

        ticket = Ticket.objects.create(
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
        """Test escalation handles multiple tickets in the same queue."""
        ticket1 = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 8, 10, 0, 0)),
            queue=self.queue,
            title="Test Ticket 1",
            description="Test Description 1",
            priority=3,
            status=Ticket.OPEN_STATUS,
        )

        ticket2 = create_ticket_with_created_date(
            created_date=timezone.make_aware(datetime(2024, 1, 8, 11, 0, 0)),
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
        """Test combined scenario with an extended holiday period."""
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
            created_date=timezone.make_aware(datetime(2023, 12, 29, 10, 0, 0)),
                queue=self.queue,
                title="Holiday Period Test",
                description="Test Description",
                                priority=4,
                status=Ticket.OPEN_STATUS,
            )

            with patch_escalation_datetime(date(2024, 1, 8)):
                call_command("escalate_tickets")

            ticket.refresh_from_db()
            self.assertEqual(ticket.priority, 4)

            followup = FollowUp.objects.create(
                ticket=ticket,
                title="Post-Holiday Update",
                date=timezone.make_aware(datetime(2024, 1, 4, 10, 0, 0)),
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
