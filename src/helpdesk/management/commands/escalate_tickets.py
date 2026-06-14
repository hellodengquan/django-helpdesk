#!/usr/bin/python
"""
django-helpdesk - A Django powered ticket tracker for small enterprise.

(c) Copyright 2008 Jutda. All Rights Reserved. See LICENSE for details.

scripts/escalate_tickets.py - Easy way to escalate tickets based on their age,
                              designed to be run from Cron or similar.
"""

from datetime import date, timedelta
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _
from helpdesk.lib import safe_template_context
from helpdesk.models import EscalationExclusion, Queue, Ticket


class Command(BaseCommand):
    def add_arguments(self, parser):
        parser.add_argument(
            "-q",
            "--queues",
            nargs="*",
            choices=list(Queue.objects.values_list("slug", flat=True)),
            help="Queues to include (default: all). Enter the queues slug as space separated list.",
        )
        parser.add_argument(
            "-x",
            "--escalate-verbosely",
            action="store_true",
            default=False,
            help="Display escalated tickets",
        )
        parser.add_argument(
            "-n",
            "--notify-only",
            action="store_true",
            default=False,
            help="Send email reminder but dont escalate tickets",
        )

    def _get_escalation_deadline(self, ticket, escalation_days):
        """
        Calculate the effective escalation deadline for a ticket,
        taking into account total paused time.
        """
        base_time = ticket.last_escalation or ticket.created
        if ticket.total_paused_time:
            effective_base = base_time + ticket.total_paused_time
        else:
            effective_base = base_time
        return effective_base + timedelta(days=escalation_days)

    def _ticket_needs_escalation(self, ticket, escalation_days):
        """
        Check if a ticket needs to be escalated, accounting for paused time.
        Returns (needs_escalation, reason) tuple.
        """
        if ticket.on_hold:
            return False, "ticket is on hold"

        if (
            Ticket.ESCALATION_EXCLUDE_STATUSES
            and ticket.status in Ticket.ESCALATION_EXCLUDE_STATUSES
        ):
            return False, "ticket status excluded from escalation"

        if ticket.priority <= 1:
            return False, "priority already at maximum"

        deadline = self._get_escalation_deadline(ticket, escalation_days)
        if deadline > timezone.now():
            return False, "escalation deadline not yet reached"

        return True, None

    def handle(self, *args, **options):
        verbose = options["escalate_verbosely"]
        notify_only = options["notify_only"]

        queue_slugs = options["queues"]
        queues = Queue.objects.filter(escalate_days__isnull=False).exclude(
            escalate_days=0
        )
        if queue_slugs is not None:
            queues = queues.filter(slug__in=queue_slugs)

        if verbose:
            self.stdout.write(f"Processing: {queues}")

        for queue in queues:
            last = date.today() - timedelta(days=queue.escalate_days)
            today = date.today()
            workdate = last

            days = 0

            while workdate < today:
                if not EscalationExclusion.objects.filter(date=workdate).exists():
                    days += 1
                workdate = workdate + timedelta(days=1)

            req_last_escl_date = timezone.now() - timedelta(days=days)

            query = (
                queue.ticket_set.filter(status__in=Ticket.OPEN_STATUSES)
                .exclude(priority=1)
                .exclude(on_hold=True)
            )

            if Ticket.ESCALATION_EXCLUDE_STATUSES:
                query = query.exclude(status__in=Ticket.ESCALATION_EXCLUDE_STATUSES)

            query = query.filter(
                Q(last_escalation__lte=req_last_escl_date)
                | Q(last_escalation__isnull=True, created__lte=req_last_escl_date)
            )

            for ticket in query.select_for_update(skip_locked=True):
                with transaction.atomic():
                    ticket.refresh_from_db()

                    needs_escalation, skip_reason = self._ticket_needs_escalation(
                        ticket, days
                    )
                    if not needs_escalation:
                        if verbose and skip_reason:
                            self.stdout.write(
                                f"  - Skipping {ticket.ticket}: {skip_reason}"
                            )
                        continue

                    old_priority = ticket.priority
                    old_last_escalation = ticket.last_escalation
                    new_last_escalation = timezone.now()
                    new_priority = ticket.priority - 1

                    recheck = Ticket.objects.filter(
                        id=ticket.id, last_escalation=old_last_escalation
                    ).update(
                        last_escalation=new_last_escalation,
                        priority=new_priority,
                        modified=new_last_escalation,
                    )
                    if recheck == 0:
                        if verbose:
                            self.stdout.write(
                                f"  - Skipping {ticket.ticket}: already escalated by another worker"
                            )
                        continue

                    ticket.last_escalation = new_last_escalation
                    ticket.priority = new_priority

                    context = safe_template_context(ticket)

                    ticket.send(
                        {
                            "submitter": ("escalated_submitter", context),
                            "ticket_cc": ("escalated_cc", context),
                            "assigned_to": ("escalated_owner", context),
                        },
                        fail_silently=True,
                    )

                    if verbose:
                        self.stdout.write(
                            f"  - Escalating {ticket.ticket} from {old_priority}>{ticket.priority}"
                        )

                    if not notify_only:
                        followup = ticket.followup_set.create(
                            title=_("Ticket Escalated"),
                            public=True,
                            comment=_("Ticket escalated after %(nb)s days")
                            % {"nb": queue.escalate_days},
                        )

                        followup.ticketchange_set.create(
                            field=_("Priority"),
                            old_value=old_priority,
                            new_value=ticket.priority,
                        )
