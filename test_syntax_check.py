import sys, os
sys.path.insert(0, 'src')
sys.path.insert(0, '.')
import django
from django.conf import settings

DIRNAME = os.path.dirname(__file__)
settings.configure(
    DEBUG=True,
    TIME_ZONE='UTC',
    DATABASES={
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': ':memory:',
        }
    },
    INSTALLED_APPS=(
        'django.contrib.admin',
        'django.contrib.auth',
        'django.contrib.contenttypes',
        'django.contrib.humanize',
        'django.contrib.messages',
        'django.contrib.sessions',
        'django.contrib.sites',
        'django.contrib.staticfiles',
        'bootstrap4form',
        'rest_framework',
        'helpdesk',
    ),
    MIDDLEWARE=[
        'django.middleware.security.SecurityMiddleware',
        'django.contrib.sessions.middleware.SessionMiddleware',
        'django.middleware.common.CommonMiddleware',
    ],
    TEMPLATES=[{
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'APP_DIRS': True,
        'OPTIONS': {'context_processors': ['django.contrib.auth.context_processors.auth']},
    }],
    ROOT_URLCONF='helpdesk.urls',
    STATIC_URL='/static/',
    SITE_ID=1,
    SECRET_KEY='test_key_12345',
    HELPDESK_TEAMS_MODEL='auth.User',
    HELPDESK_TEAMS_MIGRATION_DEPENDENCIES=[],
    HELPDESK_KBITEM_TEAM_GETTER=lambda _: None,
)
django.setup()

print("Testing imports...")
from helpdesk.views import staff
print('  staff.py import OK')

from helpdesk.forms import EditTicketForm
print('  forms.py import OK')

from helpdesk.models import Ticket, Queue, FollowUp, TicketChange
print('  models.py import OK')

print("\nTesting conflict detection functions...")
assert hasattr(staff, 'detect_conflict_fields')
print('  detect_conflict_fields exists')

assert hasattr(staff, 'detect_conflicts_simple')
print('  detect_conflicts_simple exists')

assert hasattr(staff, 'get_original_ticket_snapshot')
print('  get_original_ticket_snapshot exists')

assert hasattr(staff, 'SnapshotTicket')
print('  SnapshotTicket class exists')

print("\nTesting EditTicketForm has _last_modified field...")
form = EditTicketForm()
assert '_last_modified' in form.fields
print('  _last_modified field exists in EditTicketForm')
print('  _last_modified is hidden widget:', isinstance(form.fields['_last_modified'].widget, django.forms.HiddenInput))

print("\nAll basic checks passed!")
