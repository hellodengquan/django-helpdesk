"""
Metrics for fuzzy ticket deduplication.

Exposes three per-queue counters:
  * fingerprint_merge_hit    - incoming email was merged into an existing ticket
                               via the fuzzy fingerprint matcher
  * fingerprint_new_ticket   - incoming email did not match and a new ticket
                               was created
  * fingerprint_window_miss  - incoming email matched by sender+subject but the
                               existing ticket was older than the dedup window,
                               so a new ticket was opened

If the optional ``prometheus_client`` package is installed the counters are
registered with the default Prometheus registry and can be scraped by a
Prometheus server.  Otherwise the same counters are tracked in a plain
thread-safe Python dictionary so that the admin dashboard view continues to
work.
"""

import logging
import threading
from collections import defaultdict
from datetime import timedelta
from typing import Dict, Tuple

from django.utils import timezone

logger = logging.getLogger(__name__)

PROMETHEUS_AVAILABLE = False

try:
    from prometheus_client import Counter

    PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by fallback tests
    Counter = None


METRIC_MERGE_HIT = "fingerprint_merge_hit"
METRIC_NEW_TICKET = "fingerprint_new_ticket"
METRIC_WINDOW_MISS = "fingerprint_window_miss"

METRIC_LABELS = ("queue_slug",)

_prom_counters: Dict[str, "Counter"] = {}

_fallback_lock = threading.Lock()
_fallback_counts: Dict[str, Dict[str, Dict[str, int]]] = defaultdict(
    lambda: defaultdict(lambda: defaultdict(int))
)
_fallback_history: Dict[str, Dict[str, Dict[Tuple[int, int, int], int]]] = defaultdict(
    lambda: defaultdict(lambda: defaultdict(int))
)


def _register_prometheus() -> None:
    """Register the three counters with Prometheus if the library is available.

    Safe to call multiple times; subsequent invocations are no-ops.
    """
    if not PROMETHEUS_AVAILABLE:
        return

    specs = (
        (
            METRIC_MERGE_HIT,
            "Number of incoming emails merged into an existing ticket via the fuzzy fingerprint matcher.",
        ),
        (
            METRIC_NEW_TICKET,
            "Number of incoming emails that did not match any existing ticket and opened a new one.",
        ),
        (
            METRIC_WINDOW_MISS,
            "Number of incoming emails whose sender+subject matched an existing ticket older than the dedup window.",
        ),
    )
    for name, help_text in specs:
        if name not in _prom_counters:
            try:
                _prom_counters[name] = Counter(
                    name, help_text, labelnames=METRIC_LABELS
                )
            except ValueError:  # pragma: no cover - counter already registered in process
                pass


_register_prometheus()


def _inc_fallback(metric_name: str, queue_slug: str) -> None:
    today = timezone.now().date()
    with _fallback_lock:
        _fallback_counts[queue_slug][metric_name][str(today)] += 1
        _fallback_history[queue_slug][metric_name][
            (today.year, today.month, today.day)
        ] += 1


def inc_metric(metric_name: str, queue_slug: str) -> None:
    """Increment one of the dedup counters for ``queue_slug``.

    The counter is incremented both in the Prometheus registry (when
    available) and in the in-process fallback store that powers the
    dashboard aggregation view.
    """
    if metric_name not in (METRIC_MERGE_HIT, METRIC_NEW_TICKET, METRIC_WINDOW_MISS):
        logger.warning(
            "Ignoring unknown dedup metric %r for queue %r", metric_name, queue_slug
        )
        return

    if PROMETHEUS_AVAILABLE and metric_name in _prom_counters:
        try:
            _prom_counters[metric_name].labels(queue_slug=queue_slug).inc()
        except Exception:  # pragma: no cover - defensive
            logger.exception(
                "Failed to increment Prometheus counter %s for queue %s",
                metric_name,
                queue_slug,
            )

    _inc_fallback(metric_name, queue_slug)


def get_queue_7day_counts(queue_slug: str) -> Dict[str, int]:
    """Return the last 7 days of counter totals for ``queue_slug``.

    The result maps metric name to an integer sum of the counts observed
    between ``today - 6 days`` and ``today`` (inclusive).

    Only the in-process fallback store is consulted – Prometheus counters are
    monotonic and require an external scraper to perform windowed aggregation,
    so the admin dashboard intentionally reads from the same in-process
    accumulator that the metric increments write to.
    """
    today = timezone.now().date()
    window_start = today - timedelta(days=6)

    totals = {METRIC_MERGE_HIT: 0, METRIC_NEW_TICKET: 0, METRIC_WINDOW_MISS: 0}

    with _fallback_lock:
        for metric_name in totals:
            for (year, month, day), count in _fallback_history.get(
                queue_slug, {}
            ).get(metric_name, {}).items():
                from datetime import date as _date

                key_date = _date(year, month, day)
                if window_start <= key_date <= today:
                    totals[metric_name] += count

    return totals


def get_all_queues_7day_counts() -> Dict[str, Dict[str, int]]:
    """Return 7-day totals for every queue that has recorded any metric.

    Returns a dict mapping queue slug -> {metric_name: count}.
    """
    result: Dict[str, Dict[str, int]] = {}
    with _fallback_lock:
        for slug in list(_fallback_history.keys()):
            result[slug] = get_queue_7day_counts(slug)
    return result


def reset_fallback_counts() -> None:
    """Reset all in-process counters.  Used by tests."""
    with _fallback_lock:
        _fallback_counts.clear()
        _fallback_history.clear()
