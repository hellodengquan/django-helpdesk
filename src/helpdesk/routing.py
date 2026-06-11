"""
Email routing engine for django-helpdesk.

Evaluates EmailRoutingRule entries against incoming email attributes,
determining the queue, priority, and owner assignment.

Rules are evaluated in ascending ``order`` (lower number = higher priority).
The first matching rule wins. If no rule matches, a BypassTicketException is
raised so the caller can leave the email untouched in the mailbox.
"""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from helpdesk.exceptions import BypassTicketException
from helpdesk.models import EmailRoutingRule, Queue

logger = logging.getLogger("helpdesk.routing")


def find_matching_rule(
    sender_email: str,
    subject: str,
    body: str,
    queue: Optional[Queue] = None,
) -> Optional[EmailRoutingRule]:
    """
    Iterate all enabled routing rules in priority order and return the first
    one that matches the given email attributes.

    :param sender_email: The parsed sender email address.
    :param subject: The decoded/cleaned email subject.
    :param body: The extracted plain-text email body.
    :param queue: The Queue currently being processed (used for queue-scoped rules).
    :return: The first matching EmailRoutingRule or ``None``.
    """
    rules = EmailRoutingRule.objects.filter(enabled=True).order_by("order", "id")
    for rule in rules.iterator():
        if rule.matches(sender_email, subject, body, queue=queue):
            logger.debug(
                "Routing rule '%s' (order=%d) matched sender=%s subject=%r",
                rule.name,
                rule.order,
                sender_email,
                subject,
            )
            return rule
    return None


def apply_routing(
    payload: dict,
    sender_email: str,
    subject: str,
    body: str,
    queue: Optional[Queue] = None,
    bypass_on_no_match: bool = True,
) -> Tuple[dict, Optional[EmailRoutingRule]]:
    """
    Apply the highest-priority matching routing rule to ``payload``.

    If a rule matches and its ``bypass`` flag is set, ``BypassTicketException``
    is raised with the rule attached so callers can keep the email in the
    mailbox.

    If no rule matches and ``bypass_on_no_match`` is ``True`` (the default),
    ``BypassTicketException`` is raised indicating the email should be left
    for manual/sidetrack processing.

    :param payload: Dict with at least ``queue`` and ``priority`` keys; will
                    receive ``assigned_to`` and/or updated values from the
                    matched rule.
    :param sender_email: Sender email address.
    :param subject: Email subject line.
    :param body: Email plain-text body.
    :param queue: Queue being processed (for scope filtering).
    :param bypass_on_no_match: Whether to raise on zero rule matches.
    :return: A tuple of ``(updated_payload, matched_rule_or_None)``.
    :raises BypassTicketException: When the rule explicitly asks to bypass or
                                   when no rule matches (and ``bypass_on_no_match``).
    """
    rule = find_matching_rule(sender_email, subject, body, queue=queue)

    if rule is None:
        if bypass_on_no_match:
            logger.info(
                "No routing rule matched for sender=%s subject=%r; bypassing.",
                sender_email,
                subject,
            )
            raise BypassTicketException(reason="No matching routing rule")
        logger.debug(
            "No routing rule matched for sender=%s subject=%r; using default payload.",
            sender_email,
            subject,
        )
        return payload, None

    if rule.bypass:
        logger.info(
            "Routing rule '%s' matched with bypass flag; skipping ticket creation.",
            rule.name,
        )
        raise BypassTicketException(
            reason=f"Matched bypass rule: {rule.name}", rule=rule
        )

    updated_payload = rule.apply_to_payload(payload)
    logger.info(
        "Applied routing rule '%s': queue=%s priority=%s owner=%s",
        rule.name,
        updated_payload.get("queue"),
        updated_payload.get("priority"),
        updated_payload.get("assigned_to"),
    )
    return updated_payload, rule
