#!/usr/bin/python
"""
django-helpdesk - A Django powered ticket tracker for small enterprise.

See LICENSE for details.

reset_demo_data.py - Reset demo data to initial seed state.
    This command clears existing helpdesk data and reloads the demo fixture,
    ensuring a consistent state for demo environments.
"""

import os
import shutil
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.core.management import call_command
from django.utils.translation import gettext as _

from helpdesk.models import (
    Queue,
    Ticket,
    FollowUp,
    FollowUpAttachment,
    TicketChange,
    TicketCC,
    TicketDependency,
    TicketCustomFieldValue,
    KBCategory,
    KBItem,
    KBIAttachment,
    UserSettings,
)


User = get_user_model()


class Command(BaseCommand):
    """reset_demo_data command"""

    help = _(
        "Reset demo data to initial seed state. Clears all helpdesk data "
        "(tickets, queues, followups, attachments, KB items, etc.) and "
        "reloads the demo fixture. This ensures a consistent state for "
        "demo environments and is safe to run multiple times."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--noinput",
            action="store_true",
            help="Run without prompting for confirmation.",
        )
        parser.add_argument(
            "--keep-users",
            action="store_true",
            help="Keep existing user accounts (only clear helpdesk data).",
        )
        parser.add_argument(
            "--keep-attachments",
            action="store_true",
            help="Keep existing attachment files on disk.",
        )
        parser.add_argument(
            "--fixture",
            type=str,
            default=None,
            help="Path to the demo fixture JSON file. If not provided, "
            "will attempt to find it automatically.",
        )

    def handle(self, *args, **options):
        """Handle command line"""
        noinput = options.get("noinput", False)
        keep_users = options.get("keep_users", False)
        keep_attachments = options.get("keep_attachments", False)
        fixture_path = options.get("fixture", None)

        if not noinput:
            confirm = input(
                _(
                    "This will DELETE ALL helpdesk data and reset to demo state. "
                    "Are you sure you want to continue? (yes/no): "
                )
            )
            if confirm.lower() != "yes":
                self.stdout.write(self.style.WARNING(_("Operation cancelled.")))
                return

        self.stdout.write(_("Resetting demo data..."))

        self._clear_data(keep_users)
        self._clear_attachment_files(keep_attachments)
        self._load_demo_fixture(fixture_path)
        self._verify_data()

        self.stdout.write(
            self.style.SUCCESS(_("Demo data reset completed successfully!"))
        )
        self.stdout.write(_("Admin user: admin / Pa33w0rd"))

    def _clear_data(self, keep_users=False):
        """Clear all helpdesk-related data in correct order to avoid FK issues."""
        self.stdout.write(_("  Clearing existing data..."))

        FollowUpAttachment.objects.all().delete()
        KBIAttachment.objects.all().delete()
        TicketChange.objects.all().delete()
        TicketCC.objects.all().delete()
        TicketDependency.objects.all().delete()
        TicketCustomFieldValue.objects.all().delete()
        FollowUp.objects.all().delete()
        Ticket.objects.all().delete()
        KBItem.objects.all().delete()
        KBCategory.objects.all().delete()

        for queue in Queue.objects.all():
            queue.delete()

        if not keep_users:
            UserSettings.objects.all().delete()
            User.objects.filter(is_superuser=False).delete()
            User.objects.filter(username="admin").delete()

        self.stdout.write(self.style.SUCCESS(_("  Data cleared.")))

    def _clear_attachment_files(self, keep_attachments=False):
        """Clear attachment files from media directory, preserving demo fixtures."""
        if keep_attachments:
            self.stdout.write(_("  Keeping existing attachment files."))
            return

        self.stdout.write(
            _("  Clearing attachment files (preserving demo fixtures)...")
        )

        attachment_dir = os.path.join(settings.MEDIA_ROOT, "helpdesk", "attachments")
        demo_attachments = {"DH-3"}

        if os.path.exists(attachment_dir):
            for item in os.listdir(attachment_dir):
                if item in demo_attachments:
                    continue
                item_path = os.path.join(attachment_dir, item)
                if os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                else:
                    os.remove(item_path)

        self.stdout.write(self.style.SUCCESS(_("  Attachment files cleared.")))

    def _load_demo_fixture(self, fixture_path=None):
        """Load the demo fixture data."""
        self.stdout.write(_("  Loading demo fixture..."))

        demo_fixture = None

        if fixture_path:
            if os.path.isabs(fixture_path):
                demo_fixture = fixture_path
            else:
                demo_fixture = os.path.abspath(fixture_path)
            if not os.path.exists(demo_fixture):
                raise FileNotFoundError(
                    _("Specified demo fixture not found at: %s") % demo_fixture
                )
        else:
            fixture_dirs = getattr(settings, "FIXTURE_DIRS", [])
            for fixture_dir in fixture_dirs:
                candidate = os.path.join(fixture_dir, "demo.json")
                if os.path.exists(candidate):
                    demo_fixture = candidate
                    break

            if demo_fixture is None:
                helpdesk_app_dir = os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                )
                project_root = os.path.dirname(helpdesk_app_dir)
                candidate = os.path.join(
                    project_root, "demodesk", "fixtures", "demo.json"
                )
                if os.path.exists(candidate):
                    demo_fixture = candidate

            if demo_fixture is None:
                pkg_root = os.path.dirname(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                )
                candidate = os.path.join(
                    pkg_root, "..", "demodesk", "fixtures", "demo.json"
                )
                candidate = os.path.normpath(candidate)
                if os.path.exists(candidate):
                    demo_fixture = candidate

        if demo_fixture is None:
            raise FileNotFoundError(
                _(
                    "Demo fixture 'demo.json' not found. "
                    "Please specify using --fixture option."
                )
            )

        call_command("loaddata", demo_fixture, verbosity=0)

        self.stdout.write(self.style.SUCCESS(_("  Demo fixture loaded.")))

    def _verify_data(self):
        """Verify that the expected data is present."""
        self.stdout.write(_("  Verifying data..."))

        expected_queues = ["Django Helpdesk", "Some Product"]
        expected_tickets = 3
        expected_followups = 4
        expected_attachments = 2
        expected_kb_categories = 2
        expected_kb_items = 3

        queues = list(Queue.objects.values_list("title", flat=True))
        for queue_name in expected_queues:
            if queue_name not in queues:
                self.stdout.write(
                    self.style.WARNING(_("  Missing expected queue: %s") % queue_name)
                )

        ticket_count = Ticket.objects.count()
        if ticket_count != expected_tickets:
            self.stdout.write(
                self.style.WARNING(
                    _("  Expected %(expected)d tickets, found %(found)d")
                    % {"expected": expected_tickets, "found": ticket_count}
                )
            )

        followup_count = FollowUp.objects.count()
        if followup_count != expected_followups:
            self.stdout.write(
                self.style.WARNING(
                    _("  Expected %(expected)d followups, found %(found)d")
                    % {"expected": expected_followups, "found": followup_count}
                )
            )

        attachment_count = FollowUpAttachment.objects.count()
        if attachment_count != expected_attachments:
            self.stdout.write(
                self.style.WARNING(
                    _("  Expected %(expected)d attachments, found %(found)d")
                    % {"expected": expected_attachments, "found": attachment_count}
                )
            )

        kb_category_count = KBCategory.objects.count()
        if kb_category_count != expected_kb_categories:
            self.stdout.write(
                self.style.WARNING(
                    _("  Expected %(expected)d KB categories, found %(found)d")
                    % {"expected": expected_kb_categories, "found": kb_category_count}
                )
            )

        kb_item_count = KBItem.objects.count()
        if kb_item_count != expected_kb_items:
            self.stdout.write(
                self.style.WARNING(
                    _("  Expected %(expected)d KB items, found %(found)d")
                    % {"expected": expected_kb_items, "found": kb_item_count}
                )
            )

        admin_user = User.objects.filter(username="admin").first()
        if not admin_user:
            self.stdout.write(self.style.WARNING(_("  Admin user not found!")))

        self.stdout.write(self.style.SUCCESS(_("  Verification complete.")))
