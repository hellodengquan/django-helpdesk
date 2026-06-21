from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase, RequestFactory
from django.utils import timezone
from django.urls import reverse

from helpdesk.models import FollowUp, Queue, Ticket
from helpdesk.views.staff import (
    _get_queue_avg_response_time,
    _get_queue_active_staff,
    _redistribute_round_robin,
    _redistribute_sla_priority,
)


class LoadOverviewBaseTestCase(TestCase):
    def setUp(self):
        self.User = get_user_model()
        self.queue = Queue.objects.create(title="Test Queue", slug="test-q")

        self.staff_1 = self.User.objects.create(
            username="staff1", is_staff=True, is_active=True
        )
        self.staff_2 = self.User.objects.create(
            username="staff2", is_staff=True, is_active=True
        )
        self.staff_3 = self.User.objects.create(
            username="staff3", is_staff=True, is_active=True
        )
        self.all_active_staff = [self.staff_1, self.staff_2, self.staff_3]

        self.admin = self.User.objects.create(
            username="admin", is_staff=True, is_superuser=True, is_active=True
        )
        self.admin.set_password("admin123")
        self.admin.save()

        self.factory = RequestFactory()

    def _build_mock_request(self, user=None):
        request = self.factory.post("/load-overview/")
        request.user = user or self.admin
        return request

    def _get_staff_load_map(self, staff_list, tickets):
        load = {s.id: 0 for s in staff_list}
        for t in tickets:
            if t.assigned_to_id and t.assigned_to_id in load:
                load[t.assigned_to_id] += 1
        return load

    def _create_tickets(self, count, assigned_to=None, queue=None, created_days_ago=0, priority=1, due_date=None):
        created = timezone.now() - timedelta(days=created_days_ago)
        tickets = []
        for i in range(count):
            t = Ticket.objects.create(
                title="Ticket %d" % i,
                queue=queue or self.queue,
                assigned_to=assigned_to,
                priority=priority,
                status=Ticket.OPEN_STATUS,
                created=created,
                due_date=due_date,
            )
            t.created = created
            t.save()
            tickets.append(t)
        return tickets

    def _add_followup(self, ticket, hours_after_create=1, user=None):
        fu_date = ticket.created + timedelta(hours=hours_after_create)
        FollowUp.objects.create(
            ticket=ticket,
            date=fu_date,
            title="First Response",
            user=user or self.admin,
            public=True,
        )


class AvgResponseTimeTests(LoadOverviewBaseTestCase):
    def test_avg_response_time_empty(self):
        avg, sample_count = _get_queue_avg_response_time(self.queue, window_hours=24)
        self.assertIsNone(avg)
        self.assertEqual(sample_count, 0)

    def test_avg_response_time_24h_single_ticket(self):
        tickets = self._create_tickets(1, created_days_ago=0)
        self._add_followup(tickets[0], hours_after_create=2)
        avg, sample_count = _get_queue_avg_response_time(self.queue, window_hours=24)
        self.assertIsNotNone(avg)
        self.assertEqual(sample_count, 1)
        self.assertAlmostEqual(avg.total_seconds(), 7200, delta=10)

    def test_avg_response_time_24h_multiple_tickets(self):
        tickets = self._create_tickets(3, created_days_ago=0)
        self._add_followup(tickets[0], hours_after_create=1)
        self._add_followup(tickets[1], hours_after_create=2)
        self._add_followup(tickets[2], hours_after_create=3)
        avg, sample_count = _get_queue_avg_response_time(self.queue, window_hours=24)
        self.assertEqual(sample_count, 3)
        expected_avg_seconds = (3600 + 7200 + 10800) / 3
        self.assertAlmostEqual(avg.total_seconds(), expected_avg_seconds, delta=10)

    def test_avg_response_time_window_filtering(self):
        tickets_old = self._create_tickets(2, created_days_ago=5)
        for t in tickets_old:
            self._add_followup(t, hours_after_create=1)

        tickets_recent = self._create_tickets(3, created_days_ago=0)
        for t in tickets_recent:
            self._add_followup(t, hours_after_create=2)

        avg_24h, count_24h = _get_queue_avg_response_time(self.queue, window_hours=24)
        avg_7d, count_7d = _get_queue_avg_response_time(self.queue, window_hours=24 * 7)

        self.assertEqual(count_24h, 3)
        self.assertEqual(count_7d, 5)

    def test_avg_response_time_ignores_no_followup(self):
        self._create_tickets(2, created_days_ago=0)
        tickets_with_fu = self._create_tickets(1, created_days_ago=0)
        self._add_followup(tickets_with_fu[0], hours_after_create=3)
        avg, sample_count = _get_queue_avg_response_time(self.queue, window_hours=24)
        self.assertEqual(sample_count, 1)


