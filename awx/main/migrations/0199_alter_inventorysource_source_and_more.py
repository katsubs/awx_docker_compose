# Generated by Django 4.2.10 on 2024-10-22 15:58

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('main', '0198_remove_sso_app_content'),
    ]

    operations = [
        migrations.AlterField(
            model_name='inventorysource',
            name='source',
            field=models.CharField(default=None, max_length=32),
        ),
        migrations.AlterField(
            model_name='inventoryupdate',
            name='source',
            field=models.CharField(default=None, max_length=32),
        ),
    ]
