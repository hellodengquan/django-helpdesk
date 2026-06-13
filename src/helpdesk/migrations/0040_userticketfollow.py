# Generated manually - UserTicketFollow model for ticket following/subscription

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('helpdesk', '0039_alter_ticketchange_field'),
    ]

    operations = [
        migrations.CreateModel(
            name='UserTicketFollow',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created', models.DateTimeField(default=django.utils.timezone.now, help_text='Date when the user started following this ticket.', verbose_name='Created')),
                ('ticket', models.ForeignKey(help_text='Ticket being followed.', on_delete=django.db.models.deletion.CASCADE, related_name='followers', to='helpdesk.ticket', verbose_name='Ticket')),
                ('user', models.ForeignKey(help_text='User who is following this ticket.', on_delete=django.db.models.deletion.CASCADE, related_name='followed_tickets', to=settings.AUTH_USER_MODEL, verbose_name='User')),
            ],
            options={
                'verbose_name': 'Ticket Follow',
                'verbose_name_plural': 'Ticket Follows',
                'ordering': ('-created',),
                'unique_together': {('user', 'ticket')},
            },
        ),
    ]
