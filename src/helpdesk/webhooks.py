import requests
import requests.exceptions
import logging
from django.dispatch import receiver
from django.db.models.signals import post_save, post_delete, pre_save, pre_delete

from . import settings
from .signals import new_ticket_done, update_ticket_done
from .models import Ticket, FollowUp
from .query import invalidate_search_cache_for_ticket, invalidate_search_cache

logger = logging.getLogger(__name__)


def notify_followup_webhooks(followup):
    urls = settings.HELPDESK_GET_FOLLOWUP_WEBHOOK_URLS()
    if not urls:
        return

    # Serialize the ticket associated with the followup
    from .serializers import TicketSerializer

    ticket = followup.ticket
    ticket.set_custom_field_values()
    serialized_ticket = TicketSerializer(ticket).data

    # Prepare the data to send
    data = {
        "ticket": serialized_ticket,
        "queue_slug": ticket.queue.slug,
        "followup_id": followup.id,
    }

    for url in urls:
        try:
            requests.post(url, json=data, timeout=settings.HELPDESK_WEBHOOK_TIMEOUT)
        except requests.exceptions.Timeout:
            logger.error("Timeout while sending followup webhook to %s", url)


# listener is loaded via app.py HelpdeskConfig.ready()
@receiver(update_ticket_done)
def notify_followup_webhooks_receiver(sender, followup, **kwargs):
    notify_followup_webhooks(followup)


def send_new_ticket_webhook(ticket):
    urls = settings.HELPDESK_GET_NEW_TICKET_WEBHOOK_URLS()
    if not urls:
        return
    # Serialize the ticket
    from .serializers import TicketSerializer

    ticket.set_custom_field_values()
    serialized_ticket = TicketSerializer(ticket).data

    # Prepare the data to send
    data = {"ticket": serialized_ticket, "queue_slug": ticket.queue.slug}

    for url in urls:
        try:
            requests.post(url, json=data, timeout=settings.HELPDESK_WEBHOOK_TIMEOUT)
        except requests.exceptions.Timeout:
            logger.error("Timeout while sending new ticket webhook to %s", url)


# listener is loaded via app.py HelpdeskConfig.ready()
@receiver(new_ticket_done)
def send_new_ticket_webhook_receiver(sender, ticket, **kwargs):
    send_new_ticket_webhook(ticket)


@receiver(new_ticket_done)
def invalidate_search_cache_on_new_ticket(sender, ticket, **kwargs):
    invalidate_search_cache_for_ticket(ticket.id)


@receiver(update_ticket_done)
def invalidate_search_cache_on_update_ticket(sender, followup, **kwargs):
    ticket_id = followup.ticket_id if hasattr(followup, "ticket_id") else None
    invalidate_search_cache_for_ticket(ticket_id)


@receiver(post_save, sender=Ticket)
def invalidate_search_cache_on_ticket_save(sender, instance, created, **kwargs):
    invalidate_search_cache_for_ticket(instance.id)


@receiver(post_delete, sender=Ticket)
def invalidate_search_cache_on_ticket_delete(sender, instance, **kwargs):
    invalidate_search_cache()


@receiver(pre_delete, sender=Ticket)
def invalidate_search_cache_on_ticket_pre_delete(sender, instance, **kwargs):
    invalidate_search_cache_for_ticket(instance.id)
    invalidate_search_cache()


@receiver(pre_save, sender=Ticket)
def invalidate_search_cache_on_ticket_soft_delete(sender, instance, **kwargs):
    if instance.pk is None:
        return
    try:
        old_instance = Ticket.objects.get(pk=instance.pk)
    except Ticket.DoesNotExist:
        return
    status_changed = old_instance.status != instance.status
    merged_to_changed = (old_instance.merged_to_id != instance.merged_to_id)
    duplicate_status = instance.DUPLICATE_STATUS
    became_duplicate = (
        status_changed and instance.status == duplicate_status
    ) or merged_to_changed
    if became_duplicate or merged_to_changed:
        invalidate_search_cache_for_ticket(instance.id)
        invalidate_search_cache()


@receiver(post_save, sender=FollowUp)
def invalidate_search_cache_on_followup_save(sender, instance, created, **kwargs):
    ticket_id = instance.ticket_id if hasattr(instance, "ticket_id") else None
    invalidate_search_cache_for_ticket(ticket_id)


@receiver(post_delete, sender=FollowUp)
def invalidate_search_cache_on_followup_delete(sender, instance, **kwargs):
    ticket_id = instance.ticket_id if hasattr(instance, "ticket_id") else None
    invalidate_search_cache_for_ticket(ticket_id)
