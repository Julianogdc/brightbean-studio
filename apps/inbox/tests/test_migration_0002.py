"""Regression for the ``0002_inboxreply_draft_lifecycle`` data migration.

Before 0002 an ``InboxReply`` row existed only once a send had succeeded,
so the backfill must mark every pre-existing row ``sent`` (not the new
``draft`` default) and line its ``created_at`` up with the real send time.
"""

from __future__ import annotations

import importlib
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.inbox.models import InboxMessage, InboxReply
from apps.social_accounts.models import SocialAccount

migration_module = importlib.import_module("apps.inbox.migrations.0002_inboxreply_draft_lifecycle")


@pytest.fixture
def workspace(db, organization):
    from apps.workspaces.models import Workspace

    return Workspace.objects.create(name="Mig WS", organization=organization)


@pytest.fixture
def message(db, workspace):
    account = SocialAccount.objects.create(
        workspace=workspace,
        platform="facebook",
        account_platform_id="page-1",
        account_name="Page",
        oauth_access_token="tok",
    )
    return InboxMessage.objects.create(
        workspace=workspace,
        social_account=account,
        platform_message_id="pm-1",
        message_type=InboxMessage.MessageType.COMMENT,
        sender_name="Ada",
        body="hi?",
        received_at=timezone.now() - timedelta(hours=2),
    )


@pytest.mark.django_db
def test_backfill_marks_existing_replies_sent(message):
    from django.apps import apps as global_apps

    sent_at = timezone.now() - timedelta(hours=1)
    reply = InboxReply.objects.create(inbox_message=message, body="delivered")
    # Simulate a pre-0002 row: it predates the status column and was only
    # ever written post-send.
    InboxReply.objects.filter(pk=reply.pk).update(
        status=InboxReply.Status.DRAFT, sent_at=sent_at, created_at=timezone.now()
    )

    migration_module._mark_existing_sent(global_apps, None)

    reply.refresh_from_db()
    assert reply.status == InboxReply.Status.SENT
    assert reply.created_at == sent_at
