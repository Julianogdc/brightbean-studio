"""Tests for Zafira post_type extension (story and reel).

Covers all 16 required test cases:
1. Instagram + story + 1 image -> 201 -> PlatformPost.platform_extra["post_type"] == "story"
2. Instagram + story + 1 video -> 201 -> post_type story persisted
3. instagram_login + story -> allowed
4. Facebook + story -> rejected (422)
5. Story without media -> rejected (422)
6. Story with multiple media -> rejected (422)
7. Instagram + reel + 1 video -> allowed and persisted as reel
8. instagram_login + reel + 1 video -> allowed
9. Facebook + reel + 1 video -> allowed and persisted as reel
10. Reel with image -> rejected (422)
11. Reel without media -> rejected (422)
12. Reel with multiple media -> rejected (422)
13. Unknown post_type -> HTTP 422 by API schema validation
14. Request without post_type -> previous behavior preserved (no post_type in platform_extra)
15. Draft with post_type -> hint remains persisted for subsequent scheduling
16. Scheduled post with post_type -> hint remains persisted
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.api_keys import services as api_key_services
from apps.composer.models import PlatformPost
from apps.media_library.models import MediaAsset
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.social_accounts.models import SocialAccount
from apps.workspaces.models import Workspace


class _SecureClient(Client):
    def generic(self, method, path, *args, **kwargs):
        kwargs["secure"] = True
        return super().generic(method, path, *args, **kwargs)


@pytest.fixture
def env(db):
    from apps.accounts.models import User

    user = User.objects.create_user(
        email="zafira-test@example.com",
        password="testpassword123",
        name="Zafira Tester",
        tos_accepted_at=timezone.now(),
    )
    org = Organization.objects.create(name="Zafira Org")
    ws = Workspace.objects.create(name="Zafira WS", organization=org)
    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(user=user, workspace=ws, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER)

    # Social accounts for different platforms
    sa_ig = SocialAccount.objects.create(
        workspace=ws,
        platform="instagram",
        account_platform_id="ig-123",
        account_name="IG Test",
        connection_status="connected",
    )
    sa_ig_login = SocialAccount.objects.create(
        workspace=ws,
        platform="instagram_login",
        account_platform_id="ig-login-123",
        account_name="IG Login Test",
        connection_status="connected",
    )
    sa_fb = SocialAccount.objects.create(
        workspace=ws,
        platform="facebook",
        account_platform_id="fb-123",
        account_name="FB Test",
        connection_status="connected",
    )
    sa_li = SocialAccount.objects.create(
        workspace=ws,
        platform="linkedin_personal",
        account_platform_id="li-123",
        account_name="LI Test",
        connection_status="connected",
    )

    # Media assets
    img_asset = MediaAsset.objects.create(
        organization=org,
        workspace=ws,
        filename="photo.jpg",
        media_type=MediaAsset.MediaType.IMAGE,
        file_size=1024,
        processing_status=MediaAsset.ProcessingStatus.COMPLETED,
    )
    img_asset_2 = MediaAsset.objects.create(
        organization=org,
        workspace=ws,
        filename="photo2.jpg",
        media_type=MediaAsset.MediaType.IMAGE,
        file_size=1024,
        processing_status=MediaAsset.ProcessingStatus.COMPLETED,
    )
    vid_asset = MediaAsset.objects.create(
        organization=org,
        workspace=ws,
        filename="video.mp4",
        media_type=MediaAsset.MediaType.VIDEO,
        file_size=2048,
        processing_status=MediaAsset.ProcessingStatus.COMPLETED,
    )
    vid_asset_2 = MediaAsset.objects.create(
        organization=org,
        workspace=ws,
        filename="video2.mp4",
        media_type=MediaAsset.MediaType.VIDEO,
        file_size=2048,
        processing_status=MediaAsset.ProcessingStatus.COMPLETED,
    )

    api_key = api_key_services.issue_api_key(
        workspace=ws,
        social_accounts=[sa_ig, sa_ig_login, sa_fb, sa_li],
        issued_by=user,
        name="zafira-key",
        permissions=list(PERMISSION_KEYS),
    )
    client = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {api_key.plaintext_token}")

    return {
        "user": user,
        "org": org,
        "ws": ws,
        "sa_ig": sa_ig,
        "sa_ig_login": sa_ig_login,
        "sa_fb": sa_fb,
        "sa_li": sa_li,
        "img_asset": img_asset,
        "img_asset_2": img_asset_2,
        "vid_asset": vid_asset,
        "vid_asset_2": vid_asset_2,
        "client": client,
    }


@pytest.mark.django_db
class TestZafiraPostTypes:
    # 1. Instagram + story + 1 image -> 201 -> PlatformPost.platform_extra["post_type"] == "story"
    def test_instagram_story_image_creates_post_and_persists_hint(self, env):
        client = env["client"]
        payload = {
            "social_account_id": str(env["sa_ig"].id),
            "caption": "Instagram Story Image",
            "media_asset_ids": [str(env["img_asset"].id)],
            "action": "draft",
            "post_type": "story",
        }
        res = client.post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")
        assert res.status_code == 201, res.content
        data = res.json()
        pp = PlatformPost.objects.get(post_id=data["id"])
        assert pp.platform_extra.get("post_type") == "story"

    # 2. Instagram + story + 1 video -> 201 -> post_type story persisted
    def test_instagram_story_video_creates_post_and_persists_hint(self, env):
        client = env["client"]
        payload = {
            "social_account_id": str(env["sa_ig"].id),
            "caption": "Instagram Story Video",
            "media_asset_ids": [str(env["vid_asset"].id)],
            "action": "draft",
            "post_type": "story",
        }
        res = client.post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")
        assert res.status_code == 201, res.content
        data = res.json()
        pp = PlatformPost.objects.get(post_id=data["id"])
        assert pp.platform_extra.get("post_type") == "story"

    # 3. instagram_login + story -> allowed
    def test_instagram_login_story_allowed(self, env):
        client = env["client"]
        payload = {
            "social_account_id": str(env["sa_ig_login"].id),
            "caption": "Instagram Login Story",
            "media_asset_ids": [str(env["img_asset"].id)],
            "action": "draft",
            "post_type": "story",
        }
        res = client.post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")
        assert res.status_code == 201, res.content
        data = res.json()
        pp = PlatformPost.objects.get(post_id=data["id"])
        assert pp.platform_extra.get("post_type") == "story"

    # 4. Facebook + story -> rejected
    def test_facebook_story_rejected(self, env):
        client = env["client"]
        payload = {
            "social_account_id": str(env["sa_fb"].id),
            "caption": "FB Story",
            "media_asset_ids": [str(env["img_asset"].id)],
            "action": "draft",
            "post_type": "story",
        }
        res = client.post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")
        assert res.status_code == 422

    # 5. Story without media -> rejected
    def test_story_without_media_rejected(self, env):
        client = env["client"]
        payload = {
            "social_account_id": str(env["sa_ig"].id),
            "caption": "Story without media",
            "media_asset_ids": [],
            "action": "draft",
            "post_type": "story",
        }
        res = client.post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")
        assert res.status_code == 422

    # 6. Story with multiple media -> rejected
    def test_story_with_multiple_media_rejected(self, env):
        client = env["client"]
        payload = {
            "social_account_id": str(env["sa_ig"].id),
            "caption": "Story multiple media",
            "media_asset_ids": [str(env["img_asset"].id), str(env["img_asset_2"].id)],
            "action": "draft",
            "post_type": "story",
        }
        res = client.post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")
        assert res.status_code == 422

    # 7. Instagram + reel + 1 video -> allowed and persisted as reel
    def test_instagram_reel_video_creates_post_and_persists_hint(self, env):
        client = env["client"]
        payload = {
            "social_account_id": str(env["sa_ig"].id),
            "caption": "Instagram Reel Video",
            "media_asset_ids": [str(env["vid_asset"].id)],
            "action": "draft",
            "post_type": "reel",
        }
        res = client.post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")
        assert res.status_code == 201, res.content
        data = res.json()
        pp = PlatformPost.objects.get(post_id=data["id"])
        assert pp.platform_extra.get("post_type") == "reel"

    # 8. instagram_login + reel + 1 video -> allowed
    def test_instagram_login_reel_video_allowed(self, env):
        client = env["client"]
        payload = {
            "social_account_id": str(env["sa_ig_login"].id),
            "caption": "IG Login Reel",
            "media_asset_ids": [str(env["vid_asset"].id)],
            "action": "draft",
            "post_type": "reel",
        }
        res = client.post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")
        assert res.status_code == 201, res.content
        data = res.json()
        pp = PlatformPost.objects.get(post_id=data["id"])
        assert pp.platform_extra.get("post_type") == "reel"

    # 9. Facebook + reel + 1 video -> allowed and persisted as reel
    def test_facebook_reel_video_creates_post_and_persists_hint(self, env):
        client = env["client"]
        payload = {
            "social_account_id": str(env["sa_fb"].id),
            "caption": "Facebook Reel Video",
            "media_asset_ids": [str(env["vid_asset"].id)],
            "action": "draft",
            "post_type": "reel",
        }
        res = client.post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")
        assert res.status_code == 201, res.content
        data = res.json()
        pp = PlatformPost.objects.get(post_id=data["id"])
        assert pp.platform_extra.get("post_type") == "reel"

    # 10. Reel with image -> rejected
    def test_reel_with_image_rejected(self, env):
        client = env["client"]
        payload = {
            "social_account_id": str(env["sa_ig"].id),
            "caption": "Reel with image",
            "media_asset_ids": [str(env["img_asset"].id)],
            "action": "draft",
            "post_type": "reel",
        }
        res = client.post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")
        assert res.status_code == 422

    # 11. Reel without media -> rejected
    def test_reel_without_media_rejected(self, env):
        client = env["client"]
        payload = {
            "social_account_id": str(env["sa_ig"].id),
            "caption": "Reel without media",
            "media_asset_ids": [],
            "action": "draft",
            "post_type": "reel",
        }
        res = client.post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")
        assert res.status_code == 422

    # 12. Reel with multiple media -> rejected
    def test_reel_with_multiple_media_rejected(self, env):
        client = env["client"]
        payload = {
            "social_account_id": str(env["sa_ig"].id),
            "caption": "Reel multiple media",
            "media_asset_ids": [str(env["vid_asset"].id), str(env["vid_asset_2"].id)],
            "action": "draft",
            "post_type": "reel",
        }
        res = client.post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")
        assert res.status_code == 422

    # 13. post_type unknown -> HTTP 422 by API
    def test_unknown_post_type_returns_422(self, env):
        client = env["client"]
        payload = {
            "social_account_id": str(env["sa_ig"].id),
            "caption": "Unknown post type",
            "media_asset_ids": [str(env["img_asset"].id)],
            "action": "draft",
            "post_type": "tweet",
        }
        res = client.post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")
        assert res.status_code == 422

    # 14. request WITHOUT post_type -> previous behavior preserved
    def test_request_without_post_type_preserves_legacy_behavior(self, env):
        client = env["client"]
        payload = {
            "social_account_id": str(env["sa_ig"].id),
            "caption": "Standard feed post",
            "media_asset_ids": [str(env["img_asset"].id)],
            "action": "draft",
        }
        res = client.post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")
        assert res.status_code == 201, res.content
        data = res.json()
        pp = PlatformPost.objects.get(post_id=data["id"])
        assert "post_type" not in pp.platform_extra

    # 15. draft with post_type -> hint remains persisted for subsequent scheduling
    def test_draft_with_post_type_persists_hint(self, env):
        client = env["client"]
        payload = {
            "social_account_id": str(env["sa_ig"].id),
            "caption": "Draft hint test",
            "media_asset_ids": [str(env["vid_asset"].id)],
            "action": "draft",
            "post_type": "reel",
        }
        res = client.post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")
        assert res.status_code == 201
        data = res.json()
        pp = PlatformPost.objects.get(post_id=data["id"])
        assert pp.status == "draft"
        assert pp.platform_extra.get("post_type") == "reel"

    # 16. scheduled post with post_type -> hint remains persisted
    def test_scheduled_post_with_post_type_persists_hint(self, env):
        client = env["client"]
        future_time = (timezone.now() + timedelta(days=2)).isoformat()
        payload = {
            "social_account_id": str(env["sa_ig"].id),
            "caption": "Scheduled hint test",
            "media_asset_ids": [str(env["vid_asset"].id)],
            "action": "schedule",
            "scheduled_at": future_time,
            "post_type": "reel",
        }
        res = client.post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")
        assert res.status_code == 201, res.content
        data = res.json()
        pp = PlatformPost.objects.get(post_id=data["id"])
        assert pp.status == "scheduled"
        assert pp.platform_extra.get("post_type") == "reel"
