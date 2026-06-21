#!/usr/bin/env python
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'demodesk.config.settings')
import django
django.setup()

from django.contrib.auth import get_user_model
from django.utils import timezone
from helpdesk.models import Queue, Ticket, FollowUp
from helpdesk.views.staff import (
    _get_queue_active_staff,
    _redistribute_round_robin,
    _redistribute_sla_priority,
)
from helpdesk.user import HelpdeskUser

User = get_user_model()

def print_load_state(stage_label):
    print(f'\n=== {stage_label} ===')
    admin_user = User.objects.get(username='admin')
    staff_users = [User.objects.get(username=f'staff{i}') for i in range(1, 5)]
    all_staff = staff_users + [admin_user]
    queues = Queue.objects.all()

    for q in queues:
        print(f'\nQueue: {q.title}')
        open_tickets = Ticket.objects.filter(queue=q, status__in=[1, 2])
        unassigned = open_tickets.filter(assigned_to__isnull=True).count()
        print(f'  Open: {open_tickets.count()}, Unassigned: {unassigned}')
        
        print('  Staff load:')
        for user in all_staff:
            count = open_tickets.filter(assigned_to=user).count()
            if count > 0 or user == admin_user:
                print(f'    {user.username}: {count} tickets')

    print('\n  Overall load:')
    for user in all_staff:
        total = Ticket.objects.filter(
            status__in=[1, 2], assigned_to=user
        ).count()
        print(f'    {user.username}: {total} total')

print_load_state('BEFORE Round-Robin Redistribution')

admin_user = User.objects.get(username='admin')
queues = Queue.objects.all()

huser = HelpdeskUser(admin_user)
user_queues = huser.get_queues()

all_active_staff_set = set()
for q in user_queues:
    for staff in _get_queue_active_staff(q):
        all_active_staff_set.add(staff)
all_active_staff = list(all_active_staff_set)
print(f'\nActive staff available: {[s.username for s in all_active_staff]}')

tickets_to_redistribute = Ticket.objects.filter(
    queue__in=user_queues,
    status__in=Ticket.OPEN_STATUSES,
    on_hold=False,
)
unassigned_tickets = tickets_to_redistribute.filter(assigned_to__isnull=True)
assigned_tickets = tickets_to_redistribute.filter(assigned_to__isnull=False)
print(f'Tickets to redistribute: {tickets_to_redistribute.count()}')
print(f'  - Unassigned: {unassigned_tickets.count()}')
print(f'  - Assigned: {assigned_tickets.count()}')

class MockRequest:
    user = admin_user

request = MockRequest()

reassigned_count = _redistribute_round_robin(
    request,
    unassigned_tickets,
    assigned_tickets,
    all_active_staff,
    user_queues,
)

print(f'\nRound-Robin redistribution complete! Reassigned: {reassigned_count} tickets')

print_load_state('AFTER Round-Robin Redistribution')

print(f'\n=== Testing SLA Priority Strategy ===')
reassigned_count = _redistribute_sla_priority(
    request,
    unassigned_tickets,
    assigned_tickets,
    all_active_staff,
    user_queues,
)

print(f'\nSLA Priority redistribution complete! Reassigned: {reassigned_count} tickets')

print_load_state('AFTER SLA Priority Redistribution')

print('\n=== Testing followups created correctly ===')
recent_followups = FollowUp.objects.filter(
    title__icontains='load balancing'
).order_by('-date')[:5]
print(f'Recent load-balancing follow-ups: {recent_followups.count()}')
for fu in recent_followups:
    print(f'  - [{fu.ticket.ticket_for_url}] {fu.title}')
