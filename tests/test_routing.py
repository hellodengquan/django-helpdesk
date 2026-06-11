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
