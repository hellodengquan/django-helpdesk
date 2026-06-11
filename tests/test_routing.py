# -*- coding: utf-8 -*-
"""
Unit tests for the email routing engine (EmailRoutingRule model,
apply_routing, find_matching_rule, collect_routing_diagnostics
and bypass logging).

These tests lock in the observability behaviour so that future
refactoring of rule fields or log templates does not silently
break monitoring dashboards / alerting rules.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from helpdesk.exceptions import BypassTicketException
from helpdesk.models import (
    EmailRoutingRule,
    KEYWORDS_LOGIC_ALL,
    KEYWORDS_LOGIC_ANY,
    MATCH_TYPE_CONTAINS,
    MATCH_TYPE_EXACT,
    MATCH_TYPE_REGEX,
    MATCH_TYPE_WILDCARD,
    Queue,
    Ticket,
)
from helpdesk.routing import (
    _diag_check_rule,
    _emit_bypass_log,
    apply_routing,
    collect_routing_diagnostics,
    find_matching_rule,
)
import logging
import re
from typing import List, Optional, Tuple

User = get_user_model()


def _grab_routing_logs(caplog) -> List[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "helpdesk.routing"]


def _find_log_by_event(records, event: str) -> Optional[logging.LogRecord]:
    for r in records:
        if f"event={event}" in r.getMessage():
            return r
    return None


class RoutingPatternMatchingTests(TestCase):
    """Test the low-level pattern matching helper on EmailRoutingRule."""

    def test_wildcard_match(self):
        rule = EmailRoutingRule(name="t", order=1)
        self.assertTrue(
            rule._match_pattern("*@example.com", "user@example.com", MATCH_TYPE_WILDCARD)
        )
        self.assertFalse(
            rule._match_pattern("*@example.com", "user@other.com", MATCH_TYPE_WILDCARD)
        )
        self.assertTrue(
            rule._match_pattern("alerts-*", "alerts-prod", MATCH_TYPE_WILDCARD)
        )

    def test_contains_match_case_insensitive(self):
        rule = EmailRoutingRule(name="t", order=1)
        self.assertTrue(
            rule._match_pattern("urgent", "Very Urgent request", MATCH_TYPE_CONTAINS)
        )
        self.assertFalse(
            rule._match_pattern("urgent", "Normal request", MATCH_TYPE_CONTAINS)
        )

    def test_exact_match(self):
        rule = EmailRoutingRule(name="t", order=1)
        self.assertTrue(
            rule._match_pattern("alerts@company.com", "alerts@company.com", MATCH_TYPE_EXACT)
        )
        self.assertFalse(
            rule._match_pattern("alerts@company.com", "ALERTS@company.com", MATCH_TYPE_EXACT)
        )

    def test_regex_match(self):
        rule = EmailRoutingRule(name="t", order=1)
        self.assertTrue(
            rule._match_pattern(r"^PROD-\d+.*", "PROD-1234 is down", MATCH_TYPE_REGEX)
        )
        self.assertFalse(
            rule._match_pattern(r"^PROD-\d+.*", "STAGING-123 is down", MATCH_TYPE_REGEX)
        )

    def test_regex_invalid_silently_returns_false(self):
        rule = EmailRoutingRule(name="t", order=1)
        self.assertFalse(
            rule._match_pattern(r"[unclosed", "PROD-1234", MATCH_TYPE_REGEX)
        )

    def test_blank_pattern_matches_anything(self):
        rule = EmailRoutingRule(name="t", order=1)
        self.assertTrue(rule._match_pattern("", "anything", MATCH_TYPE_EXACT))
        self.assertTrue(rule._match_pattern("", "", MATCH_TYPE_EXACT))

    def test_none_text_returns_false(self):
        rule = EmailRoutingRule(name="t", order=1)
        self.assertFalse(rule._match_pattern("pattern", None, MATCH_TYPE_CONTAINS))


class RoutingKeywordLogicTests(TestCase):
    """Test keyword list parsing and 'all' vs 'any' logic."""

    def test_keyword_parsing_handles_multiple_separators(self):
        rule = EmailRoutingRule(name="t", order=1)
        rule.keywords = "error, bug\ncrash;failure   , timeout"
        self.assertEqual(
            rule._get_keyword_list(),
            ["error", "bug", "crash", "failure", "timeout"],
        )

    def test_keyword_parsing_empty(self):
        rule = EmailRoutingRule(name="t", order=1)
        rule.keywords = ""
        self.assertEqual(rule._get_keyword_list(), [])

    def test_keywords_any_logic(self):
        rule = EmailRoutingRule(
            name="t",
            order=1,
            enabled=True,
            keywords="error, crash",
            keywords_logic=KEYWORDS_LOGIC_ANY,
            keywords_match_type=MATCH_TYPE_CONTAINS,
        )
        self.assertTrue(rule.matches("a@b.com", "sub", "An error occurred"))
        self.assertTrue(rule.matches("a@b.com", "sub", "System crash"))
        self.assertFalse(rule.matches("a@b.com", "sub", "Everything is fine"))

    def test_keywords_all_logic(self):
        rule = EmailRoutingRule(
            name="t",
            order=1,
            enabled=True,
            keywords="payment, invoice",
            keywords_logic=KEYWORDS_LOGIC_ALL,
            keywords_match_type=MATCH_TYPE_CONTAINS,
        )
        self.assertTrue(
            rule.matches("a@b.com", "sub", "Please pay this invoice, payment due")
        )
        self.assertFalse(rule.matches("a@b.com", "sub", "Here is your invoice"))
        self.assertFalse(rule.matches("a@b.com", "sub", "Thanks for payment"))


class RoutingMatchesTests(TestCase):
    """Test the combined matches() method with queue scope and disabled states."""

    @classmethod
    def setUpTestData(cls):
        cls.q1 = Queue.objects.create(title="Support", slug="support")
        cls.q2 = Queue.objects.create(title="Billing", slug="billing")

    def test_disabled_rule_never_matches(self):
        rule = EmailRoutingRule.objects.create(
            name="t", order=1, enabled=False, sender_email="*@x.com", sender_match_type=MATCH_TYPE_WILDCARD
        )
        self.assertFalse(rule.matches("ceo@x.com", "sub", "body"))

    def test_queue_scope_accepted(self):
        rule = EmailRoutingRule.objects.create(
            name="t", order=1, enabled=True, sender_email="*@x.com", sender_match_type=MATCH_TYPE_WILDCARD
        )
        rule.queues.add(self.q1)
        self.assertTrue(rule.matches("ceo@x.com", "s", "b", queue=self.q1))
        self.assertFalse(rule.matches("ceo@x.com", "s", "b", queue=self.q2))

    def test_no_queue_scope_matches_any_queue(self):
        rule = EmailRoutingRule.objects.create(
            name="t", order=1, enabled=True, sender_email="*@x.com", sender_match_type=MATCH_TYPE_WILDCARD
        )
        self.assertTrue(rule.matches("ceo@x.com", "s", "b", queue=self.q1))
        self.assertTrue(rule.matches("ceo@x.com", "s", "b", queue=self.q2))
        self.assertTrue(rule.matches("ceo@x.com", "s", "b", queue=None))

    def test_all_conditions_combined(self):
        rule = EmailRoutingRule.objects.create(
            name="VIP billing issue",
            order=1,
            enabled=True,
            sender_email="*@vip.com",
            sender_match_type=MATCH_TYPE_WILDCARD,
            subject="*billing*",
            subject_match_type=MATCH_TYPE_WILDCARD,
            keywords="payment, invoice",
            keywords_logic=KEYWORDS_LOGIC_ANY,
            keywords_match_type=MATCH_TYPE_CONTAINS,
        )
        self.assertTrue(
            rule.matches("ceo@vip.com", "billing question", "my invoice is wrong")
        )
        # wrong sender
        self.assertFalse(
            rule.matches("ceo@other.com", "billing question", "my invoice is wrong")
        )
        # wrong subject
        self.assertFalse(
            rule.matches("ceo@vip.com", "login issue", "my invoice is wrong")
        )
        # no keywords match
        self.assertFalse(
            rule.matches("ceo@vip.com", "billing question", "just saying hi")
        )


class RoutingRulePriorityAndConflictTests(TestCase):
    """Test that rules are evaluated in the correct order and first match wins."""

    @classmethod
    def setUpTestData(cls):
        cls.q = Queue.objects.create(title="Test", slug="test")
        cls.owner_1 = User.objects.create_user(username="owner1", password="p")
        cls.owner_2 = User.objects.create_user(username="owner2", password="p")

        # Conflict: both rules could match the same email, order decides
        cls.rule_general = EmailRoutingRule.objects.create(
            name="All VIP senders",
            order=10,
            enabled=True,
            sender_email="*@vip.com",
            sender_match_type=MATCH_TYPE_WILDCARD,
            target_priority=3,
        )
        cls.rule_specific = EmailRoutingRule.objects.create(
            name="CEO VIP billing",
            order=1,
            enabled=True,
            sender_email="ceo@vip.com",
            sender_match_type=MATCH_TYPE_EXACT,
            subject="*billing*",
            subject_match_type=MATCH_TYPE_WILDCARD,
            target_priority=1,
            target_owner=cls.owner_1,
        )
        # A rule that comes after but also matches; should never win
        cls.rule_later = EmailRoutingRule.objects.create(
            name="Later rule",
            order=50,
            enabled=True,
            sender_email="*@vip.com",
            sender_match_type=MATCH_TYPE_WILDCARD,
            target_owner=cls.owner_2,
        )

    def test_order_ascending_evaluation_first_wins(self):
        """ceo@vip.com + billing subject → should hit rule_specific (order=1), not rule_general (order=10)."""
        matched = find_matching_rule(
            sender_email="ceo@vip.com",
            subject="URGENT billing issue",
            body="Pay this invoice now",
        )
        self.assertIsNotNone(matched)
        self.assertEqual(matched.pk, self.rule_specific.pk)
        self.assertEqual(matched.order, 1)

    def test_fallback_to_next_matching_rule_in_order(self):
        """ceo@vip.com + non-billing subject → hits rule_general (order=10), not rule_later (order=50)."""
        matched = find_matching_rule(
            sender_email="ceo@vip.com",
            subject="Other topic",
            body="Hello",
        )
        self.assertIsNotNone(matched)
        self.assertEqual(matched.pk, self.rule_general.pk)

    def test_apply_routing_assigns_correct_values(self):
        payload = {"queue": self.q, "priority": 3}
        p, matched = apply_routing(
            payload,
            sender_email="ceo@vip.com",
            subject="URGENT billing issue",
            body="Pay this invoice now",
            queue=self.q,
            bypass_on_no_match=False,
        )
        self.assertIsNotNone(matched)
        self.assertEqual(matched.pk, self.rule_specific.pk)
        self.assertEqual(p["priority"], 1)
        self.assertEqual(p["assigned_to"], self.owner_1)

    def test_apply_routing_no_match_with_bypass_on_no_match_false(self):
        EmailRoutingRule.objects.all().delete()
        payload = {"queue": self.q, "priority": 3}
        p, matched = apply_routing(
            payload,
            sender_email="a@b.com",
            subject="Hello",
            body="Hi",
            bypass_on_no_match=False,
        )
        self.assertIsNone(matched)
        self.assertEqual(p["priority"], 3)

    def test_equal_order_uses_id_as_tiebreaker(self):
        r1 = EmailRoutingRule.objects.create(
            name="A", order=5, enabled=True, subject="*test*", subject_match_type=MATCH_TYPE_WILDCARD
        )
        r2 = EmailRoutingRule.objects.create(
            name="B", order=5, enabled=True, subject="*test*", subject_match_type=MATCH_TYPE_WILDCARD
        )
        matched = find_matching_rule("a@b.com", "test subj", "body")
        self.assertEqual(matched.pk, r1.pk)


class RoutingDiagnosticsTests(TestCase):
    """Test _diag_check_rule and collect_routing_diagnostics produce accurate reasons."""

    @classmethod
    def setUpTestData(cls):
        cls.q = Queue.objects.create(title="Test", slug="test")

    def test_diag_check_rule_disabled(self):
        rule = EmailRoutingRule(name="t", order=1, enabled=False)
        matched, reasons = _diag_check_rule(rule, "a@b", "s", "b", None)
        self.assertFalse(matched)
        self.assertIn("rule_disabled", reasons)

    def test_diag_check_rule_sender_failure(self):
        rule = EmailRoutingRule(
            name="t",
            order=1,
            enabled=True,
            sender_email="*@vip.com",
            sender_match_type=MATCH_TYPE_WILDCARD,
        )
        matched, reasons = _diag_check_rule(rule, "a@normal.com", "s", "b", None)
        self.assertFalse(matched)
        self.assertTrue(any("sender:no_match" in r for r in reasons))

    def test_diag_check_rule_full_match(self):
        rule = EmailRoutingRule(
            name="t",
            order=1,
            enabled=True,
            sender_email="*@x.com",
            sender_match_type=MATCH_TYPE_WILDCARD,
            subject="*urgent*",
            subject_match_type=MATCH_TYPE_WILDCARD,
            keywords="error, down",
            keywords_logic=KEYWORDS_LOGIC_ANY,
            keywords_match_type=MATCH_TYPE_CONTAINS,
        )
        matched, reasons = _diag_check_rule(rule, "a@x.com", "very urgent", "system is down", None)
        self.assertTrue(matched)
        self.assertIn("result:matched", reasons)
        self.assertTrue(any("sender:ok" in r for r in reasons))
        self.assertTrue(any("subject:ok" in r for r in reasons))
        self.assertTrue(any("keywords:any_ok" in r for r in reasons))

    def test_collect_routing_diagnostics_stops_at_first_match(self):
        r1 = EmailRoutingRule.objects.create(
            name="r1", order=1, enabled=True,
            sender_email="*@x.com", sender_match_type=MATCH_TYPE_WILDCARD,
        )
        r2 = EmailRoutingRule.objects.create(
            name="r2", order=2, enabled=True,
            sender_email="*@x.com", sender_match_type=MATCH_TYPE_WILDCARD,
        )
        matched, total, diags = collect_routing_diagnostics("a@x.com", "s", "b")
        self.assertIsNotNone(matched)
        self.assertEqual(matched.pk, r1.pk)
        self.assertEqual(total, 1)
        self.assertEqual(len(diags), 1)
        self.assertTrue(diags[0]["matched"])


class RoutingBypassLoggingTests(TestCase):
    """
    Tests that bypass events produce structured log records with the
    expected keys.  This guards against accidental changes to the log
    format that would break monitoring dashboards.
    """

    @classmethod
    def setUpTestData(cls):
        cls.q = Queue.objects.create(title="Support", slug="support")
        cls.owner = User.objects.create_user(username="tech", password="p")

    def setUp(self):
        EmailRoutingRule.objects.all().delete()

    def _parse_kv_log(self, message: str) -> dict:
        """
        Parse a ``key=value key2='quoted value'`` style log line.
        Correctly handles quoted values that may contain spaces (e.g.
        the repr of a list with spaces between items).
        """
        result = {}
        # Match key=value or key='value with spaces'
        pattern = re.compile(r"(\w+)=(?:'([^']*)'|([^\s']+)(?!'))")
        pos = 0
        while pos < len(message):
            m = pattern.search(message, pos)
            if not m:
                break
            key = m.group(1)
            value = m.group(2) if m.group(2) is not None else m.group(3)
            result[key] = value
            pos = m.end()
        return result

    def _get_field_from_msg(self, message: str, key: str) -> Optional[str]:
        """Helper to robustly get a field value even if KV parse fails."""
        match = re.search(rf"{key}=(?:'([^']*)'|(\S+))", message)
        if not match:
            # Handle empty value case: key= followed by space or end
            empty_match = re.search(rf"{key}=(\s|$)", message)
            if empty_match:
                return ""
            return None
        return match.group(1) if match.group(1) is not None else match.group(2)

    def test_no_match_bypass_emits_structured_log(self):
        with self.assertLogs("helpdesk.routing", level="WARNING") as caplog:
            with self.assertRaises(BypassTicketException) as ctx:
                apply_routing(
                    {"queue": self.q, "priority": 3},
                    sender_email="unknown@outside.com",
                    subject="Out of scope inquiry",
                    body="Hi",
                    queue=self.q,
                    bypass_on_no_match=True,
                )

        self.assertEqual(ctx.exception.reason, "No matching routing rule")
        self.assertEqual(ctx.exception.sender_email, "unknown@outside.com")
        self.assertEqual(ctx.exception.subject, "Out of scope inquiry")
        self.assertIsNone(ctx.exception.rule)

        records = _grab_routing_logs(caplog)
        log = _find_log_by_event(records, "email_routing_bypass")
        self.assertIsNotNone(log)
        fields = self._parse_kv_log(log.getMessage())

        # Assert the exact keys that monitoring depends on
        self.assertEqual(fields["event"], "email_routing_bypass")
        self.assertEqual(fields["bypass_type"], "no_matching_rule")
        self.assertEqual(fields["sender"], "unknown@outside.com")
        self.assertEqual(fields["subject"], "Out of scope inquiry")
        self.assertEqual(fields["queue"], "support")
        self.assertEqual(fields["matched_rule"], "")
        matched_rule_id = self._get_field_from_msg(log.getMessage(), "matched_rule_id")
        self.assertEqual(matched_rule_id, "")
        self.assertEqual(self._get_field_from_msg(log.getMessage(), "total_rules_evaluated"), "0")
        rule_diags = self._get_field_from_msg(log.getMessage(), "rule_diagnostics")
        self.assertIsNotNone(rule_diags)

    def test_explicit_bypass_rule_emits_structured_log(self):
        bypass_rule = EmailRoutingRule.objects.create(
            name="Spam bypass",
            order=1,
            enabled=True,
            subject="*spam*",
            subject_match_type=MATCH_TYPE_WILDCARD,
            bypass=True,
        )

        with self.assertLogs("helpdesk.routing", level="WARNING") as caplog:
            with self.assertRaises(BypassTicketException) as ctx:
                apply_routing(
                    {"queue": self.q, "priority": 3},
                    sender_email="spammer@evil.com",
                    subject="Buy this spam now",
                    body="Click here",
                    queue=self.q,
                    bypass_on_no_match=True,
                )

        self.assertEqual(ctx.exception.rule.pk, bypass_rule.pk)
        self.assertEqual(ctx.exception.sender_email, "spammer@evil.com")
        self.assertEqual(ctx.exception.subject, "Buy this spam now")

        records = _grab_routing_logs(caplog)
        log = _find_log_by_event(records, "email_routing_bypass")
        self.assertIsNotNone(log)
        fields = self._parse_kv_log(log.getMessage())
        self.assertEqual(fields["bypass_type"], "rule_explicit_bypass")
        self.assertEqual(fields["matched_rule"], "Spam bypass")
        self.assertEqual(
            self._get_field_from_msg(log.getMessage(), "matched_rule_id"),
            str(bypass_rule.pk),
        )
        self.assertEqual(
            self._get_field_from_msg(log.getMessage(), "total_rules_evaluated"),
            "1",
        )
        rule_diags = self._get_field_from_msg(log.getMessage(), "rule_diagnostics")
        self.assertIsNotNone(rule_diags)
        # The diagnostic should mention the rule name and its match status
        # Use full message since rule_diagnostics may contain nested quotes
        full_msg = log.getMessage()
        self.assertIn("Spam bypass", full_msg)

    def test_subject_truncated_at_200_chars(self):
        long_subject = "A" * 300
        with self.assertLogs("helpdesk.routing", level="WARNING") as caplog:
            with self.assertRaises(BypassTicketException):
                apply_routing(
                    {"queue": self.q, "priority": 3},
                    sender_email="a@b.com",
                    subject=long_subject,
                    body="",
                    queue=self.q,
                    bypass_on_no_match=True,
                )

        records = _grab_routing_logs(caplog)
        log = _find_log_by_event(records, "email_routing_bypass")
        self.assertIsNotNone(log)
        msg = log.getMessage()
        # subject should be truncated with ellipsis; total quoted subject ~ 201 chars
        match = re.search(r"subject='([^']*)'", msg)
        self.assertIsNotNone(match)
        quoted_subject = match.group(1)
        self.assertLessEqual(len(quoted_subject), 203)  # 200 + "..."
        self.assertTrue(quoted_subject.endswith("..."))

    def test_rule_diagnostics_contains_failure_reason_for_each_rule(self):
        r1 = EmailRoutingRule.objects.create(
            name="R1",
            order=1,
            enabled=True,
            sender_email="*@vip.com",
            sender_match_type=MATCH_TYPE_WILDCARD,
        )
        r2 = EmailRoutingRule.objects.create(
            name="R2",
            order=2,
            enabled=True,
            subject="*urgent*",
            subject_match_type=MATCH_TYPE_WILDCARD,
        )
        r3 = EmailRoutingRule.objects.create(
            name="R3",
            order=3,
            enabled=True,
            keywords="error",
            keywords_match_type=MATCH_TYPE_CONTAINS,
        )

        with self.assertLogs("helpdesk.routing", level="WARNING") as caplog:
            with self.assertRaises(BypassTicketException):
                apply_routing(
                    {"queue": self.q, "priority": 3},
                    sender_email="normal@example.com",
                    subject="Hello",
                    body="All good",
                    queue=self.q,
                    bypass_on_no_match=True,
                )

        records = _grab_routing_logs(caplog)
        log = _find_log_by_event(records, "email_routing_bypass")
        full_msg = log.getMessage()
        self.assertEqual(
            self._get_field_from_msg(full_msg, "total_rules_evaluated"),
            "3",
        )

        # Each rule should appear with a diagnostic reason
        self.assertIn("R1#order=1:NO:sender:no_match(*@vip.com|wildcard)", full_msg)
        self.assertIn("R2#order=2:NO:sender:any", full_msg)
        self.assertIn("R3#order=3:NO:sender:any", full_msg)

    def test_successful_match_does_not_emit_bypass_log(self):
        EmailRoutingRule.objects.create(
            name="Normal rule",
            order=1,
            enabled=True,
            sender_email="*@x.com",
            sender_match_type=MATCH_TYPE_WILDCARD,
        )
        with self.assertLogs("helpdesk.routing", level="INFO") as caplog:
            apply_routing(
                {"queue": self.q, "priority": 3},
                sender_email="a@x.com",
                subject="s",
                body="b",
                bypass_on_no_match=True,
            )

        records = _grab_routing_logs(caplog)
        self.assertIsNone(_find_log_by_event(records, "email_routing_bypass"))
        # Should have an "Applied routing rule" info log instead
        self.assertTrue(
            any("Applied routing rule 'Normal rule'" in r.getMessage() for r in records)
        )


class RoutingDisabledRuleDegradationTests(TestCase):
    """
    Verify that disabled rules correctly degrade to "no match" behaviour and
    produce the same bypass logs as if the rule was absent.  This protects
    against future regressions where an enabled flag check is accidentally
    dropped.
    """

    @classmethod
    def setUpTestData(cls):
        cls.q = Queue.objects.create(title="Support", slug="support")
        cls.owner = User.objects.create_user(username="tech", password="p")

    def setUp(self):
        EmailRoutingRule.objects.all().delete()

    def _grab_routing_logs(self, caplog):
        return [r for r in caplog.records if r.name == "helpdesk.routing"]

    def _find_log_by_event(self, records, event):
        for r in records:
            if f"event={event}" in r.getMessage():
                return r
        return None

    def _get_field_from_msg(self, message: str, key: str) -> Optional[str]:
        match = re.search(rf"{key}=(?:'([^']*)'|(\S+))", message)
        if not match:
            empty_match = re.search(rf"{key}=(\s|$)", message)
            return "" if empty_match else None
        return match.group(1) if match.group(1) is not None else match.group(2)

    # -------- find_matching_rule() behaviour with disabled rules --------

    def test_all_rules_disabled_returns_none(self):
        """Multiple rules exist but all disabled → find_matching_rule returns None."""
        EmailRoutingRule.objects.create(
            name="Disabled VIP rule",
            order=1,
            enabled=False,
            sender_email="*@vip.com",
            sender_match_type=MATCH_TYPE_WILDCARD,
        )
        EmailRoutingRule.objects.create(
            name="Disabled billing rule",
            order=2,
            enabled=False,
            subject="*billing*",
            subject_match_type=MATCH_TYPE_WILDCARD,
        )
        self.assertIsNone(
            find_matching_rule(
                sender_email="ceo@vip.com",
                subject="urgent billing issue",
                body="invoice",
            )
        )

    def test_disabled_rule_skipped_next_enabled_wins(self):
        """First matching rule is disabled → next enabled rule wins."""
        disabled = EmailRoutingRule.objects.create(
            name="Disabled VIP",
            order=1,
            enabled=False,
            sender_email="*@vip.com",
            sender_match_type=MATCH_TYPE_WILDCARD,
            target_priority=1,
        )
        enabled = EmailRoutingRule.objects.create(
            name="Enabled wildcard",
            order=10,
            enabled=True,
            subject="*urgent*",
            subject_match_type=MATCH_TYPE_WILDCARD,
            target_priority=3,
        )
        matched = find_matching_rule(
            sender_email="ceo@vip.com",
            subject="Very urgent problem",
            body="System down",
        )
        self.assertIsNotNone(matched)
        self.assertEqual(matched.pk, enabled.pk)
        self.assertNotEqual(matched.pk, disabled.pk)

    def test_disabled_rule_skipped_even_if_would_match(self):
        """A disabled rule sits first in order but is completely skipped."""
        EmailRoutingRule.objects.create(
            name="Disabled high priority",
            order=0,
            enabled=False,
            sender_email="ceo@company.com",
            sender_match_type=MATCH_TYPE_EXACT,
            target_priority=1,
        )
        next_rule = EmailRoutingRule.objects.create(
            name="Enabled fallback",
            order=5,
            enabled=True,
            sender_email="*@company.com",
            sender_match_type=MATCH_TYPE_WILDCARD,
            target_priority=3,
        )
        matched = find_matching_rule(
            sender_email="ceo@company.com",
            subject="Hello",
            body="Hello",
        )
        self.assertEqual(matched.pk, next_rule.pk)

    # -------- apply_routing() degradation with all rules disabled --------

    def test_all_rules_disabled_bypass_log_matches_zero_rules_format(self):
        """
        When all existing rules are disabled the bypass log must be identical
        in format (bypass_type, matched_rule, etc.) to the case when zero
        rules exist in the database at all.  The only allowed difference is
        total_rules_evaluated and rule_diagnostics contents.
        """
        # --- Phase A: zero rules in DB ---
        with self.assertLogs("helpdesk.routing", level="WARNING") as caplog_a:
            with self.assertRaises(BypassTicketException):
                apply_routing(
                    {"queue": self.q, "priority": 3},
                    sender_email="a@b.com",
                    subject="Hello",
                    body="body",
                    queue=self.q,
                    bypass_on_no_match=True,
                )
        log_a = self._find_log_by_event(
            self._grab_routing_logs(caplog_a), "email_routing_bypass"
        )
        msg_a = log_a.getMessage()

        # --- Phase B: same rules all exist but disabled ---
        EmailRoutingRule.objects.create(
            name="R1", order=1, enabled=False,
            sender_email="*@x.com", sender_match_type=MATCH_TYPE_WILDCARD,
        )
        EmailRoutingRule.objects.create(
            name="R2", order=2, enabled=False,
            subject="*urgent*", subject_match_type=MATCH_TYPE_WILDCARD,
        )
        with self.assertLogs("helpdesk.routing", level="WARNING") as caplog_b:
            with self.assertRaises(BypassTicketException):
                apply_routing(
                    {"queue": self.q, "priority": 3},
                    sender_email="a@b.com",
                    subject="Hello",
                    body="body",
                    queue=self.q,
                    bypass_on_no_match=True,
                )
        log_b = self._find_log_by_event(
            self._grab_routing_logs(caplog_b), "email_routing_bypass"
        )
        msg_b = log_b.getMessage()

        # --- Compare all fields that MUST be identical ---
        for key in ("event", "bypass_type", "sender", "subject", "queue",
                    "matched_rule", "matched_rule_id"):
            self.assertEqual(
                self._get_field_from_msg(msg_a, key),
                self._get_field_from_msg(msg_b, key),
                f"Field '{key}' differs between zero-rules and all-disabled bypass",
            )
        # bypass_type must be no_matching_rule (NOT rule_explicit_bypass)
        self.assertEqual(
            self._get_field_from_msg(msg_b, "bypass_type"),
            "no_matching_rule",
        )
        # ... but total_rules_evaluated MUST differ (proves disabled rules were walked)
        self.assertEqual(
            self._get_field_from_msg(msg_a, "total_rules_evaluated"), "0"
        )
        self.assertEqual(
            self._get_field_from_msg(msg_b, "total_rules_evaluated"), "2"
        )

    def test_disabled_bypass_rule_does_not_emit_explicit_bypass(self):
        """
        A rule marked both `bypass=True` AND `enabled=False` must NOT
        trigger a rule_explicit_bypass event.  It must degrade to a regular
        no_matching_rule bypass (or fall through to later enabled rules).
        """
        EmailRoutingRule.objects.create(
            name="Disabled bypass spam",
            order=1,
            enabled=False,
            subject="*spam*",
            subject_match_type=MATCH_TYPE_WILDCARD,
            bypass=True,
        )
        with self.assertLogs("helpdesk.routing", level="WARNING") as caplog:
            with self.assertRaises(BypassTicketException):
                apply_routing(
                    {"queue": self.q, "priority": 3},
                    sender_email="s@evil.com",
                    subject="Buy this spam now",
                    body="spam",
                    queue=self.q,
                    bypass_on_no_match=True,
                )
        log = self._find_log_by_event(
            self._grab_routing_logs(caplog), "email_routing_bypass"
        )
        self.assertIsNotNone(log)
        self.assertEqual(
            self._get_field_from_msg(log.getMessage(), "bypass_type"),
            "no_matching_rule",
        )
        self.assertEqual(
            self._get_field_from_msg(log.getMessage(), "matched_rule"),
            "",
        )

    # -------- collect_routing_diagnostics() with disabled rules --------

    def test_diagnostics_lists_disabled_rules_with_rule_disabled_reason(self):
        r1 = EmailRoutingRule.objects.create(
            name="R1", order=1, enabled=False,
            sender_email="*@x.com", sender_match_type=MATCH_TYPE_WILDCARD,
        )
        r2 = EmailRoutingRule.objects.create(
            name="R2", order=2, enabled=True,
            sender_email="*@x.com", sender_match_type=MATCH_TYPE_WILDCARD,
        )
        matched, total, diags = collect_routing_diagnostics(
            "ceo@x.com", "s", "b",
        )
        # Even though R1 matches the pattern it must be skipped due to disabled
        # → R2 is the match.  BUT we stop at the first match, so only R1 is
        # in the diagnostic list (R2 never got evaluated).
        #
        # However: current implementation stops on FIRST HIT, so R1 (disabled)
        # returns False from matches() (since enabled check is inside matches),
        # and is added to diags as a miss.  Then R2 is evaluated and added
        # as hit. So total should be 2.
        self.assertEqual(total, 2)
        self.assertEqual(len(diags), 2)
        self.assertFalse(diags[0]["matched"])
        self.assertIn("rule_disabled", diags[0]["reasons"])
        self.assertTrue(diags[1]["matched"])
        self.assertEqual(diags[1]["rule_id"], r2.pk)

    # -------- Mix of disabled / enabled / bypass rules --------

    def test_mix_disabled_enabled_explicit_bypass(self):
        """
        Scenario:
          order=1: disabled rule (matches sender)
          order=2: enabled rule (matches nothing)
          order=3: enabled rule with bypass=True (matches subject)
        Expected: matches order=3 with rule_explicit_bypass bypass type.
        """
        EmailRoutingRule.objects.create(
            name="Disabled VIP", order=1, enabled=False,
            sender_email="*@vip.com", sender_match_type=MATCH_TYPE_WILDCARD,
        )
        EmailRoutingRule.objects.create(
            name="Unrelated", order=2, enabled=True,
            subject="*nope*", subject_match_type=MATCH_TYPE_WILDCARD,
        )
        bypass_rule = EmailRoutingRule.objects.create(
            name="Marketing bypass", order=3, enabled=True,
            subject="*free trial*", subject_match_type=MATCH_TYPE_WILDCARD,
            bypass=True,
        )
        with self.assertLogs("helpdesk.routing", level="WARNING") as caplog:
            with self.assertRaises(BypassTicketException) as ctx:
                apply_routing(
                    {"queue": self.q, "priority": 3},
                    sender_email="sales@vip.com",
                    subject="Claim your free trial",
                    body="Click",
                    queue=self.q,
                    bypass_on_no_match=True,
                )

        self.assertEqual(ctx.exception.rule.pk, bypass_rule.pk)
        self.assertEqual(ctx.exception.sender_email, "sales@vip.com")
        self.assertEqual(ctx.exception.subject, "Claim your free trial")
        log = self._find_log_by_event(
            self._grab_routing_logs(caplog), "email_routing_bypass"
        )
        msg = log.getMessage()
        self.assertEqual(
            self._get_field_from_msg(msg, "bypass_type"),
            "rule_explicit_bypass",
        )
        self.assertEqual(
            self._get_field_from_msg(msg, "matched_rule"),
            "Marketing bypass",
        )
        # 3 rules evaluated (disabled VIP → miss, unrelated → miss, marketing → hit+stop)
        self.assertEqual(
            self._get_field_from_msg(msg, "total_rules_evaluated"), "3"
        )
        # Disabled rule should be mentioned in diagnostics with rule_disabled
        self.assertIn("Disabled VIP", msg)
        self.assertIn("rule_disabled", msg)


class RoutingDisabledIntegrationTests(TestCase):
    """
    Integration tests that exercise the full extract_email_metadata pipeline
    with rules that are present but disabled.
    """

    @classmethod
    def setUpTestData(cls):
        cls.q = Queue.objects.create(
            title="Test",
            slug="test",
            email_address="support@test.com",
        )
        cls.owner = User.objects.create_user(username="owner2", password="p")

    def setUp(self):
        EmailRoutingRule.objects.all().delete()

    def _make_message(self, sender, subject, body):
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = "support@test.com"
        msg["Subject"] = subject
        msg.set_content(body)
        return msg.as_string()

    def _logger(self):
        return logging.getLogger("helpdesk.test.disable")

    def test_all_rules_disabled_bypass_on_raises(self):
        """
        Integration: All rules disabled + bypass_on_no_match=True →
        BypassTicketException raised, not a ticket creation.
        """
        import helpdesk.settings as hs
        orig = hs.HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH
        hs.HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH = True
        try:
            EmailRoutingRule.objects.create(
                name="Disabled everything-catcher",
                order=1,
                enabled=False,
                sender_email="*",
                sender_match_type=MATCH_TYPE_WILDCARD,
                target_owner=self.owner,
            )
            from helpdesk.email import extract_email_metadata
            msg = self._make_message("a@b.com", "Disabled rule test", "Body")
            with self.assertRaises(BypassTicketException) as ctx:
                extract_email_metadata(message=msg, queue=self.q, logger=self._logger())
            self.assertEqual(ctx.exception.sender_email, "a@b.com")
            # No rule attached (degraded to "no matching rule")
            self.assertIsNone(ctx.exception.rule)
        finally:
            hs.HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH = orig

    def test_disabled_rule_not_applied_to_created_ticket(self):
        """
        Integration: All rules disabled + bypass_on_no_match=False → ticket
        is created using default payload, WITHOUT the disabled rule's targets.
        """
        import helpdesk.settings as hs
        orig = hs.HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH
        hs.HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH = False
        try:
            EmailRoutingRule.objects.create(
                name="Disabled priority rule",
                order=1,
                enabled=False,
                sender_email="*",
                sender_match_type=MATCH_TYPE_WILDCARD,
                target_priority=1,
                target_owner=self.owner,
            )
            from helpdesk.email import extract_email_metadata
            msg = self._make_message("a@b.com", "Hello", "Body")
            ticket = extract_email_metadata(
                message=msg, queue=self.q, logger=self._logger()
            )
            self.assertIsNotNone(ticket)
            # Default priority is 3, NOT the disabled-rule value of 1
            self.assertEqual(ticket.priority, 3)
            # No owner assigned
            self.assertIsNone(ticket.assigned_to)
            # Assigned to the original queue, not a target
            self.assertEqual(ticket.queue, self.q)
        finally:
            hs.HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH = orig


class RoutingApplyToPayloadTests(TestCase):
    """Test the apply_to_payload method respects bypass and target fields."""

    @classmethod
    def setUpTestData(cls):
        cls.q1 = Queue.objects.create(title="Q1", slug="q1")
        cls.q2 = Queue.objects.create(title="Q2", slug="q2")
        cls.owner = User.objects.create_user(username="o", password="p")

    def test_bypass_flag_takes_precedence(self):
        rule = EmailRoutingRule.objects.create(
            name="b", order=1, enabled=True, bypass=True,
            target_queue=self.q2, target_priority=1, target_owner=self.owner,
        )
        payload = {"queue": self.q1, "priority": 3}
        result = rule.apply_to_payload(payload)
        self.assertTrue(result["bypass"])

    def test_target_fields_override_payload(self):
        rule = EmailRoutingRule.objects.create(
            name="t", order=1, enabled=True,
            target_queue=self.q2, target_priority=1, target_owner=self.owner,
        )
        payload = {"queue": self.q1, "priority": 3}
        result = rule.apply_to_payload(payload)
        self.assertEqual(result["queue"], self.q2)
        self.assertEqual(result["priority"], 1)
        self.assertEqual(result["assigned_to"], self.owner)

    def test_partial_overrides(self):
        rule = EmailRoutingRule.objects.create(
            name="t", order=1, enabled=True, target_priority=1,
        )
        payload = {"queue": self.q1, "priority": 3}
        result = rule.apply_to_payload(payload)
        self.assertEqual(result["queue"], self.q1)
        self.assertEqual(result["priority"], 1)
        self.assertNotIn("assigned_to", result)


class RoutingIntegrationWithEmailModuleTests(TestCase):
    """
    Integration test: verify extract_email_metadata triggers the
    routing engine and honours HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH.
    """

    @classmethod
    def setUpTestData(cls):
        cls.q = Queue.objects.create(
            title="Test",
            slug="test",
            email_address="support@test.com",
        )
        cls.owner = User.objects.create_user(username="owner", password="p")

    def setUp(self):
        EmailRoutingRule.objects.all().delete()

    def _make_email_message(self, sender: str, subject: str, body: str) -> str:
        from email.message import EmailMessage
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = "support@test.com"
        msg["Subject"] = subject
        msg.set_content(body)
        return msg.as_string()

    def _get_logger(self):
        return logging.getLogger("helpdesk.test")

    def _extract_rule_diagnostics(self, message: str) -> str:
        """Extract the rule_diagnostics value from a log message, handling nested quotes."""
        match = re.search(r"rule_diagnostics=(\[.*?\])($|\s\w+=)", message)
        if match:
            return match.group(1)
        return ""

    @override_settings(HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH=True)
    def test_no_rule_bypass_on(self):
        import helpdesk.settings as hs
        original = hs.HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH
        hs.HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH = True
        try:
            from helpdesk.email import extract_email_metadata
            msg = self._make_email_message("a@b.com", "Hello", "Body")
            with self.assertRaises(BypassTicketException) as ctx:
                extract_email_metadata(message=msg, queue=self.q, logger=self._get_logger())
            self.assertEqual(ctx.exception.sender_email, "a@b.com")
        finally:
            hs.HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH = original

    @override_settings(HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH=False)
    def test_no_rule_bypass_off_creates_ticket(self):
        import helpdesk.settings as hs
        original = hs.HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH
        hs.HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH = False
        try:
            from helpdesk.email import extract_email_metadata
            msg = self._make_email_message("a@b.com", "Hello", "Body")
            ticket = extract_email_metadata(message=msg, queue=self.q, logger=self._get_logger())
            self.assertIsNotNone(ticket)
            self.assertEqual(ticket.title, "Hello")
        finally:
            hs.HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH = original

    @override_settings(HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH=True)
    def test_matching_rule_applies_assignment(self):
        import helpdesk.settings as hs
        original = hs.HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH
        hs.HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH = True
        try:
            EmailRoutingRule.objects.create(
                name="VIP",
                order=1,
                enabled=True,
                sender_email="*@vip.com",
                sender_match_type=MATCH_TYPE_WILDCARD,
                target_priority=1,
                target_owner=self.owner,
            )
            from helpdesk.email import extract_email_metadata
            msg = self._make_email_message("ceo@vip.com", "Help", "System is down")
            ticket = extract_email_metadata(message=msg, queue=self.q, logger=self._get_logger())
            self.assertIsNotNone(ticket)
            self.assertEqual(ticket.priority, 1)
            self.assertEqual(ticket.assigned_to, self.owner)
        finally:
            hs.HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH = original


class RoutingRollbackRegressionTests(TestCase):
    """
    Simulate configuration drift / corruption and verify that rolling back
    to a known-good snapshot restores deterministic routing behaviour
    (hit order, assignment targets, and bypass behaviour).

    The tests follow a strict pattern:

      1. apply a known "production baseline" rule configuration
      2. run a diverse set of probe emails → record baseline expectations
      3. mutate the rules in several realistic "broken" ways
      4. verify the behaviour has indeed changed (sanity check)
      5. roll back to the baseline configuration
      6. re-run the same probe emails → must match baseline exactly
    """

    # ------------------------------------------------------------------
    # A "production-like" rule configuration, representative of the
    # "old version" we want to be able to roll back to.
    # ------------------------------------------------------------------
    BASELINE_RULE_SPECS: List[dict] = [
        # 1. Highest priority: explicit spam bypass
        dict(
            name="Bypass: Spam / Advertisement",
            order=1,
            enabled=True,
            subject="*spam*",
            subject_match_type=MATCH_TYPE_WILDCARD,
            bypass=True,
        ),
        # 2. C-level executives get immediate Critical priority + CTO owner
        dict(
            name="C-Level VIP",
            order=10,
            enabled=True,
            sender_email="*@exec.example.com",
            sender_match_type=MATCH_TYPE_WILDCARD,
            target_priority=1,
        ),
        # 3. Billing issues → Billing queue.  Requires BOTH subject match AND
        #    at least one keyword present (so blank-keywords corruption disables it).
        dict(
            name="Billing Queue",
            order=20,
            enabled=True,
            subject="*bill*",
            subject_match_type=MATCH_TYPE_WILDCARD,
            keywords="invoice, payment, charge, receipt",
            keywords_logic=KEYWORDS_LOGIC_ANY,
            keywords_match_type=MATCH_TYPE_CONTAINS,
        ),
        # 4. On-call alerts → assigned to oncall owner + Priority 2.
        #    NOTE: this rule also matches "[ALERT] billing failure" which is
        #    also covered by the Billing rule; hence swapping the order of
        #    rules 3 and 4 will change which rule wins for that probe.
        dict(
            name="Oncall Alert",
            order=30,
            enabled=True,
            subject=r"^\[ALERT\].*",
            subject_match_type=MATCH_TYPE_REGEX,
            target_priority=2,
        ),
        # 5. Disabled catch-all (present in DB but should be skipped)
        dict(
            name="Legacy catch-all (disabled)",
            order=999,
            enabled=False,
            sender_email="*",
            sender_match_type=MATCH_TYPE_WILDCARD,
            target_priority=5,
        ),
    ]

    # Probe emails.  Each covers a distinct code path / conflict scenario.
    PROBE_EMAILS = [
        # 0: C-Level VIP match
        dict(sender="ceo@exec.example.com", subject="URGENT: outage", body="down"),
        # 1: Billing (subject *bill* + keywords present in body) match
        dict(sender="sales@example.com", subject="bill for monthly services",
             body="please find invoice attached and process payment"),
        # 2: Oncall Alert match
        dict(sender="monitoring@ops.example.com",
             subject="[ALERT] prod-api ERROR rate high", body="stack trace"),
        # 3: Spam bypass match
        dict(sender="spammer@evil.com", subject="Free spam offer", body="click here"),
        # 4: No match at all
        dict(sender="random@external.com", subject="Hello", body="no match"),
        # 5: Billing keyword blanking victim: body has no keywords → without
        #    the correct keywords the rule will miss when keywords are corrupted.
        dict(sender="finance@example.com", subject="bill for services",
             body="please find attached invoice"),
        # 6: ORDER-CONFLICT PROBE: subject matches BOTH Billing (*bill*) AND
        #    Oncall regex (^[ALERT].*).  Whichever rule has lower order wins.
        dict(sender="ops@example.com", subject="[ALERT] billing gateway down",
             body="invoice for downtime"),
    ]

    @classmethod
    def setUpTestData(cls):
        cls.q_support = Queue.objects.create(title="Support", slug="support")
        cls.q_billing = Queue.objects.create(title="Billing", slug="billing")
        cls.cto = User.objects.create_user(username="cto", password="p")
        cls.oncall = User.objects.create_user(username="oncall", password="p")

    # ------------------------------------------------------------------
    # Helper methods
    # ------------------------------------------------------------------
    def _apply_config(self, rule_specs: List[dict]) -> None:
        """Idempotently apply a list of rule specs: wipe existing, create new."""
        EmailRoutingRule.objects.all().delete()
        for spec in rule_specs:
            # Extract M2M queues from the spec if present
            queues = spec.pop("queues_obj", None)
            target_queue = spec.pop("target_queue_obj", None)
            target_owner = spec.pop("target_owner_obj", None)
            rule = EmailRoutingRule.objects.create(**spec)
            if queues:
                rule.queues.set(queues)
            if target_queue:
                rule.target_queue = target_queue
                rule.save(update_fields=["target_queue"])
            if target_owner:
                rule.target_owner = target_owner
                rule.save(update_fields=["target_owner"])

    def _snapshot_rule_config(self) -> List[dict]:
        """Capture the current rule configuration as a list of dicts
        (equivalent to the format of BASELINE_RULE_SPECS).

        Database primary keys are excluded from the snapshot because they
        change on every delete+recreate cycle; all semantic fields are kept.
        """
        snapshot = []
        for rule in EmailRoutingRule.objects.order_by("order", "id"):
            snapshot.append(
                dict(
                    name=rule.name,
                    order=rule.order,
                    enabled=rule.enabled,
                    sender_email=rule.sender_email,
                    sender_match_type=rule.sender_match_type,
                    subject=rule.subject,
                    subject_match_type=rule.subject_match_type,
                    keywords=rule.keywords,
                    keywords_logic=rule.keywords_logic,
                    keywords_match_type=rule.keywords_match_type,
                    bypass=rule.bypass,
                    target_queue_id=rule.target_queue_id,
                    target_priority=rule.target_priority,
                    target_owner_id=rule.target_owner_id,
                )
            )
        return snapshot

    def _run_probes(self) -> dict:
        """Run each probe email through apply_routing and collect results.
        Returns a dict keyed by probe index.  Each value is a dict with
        enough fields to be sensitive to any regression in rule behaviour.

        NOTE: Database auto-incrementing primary keys are intentionally NOT
        included in the comparison because they change on every delete+recreate
        (the rollback mechanism).  Only semantic behaviour fields are tracked.
        """
        results = {}
        import helpdesk.settings as hs
        orig = hs.HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH
        hs.HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH = True
        try:
            for idx, probe in enumerate(self.PROBE_EMAILS):
                entry = {}
                try:
                    payload, matched = apply_routing(
                        {"queue": self.q_support, "priority": 3},
                        sender_email=probe["sender"],
                        subject=probe["subject"],
                        body=probe["body"],
                        queue=self.q_support,
                        bypass_on_no_match=True,
                    )
                    entry["outcome"] = "routed"
                    entry["matched_rule_name"] = matched.name if matched else None
                    entry["priority"] = payload.get("priority")
                    entry["target_queue_slug"] = (
                        payload["queue"].slug if payload.get("queue") else None
                    )
                    entry["assigned_to_id"] = (
                        payload["assigned_to"].pk
                        if payload.get("assigned_to") else None
                    )
                except BypassTicketException as e:
                    entry["outcome"] = "bypass"
                    entry["bypass_reason"] = e.reason
                    entry["bypass_type"] = (
                        "rule_explicit_bypass" if e.rule else "no_matching_rule"
                    )
                    entry["matched_rule_name"] = e.rule.name if e.rule else None
                results[idx] = entry
        finally:
            hs.HELPDESK_EMAIL_ROUTING_BYPASS_ON_NO_MATCH = orig
        return results

    def _assert_results_equal(self, baseline: dict, rolled: dict, msg: str):
        """Strict equality check on probe results."""
        self.assertEqual(
            set(baseline.keys()), set(rolled.keys()),
            f"{msg}: probe result key sets differ",
        )
        for idx in baseline:
            self.assertEqual(
                baseline[idx], rolled[idx],
                f"{msg}: probe #{idx} ({self.PROBE_EMAILS[idx]}) differs",
            )

    # ------------------------------------------------------------------
    # Rollback test helpers - each applies a realistic "bad change"
    # ------------------------------------------------------------------
    def _corrupt_disable_critical_rules(self):
        """Disable the top two rules (spam bypass + C-Level VIP)."""
        EmailRoutingRule.objects.filter(order__in=[1, 10]).update(enabled=False)

    def _corrupt_flip_order(self):
        """Swap the order of billing and oncall rules."""
        billing = EmailRoutingRule.objects.get(name="Billing Queue")
        oncall = EmailRoutingRule.objects.get(name="Oncall Alert")
        billing.order, oncall.order = oncall.order, billing.order
        billing.save(update_fields=["order"])
        oncall.save(update_fields=["order"])

    def _corrupt_remove_bypass_flag(self):
        """Remove bypass=True from the spam rule."""
        spam = EmailRoutingRule.objects.get(name="Bypass: Spam / Advertisement")
        spam.bypass = False
        spam.save(update_fields=["bypass"])

    def _corrupt_wild_targets(self):
        """Assign nonsense targets to a few rules."""
        EmailRoutingRule.objects.filter(order__in=[10, 20, 30]).update(
            target_priority=5,
        )

    def _corrupt_delete_half_rules(self):
        """Delete every other rule (simulating a bad migration rollout)."""
        all_pks = list(
            EmailRoutingRule.objects.order_by("order").values_list("pk", flat=True)
        )
        EmailRoutingRule.objects.filter(pk__in=all_pks[::2]).delete()

    def _corrupt_blank_keywords(self):
        """
        Replace billing keywords with nonsense so no real email can match.
        (Blanking keywords would actually *weaken* the rule because empty
        keywords means "no keyword check required", so we use an impossible
        string instead.)
        """
        billing = EmailRoutingRule.objects.get(name="Billing Queue")
        billing.keywords = "XYZZY_NO_MATCH_PLZ_12345"
        billing.save(update_fields=["keywords"])

    # ------------------------------------------------------------------
    # Actual rollback tests
    # ------------------------------------------------------------------
    def test_rollback_restore_match_order_and_assignment(self):
        """
        End-to-end: capture baseline, apply 6 different corruptions
        sequentially, each time verifying behaviour changed, and each
        time rolling back and verifying full equivalence to baseline.
        """
        # --- Phase 1: apply baseline and record expectations ---
        self._apply_config([dict(s) for s in self.BASELINE_RULE_SPECS])
        baseline_snapshot = self._snapshot_rule_config()
        baseline_results = self._run_probes()

        # Sanity: baseline must contain both routed AND bypass outcomes
        self.assertIn(
            "bypass",
            {v["outcome"] for v in baseline_results.values()},
            "baseline should include at least one bypass case",
        )
        self.assertIn(
            "routed",
            {v["outcome"] for v in baseline_results.values()},
            "baseline should include at least one routed case",
        )

        corruptions = [
            ("disable-critical-rules", self._corrupt_disable_critical_rules),
            ("flip-order", self._corrupt_flip_order),
            ("remove-bypass-flag", self._corrupt_remove_bypass_flag),
            ("wild-targets", self._corrupt_wild_targets),
            ("delete-half-rules", self._corrupt_delete_half_rules),
            ("blank-keywords", self._corrupt_blank_keywords),
        ]

        for name, corrupt_fn in corruptions:
            with self.subTest(corruption=name):
                # --- Phase 2: apply corruption ---
                corrupt_fn()
                corrupted_results = self._run_probes()

                # Sanity: corruption MUST have changed the outcome
                # (if not, the corruption test itself is ineffective)
                self.assertNotEqual(
                    baseline_results,
                    corrupted_results,
                    f"Corruption '{name}' had no effect on routing "
                    f"results; this subtest is ineffective",
                )

                # --- Phase 3: roll back to baseline ---
                self._apply_config([dict(s) for s in self.BASELINE_RULE_SPECS])
                rolled_snapshot = self._snapshot_rule_config()
                rolled_results = self._run_probes()

                # --- Phase 4: verify full restoration ---
                self.assertEqual(
                    baseline_snapshot,
                    rolled_snapshot,
                    f"Rollback from '{name}' failed to restore exact "
                    f"rule configuration snapshot",
                )
                self._assert_results_equal(
                    baseline_results,
                    rolled_results,
                    f"Rollback from '{name}'",
                )

    def test_rollback_bypass_log_format_identical(self):
        """
        After rollback, a probe that triggers bypass must produce the
        exact same structured log key set (bypass_type, matched_rule,
        etc.) as the baseline run.  This guards against logging changes
        that silently survive a DB rollback.
        """
        self._apply_config([dict(s) for s in self.BASELINE_RULE_SPECS])

        # The "spam" probe triggers an explicit bypass rule.
        spam_probe = dict(
            sender="spammer@evil.com",
            subject="Free spam offer",
            body="click here",
        )
        # The "random" probe triggers no rule → no_matching_rule bypass.
        random_probe = dict(
            sender="random@external.com",
            subject="Hello",
            body="no match",
        )

        def _capture_bypass_log(probe):
            with self.assertLogs("helpdesk.routing", level="WARNING") as cap:
                with self.assertRaises(BypassTicketException):
                    apply_routing(
                        {"queue": self.q_support, "priority": 3},
                        sender_email=probe["sender"],
                        subject=probe["subject"],
                        body=probe["body"],
                        queue=self.q_support,
                        bypass_on_no_match=True,
                    )
            for r in cap.records:
                msg = r.getMessage()
                if "event=email_routing_bypass" in msg:
                    # Stable fields only - exclude matched_rule_id because the
                    # auto-incrementing PK changes on every rule recreate.
                    stable_keys = ("event", "bypass_type", "sender", "subject",
                                   "queue", "matched_rule")
                    keys = {}
                    for k in stable_keys:
                        m = re.search(rf"{k}=(?:'([^']*)'|(\S+))", msg)
                        if m:
                            keys[k] = m.group(1) if m.group(1) else m.group(2)
                    return keys
            self.fail("No bypass log captured")

        baseline_explicit = _capture_bypass_log(spam_probe)
        baseline_nomatch = _capture_bypass_log(random_probe)

        # Corrupt + rollback
        self._corrupt_wild_targets()
        self._apply_config([dict(s) for s in self.BASELINE_RULE_SPECS])

        rolled_explicit = _capture_bypass_log(spam_probe)
        rolled_nomatch = _capture_bypass_log(random_probe)

        self.assertEqual(
            baseline_explicit, rolled_explicit,
            "Explicit-bypass log key set changed after rollback",
        )
        self.assertEqual(
            baseline_nomatch, rolled_nomatch,
            "No-match-bypass log key set changed after rollback",
        )

    def test_rollback_idempotency(self):
        """
        Applying the baseline config twice (or 5 times) must produce
        identical results each time - no duplicate rule side-effects.
        """
        first_results = None
        for i in range(3):
            self._apply_config([dict(s) for s in self.BASELINE_RULE_SPECS])
            results = self._run_probes()
            if first_results is None:
                first_results = results
                # Verify the number of rules matches baseline spec count
                self.assertEqual(
                    EmailRoutingRule.objects.count(),
                    len(self.BASELINE_RULE_SPECS),
                    f"_apply_config produced wrong rule count on iteration {i}",
                )
            else:
                self._assert_results_equal(
                    first_results, results, f"Idempotency iteration {i}"
                )


class RoutingLogEventNameStabilityTests(TestCase):
    """
    Meta-tests: lock in the literal string values used as event markers
    so that any accidental renaming breaks these tests immediately.
    """

    def test_bypass_log_event_name_is_literal(self):
        # The string "email_routing_bypass" must never change -
        # monitoring queries depend on it.
        with self.assertLogs("helpdesk.routing", level="WARNING") as caplog:
            try:
                _emit_bypass_log(
                    level=logging.WARNING,
                    bypass_type="no_matching_rule",
                    sender_email="a@b.com",
                    subject="s",
                    queue=None,
                    rule=None,
                    total_rules=0,
                    rule_diags=[],
                )
            except Exception:
                pass

        msg = caplog.records[0].getMessage()
        self.assertIn("event=email_routing_bypass", msg)

    def test_channel_bypass_event_name_is_literal(self):
        from helpdesk.email import _log_bypass_event
        logger = logging.getLogger("helpdesk.test")
        with self.assertLogs("helpdesk.test", level="WARNING") as caplog:
            _log_bypass_event(
                logger=logger,
                queue=Queue(slug="test"),
                message_identifier="MSG-1",
                sender_email="a@b.com",
                subject="s",
                bypass_reason="No matching routing rule",
            )
        self.assertIn("event=email_channel_bypass", caplog.records[0].getMessage())
