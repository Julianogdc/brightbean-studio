"""Give ``InboxReply`` a draft → sent/failed lifecycle.

Before this, an ``InboxReply`` row was written only *after* the platform
accepted the reply, so ``sent_at`` could be ``auto_now_add`` and every
row implicitly meant "delivered". Draft replies (created by an agent, or
saved from the composer for later) need a row that exists before any
send, so we add an explicit ``status`` plus ``created_at`` / ``updated_at``
and make ``sent_at`` nullable. Every pre-existing row is a delivered
reply, so it is backfilled to ``sent``.
"""

import django.utils.timezone
from django.db import migrations, models


def _mark_existing_sent(apps, schema_editor):
    InboxReply = apps.get_model("inbox", "InboxReply")
    InboxReply.objects.all().update(status="sent")
    # ``created_at`` got a flat default at column-add time; line it up with
    # the real send time where we have one so ordering stays sensible.
    for reply in InboxReply.objects.exclude(sent_at=None).iterator():
        InboxReply.objects.filter(pk=reply.pk).update(created_at=reply.sent_at)


def _noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("inbox", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="inboxreply",
            name="status",
            field=models.CharField(
                choices=[("draft", "Draft"), ("sent", "Sent"), ("failed", "Failed")],
                db_index=True,
                default="draft",
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name="inboxreply",
            name="send_error",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="inboxreply",
            name="created_at",
            field=models.DateTimeField(
                auto_now_add=True,
                default=django.utils.timezone.now,
            ),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="inboxreply",
            name="updated_at",
            field=models.DateTimeField(auto_now=True, default=django.utils.timezone.now),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="inboxreply",
            name="sent_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterModelOptions(
            name="inboxreply",
            options={"ordering": ["created_at"]},
        ),
        migrations.RunPython(_mark_existing_sent, _noop),
    ]
