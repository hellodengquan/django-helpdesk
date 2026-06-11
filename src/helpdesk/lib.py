"""
django-helpdesk - A Django powered ticket tracker for small enterprise.

(c) Copyright 2008 Jutda. All Rights Reserved. See LICENSE for details.

lib.py - Common functions (eg multipart e-mail)
"""

import logging
import mimetypes
from datetime import date, datetime, time
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError, ImproperlyConfigured
from django.db.models.query import QuerySet
from django.utils.encoding import smart_str
from helpdesk import settings as helpdesk_settings


logger = logging.getLogger("helpdesk")

User = get_user_model()


def ticket_template_context(ticket):
    context = {}

    for field in (
        "title",
        "created",
        "modified",
        "submitter_email",
        "status",
        "get_status_display",
        "on_hold",
        "description",
        "resolution",
        "priority",
        "get_priority_display",
        "last_escalation",
        "ticket",
        "ticket_for_url",
        "merged_to",
        "get_status",
        "ticket_url",
        "staff_url",
        "_get_assigned_to",
    ):
        attr = getattr(ticket, field, None)
        if callable(attr):
            context[field] = "%s" % attr()
        else:
            context[field] = attr
    context["assigned_to"] = context["_get_assigned_to"]

    return context


def queue_template_context(queue):
    context = {}

    for field in ("title", "slug", "email_address", "from_address", "locale"):
        attr = getattr(queue, field, None)
        if callable(attr):
            context[field] = attr()
        else:
            context[field] = attr

    return context


def safe_template_context(ticket):
    """
    Return a dictionary that can be used as a template context to render
    comments and other details with ticket or queue parameters. Note that
    we don't just provide the Ticket & Queue objects to the template as
    they could reveal confidential information. Just imagine these two options:
        * {{ ticket.queue.email_box_password }}
        * {{ ticket.assigned_to.password }}

    Ouch!

    The downside to this is that if we make changes to the model, we will also
    have to update this code. Perhaps we can find a better way in the future.
    """

    context = {
        "queue": queue_template_context(ticket.queue),
        "ticket": ticket_template_context(ticket),
    }
    context["ticket"]["queue"] = context["queue"]

    return context


def text_is_spam(text, request):
    # Based on a blog post by 'sciyoshi':
    # http://sciyoshi.com/blog/2008/aug/27/using-akismet-djangos-new-comments-framework/
    # This will return 'True' is the given text is deemed to be spam, or
    # False if it is not spam. If it cannot be checked for some reason, we
    # assume it isn't spam.
    try:
        from akismet import Akismet
    except ImportError:
        return False
    from django.contrib.sites.models import Site
    from django.core.exceptions import ImproperlyConfigured

    try:
        site = Site.objects.get_current()
    except ImproperlyConfigured:
        site = Site(domain="configure-django-sites.com")

    # see https://akismet.readthedocs.io/en/latest/overview.html#using-akismet

    apikey = None

    if hasattr(settings, "TYPEPAD_ANTISPAM_API_KEY"):
        apikey = settings.TYPEPAD_ANTISPAM_API_KEY
    elif hasattr(settings, "PYTHON_AKISMET_API_KEY"):
        # new env var expected by python-akismet package
        apikey = settings.PYTHON_AKISMET_API_KEY
    elif hasattr(settings, "AKISMET_API_KEY"):
        # deprecated, but kept for backward compatibility
        apikey = settings.AKISMET_API_KEY
    else:
        return False

    ak = Akismet(
        blog_url="http://%s/" % site.domain,
        key=apikey,
    )

    if hasattr(settings, "TYPEPAD_ANTISPAM_API_KEY"):
        ak.baseurl = "api.antispam.typepad.com/1.1/"

    if ak.verify_key():
        ak_data = {
            "user_ip": request.META.get("REMOTE_ADDR", "127.0.0.1"),
            "user_agent": request.headers.get("User-Agent", ""),
            "referrer": request.headers.get("Referer", ""),
            "comment_type": "comment",
            "comment_author": "",
        }

        return ak.comment_check(smart_str(text), data=ak_data)

    return False


