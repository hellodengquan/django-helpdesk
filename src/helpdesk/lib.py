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
    temp_files_to_cleanup = []

    from helpdesk.models import FollowUpAttachment
    from helpdesk.storage import get_multipart_tracker

    tracker = get_multipart_tracker()

    for attached in attached_files:
        if attached.size:
            filename = smart_str(attached.name)

            if hasattr(attached, 'temporary_file_path'):
                temp_files_to_cleanup.append(attached.temporary_file_path())

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
                continue

            try:
                att.save()
            except Exception:
                logger.exception("Failed to save attachment: %s", filename)

                _cleanup_attachment_failure(att, filename, temp_files_to_cleanup)

                continue

            if att.size < max_email_attachment_size:
                attachments.append([filename, att.file])

    _cleanup_temp_files(temp_files_to_cleanup)

    if errors:
        raise ValidationError(list(errors))

    return attachments


def _is_remote_storage_error(exception):
    """Check if an exception is related to remote storage connectivity issues."""
    from helpdesk.storage import RemoteStorageError
    remote_error_types = (
        ConnectionError,
        TimeoutError,
        RemoteStorageError,
    )
    if isinstance(exception, remote_error_types):
        return True
    if isinstance(exception, OSError) and hasattr(exception, 'errno'):
        import errno
        if exception.errno in (errno.ENETUNREACH, errno.ENETDOWN, errno.ETIMEDOUT):
            return True
    return False


def _cleanup_attachment_failure(att, filename, temp_files_to_cleanup):
    """
    Handle cleanup when an attachment save fails, including:
    - Cleaning up local temp files
    - Aborting any pending multipart uploads
    - Cleaning up any orphaned storage files
    """
    import logging
    logger = logging.getLogger("helpdesk")

    try:
        from helpdesk.storage import (
            get_multipart_tracker,
            cleanup_all_pending_multipart_uploads,
        )

        tracker = get_multipart_tracker()

        if hasattr(att, 'file') and att.file and hasattr(att.file, 'storage'):
            storage = att.file.storage
            storage_name = storage.__class__.__name__
            pending_uploads = tracker.get_pending_uploads(storage_name)

            for upload_info in pending_uploads:
                upload_id = upload_info['upload_id']
                try:
                    if hasattr(storage, '_abort_multipart_upload'):
                        storage._abort_multipart_upload(upload_id)
                        logger.info(
                            "Aborted pending multipart upload %s for attachment '%s'",
                            upload_id, filename,
                        )
                except Exception as abort_e:
                    logger.warning(
                        "Failed to abort multipart upload %s for '%s': %s",
                        upload_id, filename, str(abort_e),
                    )

        try:
            cleaned_count = cleanup_all_pending_multipart_uploads()
            if cleaned_count > 0:
                logger.info(
                    "Cleaned up %d pending multipart uploads after attachment '%s' save failure",
                    cleaned_count, filename,
                )
        except Exception as cleanup_e:
            logger.warning(
                "Failed to cleanup pending multipart uploads for '%s': %s",
                filename, str(cleanup_e),
            )

    except Exception as e:
        logger.warning(
            "Error during attachment failure cleanup for '%s': %s",
            filename, str(e),
        )


def _cleanup_temp_files(temp_files):
    """Clean up a list of temporary files."""
    import logging
    import os
    logger = logging.getLogger("helpdesk")

    for file_path in temp_files:
        if file_path and os.path.exists(file_path):
            try:
                os.unlink(file_path)
                logger.info("Cleaned up temporary file '%s'", file_path)
            except OSError as e:
                logger.warning("Failed to clean up temporary file '%s': %s", file_path, str(e))


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
