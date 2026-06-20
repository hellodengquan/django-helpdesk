from base64 import b64decode, b64encode
from copy import deepcopy
from django.db.models import Q, Max
from django.db.models import F, Window, Subquery, OuterRef
from .models import FollowUp
from django.urls import reverse
from django.utils.html import escape
from django.utils.translation import gettext as _
from helpdesk.serializers import DatatablesTicketSerializer
import json
from model_utils import Choices


def query_to_base64(query):
    """
    Converts a query dict object to a base64-encoded bytes object.
    """
    return b64encode(json.dumps(query).encode("UTF-8")).decode("ascii")


def query_from_base64(b64data):
    """
    Converts base64-encoded bytes object back to a query dict object.
    """
    query = {"search_string": ""}
    query.update(json.loads(b64decode(b64data).decode("utf-8")))
    if query["search_string"] is None:
        query["search_string"] = ""
    return query


def get_search_filter_args(search):
    if not search:
        return Q()
    if search.startswith("queue:"):
        return Q(queue__title__icontains=search[len("queue:") :])
    if search.startswith("priority:"):
        return Q(priority__icontains=search[len("priority:") :])
    my_filter = Q()
    for subsearch in search.split("OR"):
        subsearch = subsearch.strip()
        if not subsearch:
            continue
        my_filter |= (
            Q(id__icontains=subsearch)
            | Q(title__icontains=subsearch)
            | Q(description__icontains=subsearch)
            | Q(priority__icontains=subsearch)
            | Q(resolution__icontains=subsearch)
            | Q(submitter_email__icontains=subsearch)
            | Q(assigned_to__email__icontains=subsearch)
            | Q(ticketcustomfieldvalue__value__icontains=subsearch)
            | Q(created__icontains=subsearch)
            | Q(due_date__icontains=subsearch)
        )
    return my_filter


DATATABLES_ORDER_COLUMN_CHOICES = Choices(
    ("0", "id"),
    ("1", "title"),
    ("2", "priority"),
    ("3", "queue"),
    ("4", "status"),
    ("5", "created"),
    ("6", "due_date"),
    ("7", "assigned_to"),
    ("8", "submitter_email"),
    ("9", "last_followup"),
    ("11", "kbitem"),
)


DATATABLES_COLUMN_NUM_LOOKUP = {v: k for k, v in DATATABLES_ORDER_COLUMN_CHOICES}


DEFAULT_QUERY_PARAMS = {
    "filtering": {},
    "filtering_null": {},
    "sorting": None,
    "sortreverse": False,
    "search_string": "",
}


DEFAULT_LIST_QUERY_PARAMS = {
    "filtering": {
        "status__in": [1, 2],
    },
    "sorting": "created",
    "search_string": "",
    "sortreverse": False,
}


FILTER_IN_PARAMS = [
    ("queue", "queue__id__in"),
    ("assigned_to", "assigned_to__id__in"),
    ("status", "status__in"),
    ("priority", "priority__in"),
    ("kbitem", "kbitem__in"),
]


FILTER_NULL_PARAMS = dict(
    [
        ("queue", "queue__id__isnull"),
        ("assigned_to", "assigned_to__id__isnull"),
        ("status", "status__isnull"),
        ("priority", "priority__isnull"),
        ("kbitem", "kbitem__isnull"),
    ]
)


VALID_SORT_FIELDS = {
    "status",
    "assigned_to",
    "created",
    "title",
    "queue",
    "priority",
    "last_followup",
    "kbitem",
    "modified",
    "due_date",
    "id",
}


def build_query_params_from_request(request):
    """
    Build query_params dictionary from an HTTP request's GET parameters.

    This is the single, unified entry point for parsing filter/search/sort
    parameters from user requests. All ticket listing entry points
    (list view, reports, API, export) should use this function to ensure
    consistent behavior.

    Returns a dict with keys: filtering, filtering_null, sorting, sortreverse, search_string
    """
    query_params = deepcopy(DEFAULT_QUERY_PARAMS)

    has_query_params = {
        "queue",
        "assigned_to",
        "status",
        "priority",
        "q",
        "sort",
        "sortreverse",
        "kbitem",
        "date_from",
        "date_to",
    }.intersection(request.GET)

    if not has_query_params:
        return query_params

    for param, filter_command in FILTER_IN_PARAMS:
        if request.GET.get(param) is not None:
            patterns = request.GET.getlist(param)
            if not patterns:
                continue
            try:
                minus_1_ndx = patterns.index("-1")
                patterns.pop(minus_1_ndx)
                query_params["filtering_null"][FILTER_NULL_PARAMS[param]] = True
            except ValueError:
                pass
            if not patterns:
                continue
            try:
                pattern_pks = [int(pattern) for pattern in patterns]
                query_params["filtering"][filter_command] = pattern_pks
            except ValueError:
                pass

    date_from = request.GET.get("date_from")
    if date_from:
        query_params["filtering"]["created__gte"] = date_from

    date_to = request.GET.get("date_to")
    if date_to:
        query_params["filtering"]["created__lte"] = date_to

    q = request.GET.get("q", "")
    query_params["search_string"] = q

    sort = request.GET.get("sort", None)
    if sort and sort in VALID_SORT_FIELDS:
        query_params["sorting"] = sort
    else:
        query_params["sorting"] = "created"

    sortreverse = request.GET.get("sortreverse", None)
    query_params["sortreverse"] = bool(sortreverse)

    return query_params


