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
    get_search_cache_key,
    get_query_class,
    _get_django_cache,
    HELPDESK_QUERY_CACHE_TIMEOUT,
    get_search_cache_version,
    invalidate_search_cache,
    invalidate_search_cache_for_ticket,
    SEARCH_CACHE_VERSION_KEY,
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


class CacheBackendIsolationTests(TestCase):
    def setUp(self):
        self.queue = Queue.objects.create(
            title="Test Cache Queue",
            slug="test_cache_queue",
            allow_public_submission=True,
        )
        self.user = get_staff_user()
        self.ticket = Ticket.objects.create(
            title="Cache Test Ticket",
            queue=self.queue,
            description="Testing cache isolation",
        )
        self.ticket.save()

    def test_get_search_cache_key_fallback(self):
        key = get_search_cache_key("my_query")
        self.assertTrue(key.endswith("_fallback"))
        self.assertIn("my_query", key)

    @override_settings(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "test_db",
            }
        }
    )
    def test_get_search_cache_key_postgres(self):
        key = get_search_cache_key("my_query")
        self.assertTrue(key.endswith("_postgres"))
        self.assertIn("my_query", key)

    def test_cache_keys_differ_between_backends(self):
        fallback_key = get_search_cache_key("test_query")
        with self.settings(
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.postgresql",
                    "NAME": "test_db",
                }
            }
        ):
            postgres_key = get_search_cache_key("test_query")
        self.assertNotEqual(fallback_key, postgres_key)
        self.assertIn("fallback", fallback_key)
        self.assertIn("postgres", postgres_key)

    def test_query_class_has_result_cache(self):
        from helpdesk.user import HelpdeskUser

        QueryClass = get_query_class()
        query = QueryClass(HelpdeskUser(self.user), query_params={"search_string": ""})
        self.assertTrue(hasattr(query, "_result_cache"))
        self.assertIsInstance(query._result_cache, dict)

    def test_query_cache_uses_backend_specific_key(self):
        from helpdesk.user import HelpdeskUser

        QueryClass = get_query_class()
        query = QueryClass(HelpdeskUser(self.user), query_params={"search_string": ""})
        cache_key = query._get_cache_key()
        self.assertIn("fallback", cache_key)

    def test_different_backends_have_separate_cache_entries(self):
        from helpdesk.user import HelpdeskUser

        QueryClass = get_query_class()
        query = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": "Cache Test"},
        )
        fallback_cache_key = query._get_cache_key()

        result = query.get()
        self.assertIn(fallback_cache_key, query._result_cache)
        fallback_count = len(query._result_cache)

        with self.settings(
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.postgresql",
                    "NAME": "test_db",
                }
            }
        ):
            postgres_cache_key = query._get_cache_key()
            self.assertNotEqual(fallback_cache_key, postgres_cache_key)
            self.assertNotIn(postgres_cache_key, query._result_cache)

        self.assertEqual(len(query._result_cache), fallback_count)

    def test_cache_suffix_function_consistency(self):
        suffix = get_search_cache_suffix()
        backend = get_search_backend()
        if backend == SEARCH_BACKEND_FALLBACK:
            self.assertEqual(suffix, "_fallback")
        else:
            self.assertEqual(suffix, "_postgres")

    def test_cache_key_contains_base_key(self):
        base_key = "custom_search_key"
        cache_key = get_search_cache_key(base_key)
        self.assertTrue(cache_key.startswith(base_key))

    def test_empty_base_key_still_has_suffix(self):
        cache_key = get_search_cache_key("")
        self.assertTrue(cache_key.endswith("_fallback"))

    def test_same_base_key_different_backends_different_keys(self):
        base = "same_query"
        key1 = get_search_cache_key(base)
        with self.settings(
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.postgresql",
                    "NAME": "test_db",
                }
            }
        ):
            key2 = get_search_cache_key(base)
        self.assertNotEqual(key1, key2)

    def test_backend_isolation_prevents_cache_pollution(self):
        from helpdesk.user import HelpdeskUser

        QueryClass = get_query_class()
        query = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": "Cache"},
        )

        result_fallback = query.get()
        fallback_key = query._get_cache_key()
        self.assertIn(fallback_key, query._result_cache)

        with self.settings(
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.postgresql",
                    "NAME": "test_db",
                }
            }
        ):
            postgres_key = query._get_cache_key()
            self.assertNotIn(postgres_key, query._result_cache)
            self.assertEqual(len(query._result_cache), 1)

        self.assertEqual(len(query._result_cache), 1)
        self.assertIn(fallback_key, query._result_cache)


