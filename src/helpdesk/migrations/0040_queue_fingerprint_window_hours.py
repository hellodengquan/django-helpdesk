# Generated for fingerprint_window_hours field on Queue model

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("helpdesk", "0039_alter_ticketchange_field"),
    ]

    operations = [
        migrations.AddField(
            model_name="queue",
            name="fingerprint_window_hours",
            field=models.IntegerField(
                blank=True,
                help_text="When Message-Id and References headers are missing, "
                "incoming emails are matched against existing tickets using the "
                "sender address and subject (stripped of Re:/FW: prefixes). Set "
                "the time window (in hours) within which duplicates should be "
                "merged into the same ticket. Leave empty to use the global "
                "default (24 hours). Must be a positive integer.",
                null=True,
                verbose_name="Fingerprint Deduplication Window (hours)",
            ),
        ),
    ]
