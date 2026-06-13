from django.conf import settings
from django.utils.safestring import mark_safe
from django.utils.translation import activate, get_language
import logging
import os
from smtplib import SMTPException


logger = logging.getLogger("helpdesk")


def _render_file_template(template_name, context, locale):
    """Render email templates from files using Django's template engine.

    Supports full i18n via {% trans %} / {% blocktrans %} tags,
    and strings can be extracted with ``makemessages``.
    """
    from django.template.loader import render_to_string

    old_lang = get_language()
    try:
        activate(locale)
        subject = render_to_string(
            f"helpdesk/emails/{template_name}_subject.txt", context
        ).strip()
        text = render_to_string(f"helpdesk/emails/{template_name}.txt", context)
        if "comment" in context:
            context["comment"] = mark_safe(context["comment"].replace("\r\n", "<br>"))
        html = render_to_string(f"helpdesk/emails/{template_name}.html", context)
        return subject, text, html
    except Exception:
        logger.warning(
            'file template "%s" (locale=%s) could not be rendered',
            template_name,
            locale,
            exc_info=True,
        )
        return None, None, None
    finally:
        activate(old_lang)


def send_templated_mail(
    template_name,
    context,
    recipients,
    sender=None,
    bcc=None,
    fail_silently=False,
    files=None,
    extra_headers=None,
):
    """
    send_templated_mail() is a wrapper around Django's e-mail routines that
    allows us to easily send multipart (text/plain & text/html) e-mails using
    templates that are stored in the database. This lets the admin provide
    both a text and a HTML template for each message.

    template_name is the slug of the template to use for this message (see
        models.EmailTemplate)

    context is a dictionary to be used when rendering the template

    recipients can be either a string, eg 'a@b.com', or a list of strings.

    sender should contain a string, eg 'My Site <me@z.com>'. If you leave it
        blank, it'll use settings.DEFAULT_FROM_EMAIL as a fallback.

    bcc is an optional list of addresses that will receive this message as a
        blind carbon copy.

    fail_silently is passed to Django's mail routine. Set to 'True' to ignore
        any errors at send time.

    files can be a list of tuples. Each tuple should be a filename to attach,
        along with the File objects to be read. files can be blank.

    extra_headers is a dictionary of extra email headers, needed to process
        email replies and keep proper threading.

    """
    from django.core.mail import EmailMultiAlternatives
    from django.template import engines

    from_string = engines["django"].from_string

    from helpdesk.models import EmailTemplate
    from helpdesk.settings import (
        HELPDESK_EMAIL_FALLBACK_LOCALE,
        HELPDESK_EMAIL_SUBJECT_TEMPLATE,
    )

    headers = extra_headers or {}

    locale = context["queue"].get("locale") or HELPDESK_EMAIL_FALLBACK_LOCALE

    use_file_template = False
    subject_part = None
    text_part = None
    html_part = None

    try:
        t = EmailTemplate.objects.get(
            template_name__iexact=template_name, locale=locale
        )
    except EmailTemplate.DoesNotExist:
        try:
            t = EmailTemplate.objects.get(
                template_name__iexact=template_name, locale__isnull=True
            )
        except EmailTemplate.DoesNotExist:
            use_file_template = True

    if use_file_template:
        subject_part, text_part, html_part = _render_file_template(
            template_name, context, locale
        )
        if subject_part is None:
            logger.warning(
                'template "%s" does not exist, no mail sent', template_name
            )
            return
    else:
        subject_part = (
            from_string(HELPDESK_EMAIL_SUBJECT_TEMPLATE % {"subject": t.subject})
            .render(context)
            .replace("\n", "")
            .replace("\r", "")
        )

        footer_file = os.path.join("helpdesk", locale, "email_text_footer.txt")

        text_part = from_string(
            "%s\n\n{%% include '%s' %%}" % (t.plain_text, footer_file)
        ).render(context)

        email_html_base_file = os.path.join("helpdesk", locale, "email_html_base.html")
        if "comment" in context:
            context["comment"] = mark_safe(context["comment"].replace("\r\n", "<br>"))

        html_part = from_string(
            "{%% extends '%s' %%}"
            "{%% block title %%}%s{%% endblock %%}"
            "{%% block content %%}%s{%% endblock %%}"
            % (email_html_base_file, t.heading, t.html)
        ).render(context)

    if isinstance(recipients, str):
        if recipients.find(","):
            recipients = recipients.split(",")
    elif type(recipients) is not list:
        recipients = [recipients]

    msg = EmailMultiAlternatives(
        subject_part,
        text_part,
        sender or settings.DEFAULT_FROM_EMAIL,
        recipients,
        bcc=bcc,
        headers=headers,
    )
    msg.attach_alternative(html_part, "text/html")

    if files:
        for filename, filefield in files:
            filefield.open("rb")
            content = filefield.read()
            msg.attach(filename, content)
            filefield.close()
    logger.debug("Sending email to: {!r}".format(recipients))

    try:
        return msg.send()
    except SMTPException as e:
        logger.exception(
            "SMTPException raised while sending email to {}".format(recipients)
        )
        if not fail_silently:
            raise e
        return 0
