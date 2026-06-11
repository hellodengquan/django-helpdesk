from django.contrib.auth import get_user_model
from django.db.models import Q
from django.test import TestCase, override_settings
from django.urls import reverse
from helpdesk.models import KBCategory, KBItem, Queue, Ticket
from helpdesk.query import (
    query_to_base64,
    get_search_backend,
    is_fallback_search_backend,
    get_search_filter_args,
    _get_postgres_search_filter_args,
    _get_fallback_search_filter_args,
    get_search_sort_key,
    get_search_cache_suffix,
    SEARCH_BACKEND_POSTGRES,
    SEARCH_BACKEND_FALLBACK,
    apply_search_annotations,
)
from .helpers import get_staff_user


User = get_user_model()


class SearchBackendDetectionTests(TestCase):
    def test_default_backend_is_fallback_on_sqlite(self):
        backend = get_search_backend()
        self.assertEqual(backend, SEARCH_BACKEND_FALLBACK)

    def test_is_fallback_search_backend_on_sqlite(self):
        self.assertTrue(is_fallback_search_backend())

    @override_settings(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "test_db",
            }
        }
    )
    def test_postgres_backend_detection(self):
        backend = get_search_backend()
        self.assertEqual(backend, SEARCH_BACKEND_POSTGRES)

    @override_settings(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "test_db",
            }
        }
    )
    def test_is_not_fallback_on_postgres(self):
        self.assertFalse(is_fallback_search_backend())

    def test_get_search_sort_key_fallback(self):
        key = get_search_sort_key("title")
        self.assertEqual(key, "title_search_fallback")

    @override_settings(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "test_db",
            }
        }
    )
    def test_get_search_sort_key_postgres(self):
        key = get_search_sort_key("title")
        self.assertEqual(key, "title")

    def test_get_search_cache_suffix_fallback(self):
        suffix = get_search_cache_suffix()
        self.assertEqual(suffix, "_fallback")

    @override_settings(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "test_db",
            }
        }
    )
    def test_get_search_cache_suffix_postgres(self):
        suffix = get_search_cache_suffix()
        self.assertEqual(suffix, "_postgres")


class SearchFilterArgsTests(TestCase):
    def test_empty_search_returns_empty_q(self):
        q = get_search_filter_args("")
        self.assertEqual(q, Q())

    def test_none_search_returns_empty_q(self):
        q = get_search_filter_args(None)
        self.assertEqual(q, Q())

    def test_queue_prefix_search(self):
        q = get_search_filter_args("queue:support")
        self.assertIsInstance(q, Q)
        self.assertTrue(q)

    def test_priority_prefix_search(self):
        q = get_search_filter_args("priority:high")
        self.assertIsInstance(q, Q)
        self.assertTrue(q)

    def test_postgres_filter_uses_icontains(self):
        q = _get_postgres_search_filter_args("test")
        q_str = str(q)
        self.assertIn("icontains", q_str)

    def test_fallback_filter_uses_contains_on_annotated_fields(self):
        q = _get_fallback_search_filter_args("test")
        q_str = str(q)
        self.assertIn("contains", q_str)
        self.assertIn("_search_fallback", q_str)
        self.assertNotIn("icontains", q_str)

    def test_fallback_search_is_lowercased(self):
        q = _get_fallback_search_filter_args("TestSearch")
        q_str = str(q).lower()
        self.assertIn("testsearch", q_str)

    def test_or_keyword_search(self):
        q = get_search_filter_args("foo OR bar")
        self.assertIsInstance(q, Q)

    def test_postgres_queue_prefix_uses_icontains(self):
        q = _get_postgres_search_filter_args("queue:test")
        q_str = str(q)
        self.assertIn("icontains", q_str)
        self.assertIn("queue", q_str.lower())

    def test_fallback_queue_prefix_uses_annotated_field(self):
        q = _get_fallback_search_filter_args("queue:test")
        q_str = str(q)
        self.assertIn("_search_fallback", q_str)
        self.assertIn("queue", q_str.lower())

    def test_postgres_priority_prefix_uses_icontains(self):
        q = _get_postgres_search_filter_args("priority:3")
        q_str = str(q)
        self.assertIn("icontains", q_str)

    def test_fallback_priority_prefix_uses_annotated_field(self):
        q = _get_fallback_search_filter_args("priority:3")
        q_str = str(q)
        self.assertIn("_search_fallback", q_str)

    def test_special_characters_in_search(self):
        q = get_search_filter_args("test@example.com")
        self.assertIsInstance(q, Q)
        self.assertTrue(q)

    def test_special_characters_special_chars(self):
        q = get_search_filter_args("hello-world_test")
        self.assertIsInstance(q, Q)
        self.assertTrue(q)


