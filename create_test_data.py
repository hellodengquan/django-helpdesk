#!/usr/bin/env python
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'demodesk.config.settings')
import django
django.setup()

from django.contrib.auth import get_user_model
from helpdesk.models import Queue, Ticket, FollowUp
from django.utils import timezone
from datetime import timedelta
import random

User = get_user_model()

staff_users = []
for i in range(1, 5):
    username = f'staff{i}'
    if not User.objects.filter(username=username).exists():
        user = User.objects.create_user(
            username=username,
            email=f'staff{i}@example.com',
            password='staff123',
            is_staff=True,
            is_active=True
        )
        print(f'Created user: {username}')
    else:
        user = User.objects.get(username=username)
    staff_users.append(user)

admin_user = User.objects.get(username='admin')
all_staff = staff_users + [admin_user]

queues = Queue.objects.all()
print(f'\nAvailable queues: {list(queues.values_list("title", flat=True))}')

priorities = [1, 2, 3, 4, 5]

for q in queues:
    for i in range(15):
        status = random.choice([1, 1, 1, 2, 3])
        priority = random.choice(priorities)

        assign_to = None
        if random.random() > 0.4:
            assign_to = random.choice(all_staff)

        due_date = None
        if random.random() > 0.3:
            if random.random() > 0.7:
                due_date = timezone.now() - timedelta(hours=random.randint(1, 24))
            else:
                due_date = timezone.now() + timedelta(hours=random.randint(1, 48))

        ticket = Ticket.objects.create(
            title=f'Test Ticket {q.slug}-{i} - Priority {priority}',
            queue=q,
            status=status,
            priority=priority,
            assigned_to=assign_to,
            submitter_email=f'customer{i}@example.com',
            description='This is a test ticket for load balancing testing.',
            due_date=due_date,
        )

        if assign_to and random.random() > 0.5:
            FollowUp.objects.create(
                ticket=ticket,
                date=ticket.created + timedelta(minutes=random.randint(10, 240)),
                title='Initial response',
                comment='Thank you for your inquiry.',
                public=True,
                user=assign_to,
            )

print('\nCreated additional test tickets.')
print('\nCurrent ticket distribution:')
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
