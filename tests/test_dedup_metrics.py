"""Tests for fuzzy dedup metrics: counters, dashboard view, and Prometheus fallback."""

from django.contrib.auth import get_user_model
from django.test import override_settings, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone
from datetime import timedelta
from unittest import mock

from freezegun import freeze_time

import helpdesk.email
from helpdesk import metrics as dedup_metrics
from helpdesk.models import FollowUp, Queue, Ticket
from tests import utils


User = get_user_model()


class DedupMetricsCounterTests(TestCase):
    """Verify that the three dedup counters increment correctly as the
    email parser makes merge/new/window-miss decisions."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    def setUp(self):
        self.queue = Queue.objects.create(
            title="Metrics Test Queue", slug="metricsq", email_box_type="local"
        )
        self.logger = dedup_metrics.logger
        dedup_metrics.reset_fallback_counts()

    def tearDown(self):
        dedup_metrics.reset_fallback_counts()

    def _send_reply_without_headers(self, subject, sender_email):
        msg, _, _ = utils.generate_email_with_subject(subject=subject, body="reply")
        del msg["From"]
        msg["From"] = f"Reporter <{sender_email}>"
        del msg["Message-Id"]
        if "In-Reply-To" in msg:
            del msg["In-Reply-To"]
        if "References" in msg:
            del msg["References"]
        return helpdesk.email.extract_email_metadata(
            msg.as_string(), self.queue, self.logger
        )

    def test_merge_hit_counter_increments(self):
        """When the fuzzy matcher merges into an existing ticket the
        MERGE_HIT counter should be incremented."""
        subject = "Printer is on fire"
        sender = "merge-hit@example.com"

        msg1, _, _ = utils.generate_email_with_subject(subject=subject, body="first")
        del msg1["From"]
        msg1["From"] = f"Reporter <{sender}>"
        ticket1 = helpdesk.email.extract_email_metadata(
            msg1.as_string(), self.queue, self.logger
        )
        self.assertIsNotNone(ticket1)

        before = dedup_metrics.get_queue_7day_counts(self.queue.slug)
        self.assertEqual(before[dedup_metrics.METRIC_MERGE_HIT], 0)

        ticket2 = self._send_reply_without_headers(f"Re: {subject}", sender)
        self.assertEqual(ticket2.id, ticket1.id)

        after = dedup_metrics.get_queue_7day_counts(self.queue.slug)
        self.assertEqual(after[dedup_metrics.METRIC_MERGE_HIT], 1)
        self.assertEqual(after[dedup_metrics.METRIC_NEW_TICKET], 0)
        self.assertEqual(after[dedup_metrics.METRIC_WINDOW_MISS], 0)

    def test_new_ticket_counter_increments_when_no_match(self):
        """When the fuzzy matcher finds no matching ticket the NEW_TICKET
        counter should be incremented."""
        sender = "new-ticket@example.com"

        self._send_reply_without_headers("Re: Issue Alpha", sender)

        counts = dedup_metrics.get_queue_7day_counts(self.queue.slug)
        self.assertEqual(counts[dedup_metrics.METRIC_NEW_TICKET], 1)
        self.assertEqual(counts[dedup_metrics.METRIC_MERGE_HIT], 0)
        self.assertEqual(counts[dedup_metrics.METRIC_WINDOW_MISS], 0)

    @override_settings(HELPDESK_FUZZY_DEDUP_TIME_WINDOW_HOURS=24)
    def test_window_miss_counter_increments_when_ticket_too_old(self):
        """When sender+subject match but the existing ticket is older than
        the dedup window the WINDOW_MISS counter should be incremented."""
        subject = "WiFi keeps dropping"
        sender = "window-miss@example.com"

        with freeze_time(timezone.now() - timedelta(hours=30)):
            msg1, _, _ = utils.generate_email_with_subject(subject=subject, body="first")
            del msg1["From"]
            msg1["From"] = f"Reporter <{sender}>"
            ticket1 = helpdesk.email.extract_email_metadata(
                msg1.as_string(), self.queue, self.logger
            )
            self.assertIsNotNone(ticket1)

        ticket2 = self._send_reply_without_headers(f"Re: {subject}", sender)
        self.assertNotEqual(ticket2.id, ticket1.id)

        counts = dedup_metrics.get_queue_7day_counts(self.queue.slug)
        self.assertEqual(counts[dedup_metrics.METRIC_WINDOW_MISS], 1)
        self.assertEqual(counts[dedup_metrics.METRIC_MERGE_HIT], 0)

    def test_inc_metric_ignores_unknown_names(self):
        """Passing an unknown metric name to inc_metric should be a no-op
        rather than raising."""
        dedup_metrics.inc_metric("bogus_metric", self.queue.slug)
        counts = dedup_metrics.get_queue_7day_counts(self.queue.slug)
        self.assertEqual(counts[dedup_metrics.METRIC_MERGE_HIT], 0)
        self.assertEqual(counts[dedup_metrics.METRIC_NEW_TICKET], 0)
        self.assertEqual(counts[dedup_metrics.METRIC_WINDOW_MISS], 0)


class DedupDashboardViewTests(TestCase):
    """Verify the dedup metrics dashboard view returns the correct context."""

    def setUp(self):
        self.queue_a = Queue.objects.create(
            title="Alpha Queue", slug="alpha", email_box_type="local"
        )
        self.queue_b = Queue.objects.create(
            title="Beta Queue", slug="beta", email_box_type="local"
        )
        self.user = User.objects.create_user(
            username="staffuser", password="pass123", is_staff=True
        )
        self.factory = RequestFactory()
        dedup_metrics.reset_fallback_counts()

    def tearDown(self):
        dedup_metrics.reset_fallback_counts()

    def test_view_redirects_for_anonymous(self):
        url = reverse("helpdesk:dedup_metrics_dashboard")
        response = self.client.get(url)
        self.assertIn(response.status_code, (302, 403))

    def test_view_context_contains_queue_names_and_counts(self):
        dedup_metrics.inc_metric(dedup_metrics.METRIC_MERGE_HIT, self.queue_a.slug)
        dedup_metrics.inc_metric(dedup_metrics.METRIC_MERGE_HIT, self.queue_a.slug)
        dedup_metrics.inc_metric(dedup_metrics.METRIC_NEW_TICKET, self.queue_a.slug)
        dedup_metrics.inc_metric(dedup_metrics.METRIC_WINDOW_MISS, self.queue_b.slug)

        from helpdesk.views.staff import dedup_metrics_dashboard

        request = self.factory.get("/helpdesk/dedup-metrics/")
        request.user = self.user
        response = dedup_metrics_dashboard(request)

        self.assertEqual(response.status_code, 200)
        ctx = response.context_data

        rows = ctx["rows"]
        by_slug = {row["queue_slug"]: row for row in rows}
        self.assertEqual(by_slug["alpha"]["merge_hits"], 2)
        self.assertEqual(by_slug["alpha"]["new_tickets"], 1)
        self.assertEqual(by_slug["alpha"]["queue_title"], "Alpha Queue")
        self.assertEqual(by_slug["beta"]["window_misses"], 1)
        self.assertEqual(by_slug["beta"]["queue_title"], "Beta Queue")

        self.assertEqual(ctx["totals"]["merge_hits"], 2)
        self.assertEqual(ctx["totals"]["new_tickets"], 1)
        self.assertEqual(ctx["totals"]["window_misses"], 1)

    def test_view_empty_when_no_metrics_recorded(self):
        from helpdesk.views.staff import dedup_metrics_dashboard

        request = self.factory.get("/helpdesk/dedup-metrics/")
        request.user = self.user
        response = dedup_metrics_dashboard(request)
        ctx = response.context_data

        rows = ctx["rows"]
        for row in rows:
            self.assertEqual(row["merge_hits"], 0)
            self.assertEqual(row["new_tickets"], 0)
            self.assertEqual(row["window_misses"], 0)


class DedupMetricsPrometheusFallbackTests(TestCase):
    """Ensure the metrics module degrades gracefully when the optional
    ``prometheus_client`` package is not installed."""

    def setUp(self):
        self.queue = Queue.objects.create(
            title="Fallback Queue", slug="fb", email_box_type="local"
        )
        dedup_metrics.reset_fallback_counts()

    def tearDown(self):
        dedup_metrics.reset_fallback_counts()

    def test_fallback_store_works_without_prometheus(self):
        """Even when PROMETHEUS_AVAILABLE is False, inc_metric and
        get_queue_7day_counts should still function correctly via the
        in-process fallback store."""
        original = dedup_metrics.PROMETHEUS_AVAILABLE
        try:
            dedup_metrics.PROMETHEUS_AVAILABLE = False
            dedup_metrics.inc_metric(dedup_metrics.METRIC_MERGE_HIT, self.queue.slug)
            dedup_metrics.inc_metric(dedup_metrics.METRIC_NEW_TICKET, self.queue.slug)
            dedup_metrics.inc_metric(dedup_metrics.METRIC_WINDOW_MISS, self.queue.slug)

            counts = dedup_metrics.get_queue_7day_counts(self.queue.slug)
            self.assertEqual(counts[dedup_metrics.METRIC_MERGE_HIT], 1)
            self.assertEqual(counts[dedup_metrics.METRIC_NEW_TICKET], 1)
            self.assertEqual(counts[dedup_metrics.METRIC_WINDOW_MISS], 1)
        finally:
            dedup_metrics.PROMETHEUS_AVAILABLE = original

    def test_dashboard_view_renders_without_prometheus(self):
        """The dashboard view should render successfully and surface the
        "Prometheus client unavailable" notice when the library is missing."""
        user = User.objects.create_user(
            username="staffer", password="pw123", is_staff=True
        )
        dedup_metrics.inc_metric(dedup_metrics.METRIC_MERGE_HIT, self.queue.slug)
        self.client.login(username="staffer", password="pw123")

        original = dedup_metrics.PROMETHEUS_AVAILABLE
        try:
            dedup_metrics.PROMETHEUS_AVAILABLE = False
            url = reverse("helpdesk:dedup_metrics_dashboard")
            response = self.client.get(url)
        finally:
            dedup_metrics.PROMETHEUS_AVAILABLE = original

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["prometheus_available"])
        rows = response.context["rows"]
        fb_row = next((r for r in rows if r["queue_slug"] == "fb"), None)
        self.assertIsNotNone(fb_row)
        self.assertEqual(fb_row["merge_hits"], 1)

    def test_get_all_queues_returns_known_slugs(self):
        dedup_metrics.inc_metric(dedup_metrics.METRIC_MERGE_HIT, "x")
        dedup_metrics.inc_metric(dedup_metrics.METRIC_MERGE_HIT, "y")
        all_counts = dedup_metrics.get_all_queues_7day_counts()
        self.assertIn("x", all_counts)
        self.assertIn("y", all_counts)
        self.assertEqual(all_counts["x"][dedup_metrics.METRIC_MERGE_HIT], 1)
        self.assertEqual(all_counts["y"][dedup_metrics.METRIC_MERGE_HIT], 1)
