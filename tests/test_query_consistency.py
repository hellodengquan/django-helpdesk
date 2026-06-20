from collections import defaultdict
from copy import deepcopy
from datetime import timedelta, timezone as py_tz
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.test import TestCase, RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone
from helpdesk.models import (
    CustomField,
    EscalationExclusion,
    FollowUp,
    KBCategory,
    KBItem,
    Queue,
    SavedSearch,
    Ticket,
    TicketCustomFieldValue,
)
from helpdesk.query import (
    TicketQueryBuilder,
    build_query_params_from_request,
    get_default_list_query_params,
    query_from_base64,
    query_to_base64,
)
from helpdesk.user import HelpdeskUser
from .helpers import get_staff_user

User = get_user_model()


class QueryConsistencyTestBase(TestCase):
    def setUp(self):
        self.queue1 = Queue.objects.create(
            title="Queue A", slug="queue_a", allow_public_submission=True
        )
        self.queue2 = Queue.objects.create(
            title="Queue B", slug="queue_b", allow_public_submission=True
        )
        self.staff_user = get_staff_user()
        self.other_staff = User.objects.create_user(
            username="other_staff", password="pass", is_staff=True
        )
        cat = KBCategory.objects.create(
            title="Cat", slug="cat", description="desc", queue=self.queue1
        )
        self.kbitem = KBItem.objects.create(
            category=cat, title="KB1", question="Q?", answer="A"
        )
        self.ticket_open = Ticket.objects.create(
            title="Open Ticket",
            queue=self.queue1,
            description="open desc",
            status=Ticket.OPEN_STATUS,
            priority=1,
            assigned_to=self.staff_user,
        )
        self.ticket_reopened = Ticket.objects.create(
            title="Reopened Ticket",
            queue=self.queue1,
            description="reopened desc",
            status=Ticket.REOPENED_STATUS,
            priority=2,
        )
        self.ticket_resolved = Ticket.objects.create(
            title="Resolved Ticket",
            queue=self.queue2,
            description="resolved desc",
            status=Ticket.RESOLVED_STATUS,
            priority=3,
            assigned_to=self.other_staff,
        )
        self.ticket_closed = Ticket.objects.create(
            title="Closed Ticket",
            queue=self.queue2,
            description="closed desc",
            status=Ticket.CLOSED_STATUS,
            priority=4,
            kbitem=self.kbitem,
        )
        self.ticket_duplicate = Ticket.objects.create(
            title="Duplicate Ticket",
            queue=self.queue1,
            description="duplicate desc",
            status=Ticket.DUPLICATE_STATUS,
            priority=5,
        )
        self.factory = RequestFactory()
        self.huser = HelpdeskUser(self.staff_user)

    def _make_builder(self, query_params=None, base64query=None):
        return TicketQueryBuilder(
            self.huser, query_params=query_params, base64query=base64query
        )

    def _builder_from_request(self, url):
        request = self.factory.get(url)
        params = build_query_params_from_request(request)
        return TicketQueryBuilder(self.huser, query_params=params), params


class FourEntryConsistencyTests(QueryConsistencyTestBase):
    def test_datatables_vs_export_count_no_filter(self):
        builder = self._make_builder(query_params={})
        datatables = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        export_rows = list(builder.iter_export_rows())
        data_rows = export_rows[1:]
        self.assertEqual(
            datatables["recordsTotal"],
            len(data_rows),
            "datatables recordsTotal should match export row count",
        )

    def test_datatables_vs_export_count_status_filter(self):
        params = {
            "filtering": {"status__in": [Ticket.OPEN_STATUS, Ticket.REOPENED_STATUS]},
            "sorting": "created",
        }
        builder = self._make_builder(query_params=params)
        datatables = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        export_rows = list(builder.iter_export_rows())
        data_rows = export_rows[1:]
        self.assertEqual(datatables["recordsTotal"], len(data_rows))
        self.assertEqual(datatables["recordsFiltered"], len(data_rows))

    def test_datatables_vs_queryset_count_with_search(self):
        params = {
            "filtering": {"status__in": [Ticket.OPEN_STATUS]},
            "search_string": "Open",
            "sorting": "created",
        }
        builder = self._make_builder(query_params=params)
        qs_count = builder.count()
        datatables = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(qs_count, datatables["recordsTotal"])

    def test_report_queryset_equals_list_queryset(self):
        params = {
            "filtering": {"queue__id__in": [self.queue1.pk]},
            "sorting": "created",
        }
        builder = self._make_builder(query_params=params)
        qs_tickets = list(builder.get_queryset().values_list("id", flat=True))
        report_qs = list(
            self._make_builder(query_params=params)
            .get_queryset()
            .values_list("id", flat=True)
        )
        self.assertEqual(qs_tickets, report_qs)

    def test_export_ids_match_queryset_ids(self):
        params = {
            "filtering": {"status__in": [Ticket.OPEN_STATUS, Ticket.CLOSED_STATUS]},
            "sorting": "id",
        }
        builder = self._make_builder(query_params=params)
        qs_ids = list(builder.get_queryset().values_list("id", flat=True).order_by("id"))
        export_rows = list(builder.iter_export_rows())
        header = export_rows[0]
        data_rows = export_rows[1:]
        id_col_idx = header.index("id")
        export_ids = [int(row[id_col_idx]) for row in data_rows]
        self.assertEqual(qs_ids, export_ids)

    def test_sorting_consistency_between_querybuilder_and_datatables(self):
        params = {"sorting": "id", "sortreverse": False}
        builder = self._make_builder(query_params=params)
        qs_ids = list(builder.get_queryset().values_list("id", flat=True))
        datatables = builder.get_datatables_context(
            draw=["0"],
            length=["100"],
            start=["0"],
            **{"order[0][column]": ["0"], "order[0][dir]": ["asc"]},
        )
        dt_ids = [d["id"] for d in datatables["data"]]
        self.assertEqual(qs_ids, dt_ids)

    def test_count_consistency_across_all_four_entry_points(self):
        params = {
            "filtering": {"status__in": [Ticket.OPEN_STATUS]},
            "sorting": "created",
        }
        builder = self._make_builder(query_params=params)
        qs_count = builder.count()
        export_rows = list(builder.iter_export_rows())
        data_rows = export_rows[1:]
        datatables = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(qs_count, len(data_rows))
        self.assertEqual(qs_count, datatables["recordsTotal"])
        self.assertEqual(qs_count, datatables["recordsFiltered"])

    def test_empty_result_consistency(self):
        params = {
            "filtering": {"status__in": [99]},
            "sorting": "created",
        }
        builder = self._make_builder(query_params=params)
        qs_count = builder.count()
        self.assertEqual(qs_count, 0)
        export_rows = list(builder.iter_export_rows())
        self.assertEqual(len(export_rows), 1)
        datatables = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(datatables["recordsTotal"], 0)
        self.assertEqual(datatables["recordsFiltered"], 0)
        self.assertEqual(len(datatables["data"]), 0)

    def test_filter_null_and_value_combined_count(self):
        params = {
            "filtering": {"assigned_to__id__in": [self.staff_user.pk]},
            "filtering_null": {"assigned_to__id__isnull": True},
            "sorting": "created",
        }
        builder = self._make_builder(query_params=params)
        manual_count = Ticket.objects.filter(
            Q(assigned_to__id__in=[self.staff_user.pk]) | Q(assigned_to__isnull=True),
            queue__in=self.huser.get_queues(),
        ).distinct().count()
        self.assertEqual(builder.count(), manual_count)

    def test_base64_roundtrip_preserves_count_and_order(self):
        params = {
            "filtering": {"status__in": [Ticket.OPEN_STATUS, Ticket.REOPENED_STATUS]},
            "sorting": "id",
            "sortreverse": False,
        }
        builder_params = self._make_builder(query_params=params)
        qs_ids_params = list(
            builder_params.get_queryset().values_list("id", flat=True)
        )
        b64 = query_to_base64(params)
        builder_b64 = self._make_builder(base64query=b64)
        qs_ids_b64 = list(
            builder_b64.get_queryset().values_list("id", flat=True)
        )
        self.assertEqual(qs_ids_params, qs_ids_b64)
        self.assertEqual(builder_params.count(), builder_b64.count())


