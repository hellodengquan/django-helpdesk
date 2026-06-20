from django.contrib.auth import get_user_model
from helpdesk.models import FollowUp, FollowUpAttachment, Ticket
from helpdesk.query import TicketQueryBuilder, build_query_params_from_request
from helpdesk.serializers import (
    FollowUpAttachmentSerializer,
    FollowUpSerializer,
    TicketSerializer,
    UserSerializer,
    PublicTicketListingSerializer,
)
from helpdesk.user import HelpdeskUser
from rest_framework import viewsets
from rest_framework.mixins import CreateModelMixin
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.viewsets import GenericViewSet
from rest_framework.pagination import PageNumberPagination

from helpdesk import settings as helpdesk_settings


class ConservativePagination(PageNumberPagination):
    page_size = 25
    page_size_query_param = "page_size"


class UserTicketViewSet(viewsets.ReadOnlyModelViewSet):
    """
    A list of all the tickets submitted by the current user

    The view is paginated by default
    """

    serializer_class = PublicTicketListingSerializer
    pagination_class = ConservativePagination
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        tickets = Ticket.objects.filter(
            submitter_email=self.request.user.email
        ).order_by("-created")
        for ticket in tickets:
            ticket.set_custom_field_values()
        return tickets


class TicketViewSet(viewsets.ModelViewSet):
    """
    A viewset that provides the standard actions to handle Ticket

    You can filter the tickets using the same query parameters as the list view:
    - `queue`, `assigned_to`, `status`, `priority`, `kbitem` (can be multiple, use `-1` for NULL)
    - `date_from`, `date_to` (created date range)
    - `q` (free-text search)
    - `sort`, `sortreverse` (sorting)
    - `saved_query` (a previously saved search ID)

    For example: `/api/tickets/?status=1&status=2&priority=1` will return
    all Open/Reopened tickets with priority 1 (Highest).
    """

    queryset = Ticket.objects.all()
    serializer_class = TicketSerializer
    pagination_class = ConservativePagination
    permission_classes = [IsAdminUser]

    def get_queryset(self):
        huser = HelpdeskUser(self.request.user)
        query_params = build_query_params_from_request(self.request)
        builder = TicketQueryBuilder(huser, query_params=query_params)
        tickets = builder.get_queryset()

        for ticket in tickets:
            ticket.set_custom_field_values()
        return tickets

    def get_object(self):
        ticket = super().get_object()
        ticket.set_custom_field_values()
        return ticket


class FollowUpViewSet(viewsets.ModelViewSet):
    queryset = FollowUp.objects.all()
    serializer_class = FollowUpSerializer
    pagination_class = ConservativePagination
    permission_classes = [IsAdminUser]

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)


class FollowUpAttachmentViewSet(viewsets.ModelViewSet):
    queryset = FollowUpAttachment.objects.all()
    serializer_class = FollowUpAttachmentSerializer
    pagination_class = ConservativePagination
    permission_classes = [IsAdminUser]


class CreateUserView(CreateModelMixin, GenericViewSet):
    queryset = get_user_model().objects.all()
    serializer_class = UserSerializer
    permission_classes = [IsAdminUser]
