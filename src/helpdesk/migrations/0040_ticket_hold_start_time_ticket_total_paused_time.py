from django.db import migrations, models
import datetime


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
    ]