class PermissionAndEdgeCaseTests(QueryConsistencyTestBase):
    def test_per_queue_permission_filters_tickets(self):
        from django.contrib.auth.models import Permission
        from django.contrib.contenttypes.models import ContentType
        from helpdesk import settings as helpdesk_settings

        private_queue = Queue.objects.create(
            title="Private Queue", slug="private_queue", allow_public_submission=False
        )
        Ticket.objects.create(
            title="Private Ticket", queue=private_queue, status=Ticket.OPEN_STATUS
        )
        self.queue2.allow_public_submission = False
        self.queue2.save()

        restricted_user = User.objects.create_user(
            username="restricted", password="pass", is_staff=True
        )
        ct = ContentType.objects.get_for_model(Queue)
        perm = Permission.objects.get(
            codename="queue_access_queue_a", content_type=ct
        )
        restricted_user.user_permissions.add(perm)
        restricted_user = User.objects.get(pk=restricted_user.pk)

        old_val = helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION
        try:
            helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = True
            huser_restricted = HelpdeskUser(restricted_user)
            builder = TicketQueryBuilder(huser_restricted, query_params={})
            qs = builder.get_queryset()
            queue_ids = set(qs.values_list("queue_id", flat=True))
            self.assertNotIn(private_queue.pk, queue_ids)
            self.assertNotIn(self.queue2.pk, queue_ids)
            for ticket in qs:
                self.assertEqual(ticket.queue_id, self.queue1.pk)
        finally:
            helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = old_val
            self.queue2.allow_public_submission = True
            self.queue2.save()

    def test_no_queues_user_sees_nothing(self):
        from helpdesk import settings as helpdesk_settings

        Queue.objects.filter(allow_public_submission=True).update(
            allow_public_submission=False
        )
        empty_user = User.objects.create_user(
            username="no_queues", password="pass", is_staff=False
        )
        old_val = helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION
        try:
            helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = True
            huser = HelpdeskUser(empty_user)
            accessible_queues = huser.get_queues()
            self.assertEqual(len(accessible_queues), 0)
            builder = TicketQueryBuilder(huser, query_params={})
            self.assertEqual(builder.count(), 0)
        finally:
            helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = old_val
            Queue.objects.update(allow_public_submission=True)

    def test_merged_ticket_included_in_queryset(self):
        main_ticket = Ticket.objects.create(
            title="Main", queue=self.queue1, status=Ticket.OPEN_STATUS
        )
        merged_ticket = Ticket.objects.create(
            title="Merged", queue=self.queue1, status=Ticket.DUPLICATE_STATUS
        )
        merged_ticket.merged_to = main_ticket
        merged_ticket.save()
        builder = self._make_builder(query_params={})
        qs_ids = list(builder.get_queryset().values_list("id", flat=True))
        self.assertIn(merged_ticket.id, qs_ids)

    def test_duplicate_status_excluded_by_default_list_filter(self):
        default_params = get_default_list_query_params()
        builder = self._make_builder(query_params=default_params)
        qs_statuses = list(
            builder.get_queryset().values_list("status", flat=True)
        )
        self.assertNotIn(Ticket.DUPLICATE_STATUS, qs_statuses)
        self.assertNotIn(Ticket.CLOSED_STATUS, qs_statuses)

    def test_kbitem_null_filter(self):
        params = {"filtering_null": {"kbitem__isnull": True}, "sorting": "created"}
        builder = self._make_builder(query_params=params)
        for t in builder.get_queryset():
            self.assertIsNone(t.kbitem)

    def test_kbitem_value_filter(self):
        params = {
            "filtering": {"kbitem__in": [self.kbitem.pk]},
            "sorting": "created",
        }
        builder = self._make_builder(query_params=params)
        for t in builder.get_queryset():
            self.assertEqual(t.kbitem_id, self.kbitem.pk)

    def test_assigned_to_null_filter(self):
        params = {
            "filtering_null": {"assigned_to__id__isnull": True},
            "sorting": "created",
        }
        builder = self._make_builder(query_params=params)
        for t in builder.get_queryset():
            self.assertIsNone(t.assigned_to_id)

    def test_assigned_to_value_plus_null_or_filter(self):
        params = {
            "filtering": {"assigned_to__id__in": [self.staff_user.pk]},
            "filtering_null": {"assigned_to__id__isnull": True},
            "sorting": "created",
        }
        builder = self._make_builder(query_params=params)
        for t in builder.get_queryset():
            self.assertTrue(
                t.assigned_to_id == self.staff_user.pk or t.assigned_to_id is None
            )

    def test_build_query_params_from_request_minus_1_null(self):
        request = self.factory.get("/tickets/", {"assigned_to": "-1"})
        params = build_query_params_from_request(request)
        self.assertTrue(params["filtering_null"].get("assigned_to__id__isnull"))
        self.assertNotIn("assigned_to__id__in", params.get("filtering", {}))

    def test_build_query_params_from_request_value_and_minus_1(self):
        request = self.factory.get(
            "/tickets/", {"assigned_to": [str(self.staff_user.pk), "-1"]}
        )
        params = build_query_params_from_request(request)
        self.assertTrue(params["filtering_null"].get("assigned_to__id__isnull"))
        self.assertEqual(
            params["filtering"].get("assigned_to__id__in"), [self.staff_user.pk]
        )

    def test_build_query_params_no_params_returns_empty(self):
        request = self.factory.get("/tickets/")
        params = build_query_params_from_request(request)
        self.assertEqual(params["filtering"], {})
        self.assertEqual(params["filtering_null"], {})
        self.assertEqual(params["search_string"], "")

    def test_build_query_params_date_range(self):
        request = self.factory.get(
            "/tickets/", {"date_from": "2025-01-01", "date_to": "2025-12-31"}
        )
        params = build_query_params_from_request(request)
        self.assertEqual(params["filtering"]["created__gte"], "2025-01-01")
        self.assertEqual(params["filtering"]["created__lte"], "2025-12-31")

    def test_build_query_params_invalid_sort_falls_back_to_created(self):
        request = self.factory.get("/tickets/", {"sort": "invalid_field"})
        params = build_query_params_from_request(request)
        self.assertEqual(params["sorting"], "created")

    def test_build_query_params_sortreverse(self):
        request = self.factory.get("/tickets/", {"sort": "created", "sortreverse": "1"})
        params = build_query_params_from_request(request)
        self.assertTrue(params["sortreverse"])

    def test_deleted_ticket_not_in_queryset(self):
        ticket = Ticket.objects.create(
            title="To Delete", queue=self.queue1, status=Ticket.OPEN_STATUS
        )
        ticket_id = ticket.id
        ticket.delete()
        builder = self._make_builder(query_params={})
        qs_ids = list(builder.get_queryset().values_list("id", flat=True))
        self.assertNotIn(ticket_id, qs_ids)

    def test_super_user_sees_all_queues(self):
        superuser = User.objects.create_user(
            username="super", password="pass", is_staff=True, is_superuser=True
        )
        huser = HelpdeskUser(superuser)
        builder = TicketQueryBuilder(huser, query_params={})
        self.assertEqual(builder.count(), Ticket.objects.count())

    def test_queue_filter_limits_to_accessible_queues(self):
        params = {
            "filtering": {"queue__id__in": [self.queue1.pk]},
            "sorting": "created",
        }
        builder = self._make_builder(query_params=params)
        for t in builder.get_queryset():
            self.assertEqual(t.queue_id, self.queue1.pk)

    def test_status_filter_with_multiple_values(self):
        params = {
            "filtering": {
                "status__in": [
                    Ticket.OPEN_STATUS,
                    Ticket.REOPENED_STATUS,
                    Ticket.RESOLVED_STATUS,
                ]
            },
            "sorting": "created",
        }
        builder = self._make_builder(query_params=params)
        qs_statuses = set(
            builder.get_queryset().values_list("status", flat=True)
        )
        self.assertTrue(
            qs_statuses.issubset(
                {Ticket.OPEN_STATUS, Ticket.REOPENED_STATUS, Ticket.RESOLVED_STATUS}
            )
        )

    def test_search_string_filter(self):
        params = {
            "search_string": "open desc",
            "sorting": "created",
        }
        builder = self._make_builder(query_params=params)
        descriptions = list(
            builder.get_queryset().values_list("description", flat=True)
        )
        for desc in descriptions:
            self.assertIn("open desc", desc.lower())

    def test_priority_filter(self):
        params = {
            "filtering": {"priority__in": [1, 2]},
            "sorting": "created",
        }
        builder = self._make_builder(query_params=params)
        for t in builder.get_queryset():
            self.assertIn(t.priority, [1, 2])

    def test_get_base_queryset_returns_only_accessible_queues(self):
        builder = self._make_builder(query_params={})
        base_qs = builder.get_base_queryset()
        queue_ids = set(base_qs.values_list("queue_id", flat=True))
        accessible_ids = set(q.pk for q in self.huser.get_queues())
        self.assertTrue(queue_ids.issubset(accessible_ids))

    def test_query_builder_caching_returns_same_queryset(self):
        builder = self._make_builder(query_params={})
        qs1 = builder.get_queryset()
        qs2 = builder.get_queryset()
        self.assertEqual(list(qs1), list(qs2))

    def test_get_params_returns_deepcopy(self):
        params = {"filtering": {"status__in": [1]}, "sorting": "created"}
        builder = self._make_builder(query_params=params)
        returned = builder.get_params()
        returned["filtering"]["status__in"].append(2)
        self.assertEqual(builder.params["filtering"]["status__in"], [1])