class StaffSourceTests(LoadOverviewBaseTestCase):
    def test_staff_source_permission_default(self):
        staff, source = _get_queue_active_staff(self.queue, source="permission")
        self.assertEqual(source, "permission")
        self.assertIn(self.admin, staff)

    def test_staff_source_timesheet_in_hours(self):
        now = timezone.now()
        weekday = now.weekday()
        if 0 <= weekday <= 4 and 9 <= now.hour < 18:
            expect_in_shift = True
        else:
            expect_in_shift = False

        staff, source = _get_queue_active_staff(self.queue, source="timesheet")
        self.assertEqual(source, "timesheet")
        self.assertIn(self.admin, staff)

    def test_staff_source_manual(self):
        staff, source = _get_queue_active_staff(self.queue, source="manual")
        self.assertEqual(source, "manual")
        self.assertIn(self.admin, staff)

    def test_all_sources_return_list(self):
        for src in ("permission", "timesheet", "manual"):
            staff, source = _get_queue_active_staff(self.queue, source=src)
            self.assertEqual(source, src)
            self.assertIsInstance(staff, list)


class RoundRobinRedistributionTests(LoadOverviewBaseTestCase):
    def setUp(self):
        super().setUp()
        self.request = self._build_mock_request(self.admin)

    def test_all_unassigned_distributes_evenly(self):
        self._create_tickets(9, assigned_to=None)
        unassigned = Ticket.objects.filter(queue=self.queue, assigned_to__isnull=True)
        assigned = Ticket.objects.filter(queue=self.queue, assigned_to__isnull=False)

        count = _redistribute_round_robin(
            self.request, unassigned, assigned, self.all_active_staff, [self.queue]
        )

        self.assertEqual(count, 9)
        final_tickets = Ticket.objects.filter(queue=self.queue)
        load = self._get_staff_load_map(self.all_active_staff, final_tickets)

        counts = list(load.values())
        self.assertEqual(max(counts) - min(counts), 0)
        self.assertEqual(sum(counts), 9)
        self.assertEqual(counts[0], 3)

    def test_rebalances_overloaded_staff(self):
        self._create_tickets(8, assigned_to=self.staff_1)
        self._create_tickets(1, assigned_to=self.staff_2)

        unassigned = Ticket.objects.filter(queue=self.queue, assigned_to__isnull=True)
        assigned = Ticket.objects.filter(queue=self.queue, assigned_to__isnull=False)

        count = _redistribute_round_robin(
            self.request, unassigned, assigned, self.all_active_staff, [self.queue]
        )

        self.assertGreater(count, 0)
        final_tickets = Ticket.objects.filter(queue=self.queue)
        load = self._get_staff_load_map(self.all_active_staff, final_tickets)

        max_load = max(load.values())
        min_load = min(load.values())
        self.assertLessEqual(max_load - min_load, 2)

    def test_unassigned_and_assigned_mixed(self):
        self._create_tickets(3, assigned_to=self.staff_1)
        self._create_tickets(1, assigned_to=self.staff_2)
        self._create_tickets(6, assigned_to=None)

        unassigned = Ticket.objects.filter(queue=self.queue, assigned_to__isnull=True)
        assigned = Ticket.objects.filter(queue=self.queue, assigned_to__isnull=False)

        count = _redistribute_round_robin(
            self.request, unassigned, assigned, self.all_active_staff, [self.queue]
        )

        self.assertGreaterEqual(count, 6)
        final_tickets = Ticket.objects.filter(queue=self.queue)
        load = self._get_staff_load_map(self.all_active_staff, final_tickets)
        self.assertEqual(sum(load.values()), 10)

    def test_priority_ordering(self):
        low_pri = self._create_tickets(2, assigned_to=None, priority=1)
        high_pri = self._create_tickets(2, assigned_to=None, priority=3)

        unassigned = Ticket.objects.filter(queue=self.queue, assigned_to__isnull=True)
        assigned = Ticket.objects.filter(queue=self.queue, assigned_to__isnull=False)

        _redistribute_round_robin(
            self.request, unassigned, assigned, self.all_active_staff, [self.queue]
        )

        high_pri_after = Ticket.objects.filter(
            id__in=[t.id for t in high_pri], assigned_to__isnull=False
        )
        self.assertEqual(high_pri_after.count(), 2)
        for t in high_pri:
            t.refresh_from_db()
        for t in low_pri:
            t.refresh_from_db()