def process_attachments(followup, attached_files):
    max_email_attachment_size = getattr(
        settings, "HELPDESK_MAX_EMAIL_ATTACHMENT_SIZE", 512000
    )
    attachments = []
    errors = set()

    for attached in attached_files:
        if attached.size:
            from helpdesk.models import FollowUpAttachment

            filename = smart_str(attached.name)
            att = FollowUpAttachment(
                followup=followup,
                file=attached,
                filename=filename,
                mime_type=attached.content_type
                or mimetypes.guess_type(filename, strict=False)[0]
                or "application/octet-stream",
                size=attached.size,
            )
            try:
                att.full_clean()
            except ValidationError as e:
                errors.add(e)
            else:
                att.save()

                if attached.size < max_email_attachment_size:
                    # Only files smaller than 512kb (or as defined in
                    # settings.HELPDESK_MAX_EMAIL_ATTACHMENT_SIZE) are sent via
                    # email.
                    attachments.append([filename, att.file])

    if errors:
        raise ValidationError(list(errors))

    return attachments


def format_time_spent(time_spent):
    """Format time_spent attribute to "[H]HHh:MMm" text string to be allign in
    all graphical outputs
    """
    if time_spent:
        time_spent = "{0:02d}h:{1:02d}m".format(
            int(time_spent.total_seconds()) // 3600,
            int(time_spent.total_seconds()) % 3600 // 60,
        )
    else:
        time_spent = ""
    return time_spent


def convert_value(value):
    """Convert date/time data type to known fixed format string"""
    if type(value) is datetime:
        return value.strftime(helpdesk_settings.CUSTOMFIELD_DATETIME_FORMAT)
    elif type(value) is date:
        return value.strftime(helpdesk_settings.CUSTOMFIELD_DATE_FORMAT)
    elif type(value) is time:
        return value.strftime(helpdesk_settings.CUSTOMFIELD_TIME_FORMAT)
    else:
        return value


def daily_time_spent_calculation(earliest, latest, open_hours):
    """Returns the number of seconds for a single day time interval according to open hours."""

    time_spent_seconds = 0

    # avoid rendering day in different locale
    weekday = (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    )[earliest.weekday()]

    # enforce correct settings
    MIDNIGHT = 23.9999
    start, end = open_hours.get(weekday, (0, MIDNIGHT))
    if not 0 <= start <= end <= MIDNIGHT:
        raise ImproperlyConfigured(
            "HELPDESK_FOLLOWUP_TIME_SPENT_OPENING_HOURS"
            f" setting for {weekday} out of (0, 23.9999) boundary"
        )

    # transform decimals to minutes and seconds
    start_hour, start_minute, start_second = (
        int(start),
        int(start % 1 * 60),
        int(start * 60 % 1 * 60),
    )
    end_hour, end_minute, end_second = (
        int(end),
        int(end % 1 * 60),
        int(end * 60 % 1 * 60),
    )

    # translate time for delta calculation
    earliest_f = earliest.hour + earliest.minute / 60 + earliest.second / 3600
    latest_f = (
        latest.hour
        + latest.minute / 60
        + latest.second / (60 * 60)
        + latest.microsecond / (60 * 60 * 999999)
    )

    # if latest time is midnight and close hour is midnight, add a second to the time spent
    if latest_f >= MIDNIGHT and end == MIDNIGHT:
        time_spent_seconds += 1

    if earliest_f < start:
        earliest = earliest.replace(
            hour=start_hour, minute=start_minute, second=start_second
        )
    elif earliest_f >= end:
        earliest = earliest.replace(hour=end_hour, minute=end_minute, second=end_second)

    if latest_f < start:
        latest = latest.replace(
            hour=start_hour, minute=start_minute, second=start_second
        )
    elif latest_f >= end:
        latest = latest.replace(hour=end_hour, minute=end_minute, second=end_second)

    day_delta = latest - earliest
    time_spent_seconds += day_delta.seconds

    return time_spent_seconds


def get_assignable_users(filter_staff: bool) -> QuerySet:
    users = User.objects.filter(is_active=True)

    if filter_staff:
        users = users.filter(is_staff=True)

    return users.order_by(User.USERNAME_FIELD)


def user_template_context(user):
    """Create a safe template context for a user object."""
    context = {}
    if not user or not user.is_authenticated:
        return context

    for field in ("username", "email", "first_name", "last_name", "is_staff", "is_superuser"):
        attr = getattr(user, field, None)
        if not callable(attr):
            context[field] = attr

    context["get_full_name"] = user.get_full_name() if hasattr(user, "get_full_name") else user.username
    context["get_short_name"] = user.get_short_name() if hasattr(user, "get_short_name") else user.username

    return context


