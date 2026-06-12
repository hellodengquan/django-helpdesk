from django.conf import settings
import os
import sys
sys.path.insert(0, 'src')
sys.path.insert(0, '.')

settings.configure(
    DEBUG=True,
    TEMPLATES=[{
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'APP_DIRS': True,
        'OPTIONS': {'context_processors': ['django.contrib.auth.context_processors.auth', 'django.template.context_processors.request']},
    }],
    INSTALLED_APPS=['helpdesk', 'django.contrib.contenttypes', 'django.contrib.auth'],
    SECRET_KEY='test',
)
import django
django.setup()

from django.template.loader import get_template

templates_to_check = [
    'helpdesk/ticket_conflict.html',
    'helpdesk/edit_ticket.html',
    'helpdesk/ticket.html',
]
for t in templates_to_check:
    try:
        tmpl = get_template(t)
        print(f'{t} - OK')
    except Exception as e:
        print(f'{t} - ERROR: {e}')