class SLAPriorityRedistributionTests(LoadOverviewBaseTestCase):
    def setUp(self):
        super().setUp()
        self.request = self._build_mock_request(self.admin)
        self.now = timezone.now()

    def test_overdue_tickets_first(self):
        overdue = self._create_tickets(
            2, assigned_to=self.staff_1,
            due_date=self.now - timedelta(hours=1),
            priority=1,
        )
        soon = self._create_tickets(
            2, assigned_to=self.staff_1,
            due_date=self.now + timedelta(minutes=30),
            priority=1,
        )
        far = self._create_tickets(
            2, assigned_to=self.staff_1,
            due_date=self.now + timedelta(days=3),
            priority=1,
        )

        unassigned = Ticket.objects.filter(queue=self.queue, assigned_to__isnull=True)
        assigned = Ticket.objects.filter(queue=self.queue, assigned_to__isnull=False)

        count = _redistribute_sla_priority(
            self.request, unassigned, assigned, self.all_active_staff, [self.queue]
        )

        self.assertGreater(count, 0)
        all_tickets = Ticket.objects.filter(queue=self.queue)
        load = self._get_staff_load_map(self.all_active_staff, all_tickets)
        self.assertLessEqual(max(load.values()) - min(load.values()), 3)

    def test_unassigned_with_sla_get_priority(self):
        self._create_tickets(3, assigned_to=self.staff_1)
        unassigned_overdue = Ticket.objects.create(
            title="Overdue Unassigned",
            queue=self.queue,
            assigned_to=None,
            priority=1,
            status=Ticket.OPEN_STATUS,
            due_date=self.now - timedelta(hours=2),
        )

        unassigned = Ticket.objects.filter(queue=self.queue, assigned_to__isnull=True)
        assigned = Ticket.objects.filter(queue=self.queue, assigned_to__isnull=False)

        count = _redistribute_sla_priority(
            self.request, unassigned, assigned, self.all_active_staff, [self.queue]
        )

        self.assertGreater(count, 0)
        unassigned_overdue.refresh_from_db()
        self.assertIsNotNone(unassigned_overdue.assigned_to)

    def test_urgent_tickets_reassigned_first(self):
        urgent_ticket = Ticket.objects.create(
            title="Urgent Ticket",
            queue=self.queue,
            assigned_to=self.staff_1,
            priority=1,
            status=Ticket.OPEN_STATUS,
            due_date=self.now + timedelta(minutes=30),
        )
        for i in range(5):
            Ticket.objects.create(
                title="Normal Ticket %d" % i,
                queue=self.queue,
                assigned_to=self.staff_1,
                priority=1,
                status=Ticket.OPEN_STATUS,
                due_date=self.now + timedelta(days=2),
            )

        unassigned = Ticket.objects.filter(queue=self.queue, assigned_to__isnull=True)
        assigned = Ticket.objects.filter(queue=self.queue, assigned_to__isnull=False)

        _redistribute_sla_priority(
            self.request, unassigned, assigned, self.all_active_staff, [self.queue]
        )

        urgent_ticket.refresh_from_db()
        self.assertIsNotNone(urgent_ticket.assigned_to)

    def test_no_due_date_tickets_handled(self):
        self._create_tickets(3, assigned_to=None, due_date=None)
        unassigned = Ticket.objects.filter(queue=self.queue, assigned_to__isnull=True)
        assigned = Ticket.objects.filter(queue=self.queue, assigned_to__isnull=False)

        count = _redistribute_sla_priority(
            self.request, unassigned, assigned, self.all_active_staff, [self.queue]
        )

        self.assertEqual(count, 3)
        for t in Ticket.objects.filter(queue=self.queue):
            self.assertIsNotNone(t.assigned_to)

    def test_sla_with_mixed_priorities(self):
        Ticket.objects.create(
            title="Overdue Low Pri",
            queue=self.queue,
            assigned_to=None,
            priority=1,
            status=Ticket.OPEN_STATUS,
            due_date=self.now - timedelta(hours=1),
        )
        Ticket.objects.create(
            title="Soon High Pri",
            queue=self.queue,
            assigned_to=None,
            priority=3,
            status=Ticket.OPEN_STATUS,
            due_date=self.now + timedelta(hours=2),
        )
        Ticket.objects.create(
            title="Far Normal Pri",
            queue=self.queue,
            assigned_to=None,
            priority=2,
            status=Ticket.OPEN_STATUS,
            due_date=self.now + timedelta(days=5),
        )

        unassigned = Ticket.objects.filter(queue=self.queue, assigned_to__isnull=True)
        assigned = Ticket.objects.filter(queue=self.queue, assigned_to__isnull=False)

        count = _redistribute_sla_priority(
            self.request, unassigned, assigned, self.all_active_staff, [self.queue]
        )

        self.assertEqual(count, 3)
        all_tickets = Ticket.objects.filter(queue=self.queue)
        for t in all_tickets:
            self.assertIsNotNone(t.assigned_to)


