from django.contrib.auth import get_user_model
from django.test import TestCase, Client, RequestFactory
from django.urls import reverse
from helpdesk.models import Queue, Ticket, UserTicketFollow
from helpdesk import settings as helpdesk_settings


class UserTicketFollowModelTestCase(TestCase):
    fixtures = ["emailtemplate.json"]

    def setUp(self):
        self.queue_public = Queue.objects.create(
            title="Queue 1",
            slug="q1",
            allow_public_submission=True,
        )

    def _create_staff_user(self, username="staff_user"):
        User = get_user_model()
        user = User.objects.create(
            username=username,
            is_staff=True,
            email=f"{username}@example.com",
        )
        user.set_password("pass")
        user.save()
        return user

    def _create_ticket(self, title="Test Ticket", **kwargs):
        defaults = {
            "queue": self.queue_public,
            "title": title,
            "description": "Test ticket description",
        }
        defaults.update(kwargs)
        return Ticket.objects.create(**defaults)

    def test_toggle_follow_creates_record(self):
        user = self._create_staff_user()
        ticket = self._create_ticket()
        follow_obj, is_following = UserTicketFollow.toggle_follow(user, ticket)
        self.assertTrue(is_following)
        self.assertIsNotNone(follow_obj)
        self.assertEqual(follow_obj.user, user)
        self.assertEqual(follow_obj.ticket, ticket)

    def test_toggle_follow_removes_record(self):
        user = self._create_staff_user()
        ticket = self._create_ticket()
        UserTicketFollow.toggle_follow(user, ticket)
        follow_obj, is_following = UserTicketFollow.toggle_follow(user, ticket)
        self.assertFalse(is_following)
        self.assertIsNone(follow_obj)

    def test_is_following(self):
        user = self._create_staff_user()
        ticket = self._create_ticket()
        self.assertFalse(UserTicketFollow.is_following(user, ticket))
        UserTicketFollow.objects.create(user=user, ticket=ticket)
        self.assertTrue(UserTicketFollow.is_following(user, ticket))

    def test_unique_constraint(self):
        user = self._create_staff_user()
        ticket = self._create_ticket()
        UserTicketFollow.objects.create(user=user, ticket=ticket)
        from django.db import IntegrityError
        with self.assertRaises(IntegrityError):
            UserTicketFollow.objects.create(user=user, ticket=ticket)

    def test_is_following_returns_false_for_anonymous(self):
        ticket = self._create_ticket()

        class AnonymousUser:
            is_authenticated = False

        self.assertFalse(UserTicketFollow.is_following(AnonymousUser(), ticket))


