"""Add permalink_url to PlatformPost.

Stores the public URL of the published post returned by social platforms.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("composer", "0022_platformpost_analytics_column_defaults"),
    ]

    operations = [
        migrations.AddField(
            model_name="platformpost",
            name="permalink_url",
            field=models.URLField(
                blank=True,
                default="",
                help_text="The post public URL on the platform after publishing.",
                max_length=2000,
            ),
        ),
    ]