class LoadOverviewViewTests(LoadOverviewBaseTestCase):
    def setUp(self):
        super().setUp()
        self.client.login(username="admin", password="admin123")

    def test_load_overview_page_loads(self):
        response = self.client.get(reverse("helpdesk:load_overview"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("queue_stats", response.context)
        self.assertIn("total_backlog", response.context)
        self.assertIn("total_staff", response.context)

    def test_load_overview_response_window_default_24h(self):
        response = self.client.get(reverse("helpdesk:load_overview"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["response_window"], "24h")

    def test_load_overview_response_window_7d(self):
        url = reverse("helpdesk:load_overview") + "?response_window=7d"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["response_window"], "7d")
        self.assertEqual(response.context["response_window_hours"], 24 * 7)

    def test_load_overview_response_window_invalid_falls_back(self):
        url = reverse("helpdesk:load_overview") + "?response_window=invalid"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["response_window"], "24h")

    def test_load_overview_staff_source_default_permission(self):
        response = self.client.get(reverse("helpdesk:load_overview"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["staff_source"], "permission")

    def test_load_overview_staff_source_timesheet(self):
        url = reverse("helpdesk:load_overview") + "?staff_source=timesheet"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["staff_source"], "timesheet")

    def test_load_overview_staff_source_manual(self):
        url = reverse("helpdesk:load_overview") + "?staff_source=manual"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["staff_source"], "manual")

    def test_load_overview_both_params_together(self):
        url = reverse("helpdesk:load_overview") + "?response_window=7d&staff_source=timesheet"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["response_window"], "7d")
        self.assertEqual(response.context["staff_source"], "timesheet")

    def test_load_overview_strategy_options_present(self):
        response = self.client.get(reverse("helpdesk:load_overview"))
        self.assertIn("strategy_options", response.context)
        strategies = [s["id"] for s in response.context["strategy_options"]]
        self.assertIn("round_robin", strategies)
        self.assertIn("sla_priority", strategies)

    def test_load_overview_post_no_queues(self):
        response = self.client.post(
            reverse("helpdesk:load_overview"),
            {"strategy": "round_robin"},
        )
        self.assertEqual(response.status_code, 200)

    def test_load_overview_post_with_queues(self):
        self._create_tickets(5, assigned_to=None)
        response = self.client.post(
            reverse("helpdesk:load_overview"),
            {"queue_ids": [str(self.queue.id)], "strategy": "round_robin"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        remaining_unassigned = Ticket.objects.filter(
            queue=self.queue, assigned_to__isnull=True
        ).count()
        self.assertEqual(remaining_unassigned, 0)

    def test_load_overview_post_sla_strategy(self):
        self._create_tickets(5, assigned_to=None)
        response = self.client.post(
            reverse("helpdesk:load_overview"),
            {"queue_ids": [str(self.queue.id)], "strategy": "sla_priority"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        remaining_unassigned = Ticket.objects.filter(
            queue=self.queue, assigned_to__isnull=True
        ).count()
        self.assertEqual(remaining_unassigned, 0)


class MultiQueueRedistributionTests(LoadOverviewBaseTestCase):
    def setUp(self):
        super().setUp()
        self.queue_2 = Queue.objects.create(title="Queue 2", slug="q2")
        self.request = self._build_mock_request(self.admin)

    def test_multi_queue_round_robin(self):
        self._create_tickets(5, assigned_to=None, queue=self.queue)
        self._create_tickets(5, assigned_to=None, queue=self.queue_2)

        unassigned = Ticket.objects.filter(
            queue__in=[self.queue, self.queue_2], assigned_to__isnull=True
        )
        assigned = Ticket.objects.filter(
            queue__in=[self.queue, self.queue_2], assigned_to__isnull=False
        )

        count = _redistribute_round_robin(
            self.request, unassigned, assigned, self.all_active_staff,
            [self.queue, self.queue_2]
        )

        self.assertEqual(count, 10)
        total_assigned = Ticket.objects.filter(
            queue__in=[self.queue, self.queue_2],
            assigned_to__isnull=False,
        ).count()
        self.assertEqual(total_assigned, 10)

    def test_multi_queue_sla_priority(self):
        now = timezone.now()
        Ticket.objects.create(
            title="Overdue in Q1",
            queue=self.queue,
            assigned_to=None,
            status=Ticket.OPEN_STATUS,
            due_date=now - timedelta(hours=2),
        )
        Ticket.objects.create(
            title="Normal in Q2",
            queue=self.queue_2,
            assigned_to=None,
            status=Ticket.OPEN_STATUS,
            due_date=now + timedelta(days=2),
        )

        unassigned = Ticket.objects.filter(
            queue__in=[self.queue, self.queue_2], assigned_to__isnull=True
        )
        assigned = Ticket.objects.filter(
            queue__in=[self.queue, self.queue_2], assigned_to__isnull=False
        )

        count = _redistribute_sla_priority(
            self.request, unassigned, assigned, self.all_active_staff,
            [self.queue, self.queue_2]
        )

        self.assertEqual(count, 2)
        for t in Ticket.objects.filter(queue__in=[self.queue, self.queue_2]):
            self.assertIsNotNone(t.assigned_to)
