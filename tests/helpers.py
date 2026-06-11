# -*- coding: utf-8 -*-

from contextlib import contextmanager
from datetime import datetime, date
from unittest.mock import MagicMock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from helpdesk import settings as helpdesk_settings
from helpdesk.models import Queue, Ticket
import sys


User = get_user_model()


def get_user(
    username="helpdesk.staff", password="password", is_staff=False, is_superuser=False
):
    try:
        user = User.objects.get(username=username)
    except User.DoesNotExist:
        user = User.objects.create_user(
            username=username, password=password, email="%s@example.com" % username
        )
        user.is_staff = is_staff
        user.is_superuser = is_superuser
        user.save()
    else:
        user.set_password(password)
        user.save()
    return user


def get_staff_user():
    return get_user(is_staff=True)


def reload_urlconf(urlconf=None):
    from importlib import reload

    if urlconf is None:
        from django.conf import settings

        urlconf = settings.ROOT_URLCONF

    if HELPDESK_URLCONF in sys.modules:
        reload(sys.modules[HELPDESK_URLCONF])

    if urlconf in sys.modules:
        reload(sys.modules[urlconf])

    from django.urls import clear_url_caches

    clear_url_caches()


def create_ticket(**kwargs):
    q = kwargs.get("queue", None)
    if q is None:
        try:
            q = Queue.objects.all()[0]
        except IndexError:
            q = Queue.objects.create(
                title="Test Q",
                slug="test",
            )
    data = {
        "title": "I wish to register a complaint",
        "queue": q,
    }
    data.update(kwargs)
    return Ticket.objects.create(**data)


HELPDESK_URLCONF = "helpdesk.urls"


def print_response(response, stdout=False):
    content = response.content.decode()
    if stdout:
        print(content)
    else:
        with open("response.html", "w") as f:  # pragma: no cover
            f.write(content)  # pragma: no cover


def create_ticket_with_created_date(created_date, **kwargs):
    """Create a ticket with a specific created date, bypassing auto-now-add in save()."""
    ticket = Ticket.objects.create(**kwargs)
    ticket.created = created_date
    ticket.save()
    return ticket


class HelpdeskSettingsOverride:
    """Context manager to override helpdesk_settings module attributes.

    Since helpdesk_settings reads settings at module import time, Django's
    standard @override_settings decorator cannot affect the module-level variables.
    This context manager directly modifies and restores module attributes.
    """
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
                try:
                    delattr(helpdesk_settings, key)
                except AttributeError:
                    pass
        return False


def patch_escalation_datetime(test_date, test_time=None):
    """Context manager to patch both date.today() and timezone.now() in escalation command.

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


class HelpdeskSLAEscalationTestCase(TestCase):
    """Base test case for SLA calculation and escalation cycle tests.

    Provides:
    - Automatic setup of common test fixtures (queue, user)
    - Helper methods for creating tickets with specific created dates
    - Settings override with automatic cleanup
    - Escalation datetime patching
    """

    fixtures = ["emailtemplate.json"]

    def setUp(self):
        super().setUp()
        self.queue = Queue.objects.create(
            title="Test Queue",
            slug="test-queue",
            escalate_days=2,
        )
        self.user = User.objects.create(
            username="test_user",
            is_staff=True,
        )

    def override_helpdesk_settings(self, **kwargs):
        """Context manager to override helpdesk settings with automatic cleanup."""
        return HelpdeskSettingsOverride(**kwargs)

    def create_ticket_for_date(self, created_date, **kwargs):
        """Create a ticket with a specific created date."""
        kwargs.setdefault("queue", self.queue)
        kwargs.setdefault("title", "Test Ticket")
        kwargs.setdefault("description", "Test Description")
        kwargs.setdefault("priority", 3)
        kwargs.setdefault("status", Ticket.OPEN_STATUS)
        return create_ticket_with_created_date(created_date, **kwargs)

    def patch_escalation(self, test_date, test_time=None):
        """Patch escalation command datetime."""
        return patch_escalation_datetime(test_date, test_time)