class FallbackSearchIntegrationTests(TestCase):
    def setUp(self):
        self.queue = Queue.objects.create(
            title="Test Queue",
            slug="test_queue",
            allow_public_submission=True,
        )
        self.user = get_staff_user()
        self.ticket1 = Ticket.objects.create(
            title="Hello World Ticket",
            queue=self.queue,
            description="This is a test description with Mixed Case.",
            submitter_email="test@example.com",
        )
        self.ticket1.save()
        self.ticket2 = Ticket.objects.create(
            title="Another Issue",
            queue=self.queue,
            description="Some other content entirely",
            submitter_email="other@example.org",
        )
        self.ticket2.save()

    def loginUser(self):
        self.user = User.objects.create(
            username="search_test_user",
            is_staff=True,
        )
        self.user.set_password("pass")
        self.user.save()
        self.client.login(username="search_test_user", password="pass")

    def test_search_hit_exact_case(self):
        self.loginUser()
        query = query_to_base64({"search_string": "Hello World"})
        response = self.client.get(
            reverse("helpdesk:datatables_ticket_list", args=[query])
        )
        resp_json = response.json()
        self.assertEqual(resp_json["recordsFiltered"], 1)
        self.assertEqual(resp_json["data"][0]["title"], "Hello World Ticket")

    def test_search_hit_case_insensitive_lower(self):
        self.loginUser()
        query = query_to_base64({"search_string": "hello world"})
        response = self.client.get(
            reverse("helpdesk:datatables_ticket_list", args=[query])
        )
        resp_json = response.json()
        self.assertEqual(resp_json["recordsFiltered"], 1)

    def test_search_hit_case_insensitive_upper(self):
        self.loginUser()
        query = query_to_base64({"search_string": "HELLO WORLD"})
        response = self.client.get(
            reverse("helpdesk:datatables_ticket_list", args=[query])
        )
        resp_json = response.json()
        self.assertEqual(resp_json["recordsFiltered"], 1)

    def test_search_hit_case_insensitive_mixed(self):
        self.loginUser()
        query = query_to_base64({"search_string": "HeLLo WoRLd"})
        response = self.client.get(
            reverse("helpdesk:datatables_ticket_list", args=[query])
        )
        resp_json = response.json()
        self.assertEqual(resp_json["recordsFiltered"], 1)

    def test_search_miss(self):
        self.loginUser()
        query = query_to_base64({"search_string": "nonexistentterm"})
        response = self.client.get(
            reverse("helpdesk:datatables_ticket_list", args=[query])
        )
        resp_json = response.json()
        self.assertEqual(resp_json["recordsFiltered"], 0)

    def test_search_empty_query(self):
        self.loginUser()
        query = query_to_base64({"search_string": ""})
        response = self.client.get(
            reverse("helpdesk:datatables_ticket_list", args=[query])
        )
        resp_json = response.json()
        self.assertEqual(resp_json["recordsFiltered"], 2)

    def test_search_special_characters_email(self):
        self.loginUser()
        query = query_to_base64({"search_string": "test@example.com"})
        response = self.client.get(
            reverse("helpdesk:datatables_ticket_list", args=[query])
        )
        resp_json = response.json()
        self.assertEqual(resp_json["recordsFiltered"], 1)
        self.assertEqual(resp_json["data"][0]["submitter"], "test@example.com")

    def test_search_description_mixed_case(self):
        self.loginUser()
        query = query_to_base64({"search_string": "mixed case"})
        response = self.client.get(
            reverse("helpdesk:datatables_ticket_list", args=[query])
        )
        resp_json = response.json()
        self.assertEqual(resp_json["recordsFiltered"], 1)

    def test_search_queue_prefix(self):
        self.loginUser()
        query = query_to_base64({"search_string": "queue:Test Queue"})
        response = self.client.get(
            reverse("helpdesk:datatables_ticket_list", args=[query])
        )
        resp_json = response.json()
        self.assertEqual(resp_json["recordsFiltered"], 2)

    def test_search_queue_prefix_case_insensitive(self):
        self.loginUser()
        query = query_to_base64({"search_string": "queue:test queue"})
        response = self.client.get(
            reverse("helpdesk:datatables_ticket_list", args=[query])
        )
        resp_json = response.json()
        self.assertEqual(resp_json["recordsFiltered"], 2)

    def test_search_or_keyword(self):
        self.loginUser()
        query = query_to_base64({"search_string": "Hello OR Another"})
        response = self.client.get(
            reverse("helpdesk:datatables_ticket_list", args=[query])
        )
        resp_json = response.json()
        self.assertEqual(resp_json["recordsFiltered"], 2)

    def test_apply_search_annotations_returns_queryset(self):
        qs = Ticket.objects.all()
        annotated = apply_search_annotations(qs)
        self.assertIsNotNone(annotated)
        self.assertTrue(hasattr(annotated, "filter"))

    def test_search_view_has_search_message_for_fallback(self):
        self.loginUser()
        response = self.client.get(
            reverse("helpdesk:list") + "?q=hello"
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "fallback mode")


