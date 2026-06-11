"""
Email routing engine for django-helpdesk.

Evaluates EmailRoutingRule entries against incoming email attributes,
determining the queue, priority, and owner assignment.

Rules are evaluated in ascending ``order`` (lower number = higher priority).
The first matching rule wins. If no rule matches, a BypassTicketException is
raised so the caller can leave the email untouched in the mailbox.

Structured logging is emitted for every bypass event (rule-bypass or
no-match-bypass) with ``event="email_routing_bypass"`` so that operators
can configure alerts via log aggregation systems.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional, Tuple

from helpdesk.exceptions import BypassTicketException
from helpdesk.models import EmailRoutingRule, Queue

logger = logging.getLogger("helpdesk.routing")


def _diag_check_rule(
    rule: EmailRoutingRule,
    sender_email: str,
    subject: str,
    body: str,
    queue: Optional[Queue],
) -> Tuple[bool, List[str]]:
    """
    Evaluate a single rule against the given email attributes and return
    ``(matched, reasons)`` where ``reasons`` is a human-readable list of
    which conditions passed / failed.  Used to build structured diagnostic
    logs that help operators understand coverage gaps.
    """
    reasons: List[str] = []

    if not rule.enabled:
        reasons.append("rule_disabled")
        return False, reasons

    if rule.pk and queue is not None:
        scoped_queues = rule.queues.all()
        if scoped_queues.exists():
            if scoped_queues.filter(pk=queue.pk).exists():
                reasons.append("queue_scope:ok")
            else:
                scoped_slugs = list(scoped_queues.values_list("slug", flat=True))
                reasons.append(f"queue_scope:rejected({scoped_slugs})")
                return False, reasons

    if rule.sender_email:
        if EmailRoutingRule._match_pattern(
            rule.sender_email, sender_email, rule.sender_match_type
        ):
            reasons.append(f"sender:ok({rule.sender_email})")
        else:
            reasons.append(
                f"sender:no_match({rule.sender_email}|{rule.sender_match_type})"
            )
            return False, reasons
    else:
        reasons.append("sender:any")

    if rule.subject:
        if EmailRoutingRule._match_pattern(
            rule.subject, subject, rule.subject_match_type
        ):
            reasons.append(f"subject:ok({rule.subject})")
        else:
            reasons.append(
                f"subject:no_match({rule.subject}|{rule.subject_match_type})"
            )
            return False, reasons
    else:
        reasons.append("subject:any")

    keywords = rule._get_keyword_list()
    if keywords:
        matches = [
            EmailRoutingRule._match_pattern(
                kw, body or "", rule.keywords_match_type
            )
            for kw in keywords
        ]
        matched_kws = [kw for kw, ok in zip(keywords, matches) if ok]
        if rule.keywords_logic == "all":
            if all(matches):
                reasons.append(f"keywords:all_ok({matched_kws})")
            else:
                missing = [kw for kw, ok in zip(keywords, matches) if not ok]
                reasons.append(f"keywords:missing({missing})")
                return False, reasons
        else:
            if any(matches):
                reasons.append(f"keywords:any_ok({matched_kws})")
            else:
                reasons.append(f"keywords:none_matched({keywords})")
                return False, reasons
    else:
        reasons.append("keywords:any")

    reasons.append("result:matched")
    return True, reasons


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


def collect_routing_diagnostics(
    sender_email: str,
    subject: str,
    body: str,
    queue: Optional[Queue] = None,
) -> Tuple[Optional[EmailRoutingRule], int, List[Dict]]:
    """
    Walk **all** rules (enabled and disabled) with per-rule diagnostics.
    Disabled rules are included with ``"rule_disabled"`` reason so that
    operators can see the full evaluation trail.

    :return: ``(matched_rule, total_rules_evaluated, rule_diags)``
             where ``rule_diags`` is a list of dicts like:
             ``{"rule": rule.name, "order": rule.order, "matched": bool,
                "reasons": [...]}``
    """
    rule_diags: List[Dict] = []
    matched_rule: Optional[EmailRoutingRule] = None
    total = 0

    rules = EmailRoutingRule.objects.order_by("order", "id").iterator()
    for rule in rules:
        total += 1
        matched, reasons = _diag_check_rule(
            rule, sender_email, subject, body, queue
        )
        rule_diags.append(
            {
                "rule": rule.name,
                "rule_id": rule.pk,
                "order": rule.order,
                "matched": matched,
                "reasons": reasons,
            }
        )
        if matched and matched_rule is None:
            matched_rule = rule
            break

    return matched_rule, total, rule_diags


def _emit_bypass_log(
    level: int,
    bypass_type: str,
    sender_email: str,
    subject: str,
    queue: Optional[Queue],
    rule: Optional[EmailRoutingRule],
    total_rules: int,
    rule_diags: List[Dict],
) -> None:
    """
    Emit a structured log record for a bypass event.  The log line is
    ``key=value`` formatted so that log aggregators (Splunk, ELK, Datadog,
    Loki, etc.) can parse it without JSON escaping trouble.
    """
    queue_slug = getattr(queue, "slug", "") or ""
    rule_name = rule.name if rule else ""
    rule_id = rule.pk if rule and rule.pk else ""
    truncated_subject = subject.replace("\n", " ").replace("\r", " ")
    if len(truncated_subject) > 200:
        truncated_subject = truncated_subject[:197] + "..."

    # Keep each rule-diagnostic line compact: only the first non-match reason
    compact_diags = []
    for d in rule_diags:
        first_reason = d["reasons"][0] if d["reasons"] else ""
        compact_diags.append(
            f"{d['rule']}#order={d['order']}:{'OK' if d['matched'] else 'NO'}:{first_reason}"
        )

    msg = (
        "event=email_routing_bypass "
        f"bypass_type={bypass_type} "
        f"sender={sender_email} "
        f"subject={truncated_subject!r} "
        f"queue={queue_slug} "
        f"matched_rule={rule_name!r} "
        f"matched_rule_id={rule_id} "
        f"total_rules_evaluated={total_rules} "
        f"rule_diagnostics={compact_diags!r}"
    )
    logger.log(level, msg)


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

    Structured diagnostic logs (with ``event=email_routing_bypass``) are
    emitted in every bypass path so operators can set up real-time alerts.

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
    matched_rule, total_rules, rule_diags = collect_routing_diagnostics(
        sender_email, subject, body, queue=queue
    )

    if matched_rule is None:
        if bypass_on_no_match:
            _emit_bypass_log(
                level=logging.WARNING,
                bypass_type="no_matching_rule",
                sender_email=sender_email,
                subject=subject,
                queue=queue,
                rule=None,
                total_rules=total_rules,
                rule_diags=rule_diags,
            )
            raise BypassTicketException(
                reason="No matching routing rule",
                sender_email=sender_email,
                subject=subject,
            )
        logger.debug(
            "No routing rule matched for sender=%s subject=%r; using default payload. "
            "evaluated_rules=%d",
            sender_email,
            subject,
            total_rules,
        )
        return payload, None

    if matched_rule.bypass:
        _emit_bypass_log(
            level=logging.WARNING,
            bypass_type="rule_explicit_bypass",
            sender_email=sender_email,
            subject=subject,
            queue=queue,
            rule=matched_rule,
            total_rules=total_rules,
            rule_diags=rule_diags,
        )
        raise BypassTicketException(
            reason=f"Matched bypass rule: {matched_rule.name}",
            rule=matched_rule,
            sender_email=sender_email,
            subject=subject,
        )

    updated_payload = matched_rule.apply_to_payload(payload)
    logger.info(
        "Applied routing rule '%s': queue=%s priority=%s owner=%s "
        "(evaluated_rules=%d)",
        matched_rule.name,
        updated_payload.get("queue"),
        updated_payload.get("priority"),
        updated_payload.get("assigned_to"),
        total_rules,
    )
    return updated_payload, matched_rule
