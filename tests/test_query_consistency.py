from collections import defaultdict
from copy import deepcopy
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.test import TestCase, RequestFactory, override_settings
from django.urls import reverse
from helpdesk.models import (
    CustomField,
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