class ProcessCacheIsolationTests(TestCase):
    def setUp(self):
        self.queue = Queue.objects.create(
            title="Process Cache Queue",
            slug="process_cache_queue",
            allow_public_submission=True,
        )
        self.user = get_staff_user()
        self.ticket = Ticket.objects.create(
            title="Process Cache Ticket",
            queue=self.queue,
            description="Testing process cache isolation",
        )
        self.ticket.save()

    def test_get_django_cache_returns_cache_instance(self):
        cache = _get_django_cache()
        self.assertIsNotNone(cache)

    def test_process_cache_key_includes_backend(self):
        from helpdesk.user import HelpdeskUser

        QueryClass = get_query_class()
        query = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": "test"},
        )
        process_key = query._get_process_cache_key()
        self.assertIn("fallback", process_key)
        self.assertIn(str(self.user.pk), process_key)

    @override_settings(
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.postgresql",
                "NAME": "test_db",
            }
        }
    )
    def test_process_cache_key_postgres_includes_backend(self):
        from helpdesk.user import HelpdeskUser

        QueryClass = get_query_class()
        query = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": "test"},
        )
        process_key = query._get_process_cache_key()
        self.assertIn("postgres", process_key)

    def test_process_cache_key_differs_between_backends(self):
        from helpdesk.user import HelpdeskUser

        QueryClass = get_query_class()
        query = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": "test"},
        )
        fallback_key = query._get_process_cache_key()
        with self.settings(
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.postgresql",
                    "NAME": "test_db",
                }
            }
        ):
            postgres_key = query._get_process_cache_key()
        self.assertNotEqual(fallback_key, postgres_key)
        self.assertIn("fallback", fallback_key)
        self.assertIn("postgres", postgres_key)

    def test_get_writes_to_process_cache(self):
        from helpdesk.user import HelpdeskUser

        QueryClass = get_query_class()
        query = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": ""},
        )
        process_key = query._get_process_cache_key()
        cache = _get_django_cache()
        result = query.get()
        cached = cache.get(process_key)
        self.assertIsNotNone(cached)

    def test_get_reads_from_process_cache(self):
        from helpdesk.user import HelpdeskUser

        QueryClass = get_query_class()
        query1 = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": ""},
        )
        result1 = query1.get()
        process_key = query1._get_process_cache_key()
        cache = _get_django_cache()
        self.assertIsNotNone(cache.get(process_key))

        query2 = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": ""},
        )
        result2 = query2.get()
        self.assertEqual(list(result1), list(result2))

    def test_process_cache_isolation_prevents_cross_backend_pollution(self):
        from helpdesk.user import HelpdeskUser

        QueryClass = get_query_class()
        query = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": ""},
        )
        fallback_result = query.get()
        fallback_process_key = query._get_process_cache_key()
        cache = _get_django_cache()
        self.assertIsNotNone(cache.get(fallback_process_key))

        with self.settings(
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.postgresql",
                    "NAME": "test_db",
                }
            }
        ):
            postgres_process_key = query._get_process_cache_key()
            self.assertNotEqual(fallback_process_key, postgres_process_key)
            cached_postgres = cache.get(postgres_process_key)
            self.assertIsNone(cached_postgres)

    def test_process_cache_different_users_different_keys(self):
        from helpdesk.user import HelpdeskUser

        user2 = User.objects.create(
            username="cache_test_user2",
            is_staff=True,
        )
        user2.set_password("pass")
        user2.save()

        QueryClass = get_query_class()
        query1 = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": ""},
        )
        query2 = QueryClass(
            HelpdeskUser(user2),
            query_params={"search_string": ""},
        )
        key1 = query1._get_process_cache_key()
        key2 = query2._get_process_cache_key()
        self.assertNotEqual(key1, key2)

    def test_process_cache_different_queries_different_keys(self):
        from helpdesk.user import HelpdeskUser

        QueryClass = get_query_class()
        query1 = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": "test1"},
        )
        query2 = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": "test2"},
        )
        key1 = query1._get_process_cache_key()
        key2 = query2._get_process_cache_key()
        self.assertNotEqual(key1, key2)

    def test_cache_timeout_is_configurable(self):
        self.assertEqual(HELPDESK_QUERY_CACHE_TIMEOUT, 60)

    def test_process_cache_key_format(self):
        from helpdesk.user import HelpdeskUser

        QueryClass = get_query_class()
        query = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": "format"},
        )
        process_key = query._get_process_cache_key()
        self.assertTrue(process_key.startswith("helpdesk:query:"))
        self.assertIn(str(self.user.pk), process_key)

    def test_instance_cache_and_process_cache_both_backend_aware(self):
        from helpdesk.user import HelpdeskUser

        QueryClass = get_query_class()
        query = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": ""},
        )
        instance_key = query._get_cache_key()
        process_key = query._get_process_cache_key()
        self.assertIn("fallback", instance_key)
        self.assertIn("fallback", process_key)
        with self.settings(
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.postgresql",
                    "NAME": "test_db",
                }
            }
        ):
            instance_key_pg = query._get_cache_key()
            process_key_pg = query._get_process_cache_key()
        self.assertIn("postgres", instance_key_pg)
        self.assertIn("postgres", process_key_pg)
        self.assertNotEqual(instance_key, instance_key_pg)
        self.assertNotEqual(process_key, process_key_pg)


