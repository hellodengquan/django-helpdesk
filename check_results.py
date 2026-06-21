#!/usr/bin/env python
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'demodesk.config.settings')
import django
django.setup()

from django.contrib.auth import get_user_model
from helpdesk.models import Queue, Ticket

User = get_user_model()
admin_user = User.objects.get(username='admin')
staff_users = [User.objects.get(username=f'staff{i}') for i in range(1, 5)]
all_staff = staff_users + [admin_user]

queues = Queue.objects.all()

print('=== Ticket distribution ===')
for q in queues:
    print(f'\nQueue: {q.title}')
    total = Ticket.objects.filter(queue=q).count()
    open_tickets = Ticket.objects.filter(queue=q, status__in=[1, 2]).count()
    unassigned = Ticket.objects.filter(queue=q, status__in=[1, 2], assigned_to__isnull=True).count()
    print(f'  Total: {total}, Open: {open_tickets}, Unassigned: {unassigned}')
    
    print('  Staff load:')
    for user in all_staff:
        count = Ticket.objects.filter(
            queue=q, status__in=[1, 2], assigned_to=user
        ).count()
        if count > 0:
            print(f'    {user.username}: {count} tickets')

print()
print('=== Overall staff load across all queues ===')
for user in all_staff:
    count = Ticket.objects.filter(
        status__in=[1, 2], assigned_to=user
    ).count()
    print(f'  {user.username}: {count} total open tickets')
