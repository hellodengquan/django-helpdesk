from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from helpdesk.models import Macro, MacroUsage, Queue, ReplyDraft, Ticket
from helpdesk.lib import render_macro, get_available_macros_for_user, can_use_macro
from django.utils import timezone


class MacroModelTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.User = get_user_model()
        cls.user1 = cls.User.objects.create_user("user1", password="pass", email="user1@example.com")
        cls.user1.is_staff = True
        cls.user1.save()

        cls.user2 = cls.User.objects.create_user("user2", password="pass", email="user2@example.com")
        cls.user2.is_staff = True
        cls.user2.save()

        cls.queue1 = Queue.objects.create(title="Queue 1", slug="queue1")
        cls.queue2 = Queue.objects.create(title="Queue 2", slug="queue2")

        cls.ticket = Ticket.objects.create(
            title="Test Ticket",
            queue=cls.queue1,
            submitter_email="submitter@example.com",
            description="Test description",
        )

    def test_create_personal_macro(self):
        macro = Macro.objects.create(
            name="Test Personal Macro",
            body="Hello {{ submitter_name }}!",
            author=self.user1,
            is_shared=False,
        )
        self.assertEqual(macro.name, "Test Personal Macro")
        self.assertFalse(macro.is_shared)
        self.assertEqual(macro.author, self.user1)
        self.assertEqual(macro.status, Macro.ACTIVE_STATUS)

    def test_create_shared_macro(self):
        macro = Macro.objects.create(
            name="Test Shared Macro",
            body="Shared greeting for {{ ticket.title }}",
            author=self.user1,
            is_shared=True,
        )
        macro.queues.add(self.queue1)
        self.assertTrue(macro.is_shared)
        self.assertIn(self.queue1, macro.queues.all())

    def test_macro_can_view_owner(self):
        macro = Macro.objects.create(
            name="Personal Macro",
            body="Test",
            author=self.user1,
            is_shared=False,
        )
        self.assertTrue(macro.can_view(self.user1))
        self.assertFalse(macro.can_view(self.user2))

    def test_macro_can_view_shared(self):
        macro = Macro.objects.create(
            name="Shared Macro",
            body="Test",
            author=self.user1,
            is_shared=True,
            status=Macro.ACTIVE_STATUS,
        )
        self.assertTrue(macro.can_view(self.user1))
        self.assertTrue(macro.can_view(self.user2))

    def test_macro_can_view_archived(self):
        macro = Macro.objects.create(
            name="Archived Macro",
            body="Test",
            author=self.user1,
            is_shared=True,
            status=Macro.ARCHIVED_STATUS,
        )
        self.assertTrue(macro.can_view(self.user1))
        self.assertFalse(macro.can_view(self.user2))

    def test_macro_can_edit_owner(self):
        macro = Macro.objects.create(
            name="Test Macro",
            body="Test",
            author=self.user1,
            is_shared=True,
        )
        self.assertTrue(macro.can_edit(self.user1))
        self.assertFalse(macro.can_edit(self.user2))

    def test_macro_can_use_with_queue_filter(self):
        macro = Macro.objects.create(
            name="Queue 1 Macro",
            body="Test",
            author=self.user1,
            is_shared=True,
        )
        macro.queues.add(self.queue1)

        ticket_queue1 = Ticket.objects.create(
            title="Queue 1 Ticket",
            queue=self.queue1,
        )
        ticket_queue2 = Ticket.objects.create(
            title="Queue 2 Ticket",
            queue=self.queue2,
        )

        self.assertTrue(macro.can_use(self.user1, ticket_queue1))
        self.assertFalse(macro.can_use(self.user1, ticket_queue2))

    def test_macro_can_use_no_queue_filter(self):
        macro = Macro.objects.create(
            name="All Queues Macro",
            body="Test",
            author=self.user1,
            is_shared=True,
        )

        self.assertTrue(macro.can_use(self.user1, self.ticket))

    def test_macro_increment_usage(self):
        macro = Macro.objects.create(
            name="Test Macro",
            body="Test",
            author=self.user1,
        )
        self.assertEqual(macro.usage_count, 0)
        macro.increment_usage()
        macro.refresh_from_db()
        self.assertEqual(macro.usage_count, 1)


class MacroRenderTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.User = get_user_model()
        cls.user = cls.User.objects.create_user("testuser", password="pass", email="test@example.com")
        cls.user.first_name = "Test"
        cls.user.last_name = "User"
        cls.user.is_staff = True
        cls.user.save()

        cls.queue = Queue.objects.create(title="Support Queue", slug="support")

        cls.ticket = Ticket.objects.create(
            title="Help with login",
            queue=cls.queue,
            submitter_email="john@example.com",
            description="I can't log in.",
        )

    def test_render_macro_basic_variables(self):
        body = "Ticket: {{ ticket.title }}\nQueue: {{ queue.title }}"
        rendered = render_macro(body, self.ticket, self.user)
        self.assertIn("Help with login", rendered)
        self.assertIn("Support Queue", rendered)

    def test_render_macro_submitter_variables(self):
        body = "Hello {{ submitter_name }}!"
        rendered = render_macro(body, self.ticket, self.user)
        self.assertIn("john", rendered)

    def test_render_macro_user_variables(self):
        body = "Best regards, {{ user.get_full_name }}"
        rendered = render_macro(body, self.ticket, self.user)
        self.assertIn("Test User", rendered)

    def test_render_macro_convenience_variables(self):
        body = "Re: {{ ticket_title }} (Ticket #{{ ticket_id }})"
        rendered = render_macro(body, self.ticket, self.user)
        self.assertIn("Help with login", rendered)
        self.assertIn(str(self.ticket.id), rendered)

    def test_render_macro_unknown_variable(self):
        body = "Hello {{ nonexistent_var }}"
        rendered = render_macro(body, self.ticket, self.user)
        self.assertIn("", rendered)

    def test_render_macro_no_user(self):
        body = "Ticket: {{ ticket.title }}"
        rendered = render_macro(body, self.ticket)
        self.assertIn("Help with login", rendered)


class MacroUsageTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.User = get_user_model()
        cls.user = cls.User.objects.create_user("testuser", password="pass")
        cls.user.is_staff = True
        cls.user.save()

        cls.queue = Queue.objects.create(title="Test Queue", slug="test")
        cls.ticket = Ticket.objects.create(title="Test", queue=cls.queue)
        cls.macro = Macro.objects.create(
            name="Test Macro",
            body="Test body {{ ticket.title }}",
            author=cls.user,
            is_shared=True,
        )

    def test_record_usage(self):
        rendered_body = render_macro(self.macro.body, self.ticket, self.user)

        usage = MacroUsage.record_usage(
            macro=self.macro,
            user=self.user,
            ticket=self.ticket,
            rendered_body=rendered_body,
            context={"ticket_title": self.ticket.title},
        )

        self.assertEqual(usage.macro, self.macro)
        self.assertEqual(usage.user, self.user)
        self.assertEqual(usage.ticket, self.ticket)
        self.assertEqual(usage.macro_name, "Test Macro")
        self.assertIn("Test", usage.rendered_body)

        self.macro.refresh_from_db()
        self.assertEqual(self.macro.usage_count, 1)

    def test_record_usage_macro_deleted(self):
        macro = Macro.objects.create(
            name="Temporary Macro",
            body="Test",
            author=self.user,
        )
        macro_id = macro.id

        usage = MacroUsage.record_usage(
            macro=macro,
            user=self.user,
            ticket=self.ticket,
            rendered_body="Test",
        )

        macro.delete()

        usage.refresh_from_db()
        self.assertIsNone(usage.macro)
        self.assertEqual(usage.macro_name, "Temporary Macro")


class ReplyDraftTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.User = get_user_model()
        cls.user1 = cls.User.objects.create_user("user1", password="pass")
        cls.user1.is_staff = True
        cls.user1.save()

        cls.user2 = cls.User.objects.create_user("user2", password="pass")
        cls.user2.is_staff = True
        cls.user2.save()

        cls.queue = Queue.objects.create(title="Test Queue", slug="test")
        cls.ticket = Ticket.objects.create(title="Test Ticket", queue=cls.queue)
        cls.macro = Macro.objects.create(
            name="Source Macro",
            body="Template body",
            author=cls.user1,
            is_shared=True,
        )

    def test_create_draft(self):
        draft = ReplyDraft.objects.create(
            ticket=self.ticket,
            author=self.user1,
            title="My Draft",
            body="Draft reply content",
            status=ReplyDraft.DRAFT,
        )
        self.assertEqual(draft.title, "My Draft")
        self.assertEqual(draft.status, ReplyDraft.DRAFT)
        self.assertTrue(draft.public)

    def test_draft_can_view(self):
        draft = ReplyDraft.objects.create(
            ticket=self.ticket,
            author=self.user1,
            body="Test",
        )
        self.assertTrue(draft.can_view(self.user1))
        self.assertFalse(draft.can_view(self.user2))

    def test_draft_can_edit(self):
        draft = ReplyDraft.objects.create(
            ticket=self.ticket,
            author=self.user1,
            body="Test",
            status=ReplyDraft.DRAFT,
        )
        self.assertTrue(draft.can_edit(self.user1))
        self.assertFalse(draft.can_edit(self.user2))

    def test_draft_cannot_edit_submitted(self):
        draft = ReplyDraft.objects.create(
            ticket=self.ticket,
            author=self.user1,
            body="Test",
            status=ReplyDraft.SUBMITTED,
        )
        self.assertFalse(draft.can_edit(self.user1))

    def test_draft_mark_submitted(self):
        draft = ReplyDraft.objects.create(
            ticket=self.ticket,
            author=self.user1,
            body="Test",
        )
        draft.mark_submitted()
        self.assertEqual(draft.status, ReplyDraft.SUBMITTED)
        self.assertIsNotNone(draft.submitted_at)

    def test_draft_mark_discarded(self):
        draft = ReplyDraft.objects.create(
            ticket=self.ticket,
            author=self.user1,
            body="Test",
        )
        draft.mark_discarded()
        self.assertEqual(draft.status, ReplyDraft.DISCARDED)

    def test_draft_with_macro(self):
        draft = ReplyDraft.objects.create(
            ticket=self.ticket,
            author=self.user1,
            body="Modified from macro",
            macro=self.macro,
        )
        self.assertEqual(draft.macro, self.macro)


class AvailableMacrosTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.User = get_user_model()
        cls.user1 = cls.User.objects.create_user("user1", password="pass")
        cls.user1.is_staff = True
        cls.user1.save()

        cls.user2 = cls.User.objects.create_user("user2", password="pass")
        cls.user2.is_staff = True
        cls.user2.save()

        cls.queue1 = Queue.objects.create(title="Queue 1", slug="q1")
        cls.queue2 = Queue.objects.create(title="Queue 2", slug="q2")

        cls.shared_macro = Macro.objects.create(
            name="Shared Macro",
            body="Shared",
            author=cls.user1,
            is_shared=True,
            status=Macro.ACTIVE_STATUS,
        )
        cls.shared_macro.queues.add(cls.queue1)

        cls.personal_macro_user1 = Macro.objects.create(
            name="User1 Personal",
            body="Personal 1",
            author=cls.user1,
            is_shared=False,
        )

        cls.personal_macro_user2 = Macro.objects.create(
            name="User2 Personal",
            body="Personal 2",
            author=cls.user2,
            is_shared=False,
        )

        cls.archived_shared_macro = Macro.objects.create(
            name="Archived Shared",
            body="Archived",
            author=cls.user1,
            is_shared=True,
            status=Macro.ARCHIVED_STATUS,
        )

    def test_user1_available_macros(self):
        macros = get_available_macros_for_user(self.user1)
        macro_names = [m.name for m in macros]

        self.assertIn("Shared Macro", macro_names)
        self.assertIn("User1 Personal", macro_names)
        self.assertNotIn("User2 Personal", macro_names)
        self.assertNotIn("Archived Shared", macro_names)

    def test_user2_available_macros(self):
        macros = get_available_macros_for_user(self.user2)
        macro_names = [m.name for m in macros]

        self.assertIn("Shared Macro", macro_names)
        self.assertIn("User2 Personal", macro_names)
        self.assertNotIn("User1 Personal", macro_names)

    def test_available_macros_filtered_by_queue(self):
        macros = get_available_macros_for_user(self.user1, queue=self.queue1)
        macro_names = [m.name for m in macros]
        self.assertIn("Shared Macro", macro_names)
        self.assertIn("User1 Personal", macro_names)

        macros_q2 = get_available_macros_for_user(self.user1, queue=self.queue2)
        macro_names_q2 = [m.name for m in macros_q2]
        self.assertNotIn("Shared Macro", macro_names_q2)
        self.assertIn("User1 Personal", macro_names_q2)

    def test_can_use_macro_helper(self):
        self.assertTrue(can_use_macro(self.shared_macro, self.user1))
        self.assertTrue(can_use_macro(self.shared_macro, self.user2))
        self.assertTrue(can_use_macro(self.personal_macro_user1, self.user1))
        self.assertFalse(can_use_macro(self.personal_macro_user1, self.user2))
        self.assertFalse(can_use_macro(None, self.user1))


class MacroViewTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.User = get_user_model()
        cls.user = cls.User.objects.create_user("testuser", password="pass")
        cls.user.is_staff = True
        cls.user.save()

        cls.queue = Queue.objects.create(title="Test Queue", slug="test")
        cls.ticket = Ticket.objects.create(title="Test Ticket", queue=cls.queue)

    def setUp(self):
        self.client.login(username="testuser", password="pass")

    def test_macro_list_view(self):
        Macro.objects.create(
            name="Test Macro",
            body="Test",
            author=self.user,
            is_shared=False,
        )

        response = self.client.get(reverse("helpdesk:macro_list"))
        self.assertEqual(response.status_code, 200)

    def test_macro_create_view_get(self):
        response = self.client.get(reverse("helpdesk:macro_create"))
        self.assertEqual(response.status_code, 200)

    def test_macro_create_view_post(self):
        data = {
            "name": "New Macro",
            "body": "Hello {{ ticket.title }}",
            "is_shared": False,
            "status": "active",
        }
        response = self.client.post(reverse("helpdesk:macro_create"), data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(Macro.objects.filter(name="New Macro").exists())

    def test_macro_render_api(self):
        macro = Macro.objects.create(
            name="Test Macro",
            body="Hello {{ ticket.title }}",
            author=self.user,
            is_shared=True,
        )

        data = {
            "macro_id": macro.id,
            "ticket_id": self.ticket.id,
        }
        response = self.client.post(reverse("helpdesk:macro_render"), data)
        self.assertEqual(response.status_code, 200)
        json_data = response.json()
        self.assertIn("rendered_body", json_data)
        self.assertIn("Test Ticket", json_data["rendered_body"])

    def test_reply_draft_list_view(self):
        ReplyDraft.objects.create(
            ticket=self.ticket,
            author=self.user,
            body="Test draft",
        )

        response = self.client.get(reverse("helpdesk:reply_draft_list"))
        self.assertEqual(response.status_code, 200)

    def test_reply_draft_create_view_get(self):
        response = self.client.get(
            reverse("helpdesk:reply_draft_create", kwargs={"ticket_id": self.ticket.id})
        )
        self.assertEqual(response.status_code, 200)

    def test_reply_draft_create_view_post(self):
        data = {
            "title": "My Draft",
            "body": "Draft content",
            "public": True,
        }
        response = self.client.post(
            reverse("helpdesk:reply_draft_create", kwargs={"ticket_id": self.ticket.id}),
            data,
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(ReplyDraft.objects.filter(title="My Draft").exists())

    def test_macro_usage_stats_view(self):
        response = self.client.get(reverse("helpdesk:macro_usage_stats"))
        self.assertEqual(response.status_code, 200)

    def test_macro_use_endpoint(self):
        macro = Macro.objects.create(
            name="Use Test Macro",
            body="Hello {{ ticket.title }}",
            author=self.user,
            is_shared=True,
        )

        response = self.client.post(
            reverse(
                "helpdesk:macro_use",
                kwargs={"macro_id": macro.id, "ticket_id": self.ticket.id},
            )
        )
        self.assertEqual(response.status_code, 200)
        json_data = response.json()
        self.assertEqual(json_data["macro_name"], "Use Test Macro")
        self.assertIn("Test Ticket", json_data["rendered_body"])

        macro.refresh_from_db()
        self.assertEqual(macro.usage_count, 1)

        self.assertTrue(
            MacroUsage.objects.filter(macro=macro, ticket=self.ticket, user=self.user).exists()
        )


class TicketDetailMacroIntegrationTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.User = get_user_model()
        cls.user = cls.User.objects.create_user("staffuser", password="pass")
        cls.user.is_staff = True
        cls.user.save()

        cls.other_user = cls.User.objects.create_user("otherstaff", password="pass")
        cls.other_user.is_staff = True
        cls.other_user.save()

        cls.queue = Queue.objects.create(title="Support Queue", slug="support")
        cls.other_queue = Queue.objects.create(title="Billing Queue", slug="billing")

        cls.ticket = Ticket.objects.create(
            title="Login Issue",
            queue=cls.queue,
            submitter_email="customer@example.com",
        )

        cls.shared_macro = Macro.objects.create(
            name="Greeting",
            body="Hello {{ submitter_name }},\n\nThank you for contacting us about: {{ ticket.title }}.\n\nBest regards,\n{{ user.get_full_name }}",
            author=cls.other_user,
            is_shared=True,
        )

        cls.personal_macro = Macro.objects.create(
            name="My Quick Reply",
            body="Hi {{ submitter_name }},\nWe're looking into this.",
            author=cls.user,
            is_shared=False,
        )

        cls.other_queue_macro = Macro.objects.create(
            name="Billing Only",
            body="Billing response",
            author=cls.other_user,
            is_shared=True,
        )
        cls.other_queue_macro.queues.add(cls.other_queue)

    def setUp(self):
        self.client.login(username="staffuser", password="pass")

    def test_ticket_detail_has_available_macros_in_context(self):
        response = self.client.get(reverse("helpdesk:view", kwargs={"ticket_id": self.ticket.id}))
        self.assertEqual(response.status_code, 200)
        self.assertIn("available_macros", response.context)

        macro_names = [m.name for m in response.context["available_macros"]]
        self.assertIn("Greeting", macro_names)
        self.assertIn("My Quick Reply", macro_names)
        self.assertNotIn("Billing Only", macro_names)

    def test_ticket_detail_renders_macro_panel(self):
        response = self.client.get(reverse("helpdesk:view", kwargs={"ticket_id": self.ticket.id}))
        content = response.content.decode("utf-8")
        self.assertIn("macro-btn", content)
        self.assertIn("Quick Reply Macro", content)
        self.assertIn("Greeting", content)
        self.assertIn("My Quick Reply", content)

    def test_ticket_detail_no_macros_for_other_queue(self):
        ticket = Ticket.objects.create(
            title="Billing Ticket",
            queue=self.other_queue,
        )
        response = self.client.get(reverse("helpdesk:view", kwargs={"ticket_id": ticket.id}))
        macro_names = [m.name for m in response.context["available_macros"]]
        self.assertIn("Billing Only", macro_names)
        self.assertIn("Greeting", macro_names)
        self.assertIn("My Quick Reply", macro_names)

    def test_ticket_detail_macro_panel_shows_queue_filtered_macros(self):
        queue_limited = Queue.objects.create(title="Limited Queue", slug="limited")
        limited_macro = Macro.objects.create(
            name="Limited Only",
            body="Limited",
            author=self.user,
            is_shared=True,
        )
        limited_macro.queues.add(queue_limited)
        ticket_limited = Ticket.objects.create(title="Limited Ticket", queue=queue_limited)

        response = self.client.get(reverse("helpdesk:view", kwargs={"ticket_id": ticket_limited.id}))
        macro_names = [m.name for m in response.context["available_macros"]]
        self.assertIn("Limited Only", macro_names)
        self.assertIn("Greeting", macro_names)
        self.assertIn("My Quick Reply", macro_names)

        other_ticket = Ticket.objects.create(title="Support Ticket", queue=self.queue)
        response2 = self.client.get(reverse("helpdesk:view", kwargs={"ticket_id": other_ticket.id}))
        macro_names2 = [m.name for m in response2.context["available_macros"]]
        self.assertNotIn("Limited Only", macro_names2)
        self.assertIn("Greeting", macro_names2)

    def test_ticket_detail_macro_panel_empty_when_no_macros_at_all(self):
        new_user = self.User.objects.create_user("newstaff", password="pass")
        new_user.is_staff = True
        new_user.save()
        self.client.login(username="newstaff", password="pass")

        isolated_queue = Queue.objects.create(title="Isolated Queue", slug="isolated")
        restricted_macro = Macro.objects.create(
            name="Restricted",
            body="Restricted",
            author=self.other_user,
            is_shared=True,
        )
        restricted_macro.queues.add(self.queue)

        isolated_ticket = Ticket.objects.create(title="Isolated", queue=isolated_queue)
        response = self.client.get(reverse("helpdesk:view", kwargs={"ticket_id": isolated_ticket.id}))
        macro_names = [m.name for m in response.context["available_macros"]]
        self.assertNotIn("Restricted", macro_names)
        self.assertNotIn("My Quick Reply", macro_names)

    def test_ticket_detail_macro_buttons_have_correct_data_attributes(self):
        response = self.client.get(reverse("helpdesk:view", kwargs={"ticket_id": self.ticket.id}))
        content = response.content.decode("utf-8")
        self.assertIn(f'data-macro-id="{self.shared_macro.id}"', content)
        self.assertIn(f'data-macro-id="{self.personal_macro.id}"', content)
        self.assertIn('data-macro-name="Greeting"', content)

    def test_ticket_detail_personal_macro_has_mine_badge(self):
        response = self.client.get(reverse("helpdesk:view", kwargs={"ticket_id": self.ticket.id}))
        content = response.content.decode("utf-8")
        self.assertIn("Mine", content)

    def test_end_to_end_macro_render_fills_comment(self):
        response = self.client.post(
            reverse("helpdesk:macro_render"),
            data={
                "macro_id": self.shared_macro.id,
                "ticket_id": self.ticket.id,
            },
        )
        self.assertEqual(response.status_code, 200)
        json_data = response.json()
        self.assertIn("Login Issue", json_data["rendered_body"])
        self.assertIn("customer", json_data["rendered_body"])