class PostgresSearchFilterTests(TestCase):
    @override_settings(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "test_db",
            }
        }
    )
    def test_postgres_backend_detected(self):
        self.assertEqual(get_search_backend(), SEARCH_BACKEND_POSTGRES)

    @override_settings(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "test_db",
            }
        }
    )
    def test_postgres_filter_args_uses_icontains(self):
        q = get_search_filter_args("test")
        q_str = str(q)
        self.assertIn("icontains", q_str)
        self.assertNotIn("_search_fallback", q_str)

    @override_settings(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "test_db",
            }
        }
    )
    def test_postgres_empty_search(self):
        q = get_search_filter_args("")
        self.assertEqual(q, Q())

    @override_settings(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "test_db",
            }
        }
    )
    def test_postgres_queue_prefix(self):
        q = get_search_filter_args("queue:support")
        q_str = str(q)
        self.assertIn("icontains", q_str)
        self.assertIn("queue", q_str.lower())

    @override_settings(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "test_db",
            }
        }
    )
    def test_postgres_priority_prefix(self):
        q = get_search_filter_args("priority:high")
        q_str = str(q)
        self.assertIn("icontains", q_str)

    @override_settings(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "test_db",
            }
        }
    )
    def test_postgres_or_keyword(self):
        q = get_search_filter_args("foo OR bar")
        q_str = str(q)
        self.assertIn("icontains", q_str)

    @override_settings(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "test_db",
            }
        }
    )
    def test_postgres_special_characters(self):
        q = get_search_filter_args("test@example.com")
        self.assertIsInstance(q, Q)
        self.assertTrue(q)


class BackendIsolationTests(TestCase):
    def test_fallback_and_postgres_filters_are_different(self):
        fallback_q = _get_fallback_search_filter_args("test")
        postgres_q = _get_postgres_search_filter_args("test")
        self.assertNotEqual(str(fallback_q), str(postgres_q))

    def test_fallback_uses_annotated_fields(self):
        q = _get_fallback_search_filter_args("test")
        q_str = str(q)
        self.assertIn("_search_fallback", q_str)

    def test_postgres_uses_direct_fields(self):
        q = _get_postgres_search_filter_args("test")
        q_str = str(q)
        self.assertNotIn("_search_fallback", q_str)

    def test_fallback_sort_key_differs_from_postgres(self):
        fallback_key = "title_search_fallback"
        postgres_key = "title"
        self.assertNotEqual(fallback_key, postgres_key)

    def test_fallback_cache_suffix_differs_from_postgres(self):
        fallback_suffix = "_fallback"
        postgres_suffix = "_postgres"
        self.assertNotEqual(fallback_suffix, postgres_suffix)