def submitter_template_context(ticket):
    """Create a template context for the ticket submitter."""
    context = {}

    context["email"] = ticket.submitter_email

    if ticket.submitter_email:
        try:
            submitter_user = User.objects.get(email=ticket.submitter_email)
            context["name"] = submitter_user.get_full_name() or submitter_user.username
            context["username"] = submitter_user.username
            context["first_name"] = submitter_user.first_name
            context["last_name"] = submitter_user.last_name
        except (User.DoesNotExist, User.MultipleObjectsReturned):
            context["name"] = ticket.submitter_email.split("@")[0]
            context["username"] = None
            context["first_name"] = None
            context["last_name"] = None
    else:
        context["name"] = ""
        context["username"] = None
        context["first_name"] = None
        context["last_name"] = None

    return context


def macro_template_context(ticket, user=None):
    """
    Build a comprehensive template context for rendering macros.
    Includes ticket, queue, user, and submitter contexts plus
    convenience variables for easy access.
    """
    from helpdesk.models import TicketCustomFieldValue

    context = safe_template_context(ticket)

    if user:
        context["user"] = user_template_context(user)

    context["submitter"] = submitter_template_context(ticket)

    context["submitter_name"] = context["submitter"]["name"]
    context["submitter_email"] = ticket.submitter_email
    context["ticket_id"] = ticket.id
    context["ticket_title"] = ticket.title
    context["queue_name"] = ticket.queue.title
    context["queue_email"] = ticket.queue.email_address

    custom_fields = {}
    try:
        for cfv in TicketCustomFieldValue.objects.filter(ticket=ticket).select_related("field"):
            custom_fields[cfv.field.name] = cfv.value
    except Exception:
        pass
    context["custom_fields"] = custom_fields

    return context


def render_macro(macro_body, ticket, user=None, extra_context=None):
    """
    Render a macro template body with the given ticket and user context.

    Uses Django's template engine for safe variable substitution.
    Returns the rendered string.

    Args:
        macro_body: The template string with {{ variable }} placeholders
        ticket: The Ticket object to use for context
        user: Optional User object (the current user)
        extra_context: Optional dict of additional context variables

    Returns:
        str: The rendered template string
    """
    from django.template import Template, Context, TemplateDoesNotExist
    from django.template.engine import Engine
    from django.utils.safestring import mark_safe

    context = macro_template_context(ticket, user)

    if extra_context:
        context.update(extra_context)

    try:
        template = Template(macro_body, engine=Engine.get_default())
    except Exception:
        return macro_body

    try:
        rendered = template.render(Context(context))
    except Exception:
        return macro_body

    return rendered


def get_available_macros_for_user(user, queue=None, include_shared=True, include_personal=True):
    """
    Get all macros available to a user, optionally filtered by queue.

    Args:
        user: The user to check permissions for
        queue: Optional Queue object to filter by
        include_shared: Whether to include shared macros
        include_personal: Whether to include personal macros

    Returns:
        QuerySet of Macro objects
    """
    from helpdesk.models import Macro
    from django.db.models import Q

    if not user or not user.is_authenticated:
        return Macro.objects.none()

    query = Q(status=Macro.ACTIVE_STATUS)

    conditions = []
    if include_shared:
        conditions.append(Q(is_shared=True))
    if include_personal:
        conditions.append(Q(author=user, is_shared=False))

    if not conditions:
        return Macro.objects.none()

    query &= Q(*conditions, _connector=Q.OR)

    macros = Macro.objects.filter(query).distinct().order_by("-is_shared", "name")

    if queue is not None:
        macros = macros.filter(Q(queues=queue) | Q(queues=None)).distinct()

    return macros


def can_use_macro(macro, user, ticket=None):
    """
    Check if a user can use a specific macro, optionally for a specific ticket.

    This is a convenience wrapper around macro.can_use() that handles
    None macros gracefully.

    Args:
        macro: The Macro object (can be None)
        user: The user to check
        ticket: Optional Ticket object for queue-based permission

    Returns:
        bool: True if the user can use the macro
    """
    if macro is None:
        return False
    return macro.can_use(user, ticket)

