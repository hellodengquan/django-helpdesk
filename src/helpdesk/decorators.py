from django.contrib.auth.decorators import user_passes_test
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect
from functools import wraps
from helpdesk import settings as helpdesk_settings
from helpdesk.user import HelpdeskUser


def is_helpdesk_staff(user):
    return HelpdeskUser(user).is_staff()


def is_helpdesk_superuser(user):
    return HelpdeskUser(user).is_superuser()


helpdesk_staff_member_required = user_passes_test(is_helpdesk_staff)
helpdesk_superuser_required = user_passes_test(is_helpdesk_superuser)


def protect_view(view_func):
    """
    Decorator for protecting the views checking user, redirecting
    to the log-in page if necessary or returning 404 status code
    """

    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        huser = HelpdeskUser(request.user)
        if (
            not huser.is_authenticated()
            and helpdesk_settings.HELPDESK_REDIRECT_TO_LOGIN_BY_DEFAULT
        ):
            return redirect("helpdesk:login")
        elif (
            not huser.is_authenticated()
            and helpdesk_settings.HELPDESK_ANON_ACCESS_RAISES_404
        ):
            raise Http404
        if auth_redirect := helpdesk_settings.HELPDESK_PUBLIC_VIEW_PROTECTOR(request):
            return auth_redirect
        return view_func(request, *args, **kwargs)

    return _wrapped_view


def staff_member_required(view_func):
    """
    Decorator for staff member the views checking user, redirecting
    to the log-in page if necessary or returning 403
    """

    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        huser = HelpdeskUser(request.user)
        if not huser.is_authenticated() and not huser.is_active():
            return redirect("helpdesk:login")
        if not huser.is_staff():
            raise PermissionDenied()
        if auth_redirect := helpdesk_settings.HELPDESK_STAFF_VIEW_PROTECTOR(request):
            return auth_redirect
        return view_func(request, *args, **kwargs)

    return _wrapped_view


def superuser_required(view_func):
    """
    Decorator for superuser member the views checking user, redirecting
    to the log-in page if necessary or returning 403
    """

    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        huser = HelpdeskUser(request.user)
        if not huser.is_authenticated() and not huser.is_active():
            return redirect("helpdesk:login")
        if not huser.is_superuser():
            raise PermissionDenied()
        return view_func(request, *args, **kwargs)

    return _wrapped_view
