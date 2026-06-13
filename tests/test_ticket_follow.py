from django.contrib.auth import get_user_model
from django.test import TestCase
from django.test.client import Client
from django.urls import reverse
from helpdesk.models import Queue, Ticket, UserTicketFollow
from helpdesk import settings as helpdesk_settings


class UserTicketFollowTestCase(TestCase):
    fixtures = ["emailtemplate.json"]

    def setUp(self):
        self.queue_public = Queue.objects.create(
            title="Queue 1",
            slug="q1",
            allow_public_submission=True,
            new_ticket_cc="new.public@example.com",
            updated_ticket_cc="update.public@example.com",
        )

        self.client = Client()
        helpdesk_settings.HELPDESK_ENABLE_PER_QUEUE_STAFF_PERMISSION = False

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

    def _login_staff_user(self, username="staff_user"):
        user = self._create_staff_user(username)
        self.client.login(username=username, password="pass")
        return user

    def _create_ticket(self, title="Test Ticket", status=None, assigned_to=None):
        ticket_data = {
            "queue": self.queue_public,
            "title": title,
            "description": "Test ticket description",
        }
        if status is not None:
            ticket_data["status"] = status
        if assigned_to is not None:
            ticket_data["assigned_to"] = assigned_to
        return Ticket.objects.create(**ticket_data)

    def test_follow_ticket_success(self):
        user = self._login_staff_user()
        ticket = self._create_ticket()

        self.assertFalse(UserTicketFollow.is_following(user, ticket))

        response = self.client.post(
            reverse("helpdesk:follow_ticket", kwargs={"ticket_id": ticket.id}),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(UserTicketFollow.is_following(user, ticket))
        follow_obj = UserTicketFollow.objects.get(user=user, ticket=ticket)
        self.assertIsNotNone(follow_obj.created)

    def test_unfollow_ticket_success(self):
        user = self._login_staff_user()
        ticket = self._create_ticket()

        UserTicketFollow.objects.create(user=user, ticket=ticket)
        self.assertTrue(UserTicketFollow.is_following(user, ticket))

        response = self.client.post(
            reverse("helpdesk:follow_ticket", kwargs={"ticket_id": ticket.id}),
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(UserTicketFollow.is_following(user, ticket))

    def test_follow_toggle_returns_correct_state(self):
        user = self._login_staff_user()
        ticket = self._create_ticket()

        follow_obj, is_following = UserTicketFollow.toggle_follow(user, ticket)
        self.assertTrue(is_following)
        self.assertIsNotNone(follow_obj)

        follow_obj, is_following = UserTicketFollow.toggle_follow(user, ticket)
        self.assertFalse(is_following)
        self.assertIsNone(follow_obj)

    def test_follow_ticket_requires_staff(self):
        User = get_user_model()
        user = User.objects.create(
            username="non_staff",
            is_staff=False,
        )
        user.set_password("pass")
        user.save()
        self.client.login(username="non_staff", password="pass")

        ticket = self._create_ticket()

        response = self.client.post(
            reverse("helpdesk:follow_ticket", kwargs={"ticket_id": ticket.id}),
        )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(UserTicketFollow.is_following(user, ticket))

    def test_follow_nonexistent_ticket_returns_404(self):
        self._login_staff_user()

        response = self.client.post(
            reverse("helpdesk:follow_ticket", kwargs={"ticket_id": 99999}),
        )

        self.assertEqual(response.status_code, 404)

    def test_follow_list_appears_on_dashboard(self):
        user = self._login_staff_user()
        ticket = self._create_ticket(title="Followed Ticket 1")
        UserTicketFollow.objects.create(user=user, ticket=ticket)

        response = self.client.get(reverse("helpdesk:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Tickets you are following")
        self.assertContains(response, "Followed Ticket 1")

    def test_follow_list_pagination(self):
        user = self._login_staff_user()
        for i in range(30):
            ticket = self._create_ticket(title=f"Paginated Followed Ticket {i}")
            UserTicketFollow.objects.create(user=user, ticket=ticket)

        response = self.client.get(reverse("helpdesk:dashboard") + "?ft_page=2")

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "ft_page=1")
        self.assertContains(response, "Paginated Followed Ticket")

    def test_follow_list_status_filter(self):
        user = self._login_staff_user()

        open_ticket = self._create_ticket(
            title="Open Followed Ticket", status=Ticket.OPEN_STATUS
        )
        closed_ticket = self._create_ticket(
            title="Closed Followed Ticket", status=Ticket.CLOSED_STATUS
        )
        UserTicketFollow.objects.create(user=user, ticket=open_ticket)
        UserTicketFollow.objects.create(user=user, ticket=closed_ticket)

        response = self.client.get(
            reverse("helpdesk:dashboard") + f"?ft_status={Ticket.OPEN_STATUS}"
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Open Followed Ticket")
        self.assertNotContains(response, "Closed Followed Ticket")

    def test_follow_list_sort_by_modified(self):
        user = self._login_staff_user()

        old_ticket = self._create_ticket(title="Old Ticket")
        new_ticket = self._create_ticket(title="New Ticket")
        UserTicketFollow.objects.create(user=user, ticket=old_ticket)
        UserTicketFollow.objects.create(user=user, ticket=new_ticket)

        from django.utils import timezone
        old_ticket.modified = timezone.now() - timezone.timedelta(days=1)
        old_ticket.save()

        response = self.client.get(reverse("helpdesk:dashboard") + "?ft_sort=-modified")

        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        new_pos = content.find("New Ticket")
        old_pos = content.find("Old Ticket")
        self.assertGreater(old_pos, 0)
        self.assertGreater(new_pos, 0)
        self.assertLess(new_pos, old_pos)

    def test_follow_list_excludes_assigned_tickets(self):
        user = self._login_staff_user()

        assigned_ticket = self._create_ticket(
            title="My Assigned Ticket", assigned_to=user
        )
        followed_ticket = self._create_ticket(title="My Followed Ticket")
        UserTicketFollow.objects.create(user=user, ticket=assigned_ticket)
        UserTicketFollow.objects.create(user=user, ticket=followed_ticket)

        response = self.client.get(reverse("helpdesk:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "My Followed Ticket")
        followed_section = response.content.decode().split("Tickets you are following")[1]
        self.assertNotIn("My Assigned Ticket", followed_section)

    def test_follow_unique_constraint(self):
        user = self._create_staff_user()
        ticket = self._create_ticket()

        UserTicketFollow.objects.create(user=user, ticket=ticket)

        from django.db import IntegrityError

        with self.assertRaises(IntegrityError):
            UserTicketFollow.objects.create(user=user, ticket=ticket)

    def test_is_following_returns_false_for_anonymous(self):
        ticket = self._create_ticket()
        User = get_user_model()

        class AnonymousUser:
            is_authenticated = False

        self.assertFalse(UserTicketFollow.is_following(AnonymousUser(), ticket))