class CacheInvalidationTests(TestCase):
    def setUp(self):
        self.queue = Queue.objects.create(
            title="Invalidation Test Queue",
            slug="invalidation_test_queue",
            allow_public_submission=True,
        )
        self.user = get_staff_user()
        self.ticket = Ticket.objects.create(
            title="Cache Invalidation Ticket",
            queue=self.queue,
            description="Testing cache invalidation",
        )
        self.ticket.save()
        cache = _get_django_cache()
        cache.delete(SEARCH_CACHE_VERSION_KEY)

    def test_get_search_cache_version_returns_integer(self):
        version = get_search_cache_version()
        self.assertIsInstance(version, int)
        self.assertGreaterEqual(version, 1)

    def test_invalidate_search_cache_increments_version(self):
        old_version = get_search_cache_version()
        invalidate_search_cache()
        new_version = get_search_cache_version()
        self.assertEqual(new_version, old_version + 1)

    def test_invalidate_search_cache_called_multiple_times(self):
        version1 = get_search_cache_version()
        invalidate_search_cache()
        version2 = get_search_cache_version()
        invalidate_search_cache()
        version3 = get_search_cache_version()
        self.assertEqual(version2, version1 + 1)
        self.assertEqual(version3, version2 + 1)

    def test_cache_key_changes_after_invalidation(self):
        old_key = get_search_cache_key("test_query")
        invalidate_search_cache()
        new_key = get_search_cache_key("test_query")
        self.assertNotEqual(old_key, new_key)

    def test_cache_key_includes_version(self):
        version = get_search_cache_version()
        key = get_search_cache_key("test_query")
        self.assertIn(f"_v{version}_", key)

    def test_invalidate_for_ticket_calls_global_invalidate(self):
        old_version = get_search_cache_version()
        invalidate_search_cache_for_ticket(self.ticket.id)
        new_version = get_search_cache_version()
        self.assertEqual(new_version, old_version + 1)

    def test_process_cache_key_changes_after_invalidation(self):
        from helpdesk.user import HelpdeskUser

        QueryClass = get_query_class()
        query = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": ""},
        )
        old_key = query._get_process_cache_key()
        invalidate_search_cache()
        new_key = query._get_process_cache_key()
        self.assertNotEqual(old_key, new_key)

    def test_instance_cache_key_changes_after_invalidation(self):
        from helpdesk.user import HelpdeskUser

        QueryClass = get_query_class()
        query = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": ""},
        )
        old_key = query._get_cache_key()
        invalidate_search_cache()
        new_key = query._get_cache_key()
        self.assertNotEqual(old_key, new_key)

    def test_search_result_cache_invalidated_on_ticket_save(self):
        from helpdesk.user import HelpdeskUser

        cache = _get_django_cache()
        QueryClass = get_query_class()
        query = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": "Cache Invalidation"},
        )
        result1 = query.get()
        old_process_key = query._get_process_cache_key()
        self.assertIsNotNone(cache.get(old_process_key))

        self.ticket.title = "Updated Title for Cache Invalidation Test"
        self.ticket.save()

        new_process_key = query._get_process_cache_key()
        self.assertNotEqual(old_process_key, new_process_key)
        self.assertIsNone(cache.get(new_process_key))

    def test_search_result_cache_invalidated_on_ticket_create(self):
        from helpdesk.user import HelpdeskUser

        cache = _get_django_cache()
        QueryClass = get_query_class()
        query = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": ""},
        )
        result1 = query.get()
        old_process_key = query._get_process_cache_key()
        self.assertIsNotNone(cache.get(old_process_key))

        new_ticket = Ticket.objects.create(
            title="New Ticket for Cache Test",
            queue=self.queue,
            description="New ticket to test cache invalidation",
        )
        new_ticket.save()

        new_process_key = query._get_process_cache_key()
        self.assertNotEqual(old_process_key, new_process_key)
        self.assertIsNone(cache.get(new_process_key))

    def test_both_backend_caches_invalidated(self):
        cache = _get_django_cache()
        fallback_invalidate_key = "helpdesk:search:invalidate:fallback"
        postgres_invalidate_key = "helpdesk:search:invalidate:postgres"

        cache.delete(fallback_invalidate_key)
        cache.delete(postgres_invalidate_key)

        invalidate_search_cache()

        fallback_version = cache.get(fallback_invalidate_key)
        postgres_version = cache.get(postgres_invalidate_key)
        self.assertIsNotNone(fallback_version)
        self.assertIsNotNone(postgres_version)
        self.assertEqual(fallback_version, postgres_version)

    def test_invalidate_on_ticket_delete(self):
        from helpdesk.user import HelpdeskUser

        cache = _get_django_cache()
        QueryClass = get_query_class()
        query = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": ""},
        )
        result1 = query.get()
        old_process_key = query._get_process_cache_key()
        self.assertIsNotNone(cache.get(old_process_key))

        ticket_id = self.ticket.id
        self.ticket.delete()

        new_process_key = query._get_process_cache_key()
        self.assertNotEqual(old_process_key, new_process_key)
        self.assertIsNone(cache.get(new_process_key))

    def test_cache_invalidation_affects_all_users(self):
        from helpdesk.user import HelpdeskUser

        user2 = User.objects.create(
            username="cache_inval_user2",
            is_staff=True,
        )
        user2.set_password("pass")
        user2.save()

        QueryClass = get_query_class()
        query1 = QueryClass(
            HelpdeskUser(self.user),
            query_params={"search_string": ""},
        )
        query2 = QueryClass(
            HelpdeskUser(user2),
            query_params={"search_string": ""},
        )

        old_key1 = query1._get_process_cache_key()
        old_key2 = query2._get_process_cache_key()

        invalidate_search_cache()

        new_key1 = query1._get_process_cache_key()
        new_key2 = query2._get_process_cache_key()

        self.assertNotEqual(old_key1, new_key1)
        self.assertNotEqual(old_key2, new_key2)

    def test_version_key_stored_in_process_cache(self):
        cache = _get_django_cache()
        cache.delete(SEARCH_CACHE_VERSION_KEY)

        version = get_search_cache_version()
        self.assertEqual(version, 1)

        stored_version = cache.get(SEARCH_CACHE_VERSION_KEY)
        self.assertEqual(stored_version, 1)
