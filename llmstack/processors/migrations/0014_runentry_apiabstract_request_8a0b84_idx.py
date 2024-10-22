# Generated by Django 5.1.2 on 2024-10-22 18:27

from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('apiabstractor', '0013_alter_feedback_response_quality'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddIndex(
            model_name='runentry',
            index=models.Index(fields=['request_user_email'], name='apiabstract_request_8a0b84_idx'),
        ),
    ]