def get_default_list_query_params():
    """Return the default query params used by the ticket list view."""
    return deepcopy(DEFAULT_LIST_QUERY_PARAMS)


def get_query_class():
    from django.conf import settings

    def _get_query_class():
        return TicketQueryBuilder

    return getattr(settings, "HELPDESK_QUERY_CLASS", _get_query_class)()


class TicketQueryBuilder:
    """
    Unified query builder for Ticket queries.

    This class centralizes all ticket query construction logic so that:
    - List view
    - Search results
    - Reports
    - API endpoints
    - Exports
    All produce the same filtered, sorted, and distinct result sets.

    Usage:
        huser = HelpdeskUser(request.user)
        builder = TicketQueryBuilder(huser, query_params=params)
        queryset = builder.get_queryset()
        count = builder.count()
    """

    def __init__(self, huser, base64query=None, query_params=None):
        """
        :param huser: HelpdeskUser instance (for queue permission checks)
        :param base64query: Optional base64-encoded query string
        :param query_params: Optional dict of query parameters. Takes precedence over base64query.
        """
        self.huser = huser
        if query_params is not None:
            self.params = deepcopy(DEFAULT_QUERY_PARAMS)
            self.params.update(query_params)
            self.base64 = query_to_base64(self.params)
        elif base64query is not None:
            self.params = query_from_base64(base64query)
            self.base64 = base64query
        else:
            self.params = deepcopy(DEFAULT_QUERY_PARAMS)
            self.base64 = query_to_base64(self.params)
        self._cached_queryset = None

    def get_search_filter_args(self):
        search = self.params.get("search_string", "")
        return get_search_filter_args(search)

    def _build_queryset(self, queryset):
        """
        Apply the stored filter/search/sort parameters to a queryset.

        This is the canonical implementation of the filtering logic.
        All callers should go through this (directly or via get_queryset)
        to ensure identical results.
        """
        q_args = []
        value_filters = dict(self.params.get("filtering", {}))
        null_filters = dict(self.params.get("filtering_null", {}))

        if null_filters:
            if value_filters:
                matched_null_keys = []
                for null_key in list(null_filters.keys()):
                    field_path = null_key[:-8]
                    matched_key = None
                    for val_key in list(value_filters.keys()):
                        if val_key.startswith(field_path):
                            matched_key = val_key
                            break
                    if matched_key:
                        matched_null_keys.append(null_key)
                        v = {}
                        v[val_key] = value_filters[val_key]
                        n = {}
                        n[null_key] = null_filters[null_key]
                        q_args.append((Q(**v) | Q(**n)))
                        del value_filters[matched_key]
                for null_key in matched_null_keys:
                    del null_filters[null_key]

        queryset = queryset.filter(
            *q_args,
            (Q(**value_filters) & Q(**null_filters)) & self.get_search_filter_args(),
        )

        sorting = self.params.get("sorting", None)
        if sorting:
            sortreverse = self.params.get("sortreverse", None)
            if sortreverse:
                sorting = "-%s" % sorting
            queryset = queryset.order_by(sorting)

        return queryset.distinct()

    def get_base_queryset(self):
        """
        Return the permission-filtered base queryset (no user filters applied yet).
        Uses select_related() for efficient access to related objects.
        """
        return self.huser.get_tickets_in_queues().select_related()

    def get_queryset(self):
        """
        Return the fully filtered, sorted, and distinct queryset.

        This is the main public API for obtaining the ticket list.
        The result is cached per-instance so repeated calls are cheap.
        """
        if self._cached_queryset is None:
            base = self.get_base_queryset()
            self._cached_queryset = self._build_queryset(base)
        return self._cached_queryset

    def get(self):
        """Alias for get_queryset() — kept for backward compatibility."""
        return self.get_queryset()

    def count(self):
        """Return the count of matching tickets (efficient)."""
        return self.get_queryset().count()

    def get_params(self):
        """Return the current query parameters dict."""
        return deepcopy(self.params)

    def get_base64(self):
        """Return the base64-encoded query string."""
        return self.base64

    def with_last_followup_annotation(self):
        """
        Return the queryset annotated with a `last_followup` datetime
        (the date of the most recent FollowUp for each ticket).
        """
        return self.get_queryset().annotate(
            last_followup=Subquery(
                FollowUp.objects.order_by()
                .annotate(
                    last_followup=Window(
                        expression=Max("date"),
                        partition_by=[
                            F("ticket_id"),
                        ],
                        order_by="-date",
                    )
                )
                .filter(ticket_id=OuterRef("id"))
                .values("last_followup")
                .distinct()
            )
        )

    def get_datatables_context(self, *, column_lookup=None, **kwargs):
        """
        Return pagination-aware context for the DataTables AJAX endpoint.

        This function takes the filtered ticket queryset and returns a dict
        formatted for DataTables consumption: `data`, `recordsFiltered`,
        `recordsTotal`, `draw`.
        """
        objects = self.get_queryset()
        draw = int(kwargs.get("draw", [0])[0])
        length = int(kwargs.get("length", [25])[0])
        start = int(kwargs.get("start", [0])[0])
        search_value = kwargs.get("search[value]", [""])[0]
        if column_lookup is None:
            column_lookup = DATATABLES_ORDER_COLUMN_CHOICES
            num_lookup = DATATABLES_COLUMN_NUM_LOOKUP
        else:
            num_lookup = {v: k for k, v in column_lookup}

        sorting = self.params.get("sorting", "created")
        default_order_col = num_lookup.get(sorting, "5")
        sortreverse = self.params.get("sortreverse", None)
        default_order = "desc" if sortreverse else "asc"

        order_column = kwargs.get("order[0][column]", [default_order_col])[0]
        order = kwargs.get("order[0][dir]", [default_order])[0]

        order_column = column_lookup[order_column]
        if order == "desc":
            order_column = "-" + order_column

        queryset = self.with_last_followup_annotation()

        total = queryset.count()

        if search_value:
            queryset = queryset.filter(get_search_filter_args(search_value))

        count = queryset.count()
        queryset = queryset.order_by(order_column)[start : start + length]
        return {
            "data": DatatablesTicketSerializer(queryset, many=True).data,
            "recordsFiltered": count,
            "recordsTotal": total,
            "draw": draw,
        }

    def get_timeline_context(self):
        events = []

        for ticket in self.get_queryset():
            for followup in ticket.followup_set.all():
                event = {
                    "start_date": self.mk_timeline_date(followup.date),
                    "text": {
                        "headline": ticket.title + " - " + followup.title,
                        "text": (
                            (
                                escape(followup.comment)
                                if followup.comment
                                else _("No text")
                            )
                            + '<br/> <a href="%s" class="btn" role="button">%s</a>'
                            % (
                                reverse(
                                    "helpdesk:view", kwargs={"ticket_id": ticket.pk}
                                ),
                                _("View ticket"),
                            )
                        ),
                    },
                    "group": _("Messages"),
                }
                events.append(event)

        return {
            "events": events,
        }

    def mk_timeline_date(self, date):
        return {
            "year": date.year,
            "month": date.month,
            "day": date.day,
            "hour": date.hour,
            "minute": date.minute,
            "second": date.second,
        }

    def iter_export_rows(self, include_custom_fields=True):
        """
        Iterate over rows suitable for CSV / Excel export.

        Yields each ticket as a dict with human-readable values.
        All entry points that produce exports should use this method
        to guarantee the same data set and ordering as the list view.

        :param include_custom_fields: If True, include custom field values.
        """
        from helpdesk.models import CustomField, TicketCustomFieldValue

        tickets = self.get_queryset()
        custom_fields = list(CustomField.objects.all()) if include_custom_fields else []

        headers = [
            "id",
            "queue",
            "title",
            "status",
            "priority",
            "assigned_to",
            "submitter_email",
            "created",
            "modified",
            "due_date",
            "kbitem",
            "description",
            "resolution",
        ]
        if custom_fields:
            headers += [cf.name for cf in custom_fields]

        yield headers

        for ticket in tickets:
            row = [
                str(ticket.id),
                str(ticket.queue) if ticket.queue else "",
                ticket.title or "",
                ticket.get_status_display() if hasattr(ticket, "get_status_display") else str(ticket.status),
                ticket.get_priority_display() if hasattr(ticket, "get_priority_display") else str(ticket.priority),
                str(ticket.assigned_to) if ticket.assigned_to else "",
                ticket.submitter_email or "",
                ticket.created.strftime("%Y-%m-%d %H:%M:%S") if ticket.created else "",
                ticket.modified.strftime("%Y-%m-%d %H:%M:%S") if ticket.modified else "",
                ticket.due_date.strftime("%Y-%m-%d %H:%M:%S") if ticket.due_date else "",
                str(ticket.kbitem) if ticket.kbitem else "",
                ticket.description or "",
                ticket.resolution or "",
            ]
            if custom_fields:
                cf_values = {
                    cfv.field_id: cfv.value
                    for cfv in TicketCustomFieldValue.objects.filter(ticket=ticket)
                }
                for cf in custom_fields:
                    row.append(cf_values.get(cf.id, ""))
            yield row


__Query__ = TicketQueryBuilder
