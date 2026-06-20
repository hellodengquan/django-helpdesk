from django.contrib.auth import get_user_model
from django.db.models import Prefetch
from helpdesk.models import FollowUp, FollowUpAttachment, Ticket, TICKET_SENSITIVE_FIELDS
from helpdesk.serializers import (
    FollowUpAttachmentSerializer,
    FollowUpSerializer,
    TicketSerializer,
    UserSerializer,
    PublicTicketListingSerializer,
)
from rest_framework import viewsets
from rest_framework.mixins import CreateModelMixin
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.viewsets import GenericViewSet
from rest_framework.pagination import PageNumberPagination

from helpdesk import settings as helpdesk_settings


def _ticket_queryset_defer(queryset):
    return queryset.defer(*TICKET_SENSITIVE_FIELDS).prefetch_related(
        Prefetch(
            "followup_set",
            queryset=FollowUp.objects.prefetch_related(
                Prefetch(
                    "followupattachment_set",
                    queryset=FollowUpAttachment.objects.all()
                )
            ),
        ),
        Prefetch(
            "followup_set__ticket",
            queryset=Ticket.objects.defer(*TICKET_SENSITIVE_FIELDS),
        ),
    )


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
        tickets = _ticket_queryset_defer(
            Ticket.objects.filter(
                submitter_email=self.request.user.email
            ).order_by("-created")
        )
        for ticket in tickets:
            ticket.set_custom_field_values()
        return tickets


class TicketViewSet(viewsets.ModelViewSet):
    """
    A viewset that provides the standard actions to handle Ticket

    You can filter the tickets by status using the `status` query parameter. For example:

    `/api/tickets/?status=Open,Resolved` will return all the tickets that are Open or Resolved.
    """

    queryset = Ticket.objects.all().defer(*TICKET_SENSITIVE_FIELDS)
    serializer_class = TicketSerializer
    pagination_class = ConservativePagination
    permission_classes = [IsAdminUser]

    def get_queryset(self):
        tickets = Ticket.objects.all().defer(*TICKET_SENSITIVE_FIELDS)

        # filter by status
        status = self.request.query_params.get("status", None)
        if status:
            statuses = status.split(",") if status else []
            status_choices = helpdesk_settings.TICKET_STATUS_CHOICES
            number_statuses = []
            for status in statuses:
                for choice in status_choices:
                    if str(choice[0]) == status:
                        number_statuses.append(choice[0])
            if number_statuses:
                tickets = tickets.filter(status__in=number_statuses)

        tickets = _ticket_queryset_defer(tickets)

        for ticket in tickets:
            ticket.set_custom_field_values()
        return tickets

    def get_object(self):
        qs = _ticket_queryset_defer(self.filter_queryset(self.get_queryset()))
        obj = qs.get(pk=self.kwargs["pk"])
        obj.set_custom_field_values()
        self.check_object_permissions(self.request, obj)
        return obj


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
