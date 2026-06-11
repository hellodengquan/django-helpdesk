from django.apps import AppConfig


class HelpdeskConfig(AppConfig):
    name = "helpdesk"
    verbose_name = "Helpdesk"
    # for Django 3.2 support:
    # see:
    # https://docs.djangoproject.com/en/3.2/ref/applications/#django.apps.AppConfig.default_auto_field
    default_auto_field = "django.db.models.AutoField"

    def ready(self):
        from . import webhooks  # noqa: F401

        from django.db.models.signals import post_delete
        from .models import (
            FollowUpAttachment,
            KBIAttachment,
            _delete_attachment_file,
        )

        post_delete.connect(
            _delete_attachment_file,
            sender=FollowUpAttachment,
            weak=False,
        )
        post_delete.connect(
            _delete_attachment_file,
            sender=KBIAttachment,
            weak=False,
        )