class FollowTicketEndpointTestCase(TestCase):
    fixtures = ["emailtemplate.json"]

    def setUp(self):
        self.queue = Queue.objects.create(
            title="Q", slug="q", allow_public_submission=True,
        )
        self.client = Client()
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = False

    def _make_staff(self, username="staff"):
        User = get_user_model()
        u = User.objects.create(
            username=username, is_staff=True, email=f"{username}@ex.com",
        )
        u.set_password("pw")
        u.save()
        return u

    def _login_staff(self, username="staff"):
        user = self._make_staff(username)
        self.client.login(username=username, password="pw")
        return user

    def _make_ticket(self, **kw):
        defaults = {"queue": self.queue, "title": "T", "description": "d"}
        defaults.update(kw)
        return Ticket.objects.create(**defaults)

    def test_follow_post_creates_follow(self):
        user = self._login_staff()
        ticket = self._make_ticket()
        url = reverse("helpdesk:follow_ticket", kwargs={"ticket_id": ticket.id})
        resp = self.client.post(url, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(UserTicketFollow.is_following(user, ticket))

    def test_unfollow_post_removes_follow(self):
        user = self._login_staff()
        ticket = self._make_ticket()
        UserTicketFollow.objects.create(user=user, ticket=ticket)
        url = reverse("helpdesk:follow_ticket", kwargs={"ticket_id": ticket.id})
        resp = self.client.post(url, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(UserTicketFollow.is_following(user, ticket))

    def test_follow_requires_login(self):
        ticket = self._make_ticket()
        url = reverse("helpdesk:follow_ticket", kwargs={"ticket_id": ticket.id})
        resp = self.client.post(url)
        self.assertIn(resp.status_code, (302, 403))
        self.assertEqual(UserTicketFollow.objects.count(), 0)

    def test_follow_requires_staff(self):
        User = get_user_model()
        user = User.objects.create(username="nonstaff", is_staff=False)
        user.set_password("pw")
        user.save()
        self.client.login(username="nonstaff", password="pw")
        ticket = self._make_ticket()
        url = reverse("helpdesk:follow_ticket", kwargs={"ticket_id": ticket.id})
        resp = self.client.post(url)
        self.assertIn(resp.status_code, (302, 403))
        self.assertFalse(UserTicketFollow.is_following(user, ticket))

    def test_follow_nonexistent_ticket_404(self):
        self._login_staff()
        url = reverse("helpdesk:follow_ticket", kwargs={"ticket_id": 99999})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 404)

    def test_follow_get_method_triggers_toggle(self):
        self._login_staff()
        ticket = self._make_ticket()
        url = reverse("helpdesk:follow_ticket", kwargs={"ticket_id": ticket.id})
        resp = self.client.get(url)
        self.assertIn(resp.status_code, (200, 301, 302))

    def test_follow_without_csrf_rejected(self):
        user = self._login_staff()
        ticket = self._make_ticket()
        url = reverse("helpdesk:follow_ticket", kwargs={"ticket_id": ticket.id})
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.login(username="staff", password="pw")
        resp = csrf_client.post(url)
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(UserTicketFollow.is_following(user, ticket))

    def test_follow_redirects_to_ticket_view(self):
        self._login_staff()
        ticket = self._make_ticket()
        url = reverse("helpdesk:follow_ticket", kwargs={"ticket_id": ticket.id})
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("helpdesk:view", args=[ticket.id]), resp.url)

    def test_follow_other_users_ticket(self):
        owner = self._make_staff("owner")
        follower = self._login_staff("follower")
        ticket = self._make_ticket(assigned_to=owner)
        url = reverse("helpdesk:follow_ticket", kwargs={"ticket_id": ticket.id})
        resp = self.client.post(url, follow=True)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(UserTicketFollow.is_following(follower, ticket))
        self.assertFalse(UserTicketFollow.is_following(owner, ticket))

    def test_multiple_users_can_follow_same_ticket(self):
        u1 = self._login_staff("u1")
        ticket = self._make_ticket()
        self.client.post(reverse("helpdesk:follow_ticket", kwargs={"ticket_id": ticket.id}))
        self.client.logout()

        u2 = self._make_staff("u2")
        self.client.login(username="u2", password="pw")
        self.client.post(reverse("helpdesk:follow_ticket", kwargs={"ticket_id": ticket.id}))

        self.assertTrue(UserTicketFollow.is_following(u1, ticket))
        self.assertTrue(UserTicketFollow.is_following(u2, ticket))
        self.assertEqual(UserTicketFollow.objects.filter(ticket=ticket).count(), 2)


