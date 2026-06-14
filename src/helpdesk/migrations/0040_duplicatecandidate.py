from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("helpdesk", "0039_alter_ticketchange_field"),
    ]

    operations = [
        migrations.CreateModel(
            name="DuplicateCandidate",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "title_similarity",
                    models.FloatField(
                        default=0.0,
                        help_text="Similarity score between titles (0.0-1.0)",
                        verbose_name="Title similarity",
                    ),
                ),
                (
                    "description_similarity",
                    models.FloatField(
                        default=0.0,
                        help_text="Similarity score between descriptions (0.0-1.0)",
                        verbose_name="Description similarity",
                    ),
                ),
                (
                    "overall_score",
                    models.FloatField(
                        db_index=True,
                        default=0.0,
                        help_text="Weighted overall similarity score",
                        verbose_name="Overall score",
                    ),
                ),
                (
                    "detected_by",
                    models.CharField(
                        choices=[
                            ("auto", "Auto-detected"),
                            ("manual", "Manually marked"),
                        ],
                        default="auto",
                        max_length=10,
                        verbose_name="Detected by",
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending Review"),
                            ("merged", "Merged"),
                            ("dismissed", "Dismissed"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=10,
                        verbose_name="Status",
                    ),
                ),
                (
                    "reviewed_at",
                    models.DateTimeField(blank=True, null=True, verbose_name="Reviewed at"),
                ),
                (
                    "created",
                    models.DateTimeField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name="Created",
                    ),
                ),
                (
                    "detected_by_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="duplicate_detector",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Detected by user",
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="duplicate_reviewer",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Reviewed