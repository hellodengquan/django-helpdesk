from django.db import migrations, models
import datetime
from django.utils.translation import gettext as _


def _find_hold_start_time_from_history(ticket, TicketChange, FollowUp):
    """
    Find the timestamp when the ticket was last placed on hold by
    examining FollowUp/TicketChange history.

    Strategy:
    1. Look for TicketChange records with field="On Hold" and new_value="True"
    2. For each, check if there's a subsequent unhold (new_value="False")
    3. Return the date of the last hold that has no subsequent unhold
    4. If no history found, return None
    """
    hold_changes = TicketChange.objects.filter(
        followup__ticket=ticket,
        field=_("On Hold"),
        new_value="True",
    ).order_by("-followup__date")

    for hold_change in hold_changes:
        hold_date = hold_change.followup.date
        subsequent_unhold = TicketChange.objects.filter(
            followup__ticket=ticket,
            field=_("On Hold"),
            new_value="False",
            followup__date__gt=hold_date,
        ).exists()
        if not subsequent_unhold:
            return hold_date

    return None


def backfill_hold_times(apps, schema_editor):
    """
    Backfill hold_start_time and total_paused_time for existing records.

    Strategy:
    - For all tickets, set total_paused_time to timedelta(0) if NULL.
    - For tickets where on_hold=True but hold_start_time is NULL:
      1. First try to find the hold time from FollowUp/TicketChange history
      2. If no history found, use ticket.created as the fallback
         (NOT ticket.modified, which can be changed by comments/edits)
    """
    Ticket = apps.get_model("helpdesk", "Ticket")
    TicketChange = apps.get_model("helpdesk", "TicketChange")
    FollowUp = apps.get_model("helpdesk", "FollowUp")

    Ticket.objects.filter(total_paused_time__isnull=True).update(
        total_paused_time=datetime.timedelta()
    )

    stale_hold = Ticket.objects.filter(on_hold=True, hold_start_time__isnull=True)
    for ticket in stale_hold.only("id", "created"):
        hold_start = _find_hold_start_time_from_history(ticket, TicketChange, FollowUp)
        if hold_start is None:
            hold_start = ticket.created
        ticket.hold_start_time = hold_start
        ticket.save(update_fields=["hold_start_time"])


def reverse_backfill(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("helpdesk", "0039_alter_ticketchange_field"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="hold_start_time",
            field=models.DateTimeField(
                blank=True,
                editable=False,
                help_text="The date and time when the ticket was placed on hold. Used to calculate total paused time for SLA calculations.",
                null=True,
                verbose_name="Hold Start Time",
            ),
        ),
        migrations.AddField(
            model_name="ticket",
            name="total_paused_time",
            field=models.DurationField(
                blank=True,
                default=datetime.timedelta,
                help_text="Total duration the ticket has been on hold. This is used to adjust SLA escalation calculations.",
                verbose_name="Total Paused Time",
            ),
        ),
        migrations.RunPython(backfill_hold_times, reverse_backfill),
    ]