class DashboardFollowListTestCase(TestCase):
    fixtures = ["emailtemplate.json"]

    def setUp(self):
        self.queue = Queue.objects.create(
            title="Q", slug="q", allow_public_submission=True,
        )
        self.client = Client()
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = False

    def _make_staff(self, username="staff"):
        User = get_user_model()
        u = User.objects.create(
            username=username, is_staff=True, email=f"{username}@ex.com",
        )
        u.set_password("pw")
        u.save()
        return u

    def _login_staff(self, username="staff"):
        user = self._make_staff(username)
        self.client.login(username=username, password="pw")
        return user

    def _make_ticket(self, **kw):
        defaults = {"queue": self.queue, "title": "T", "description": "d"}
        defaults.update(kw)
        return Ticket.objects.create(**defaults)

    def test_dashboard_shows_followed_tickets(self):
        user = self._login_staff()
        ticket = self._make_ticket(title="Dashboard Followed")
        UserTicketFollow.objects.create(user=user, ticket=ticket)
        resp = self.client.get(reverse("helpdesk:dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Dashboard Followed")

    def test_dashboard_empty_followed_section(self):
        self._login_staff()
        resp = self.client.get(reverse("helpdesk:dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "not following any tickets")

    def test_dashboard_requires_staff(self):
        User = get_user_model()
        User.objects.create(username="nonstaff", is_staff=False).set_password("pw")
        u = User.objects.get(username="nonstaff")
        u.set_password("pw")
        u.save()
        self.client.login(username="nonstaff", password="pw")
        resp = self.client.get(reverse("helpdesk:dashboard"))
        self.assertIn(resp.status_code, (302, 403))

    def test_follow_list_pagination(self):
        user = self._login_staff()
        for i in range(30):
            t = self._make_ticket(title=f"PagTkt{i}")
            UserTicketFollow.objects.create(user=user, ticket=t)
        resp = self.client.get(reverse("helpdesk:dashboard") + "?ft_page=2")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "PagTkt")

    def test_follow_list_status_filter(self):
        user = self._login_staff()
        open_t = self._make_ticket(title="OpenFollow", status=Ticket.OPEN_STATUS)
        closed_t = self._make_ticket(title="ClosedFollow", status=Ticket.CLOSED_STATUS)
        UserTicketFollow.objects.create(user=user, ticket=open_t)
        UserTicketFollow.objects.create(user=user, ticket=closed_t)
        resp = self.client.get(
            reverse("helpdesk:dashboard") + f"?ft_status={Ticket.OPEN_STATUS}"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "OpenFollow")
        self.assertNotContains(resp, "ClosedFollow")

    def test_follow_list_sort_by_modified_desc(self):
        user = self._login_staff()
        old = self._make_ticket(title="OldTkt")
        new = self._make_ticket(title="NewTkt")
        UserTicketFollow.objects.create(user=user, ticket=old)
        UserTicketFollow.objects.create(user=user, ticket=new)
        from django.utils import timezone
        old.modified = timezone.now() - timezone.timedelta(days=1)
        old.save()
        resp = self.client.get(reverse("helpdesk:dashboard") + "?ft_sort=-modified")
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode()
        self.assertLess(body.find("NewTkt"), body.find("OldTkt"))

    def test_follow_list_excludes_assigned_to_self(self):
        user = self._login_staff()
        mine = self._make_ticket(title="Mine", assigned_to=user)
        other = self._make_ticket(title="Other")
        UserTicketFollow.objects.create(user=user, ticket=mine)
        UserTicketFollow.objects.create(user=user, ticket=other)
        resp = self.client.get(reverse("helpdesk:dashboard"))
        body = resp.content.decode()
        section = body.split("Tickets you are following")[1]
        self.assertNotIn("Mine", section)
        self.assertIn("Other", section)

    def test_dashboard_uses_ticket_follow_list_template(self):
        self._login_staff()
        resp = self.client.get(reverse("helpdesk:dashboard"))
        templates = [t.name for t in resp.templates]
        self.assertIn("helpdesk/include/ticket_follow_list.html", templates)

    def test_dashboard_context_has_followed_tickets(self):
        user = self._login_staff()
        ticket = self._make_ticket(title="CtxTkt")
        UserTicketFollow.objects.create(user=user, ticket=ticket)
        resp = self.client.get(reverse("helpdesk:dashboard"))
        self.assertIn("followed_tickets", resp.context)
        self.assertIn("status_choices", resp.context)
        self.assertIn("followed_tickets_sort", resp.context)
        self.assertIn("followed_tickets_status", resp.context)


class TicketViewFollowButtonTestCase(TestCase):
    fixtures = ["emailtemplate.json"]

    def setUp(self):
        self.queue = Queue.objects.create(
            title="Q", slug="q", allow_public_submission=True,
        )
        self.client = Client()
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = False

    def _make_staff(self, username="staff"):
        User = get_user_model()
        u = User.objects.create(
            username=username, is_staff=True, email=f"{username}@ex.com",
        )
        u.set_password("pw")
        u.save()
        return u

    def _login_staff(self, username="staff"):
        user = self._make_staff(username)
        self.client.login(username=username, password="pw")
        return user

    def _make_ticket(self, **kw):
        defaults = {"queue": self.queue, "title": "T", "description": "d"}
        defaults.update(kw)
        return Ticket.objects.create(**defaults)

    def test_view_ticket_shows_follow_button_when_not_following(self):
        self._login_staff()
        ticket = self._make_ticket()
        resp = self.client.get(reverse("helpdesk:view", args=[ticket.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Follow")
        self.assertNotContains(resp, "Following")

    def test_view_ticket_shows_following_button_when_following(self):
        user = self._login_staff()
        ticket = self._make_ticket()
        UserTicketFollow.objects.create(user=user, ticket=ticket)
        resp = self.client.get(reverse("helpdesk:view", args=[ticket.id]))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Following")

    def test_view_ticket_context_has_is_following(self):
        user = self._login_staff()
        ticket = self._make_ticket()
        resp = self.client.get(reverse("helpdesk:view", args=[ticket.id]))
        self.assertIn("is_following", resp.context)
        self.assertFalse(resp.context["is_following"])
        UserTicketFollow.objects.create(user=user, ticket=ticket)
        resp = self.client.get(reverse("helpdesk:view", args=[ticket.id]))
        self.assertTrue(resp.context["is_following"])

    def test_follow_button_form_posts_to_correct_url(self):
        self._login_staff()
        ticket = self._make_ticket()
        resp = self.client.get(reverse("helpdesk:view", args=[ticket.id]))
        follow_url = reverse("helpdesk:follow_ticket", kwargs={"ticket_id": ticket.id})
        self.assertContains(resp, f'action=\'{follow_url}\'')

    def test_follow_button_includes_csrf(self):
        self._login_staff()
        ticket = self._make_ticket()
        resp = self.client.get(reverse("helpdesk:view", args=[ticket.id]))
        self.assertContains(resp, "csrfmiddlewaretoken")