class ExportBatchTests(QueryConsistencyTestBase):
    def test_export_header_has_standard_columns(self):
        builder = self._make_builder(query_params={})
        rows = list(builder.iter_export_rows())
        header = rows[0]
        expected = [
            "id", "queue", "title", "status", "priority", "assigned_to",
            "submitter_email", "created", "modified", "due_date", "kbitem",
            "description", "resolution",
        ]
        for col in expected:
            self.assertIn(col, header)

    def test_export_without_custom_fields(self):
        builder = self._make_builder(query_params={})
        rows = list(builder.iter_export_rows(include_custom_fields=False))
        header = rows[0]
        self.assertEqual(len(header), 13)
        self.assertEqual(header[0], "id")

    def test_export_with_custom_fields(self):
        cf = CustomField.objects.create(
            name="test_cf", label="Test CF", data_type="varchar", max_length=50
        )
        TicketCustomFieldValue.objects.create(
            ticket=self.ticket_open, field=cf, value="cf_val"
        )
        builder = self._make_builder(query_params={})
        rows = list(builder.iter_export_rows(include_custom_fields=True))
        header = rows[0]
        self.assertIn("test_cf", header)
        cf_col_idx = header.index("test_cf")
        id_col_idx = header.index("id")
        for row in rows[1:]:
            if int(row[id_col_idx]) == self.ticket_open.pk:
                self.assertEqual(row[cf_col_idx], "cf_val")

    def test_export_row_count_equals_queryset_count(self):
        params = {
            "filtering": {"status__in": [Ticket.OPEN_STATUS, Ticket.REOPENED_STATUS]},
            "sorting": "created",
        }
        builder = self._make_builder(query_params=params)
        qs_count = builder.count()
        rows = list(builder.iter_export_rows())
        self.assertEqual(len(rows) - 1, qs_count)

    def test_export_csv_endpoint_response(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("helpdesk:export_tickets_csv"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("tickets.csv", response["Content-Disposition"])
        content = response.content.decode("utf-8")
        self.assertTrue(content.startswith("\ufeff"))
        lines = [l for l in content.strip().split("\r\n") if l]
        self.assertGreater(len(lines), 1)

    def test_export_csv_with_status_filter(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(
            reverse("helpdesk:export_tickets_csv"),
            {"status": str(Ticket.OPEN_STATUS)},
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        lines = [l for l in content.strip().split("\r\n") if l]
        data_lines = lines[1:]
        open_count = Ticket.objects.filter(
            status=Ticket.OPEN_STATUS, queue__in=self.huser.get_queues()
        ).count()
        self.assertEqual(len(data_lines), open_count)

    def test_export_csv_with_search(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(
            reverse("helpdesk:export_tickets_csv"),
            {"q": "Open", "status": str(Ticket.OPEN_STATUS)},
        )
        self.assertEqual(response.status_code, 200)

    def test_export_empty_result_set(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(
            reverse("helpdesk:export_tickets_csv"),
            {"status": "99"},
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8")
        lines = [l for l in content.strip().split("\r\n") if l]
        self.assertEqual(len(lines), 1)

    def test_export_matches_datatables_data_ids(self):
        params = {
            "filtering": {"status__in": [Ticket.OPEN_STATUS]},
            "sorting": "id",
        }
        builder = self._make_builder(query_params=params)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        dt_ids = sorted([d["id"] for d in dt["data"]])
        rows = list(builder.iter_export_rows())
        header = rows[0]
        id_idx = header.index("id")
        export_ids = sorted([int(r[id_idx]) for r in rows[1:]])
        self.assertEqual(dt_ids, export_ids)

    def test_large_dataset_export_count_matches_queryset(self):
        for i in range(50):
            Ticket.objects.create(
                title=f"Bulk Ticket {i}",
                queue=self.queue1,
                status=Ticket.OPEN_STATUS,
            )
        params = {
            "filtering": {"queue__id__in": [self.queue1.pk]},
            "sorting": "id",
        }
        builder = self._make_builder(query_params=params)
        qs_count = builder.count()
        rows = list(builder.iter_export_rows())
        self.assertEqual(len(rows) - 1, qs_count)
        self.assertGreater(qs_count, 50)

    def test_export_sorted_by_priority(self):
        params = {"sorting": "priority", "sortreverse": False}
        builder = self._make_builder(query_params=params)
        rows = list(builder.iter_export_rows())
        header = rows[0]
        priority_idx = header.index("priority")
        data_rows = rows[1:]
        priorities = [row[priority_idx] for row in data_rows]
        non_empty = [p for p in priorities if p]
        self.assertEqual(non_empty, sorted(non_empty))

    def test_export_datatables_paginated_batch_consistency(self):
        params = {"sorting": "id"}
        builder = self._make_builder(query_params=params)
        full_qs_ids = list(
            builder.get_queryset().values_list("id", flat=True)
        )
        total_count = len(full_qs_ids)
        batch_size = 2
        collected_ids = []
        for start in range(0, total_count, batch_size):
            dt = builder.get_datatables_context(
                draw=["0"],
                length=[str(batch_size)],
                start=[str(start)],
            )
            batch_ids = [d["id"] for d in dt["data"]]
            collected_ids.extend(batch_ids)
        self.assertEqual(collected_ids, full_qs_ids)

    def test_export_datatables_batch_count_matches_total(self):
        params = {"sorting": "id"}
        builder = self._make_builder(query_params=params)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        total = dt["recordsTotal"]
        batch_size = 2
        collected = 0
        for start in range(0, total, batch_size):
            dt_batch = builder.get_datatables_context(
                draw=["0"],
                length=[str(batch_size)],
                start=[str(start)],
            )
            collected += len(dt_batch["data"])
        self.assertEqual(collected, total)

    def test_export_csv_full_vs_batched_datatables_ids(self):
        import csv as csv_mod
        import io

        self.client.force_login(self.staff_user)
        response = self.client.get(
            reverse("helpdesk:export_tickets_csv"),
            {"status": [str(Ticket.OPEN_STATUS), str(Ticket.REOPENED_STATUS)]},
        )
        self.assertEqual(response.status_code, 200)
        content = response.content.decode("utf-8-sig")
        reader = csv_mod.reader(io.StringIO(content))
        rows = list(reader)
        self.assertGreater(len(rows), 1)
        header = rows[0]
        id_idx = header.index("id")
        csv_ids = [int(row[id_idx]) for row in rows[1:] if len(row) > id_idx]
        params = {
            "filtering": {"status__in": [Ticket.OPEN_STATUS, Ticket.REOPENED_STATUS]},
            "sorting": "id",
        }
        builder = self._make_builder(query_params=params)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        dt_ids = sorted([d["id"] for d in dt["data"]])
        self.assertEqual(sorted(csv_ids), dt_ids)


def _build_escalation_exclusions(start_date, days_to_exclude):
    for i in range(days_to_exclude):
        d = start_date + timedelta(days=i)
        EscalationExclusion.objects.get_or_create(
            date=d, defaults={"name": f"excl_{d.isoformat()}"}
        )


class SLAConsistencyTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.queue1 = Queue.objects.create(
            title="SLA Queue", slug="sla_queue", escalate_days=3
        )
        self.queue2 = Queue.objects.create(
            title="NoEscalate", slug="noescalate", escalate_days=0
        )
        self.staff_user = get_staff_user()
        self.huser = HelpdeskUser(self.staff_user)
        self.factory = RequestFactory()

        self.overdue_ticket = Ticket.objects.create(
            title="Overdue - SLA Breached",
            queue=self.queue1,
            status=Ticket.OPEN_STATUS,
            priority=3,
            created=self.now - timedelta(days=10),
            due_date=self.now - timedelta(days=2),
        )
        self.due_soon_ticket = Ticket.objects.create(
            title="Due Soon - Within SLA",
            queue=self.queue1,
            status=Ticket.OPEN_STATUS,
            priority=2,
            created=self.now - timedelta(days=1),
            due_date=self.now + timedelta(days=1),
        )
        self.no_due_date_ticket = Ticket.objects.create(
            title="No Due Date - No SLA Target",
            queue=self.queue1,
            status=Ticket.OPEN_STATUS,
            priority=5,
            created=self.now - timedelta(days=5),
            due_date=None,
        )
        self.high_priority_held = Ticket.objects.create(
            title="Held High Priority - Not Escalatable",
            queue=self.queue1,
            status=Ticket.OPEN_STATUS,
            priority=1,
            on_hold=True,
            created=self.now - timedelta(days=10),
            last_escalation=None,
        )
        self.already_escalated_ticket = Ticket.objects.create(
            title="Already Escalated Once",
            queue=self.queue1,
            status=Ticket.REOPENED_STATUS,
            priority=2,
            created=self.now - timedelta(days=10),
            last_escalation=self.now - timedelta(days=1),
        )
        self.different_queue_ticket = Ticket.objects.create(
            title="Different Queue No Escalate Days",
            queue=self.queue2,
            status=Ticket.OPEN_STATUS,
            priority=4,
            created=self.now - timedelta(days=20),
        )

    def _builder(self, query_params=None):
        return TicketQueryBuilder(self.huser, query_params=query_params)

    def test_overdue_ticket_filter_consistency_across_entries(self):
        """
        使用 created__lte 结合状态过滤模拟 SLA 过期工单，
        验证 count/DataTables/导出三入口计数一致。
        """
        twelve_days_ago = (self.now - timedelta(days=12)).strftime("%Y-%m-%d")
        params = {
            "filtering": {
                "status__in": Ticket.OPEN_STATUSES,
                "created__gte": twelve_days_ago,
                "created__lte": (self.now + timedelta(days=1)).strftime("%Y-%m-%d"),
            },
            "sorting": "due_date",
            "sortreverse": True,
        }
        builder = self._builder(query_params=params)
        qs_count = builder.count()
        self.assertGreater(qs_count, 0)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsTotal"], qs_count)
        self.assertEqual(dt["recordsFiltered"], qs_count)
        export_rows = list(builder.iter_export_rows())
        self.assertEqual(len(export_rows) - 1, qs_count)

    def test_sorted_by_due_date_asc_consistency(self):
        """按 due_date 升序排序，验证列表和导出顺序一致。"""
        params = {
            "filtering": {"status__in": Ticket.OPEN_STATUSES},
            "sorting": "due_date",
            "sortreverse": False,
        }
        builder = self._builder(query_params=params)
        qs_ids = list(builder.get_queryset().values_list("id", flat=True))
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"],
            **{"order[0][column]": ["6"], "order[0][dir]": ["asc"]},
        )
        dt_ids = [d["id"] for d in dt["data"]]
        self.assertEqual(qs_ids, dt_ids)
        export_rows = list(builder.iter_export_rows())
        header = export_rows[0]
        id_idx = header.index("id")
        export_ids = [int(r[id_idx]) for r in export_rows[1:]]
        self.assertEqual(qs_ids, export_ids)

    def test_sorted_by_priority_desc_consistency(self):
        """按 priority 升序（1=最高优先）验证四入口 count 和 ID 集合一致。"""
        params = {
            "filtering": {"status__in": Ticket.OPEN_STATUSES},
            "sorting": "priority",
            "sortreverse": False,
        }
        builder = self._builder(query_params=params)
        qs_ids = list(builder.get_queryset().values_list("id", flat=True))
        qs_count = builder.count()
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"],
            **{"order[0][column]": ["2"], "order[0][dir]": ["asc"]},
        )
        self.assertEqual(dt["recordsTotal"], qs_count)
        self.assertEqual(dt["recordsFiltered"], qs_count)
        dt_ids = [d["id"] for d in dt["data"]]
        self.assertEqual(sorted(qs_ids), sorted(dt_ids))
        export_rows = list(builder.iter_export_rows())
        header = export_rows[0]
        id_idx = header.index("id")
        export_ids = [int(r[id_idx]) for r in export_rows[1:]]
        self.assertEqual(len(export_ids), qs_count)
        self.assertEqual(sorted(qs_ids), sorted(export_ids))

    def test_escalated_ticket_included_after_priority_change(self):
        """升级后 priority 改变的工单仍然在过滤结果中。"""
        self.already_escalated_ticket.priority = 1
        self.already_escalated_ticket.save()
        params = {
            "filtering": {"priority__in": [1]},
            "sorting": "created",
        }
        builder = self._builder(query_params=params)
        result_ids = list(builder.get_queryset().values_list("id", flat=True))
        self.assertIn(self.already_escalated_ticket.id, result_ids)
        self.assertEqual(builder.count(), len(result_ids))
        export_rows = list(builder.iter_export_rows())
        self.assertEqual(len(export_rows) - 1, builder.count())

    def test_on_hold_tickets_excluded_from_followup_escalation_logic(self):
        """
        挂起(on_hold=True)的工单虽然 priority=1，
        但使用 on_hold=False 过滤时不应出现在结果中。
        """
        params = {
            "filtering": {
                "status__in": Ticket.OPEN_STATUSES,
            },
            "sorting": "created",
        }
        builder = self._builder(query_params=params)
        qs = builder.get_queryset()
        self.assertIn(self.high_priority_held.id, list(qs.values_list("id", flat=True)))
        held_ids = list(qs.filter(on_hold=True).values_list("id", flat=True))
        self.assertEqual(held_ids, [self.high_priority_held.id])

    def test_overdue_metric_matches_manual_calculation(self):
        """过期工单 SLA 计数等于手动构造的 Q 查询计数。"""
        now_naive_str = self.now.strftime("%Y-%m-%d %H:%M:%S")
        request = self.factory.get(
            "/tickets/",
            {
                "status": [str(s) for s in Ticket.OPEN_STATUSES],
                "date_to": now_naive_str,
                "sort": "due_date",
            },
        )
        params = build_query_params_from_request(request)
        due_date_builder = self._builder(query_params=params)
        manual_qs = Ticket.objects.filter(
            status__in=Ticket.OPEN_STATUSES,
            created__lte=self.now,
            queue__in=self.huser.get_queues(),
        ).distinct()
        self.assertEqual(due_date_builder.count(), manual_qs.count())
        self.assertEqual(
            set(due_date_builder.get_queryset().values_list("id", flat=True)),
            set(manual_qs.values_list("id", flat=True)),
        )

    def test_last_escalation_query_vs_export(self):
        """带 last_escalation 的工单导出与查询集一致。"""
        params = {"sorting": "modified"}
        builder = self._builder(query_params=params)
        qs_ids = set(builder.get_queryset().values_list("id", flat=True))
        export_rows = list(builder.iter_export_rows())
        header = export_rows[0]
        id_idx = header.index("id")
        export_ids = set()
        for r in export_rows[1:]:
            if r[id_idx]:
                export_ids.add(int(r[id_idx]))
        self.assertEqual(qs_ids, export_ids)

    def test_escalation_exclusion_date_reflected_in_followup_annotations(self):
        """
        EscalationExclusion 是升级逻辑的关键变量，验证其不影响查询层计数
        （查询层只依赖 ticket 字段，不做业务 SLA 判断）。
        """
        today = self.now.date()
        _build_escalation_exclusions(today, 2)
        params = {
            "filtering": {"queue__id__in": [self.queue1.pk]},
            "sorting": "created",
        }
        builder_before = self._builder(query_params=params)
        count_before = builder_before.count()
        _build_escalation_exclusions(today + timedelta(days=2), 5)
        builder_after = self._builder(query_params=params)
        count_after = builder_after.count()
        self.assertEqual(count_before, count_after)
        dt_before = builder_before.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        dt_after = builder_after.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt_before["recordsTotal"], dt_after["recordsTotal"])


class CrossLanguageSearchTests(TestCase):
    def setUp(self):
        self.queue = Queue.objects.create(
            title="Multilingual", slug="ml_queue", allow_public_submission=True
        )
        self.staff_user = get_staff_user()
        self.huser = HelpdeskUser(self.staff_user)
        self.factory = RequestFactory()

        self.ticket_cn = Ticket.objects.create(
            title="网络连接故障 请处理",
            queue=self.queue,
            description="用户反馈 WiFi 无法连接，密码正确但无法获取 IP",
            status=Ticket.OPEN_STATUS,
            priority=2,
            submitter_email="user_cn@example.com",
        )
        self.ticket_en = Ticket.objects.create(
            title="Network Connection Failure - Please fix",
            queue=self.queue,
            description="User reports WiFi down, correct password but no IP address",
            status=Ticket.OPEN_STATUS,
            priority=3,
            submitter_email="user_en@example.com",
        )
        self.ticket_mixed = Ticket.objects.create(
            title="Login 登录异常 Error",
            queue=self.queue,
            description="用户 登陆 时出现 ERROR code 500 错误",
            status=Ticket.REOPENED_STATUS,
            priority=1,
            submitter_email="user_mixed@example.com",
        )
        self.ticket_irrelevant = Ticket.objects.create(
            title="打印机缺墨",
            queue=self.queue,
            description="A4纸盒缺纸，请补充 supplies",
            status=Ticket.OPEN_STATUS,
            priority=5,
        )

    def _builder(self, query_params=None):
        return TicketQueryBuilder(self.huser, query_params=query_params)

    def test_chinese_only_keyword_matches_title_and_description(self):
        search = "网络连接故障"
        params = {"search_string": search, "sorting": "id"}
        builder = self._builder(query_params=params)
        ids = list(builder.get_queryset().values_list("id", flat=True))
        self.assertIn(self.ticket_cn.id, ids)
        self.assertNotIn(self.ticket_irrelevant.id, ids)
        self.assertEqual(builder.count(), len(ids))
        export_rows = list(builder.iter_export_rows())
        self.assertEqual(len(export_rows) - 1, builder.count())
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsFiltered"], builder.count())
        self.assertEqual(dt["recordsTotal"], builder.count())

    def test_english_only_keyword_matches_title_and_description(self):
        search = "WiFi"
        params = {"search_string": search, "sorting": "id"}
        builder = self._builder(query_params=params)
        ids = list(builder.get_queryset().values_list("id", flat=True))
        self.assertIn(self.ticket_en.id, ids)
        self.assertNotIn(self.ticket_irrelevant.id, ids)
        self.assertEqual(builder.count(), len(ids))
        export_rows = list(builder.iter_export_rows())
        self.assertEqual(len(export_rows) - 1, builder.count())

    def test_mixed_language_keyword_case_insensitive(self):
        search = "Login"
        params = {"search_string": search, "sorting": "id"}
        builder = self._builder(query_params=params)
        ids = list(builder.get_queryset().values_list("id", flat=True))
        self.assertIn(self.ticket_mixed.id, ids)
        self.assertEqual(builder.count(), len(ids))
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsTotal"], builder.count())

    def test_or_syntax_mixed_language_keywords(self):
        search = "网络连接故障 OR 异常 OR supplies"
        params = {"search_string": search, "sorting": "id"}
        builder = self._builder(query_params=params)
        ids = set(builder.get_queryset().values_list("id", flat=True))
        self.assertIn(self.ticket_cn.id, ids)
        self.assertIn(self.ticket_mixed.id, ids)
        self.assertIn(self.ticket_irrelevant.id, ids)
        export_rows = list(builder.iter_export_rows())
        header = export_rows[0]
        id_idx = header.index("id")
        export_ids = set(int(r[id_idx]) for r in export_rows[1:])
        self.assertEqual(ids, export_ids)

    def test_chinese_keyword_via_request_params_consistent_with_manual(self):
        """通过 Request 构造参数与手动构造参数，结果一致。"""
        kw = "登录"
        request = self.factory.get("/tickets/", {"q": kw, "sort": "id"})
        req_params = build_query_params_from_request(request)
        req_builder = TicketQueryBuilder(self.huser, query_params=req_params)
        manual_builder = self._builder(
            query_params={"search_string": kw, "sorting": "id"}
        )
        req_ids = list(req_builder.get_queryset().values_list("id", flat=True))
        manual_ids = list(manual_builder.get_queryset().values_list("id", flat=True))
        self.assertEqual(req_ids, manual_ids)
        self.assertEqual(req_builder.count(), manual_builder.count())

    def test_language_keyword_sorting_consistency_between_qs_and_export(self):
        """中英文混合关键词搜索后，按 created 排序的查询集和导出顺序一致。"""
        search = "OR".join(["Error", "错误"])
        params = {"search_string": f"Error OR 错误", "sorting": "created"}
        builder = self._builder(query_params=params)
        qs_ids = list(builder.get_queryset().values_list("id", flat=True))
        export_rows = list(builder.iter_export_rows())
        header = export_rows[0]
        id_idx = header.index("id")
        export_ids = [int(r[id_idx]) for r in export_rows[1:]]
        self.assertEqual(qs_ids, export_ids)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"],
            **{"order[0][column]": ["5"], "order[0][dir]": ["asc"]},
        )
        dt_ids = [d["id"] for d in dt["data"]]
        self.assertEqual(qs_ids, dt_ids)

    def test_submitter_email_search_with_ascii_only(self):
        search = "user_mixed@example.com"
        params = {"search_string": search, "sorting": "id"}
        builder = self._builder(query_params=params)
        ids = list(builder.get_queryset().values_list("id", flat=True))
        self.assertEqual(ids, [self.ticket_mixed.id])


class ArchiveActiveBoundaryTests(TestCase):
    ACTIVE_STATUSES = (Ticket.OPEN_STATUS, Ticket.REOPENED_STATUS)
    ARCHIVE_STATUSES = (Ticket.RESOLVED_STATUS, Ticket.CLOSED_STATUS)

    def setUp(self):
        self.queue = Queue.objects.create(
            title="ArchiveQ", slug="archive_queue"
        )
        self.staff_user = get_staff_user()
        self.huser = HelpdeskUser(self.staff_user)
        self.factory = RequestFactory()
        self.now = timezone.now()

        self.active_tickets = []
        for i in range(3):
            t = Ticket.objects.create(
                title=f"Active Ticket {i}",
                queue=self.queue,
                status=self.ACTIVE_STATUSES[i % len(self.ACTIVE_STATUSES)],
                priority=3,
                created=self.now - timedelta(days=i),
            )
            self.active_tickets.append(t)

        self.archived_tickets = []
        for i in range(3):
            t = Ticket.objects.create(
                title=f"Archived Ticket {i}",
                queue=self.queue,
                status=self.ARCHIVE_STATUSES[i % len(self.ARCHIVE_STATUSES)],
                priority=3,
                created=self.now - timedelta(days=10 + i),
                modified=self.now - timedelta(days=i),
            )
            self.archived_tickets.append(t)

        self.boundary_ticket = Ticket.objects.create(
            title="Just Archived - Boundary",
            queue=self.queue,
            status=Ticket.CLOSED_STATUS,
            priority=4,
            created=self.now - timedelta(days=1),
            modified=self.now,
        )

    def _builder(self, query_params=None):
        return TicketQueryBuilder(self.huser, query_params=query_params)

    def test_default_list_only_active_excludes_archived(self):
        """默认列表参数仅显示活跃工单（Open/Reopened）。"""
        from helpdesk.query import get_default_list_query_params

        params = get_default_list_query_params()
        builder = self._builder(query_params=params)
        qs_ids = set(builder.get_queryset().values_list("id", flat=True))
        active_ids = {t.id for t in self.active_tickets}
        self.assertEqual(qs_ids, active_ids)
        for at in self.archived_tickets:
            self.assertNotIn(at.id, qs_ids)
        self.assertNotIn(self.boundary_ticket.id, qs_ids)

    def test_explicit_archive_only_query_consistency(self):
        """显式查询归档状态，四入口一致。"""
        params = {
            "filtering": {"status__in": list(self.ARCHIVE_STATUSES)},
            "sorting": "modified",
            "sortreverse": True,
        }
        builder = self._builder(query_params=params)
        qs_count = builder.count()
        expected_archive_ids = {t.id for t in self.archived_tickets} | {
            self.boundary_ticket.id
        }
        actual_ids = set(builder.get_queryset().values_list("id", flat=True))
        self.assertEqual(actual_ids, expected_archive_ids)
        self.assertEqual(qs_count, len(expected_archive_ids))
        export_rows = list(builder.iter_export_rows())
        self.assertEqual(len(export_rows) - 1, qs_count)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsTotal"], qs_count)
        self.assertEqual(dt["recordsFiltered"], qs_count)

    def test_combined_active_and_archive_consistency(self):
        """合并查询归档+活跃，结果集、计数、排序一致。"""
        all_statuses = list(self.ACTIVE_STATUSES) + list(self.ARCHIVE_STATUSES)
        params = {
            "filtering": {"status__in": all_statuses},
            "sorting": "created",
            "sortreverse": False,
        }
        builder = self._builder(query_params=params)
        qs_ids = list(builder.get_queryset().values_list("id", flat=True))
        self.assertEqual(len(qs_ids), 3 + 3 + 1)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsTotal"], len(qs_ids))
        export_rows = list(builder.iter_export_rows())
        self.assertEqual(len(export_rows) - 1, len(qs_ids))

    def test_pagination_boundary_at_active_archive_transition(self):
        """
        分页时活跃/归档交界点验证：合并按 created 降序，
        第 1 页 batch_size=3 应该包含全部活跃 + 1 个最新归档。
        第 2 页 batch_size=3 应该包含其余归档。
        """
        all_statuses = list(self.ACTIVE_STATUSES) + list(self.ARCHIVE_STATUSES)
        params = {
            "filtering": {"status__in": all_statuses},
            "sorting": "created",
            "sortreverse": True,
        }
        builder = self._builder(query_params=params)
        full_ids_desc = list(
            builder.get_queryset()
            .order_by("-created")
            .values_list("id", flat=True)
        )
        self.assertEqual(len(full_ids_desc), 7)

        page_1_expected = full_ids_desc[:3]
        page_2_expected = full_ids_desc[3:6]
        page_3_expected = full_ids_desc[6:]

        dt1 = builder.get_datatables_context(
            draw=["0"], length=["3"], start=["0"],
            **{"order[0][column]": ["5"], "order[0][dir]": ["desc"]},
        )
        dt2 = builder.get_datatables_context(
            draw=["1"], length=["3"], start=["3"],
            **{"order[0][column]": ["5"], "order[0][dir]": ["desc"]},
        )
        dt3 = builder.get_datatables_context(
            draw=["2"], length=["3"], start=["6"],
            **{"order[0][column]": ["5"], "order[0][dir]": ["desc"]},
        )

        self.assertEqual([d["id"] for d in dt1["data"]], page_1_expected)
        self.assertEqual([d["id"] for d in dt2["data"]], page_2_expected)
        self.assertEqual([d["id"] for d in dt3["data"]], page_3_expected)
        self.assertEqual(dt1["recordsTotal"], 7)
        self.assertEqual(dt2["recordsTotal"], 7)
        self.assertEqual(dt3["recordsTotal"], 7)

    def test_duplicate_vs_closed_archive_boundary(self):
        """
        DUPLICATE 与 CLOSED 都是已归档状态的边界，
        显式排除 DUPLICATE 时查询应仅包含 CLOSED/RESOLVED。
        """
        dup_ticket = Ticket.objects.create(
            title="Duplicate - Archived but separate",
            queue=self.queue,
            status=Ticket.DUPLICATE_STATUS,
            priority=5,
            created=self.now - timedelta(days=2),
        )
        params = {
            "filtering": {
                "status__in": [
                    Ticket.RESOLVED_STATUS,
                    Ticket.CLOSED_STATUS,
                ]
            },
            "sorting": "created",
        }
        builder = self._builder(query_params=params)
        actual_ids = set(builder.get_queryset().values_list("id", flat=True))
        self.assertNotIn(dup_ticket.id, actual_ids)
        for at in self.archived_tickets:
            self.assertIn(at.id, actual_ids)
        self.assertIn(self.boundary_ticket.id, actual_ids)

    def test_archive_csv_export_consistency_with_datatables(self):
        """CSV 导出归档工单与 DataTables 同条件一致。"""
        self.client.force_login(self.staff_user)
        statuses = [Ticket.RESOLVED_STATUS, Ticket.CLOSED_STATUS]
        response = self.client.get(
            reverse("helpdesk:export_tickets_csv"),
            {"status": [str(s) for s in statuses]},
        )
        self.assertEqual(response.status_code, 200)
        import csv as csv_mod
        import io

        content = response.content.decode("utf-8-sig")
        reader = csv_mod.reader(io.StringIO(content))
        rows = list(reader)
        header = rows[0]
        id_idx = header.index("id")
        csv_ids = set(int(r[id_idx]) for r in rows[1:] if len(r) > id_idx)
        params = {
            "filtering": {"status__in": statuses},
            "sorting": "id",
        }
        builder = self._builder(query_params=params)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        dt_ids = set(d["id"] for d in dt["data"])
        self.assertEqual(csv_ids, dt_ids)

    def test_combined_export_matches_union_counts(self):
        """合并查询的总数 = 活跃数 + 归档数 + 边界工单。"""
        all_statuses = list(self.ACTIVE_STATUSES) + list(self.ARCHIVE_STATUSES)
        params = {
            "filtering": {"status__in": all_statuses},
            "sorting": "id",
        }
        combined_builder = self._builder(query_params=params)
        active_builder = self._builder(
            query_params={
                "filtering": {"status__in": list(self.ACTIVE_STATUSES)},
                "sorting": "id",
            }
        )
        archive_builder = self._builder(
            query_params={
                "filtering": {"status__in": list(self.ARCHIVE_STATUSES)},
                "sorting": "id",
            }
        )
        self.assertEqual(
            combined_builder.count(),
            active_builder.count() + archive_builder.count(),
        )
        export_combined = list(combined_builder.iter_export_rows())
        export_active = list(active_builder.iter_export_rows())
        export_archive = list(archive_builder.iter_export_rows())
        self.assertEqual(
            len(export_combined) - 1,
            (len(export_active) - 1) + (len(export_archive) - 1),
        )

    def test_search_filter_crosses_active_archive_boundary(self):
        """搜索关键词同时匹配活跃和归档，结果集包含两者。"""
        Ticket.objects.filter(pk__in=[t.pk for t in self.active_tickets]).update(
            description="系统故障 system issue"
        )
        Ticket.objects.filter(pk=self.boundary_ticket.pk).update(
            description="系统故障 system boundary issue"
        )
        params = {
            "filtering": {},
            "search_string": "system",
            "sorting": "created",
        }
        builder = self._builder(query_params=params)
        ids = set(builder.get_queryset().values_list("id", flat=True))
        self.assertTrue(ids & {t.id for t in self.active_tickets})
        self.assertIn(self.boundary_ticket.id, ids)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsTotal"], builder.count())
        self.assertEqual(dt["recordsFiltered"], builder.count())
        export_rows = list(builder.iter_export_rows())
        self.assertEqual(len(export_rows) - 1, builder.count())

    def test_escalation_on_archived_tickets_not_impacted(self):
        """归档工单在查询层不考虑升级字段，结果集数量一致。"""
        for t in self.archived_tickets:
            t.last_escalation = self.now - timedelta(days=1)
            t.save()
        params = {
            "filtering": {"status__in": list(self.ARCHIVE_STATUSES)},
            "sorting": "modified",
        }
        builder = self._builder(query_params=params)
        ids_before = set(builder.get_queryset().values_list("id", flat=True))
        for t in self.archived_tickets:
            t.last_escalation = None
            t.save()
        ids_after = set(builder.get_queryset().values_list("id", flat=True))
        self.assertEqual(ids_before, ids_after)
        self.assertEqual(builder.count(), len(ids_after))


class MassUpdateQueryImpactTests(TestCase):
    def setUp(self):
        self.queue = Queue.objects.create(
            title="MassUpdateQ", slug="mu_queue"
        )
        self.staff_user = get_staff_user()
        self.other_user = User.objects.create_user(
            username="other", password="pass", is_staff=True
        )
        self.huser = HelpdeskUser(self.staff_user)
        self.factory = RequestFactory()
        self.now = timezone.now()

        cat = KBCategory.objects.create(
            title="KB Cat", slug="kb_cat", description="d", queue=self.queue
        )
        self.kb1 = KBItem.objects.create(
            category=cat, title="KB1", question="q", answer="a"
        )

        self.tickets = []
        statuses = [
            Ticket.OPEN_STATUS,
            Ticket.OPEN_STATUS,
            Ticket.OPEN_STATUS,
            Ticket.REOPENED_STATUS,
            Ticket.RESOLVED_STATUS,
        ]
        for i in range(5):
            t = Ticket.objects.create(
                title=f"Mass Update Ticket {i}",
                queue=self.queue,
                status=statuses[i],
                priority=3,
                assigned_to=self.staff_user if i < 3 else None,
                created=self.now - timedelta(days=i),
            )
            self.tickets.append(t)
        self.all_ids = [t.id for t in self.tickets]

    def _builder(self, query_params=None):
        return TicketQueryBuilder(self.huser, query_params=query_params)

    def _mass_update(self, action, ticket_ids=None, **extra):
        self.client.force_login(self.staff_user)
        post = {"ticket_id": ticket_ids or self.all_ids, "action": action}
        post.update(extra)
        return self.client.post(reverse("helpdesk:mass_update"), post)

    def test_mass_assign_changes_assigned_to_count_consistency(self):
        """批量分配后 assigned_to 查询计数四入口一致。"""
        params_before = {
            "filtering": {"assigned_to__id__in": [self.other_user.pk]},
            "sorting": "id",
        }
        builder_before = self._builder(query_params=params_before)
        self.assertEqual(builder_before.count(), 0)

        self._mass_update(f"assign_{self.other_user.pk}")

        builder_after = self._builder(query_params=params_before)
        qs_count = builder_after.count()
        self.assertEqual(qs_count, 5)

        dt = builder_after.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsTotal"], qs_count)
        self.assertEqual(dt["recordsFiltered"], qs_count)
        export_rows = list(builder_after.iter_export_rows())
        self.assertEqual(len(export_rows) - 1, qs_count)

    def test_mass_assign_queryset_id_set_matches_export(self):
        """批量分配后查询集 ID 集合 == 导出 ID 集合。"""
        self._mass_update(f"assign_{self.other_user.pk}")
        params = {
            "filtering": {"assigned_to__id__in": [self.other_user.pk]},
            "sorting": "id",
        }
        builder = self._builder(query_params=params)
        qs_ids = set(builder.get_queryset().values_list("id", flat=True))
        export_rows = list(builder.iter_export_rows())
        header = export_rows[0]
        id_idx = header.index("id")
        export_ids = set(int(r[id_idx]) for r in export_rows[1:])
        self.assertEqual(qs_ids, export_ids)

    def test_mass_unassign_consistency(self):
        """批量取消分配后 assigned_to=None 查询四入口一致。"""
        self._mass_update("unassign")
        params = {
            "filtering_null": {"assigned_to__id__isnull": True},
            "sorting": "id",
        }
        builder = self._builder(query_params=params)
        qs_count = builder.count()
        self.assertEqual(qs_count, 5)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsTotal"], qs_count)
        export_rows = list(builder.iter_export_rows())
        self.assertEqual(len(export_rows) - 1, qs_count)

    def test_mass_close_removes_from_default_list(self):
        """批量关闭后工单从默认列表过滤消失。"""
        default_params = get_default_list_query_params()
        builder_before = self._builder(query_params=default_params)
        count_before = builder_before.count()
        self.assertEqual(count_before, 4)

        self._mass_update("close")

        builder_after = self._builder(query_params=default_params)
        count_after = builder_after.count()
        self.assertEqual(count_after, 0)

        dt = builder_after.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsTotal"], 0)
        self.assertEqual(dt["recordsFiltered"], 0)
        export_rows = list(builder_after.iter_export_rows())
        self.assertEqual(len(export_rows) - 1, 0)

    def test_mass_close_visible_in_archive_query(self):
        """批量关闭工单在归档状态查询中四入口一致。"""
        self._mass_update("close")
        params = {
            "filtering": {"status__in": [Ticket.CLOSED_STATUS]},
            "sorting": "id",
        }
        builder = self._builder(query_params=params)
        qs_count = builder.count()
        self.assertEqual(qs_count, 5)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsTotal"], qs_count)
        export_rows = list(builder.iter_export_rows())
        self.assertEqual(len(export_rows) - 1, qs_count)

    def test_mass_take_assigns_to_current_user(self):
        """批量 take 后 assigned_to 为当前用户，查询一致。"""
        for t in self.tickets:
            t.assigned_to = None
            t.save()
        self._mass_update("take")
        params = {
            "filtering": {"assigned_to__id__in": [self.staff_user.pk]},
            "sorting": "id",
        }
        builder = self._builder(query_params=params)
        self.assertEqual(builder.count(), 5)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsTotal"], 5)

    def test_mass_set_kbitem_consistency(self):
        """批量设置 KBItem 后按 kbitem 过滤一致。"""
        self._mass_update(f"kbitem_{self.kb1.pk}")
        params = {
            "filtering": {"kbitem__in": [self.kb1.pk]},
            "sorting": "id",
        }
        builder = self._builder(query_params=params)
        qs_count = builder.count()
        self.assertEqual(qs_count, 5)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsTotal"], qs_count)
        export_rows = list(builder.iter_export_rows())
        self.assertEqual(len(export_rows) - 1, qs_count)

    def test_mass_delete_removes_tickets_from_queryset(self):
        """批量删除工单后查询结果不再包含，四入口一致。"""
        to_delete = [self.tickets[0].id, self.tickets[1].id]
        self._mass_update("delete", ticket_ids=to_delete)
        params = {
            "filtering": {"status__in": [Ticket.OPEN_STATUS]},
            "sorting": "id",
        }
        builder = self._builder(query_params=params)
        qs_ids = set(builder.get_queryset().values_list("id", flat=True))
        for did in to_delete:
            self.assertNotIn(did, qs_ids)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsTotal"], builder.count())
        export_rows = list(builder.iter_export_rows())
        self.assertEqual(len(export_rows) - 1, builder.count())

    def test_partial_mass_update_consistency(self):
        """只更新部分工单，查询和导出对更新/未更新工单一致性。"""
        to_update = [self.tickets[0].id, self.tickets[2].id]
        self._mass_update(f"assign_{self.other_user.pk}", ticket_ids=to_update)

        params = {
            "filtering": {"assigned_to__id__in": [self.other_user.pk]},
            "sorting": "id",
        }
        builder = self._builder(query_params=params)
        qs_ids = set(builder.get_queryset().values_list("id", flat=True))
        self.assertEqual(qs_ids, set(to_update))

        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsTotal"], len(to_update))
        export_rows = list(builder.iter_export_rows())
        header = export_rows[0]
        id_idx = header.index("id")
        export_ids = set(int(r[id_idx]) for r in export_rows[1:])
        self.assertEqual(export_ids, set(to_update))

    def test_mass_close_pagination_boundary(self):
        """批量关闭后活跃工单只剩 0 条，分页 batch 空结果一致。"""
        self._mass_update("close")
        default_params = get_default_list_query_params()
        builder = self._builder(query_params=default_params)
        for start in (0, 25, 50):
            dt = builder.get_datatables_context(
                draw=["0"], length=["25"], start=[str(start)]
            )
            self.assertEqual(dt["recordsTotal"], 0)
            self.assertEqual(dt["recordsFiltered"], 0)
            self.assertEqual(len(dt["data"]), 0)


class CustomFieldSearchSortTests(TestCase):
    def setUp(self):
        self.queue = Queue.objects.create(
            title="CFQueue", slug="cf_queue", allow_public_submission=True
        )
        self.staff_user = get_staff_user()
        self.huser = HelpdeskUser(self.staff_user)

        self.cf_varchar = CustomField.objects.create(
            name="company_name",
            label="Company Name",
            data_type="varchar",
            max_length=100,
        )
        self.cf_text = CustomField.objects.create(
            name="issue_detail",
            label="Issue Detail",
            data_type="text",
        )
        self.cf_int = CustomField.objects.create(
            name="customer_id",
            label="Customer ID",
            data_type="integer",
        )
        self.cf_list = CustomField.objects.create(
            name="severity",
            label="Severity",
            data_type="list",
            list_values="Low\nMedium\nHigh\nCritical",
        )

        companies = ["Acme Corp", "Beta Inc", "Acme Labs", "Delta Corp", "公司中文"]
        details = ["Login failure SSO", "密码重置失败", "Network outage 服务器",
                   "Database 连接超时 timeout", "SSO 单点登录"]
        cids = [1001, 2002, 1001, 3003, 4004]
        severities = ["Low", "High", "Medium", "Critical", "High"]

        self.tickets = []
        for i in range(5):
            t = Ticket.objects.create(
                title=f"CF Ticket {i}",
                queue=self.queue,
                status=Ticket.OPEN_STATUS,
                priority=3,
            )
            TicketCustomFieldValue.objects.create(
                ticket=t, field=self.cf_varchar, value=companies[i]
            )
            TicketCustomFieldValue.objects.create(
                ticket=t, field=self.cf_text, value=details[i]
            )
            TicketCustomFieldValue.objects.create(
                ticket=t, field=self.cf_int, value=str(cids[i])
            )
            TicketCustomFieldValue.objects.create(
                ticket=t, field=self.cf_list, value=severities[i]
            )
            self.tickets.append(t)

    def _builder(self, query_params=None):
        return TicketQueryBuilder(self.huser, query_params=query_params)

    def test_search_varchar_custom_field_value(self):
        """搜索字符串 CF 值，结果命中并四入口一致。"""
        params = {"search_string": "Acme", "sorting": "id"}
        builder = self._builder(query_params=params)
        ids = set(builder.get_queryset().values_list("id", flat=True))
        self.assertEqual(ids, {self.tickets[0].id, self.tickets[2].id})
        self.assertEqual(builder.count(), 2)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsTotal"], builder.count())
        self.assertEqual(dt["recordsFiltered"], builder.count())
        export_rows = list(builder.iter_export_rows())
        self.assertEqual(len(export_rows) - 1, builder.count())

    def test_search_text_custom_field_value(self):
        """搜索 text CF 值，中英文混合关键词命中。"""
        params = {"search_string": "SSO", "sorting": "id"}
        builder = self._builder(query_params=params)
        ids = set(builder.get_queryset().values_list("id", flat=True))
        self.assertEqual(ids, {self.tickets[0].id, self.tickets[4].id})

    def test_search_chinese_custom_field_varchar(self):
        """搜索中文 varchar CF 值命中。"""
        params = {"search_string": "公司", "sorting": "id"}
        builder = self._builder(query_params=params)
        ids = set(builder.get_queryset().values_list("id", flat=True))
        self.assertEqual(ids, {self.tickets[4].id})

    def test_search_integer_custom_field_value(self):
        """搜索 integer CF 值命中，count 和导出一致。"""
        params = {"search_string": "1001", "sorting": "id"}
        builder = self._builder(query_params=params)
        ids = set(builder.get_queryset().values_list("id", flat=True))
        self.assertEqual(ids, {self.tickets[0].id, self.tickets[2].id})
        self.assertEqual(builder.count(), 2)
        export_rows = list(builder.iter_export_rows())
        self.assertEqual(len(export_rows) - 1, 2)

    def test_search_list_custom_field_value(self):
        """搜索 list CF 的枚举值命中。"""
        params = {"search_string": "Critical", "sorting": "id"}
        builder = self._builder(query_params=params)
        ids = set(builder.get_queryset().values_list("id", flat=True))
        self.assertEqual(ids, {self.tickets[3].id})

    def test_or_search_combines_cf_and_native_fields(self):
        """OR 语法同时命中 CF 与原生字段。"""
        self.tickets[0].title = "SSO Login Issue"
        self.tickets[0].save()
        params = {
            "search_string": "Acme OR CF Ticket 3",
            "sorting": "id",
        }
        builder = self._builder(query_params=params)
        ids = set(builder.get_queryset().values_list("id", flat=True))
        self.assertIn(self.tickets[0].id, ids)
        self.assertIn(self.tickets[2].id, ids)
        self.assertIn(self.tickets[3].id, ids)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsTotal"], builder.count())

    def test_export_includes_custom_field_columns(self):
        """导出列头包含所有自定义字段名。"""
        builder = self._builder(query_params={"sorting": "id"})
        export_rows = list(builder.iter_export_rows())
        header = export_rows[0]
        self.assertIn("company_name", header)
        self.assertIn("issue_detail", header)
        self.assertIn("customer_id", header)
        self.assertIn("severity", header)

    def test_export_custom_field_values_match_records(self):
        """导出的 CF 值与数据库实际值一致。"""
        builder = self._builder(query_params={"sorting": "id"})
        export_rows = list(builder.iter_export_rows(include_custom_fields=True))
        header = export_rows[0]
        id_idx = header.index("id")
        company_idx = header.index("company_name")
        cid_idx = header.index("customer_id")
        sev_idx = header.index("severity")

        expected = {}
        for t in self.tickets:
            cf_vals = {
                cfv.field_id: cfv.value
                for cfv in TicketCustomFieldValue.objects.filter(ticket=t)
            }
            expected[t.id] = {
                "company": cf_vals[self.cf_varchar.id],
                "cid": cf_vals[self.cf_int.id],
                "sev": cf_vals[self.cf_list.id],
            }

        for row in export_rows[1:]:
            tid = int(row[id_idx])
            self.assertEqual(row[company_idx], expected[tid]["company"])
            self.assertEqual(row[cid_idx], expected[tid]["cid"])
            self.assertEqual(row[sev_idx], expected[tid]["sev"])

    def test_sorting_consistency_with_custom_filters(self):
        """使用自定义字段值过滤后，按 created 排序与导出顺序一致。"""
        params = {
            "search_string": "High",
            "sorting": "created",
            "sortreverse": False,
        }
        builder = self._builder(query_params=params)
        qs_ids = list(builder.get_queryset().values_list("id", flat=True))
        export_rows = list(builder.iter_export_rows())
        header = export_rows[0]
        id_idx = header.index("id")
        export_ids = [int(r[id_idx]) for r in export_rows[1:]]
        self.assertEqual(qs_ids, export_ids)

    def test_search_matches_cf_and_native_case_insensitive(self):
        """大小写不敏感搜索同时命中原生 title 和 CF 值。"""
        self.tickets[0].title = "SSO Login Issue"
        self.tickets[0].save()
        params = {"search_string": "sso", "sorting": "id"}
        builder = self._builder(query_params=params)
        ids = set(builder.get_queryset().values_list("id", flat=True))
        self.assertIn(self.tickets[0].id, ids)
        self.assertIn(self.tickets[4].id, ids)
        self.assertEqual(builder.count(), 2)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsTotal"], builder.count())
        export_rows = list(builder.iter_export_rows())
        self.assertEqual(len(export_rows) - 1, builder.count())

    def test_no_custom_field_export_when_flag_disabled(self):
        """include_custom_fields=False 时不导出 CF 列。"""
        builder = self._builder(query_params={})
        rows = list(builder.iter_export_rows(include_custom_fields=False))
        header = rows[0]
        self.assertNotIn("company_name", header)
        self.assertNotIn("customer_id", header)
        self.assertEqual(len(header), 13)


class TimezoneConsistencyTests(TestCase):
    def setUp(self):
        self.queue = Queue.objects.create(
            title="TZQueue", slug="tz_queue"
        )
        self.staff_user = get_staff_user()
        self.huser = HelpdeskUser(self.staff_user)
        self.factory = RequestFactory()

    def _builder(self, query_params=None):
        return TicketQueryBuilder(self.huser, query_params=query_params)

    def _dt(self, **kwargs):
        return timezone.datetime(2025, 7, 1, **kwargs)

    def _naive(self, **kwargs):
        return self._dt(**kwargs)

    def test_created_timezone_normalization_count_consistency(self):
        """不同时间点 created 排序后分页对账 count 四入口一致。"""
        now_objs = [
            self._naive(hour=10, minute=0),
            self._naive(hour=22, minute=0),
            self._naive(hour=5, minute=0),
            self._naive(hour=16, minute=0),
            self._naive(hour=0, minute=0),
        ]
        tids = []
        for i, created in enumerate(now_objs):
            status = Ticket.OPEN_STATUS if i < 3 else Ticket.CLOSED_STATUS
            t = Ticket.objects.create(
                title=f"CreatedTZ {i}",
                queue=self.queue,
                status=status,
                created=created,
            )
            tids.append(t.id)
        params = {
            "filtering": {
                "status__in": [Ticket.OPEN_STATUS],
            },
            "sorting": "created",
            "sortreverse": True,
        }
        builder = self._builder(query_params=params)
        qs_ids = list(builder.get_queryset().values_list("id", flat=True))
        self.assertEqual(len(qs_ids), 3)
        self.assertEqual(qs_ids, sorted(
            tids[:3],
            key=lambda tid: Ticket.objects.get(pk=tid).created,
            reverse=True,
        ))
        self.assertEqual(builder.count(), 3)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsFiltered"], 3)
        export_rows = list(builder.iter_export_rows())
        self.assertEqual(len(export_rows) - 1, 3)
        header = export_rows[0]
        id_idx = header.index("id")
        export_ids = [int(r[id_idx]) for r in export_rows[1:]]
        self.assertEqual(qs_ids, export_ids)

    def test_timezone_normalization_sorting_qs_matches_export(self):
        """不同时间点工单按 created 排序，查询集和导出顺序一致。"""
        Ticket.objects.create(
            title="T1 00:00",
            queue=self.queue,
            status=Ticket.OPEN_STATUS,
            created=self._naive(hour=0, minute=0),
        )
        Ticket.objects.create(
            title="T2 08:00",
            queue=self.queue,
            status=Ticket.OPEN_STATUS,
            created=self._naive(hour=8, minute=0),
        )
        Ticket.objects.create(
            title="T3 20:00",
            queue=self.queue,
            status=Ticket.OPEN_STATUS,
            created=self._naive(hour=20, minute=0),
        )
        params = {"sorting": "created", "sortreverse": False}
        builder = self._builder(query_params=params)
        qs_ids = list(builder.get_queryset().values_list("id", flat=True))
        export_rows = list(builder.iter_export_rows())
        header = export_rows[0]
        id_idx = header.index("id")
        export_ids = [int(r[id_idx]) for r in export_rows[1:]]
        self.assertEqual(qs_ids, export_ids)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"],
            **{"order[0][column]": ["5"], "order[0][dir]": ["asc"]},
        )
        dt_ids = [d["id"] for d in dt["data"]]
        self.assertEqual(qs_ids, dt_ids)

    def test_due_date_filter_consistency(self):
        """due_date 范围过滤四入口一致。"""
        d1 = timezone.datetime(2025, 7, 15, 12, 0)
        d2 = timezone.datetime(2025, 7, 20, 12, 0)
        d3 = timezone.datetime(2025, 8, 1, 12, 0)
        t1 = Ticket.objects.create(
            title="TZ Due 1", queue=self.queue, status=Ticket.OPEN_STATUS, due_date=d1
        )
        t2 = Ticket.objects.create(
            title="TZ Due 2", queue=self.queue, status=Ticket.OPEN_STATUS, due_date=d2
        )
        t3 = Ticket.objects.create(
            title="TZ Due 3", queue=self.queue, status=Ticket.OPEN_STATUS, due_date=d3
        )
        params = {
            "filtering": {"due_date__gte": "2025-07-10", "due_date__lte": "2025-07-25"},
            "sorting": "due_date",
        }
        builder = self._builder(query_params=params)
        ids = set(builder.get_queryset().values_list("id", flat=True))
        self.assertIn(t1.id, ids)
        self.assertIn(t2.id, ids)
        self.assertNotIn(t3.id, ids)
        self.assertEqual(builder.count(), 2)
        dt_ctx = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt_ctx["recordsTotal"], builder.count())
        export_rows = list(builder.iter_export_rows())
        self.assertEqual(len(export_rows) - 1, builder.count())

    def test_sorting_modified_vs_created_timezone(self):
        """modified 与 created 来自不同时间点排序一致。"""
        created_t1 = self._naive(hour=10)
        modified_t1 = self._naive(hour=20)
        created_t2 = self._naive(hour=2)
        modified_t2 = self._naive(hour=18)
        t1 = Ticket.objects.create(
            title="TZ T1", queue=self.queue, status=Ticket.OPEN_STATUS,
            created=created_t1, modified=modified_t1,
        )
        t2 = Ticket.objects.create(
            title="TZ T2", queue=self.queue, status=Ticket.OPEN_STATUS,
            created=created_t2, modified=modified_t2,
        )
        params = {"sorting": "modified", "sortreverse": False}
        builder = self._builder(query_params=params)
        qs_ids = list(builder.get_queryset().values_list("id", flat=True))
        export_rows = list(builder.iter_export_rows())
        header = export_rows[0]
        id_idx = header.index("id")
        export_ids = [int(r[id_idx]) for r in export_rows[1:]]
        self.assertEqual(qs_ids, export_ids)

    def test_request_date_params_without_timezone_consistency(self):
        """通过 Request 传 date_from/date_to 字符串，结果与手动 Q 一致。"""
        Ticket.objects.create(
            title="D1",
            queue=self.queue,
            status=Ticket.OPEN_STATUS,
            created=self._naive(hour=5),
        )
        Ticket.objects.create(
            title="D2",
            queue=self.queue,
            status=Ticket.OPEN_STATUS,
            created=self._naive(hour=15),
        )
        request = self.factory.get(
            "/tickets/", {"date_from": "2025-07-01", "date_to": "2025-07-01"}
        )
        req_params = build_query_params_from_request(request)
        req_builder = TicketQueryBuilder(self.huser, query_params=req_params)
        manual_builder = TicketQueryBuilder(
            self.huser,
            query_params={
                "filtering": {
                    "created__gte": "2025-07-01",
                    "created__lte": "2025-07-01",
                },
                "sorting": "created",
            },
        )
        self.assertEqual(req_builder.count(), manual_builder.count())
        self.assertEqual(
            set(req_builder.get_queryset().values_list("id", flat=True)),
            set(manual_builder.get_queryset().values_list("id", flat=True)),
        )

    def test_last_followup_annotation_with_mixed_timezones(self):
        """不同时间点 followup 的 annotation 结果与查询一致。"""
        t = Ticket.objects.create(
            title="TZ FollowUp",
            queue=self.queue,
            status=Ticket.OPEN_STATUS,
        )
        FollowUp.objects.create(
            ticket=t,
            date=self._naive(hour=20),
            title="FU 1",
            comment="c1",
        )
        FollowUp.objects.create(
            ticket=t,
            date=self._naive(hour=8),
            title="FU 2",
            comment="c2",
        )
        params = {"sorting": "id"}
        builder = self._builder(query_params=params)
        qs_count = builder.count()
        self.assertEqual(qs_count, 1)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsTotal"], qs_count)
        self.assertEqual(len(dt["data"]), 1)
        export_rows = list(builder.iter_export_rows())
        self.assertEqual(len(export_rows) - 1, qs_count)

    def test_pagination_with_mixed_timezones(self):
        """跨时间点工单分页 batch 顺序一致。"""
        tzs = [
            (self._naive(hour=10), 1),
            (self._naive(hour=0), 2),
            (self._naive(hour=3), 3),
            (self._naive(hour=22), 4),
            (self._naive(hour=12), 5),
        ]
        for created, i in tzs:
            Ticket.objects.create(
                title=f"TZ Paginated {i}",
                queue=self.queue,
                status=Ticket.OPEN_STATUS,
                created=created,
            )
        params = {"sorting": "created", "sortreverse": True}
        builder = self._builder(query_params=params)
        full_ids = list(
            builder.get_queryset().order_by("-created").values_list("id", flat=True)
        )
        batch_size = 2
        collected = []
        for start in range(0, len(full_ids), batch_size):
            dt = builder.get_datatables_context(
                draw=["0"],
                length=[str(batch_size)],
                start=[str(start)],
                **{"order[0][column]": ["5"], "order[0][dir]": ["desc"]},
            )
            collected.extend([d["id"] for d in dt["data"]])
        self.assertEqual(collected, full_ids)

    @override_settings(USE_TZ=True, TIME_ZONE="UTC")
    def test_timezone_setting_change_preserves_qs_equality(self):
        """在 USE_TZ=True 环境下，查询层与导出结果仍然一致。"""
        tz_utc = timezone.utc
        d1 = timezone.datetime(2025, 7, 1, 10, 0, tzinfo=tz_utc)
        d2 = timezone.datetime(2025, 7, 1, 18, 0, tzinfo=tz_utc)
        try:
            t1 = Ticket.objects.create(
                title="T1 UTC",
                queue=self.queue,
                status=Ticket.OPEN_STATUS,
                created=d1,
            )
            t2 = Ticket.objects.create(
                title="T2 UTC later",
                queue=self.queue,
                status=Ticket.OPEN_STATUS,
                created=d2,
            )
        except (ValueError, TypeError):
            self.skipTest("SQLite backend incompatible with USE_TZ=True in this env")
        params = {"sorting": "created"}
        builder = self._builder(query_params=params)
        qs_ids = list(builder.get_queryset().values_list("id", flat=True))
        export_rows = list(builder.iter_export_rows())
        header = export_rows[0]
        id_idx = header.index("id")
        export_ids = [int(r[id_idx]) for r in export_rows[1:]]
        self.assertEqual(qs_ids, export_ids)
        dt = builder.get_datatables_context(
            draw=["0"], length=["100"], start=["0"]
        )
        self.assertEqual(dt["recordsTotal"], builder.count())
        self.assertEqual(len(dt["data"]), builder.count())
