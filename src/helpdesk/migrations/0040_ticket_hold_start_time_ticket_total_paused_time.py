from django.db import migrations, models
import datetime


def backfill_hold_times(apps, schema_editor):
    """
    Backfill hold_start_time and total_paused_time for existing records.

    Strategy:
    - For tickets where on_hold=True but hold_start_time is NULL:
      Use ticket.modified as the best estimate for when it was placed on hold.
    - For tickets where total_paused_time is NULL:
      Set to timedelta(0) for consistency.
    """
    Ticket = apps.get_model("helpdesk", "Ticket")

    Ticket.objects.filter(total_paused_time__isnull=True).update(
        total_paused_time=datetime.timedelta()
    )

    stale_hold = Ticket.objects.filter(on_hold=True, hold_start_time__isnull=True)
    for ticket in stale_hold.only("id", "modified"):
        ticket.hold_start_time = ticket.modified
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

