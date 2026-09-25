"""Tests for GET /api/v1/posts/ list endpoint and Zafira Hub extensions.

Covers test requirements A through J:
A. GET /posts/ retorna somente posts do workspace correto.
B. API key allowlist conta A NÃO vê post da conta B.
C. Post misto com filho de conta estrangeira NÃO é retornado.
D. social_account_id fora da allowlist falha fechado.
E. limit/offset funcionam.
F. media_assets retornam: id, url, mime_type, media_type, position.
G. post_type story/reel é retornado do platform_extra.
H. permalink_url é persistido após publicação síncrona.
I. post existente por ID continua funcionando.
J. Nenhum secret/token aparece na resposta.
"""

from __future__ import annotations

import json
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from apps.api_keys import services as api_key_services
from apps.composer.models import PlatformPost, Post
from apps.composer.services import create_post
from apps.media_library.models import MediaAsset
from apps.members.models import PERMISSION_KEYS, OrgMembership, WorkspaceMembership
from apps.organizations.models import Organization
from apps.publisher.engine import PublishEngine
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
        email="zafira-list@example.com",
        password="testpassword123",
        name="Zafira Tester",
        tos_accepted_at=timezone.now(),
    )
    org = Organization.objects.create(name="Zafira Org")
    ws1 = Workspace.objects.create(name="Zafira WS 1", organization=org)
    ws2 = Workspace.objects.create(name="Zafira WS 2", organization=org)

    OrgMembership.objects.create(user=user, organization=org, org_role=OrgMembership.OrgRole.OWNER)
    WorkspaceMembership.objects.create(user=user, workspace=ws1, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER)
    WorkspaceMembership.objects.create(user=user, workspace=ws2, workspace_role=WorkspaceMembership.WorkspaceRole.OWNER)

    # Social Accounts in WS1
    sa_a = SocialAccount.objects.create(
        workspace=ws1,
        platform="instagram",
        account_platform_id="ig-a",
        account_name="Account A",
        connection_status="connected",
        oauth_access_token="super_secret_token_123",
    )
    sa_b = SocialAccount.objects.create(
        workspace=ws1,
        platform="instagram",
        account_platform_id="ig-b",
        account_name="Account B",
        connection_status="connected",
        oauth_access_token="super_secret_token_456",
    )

    # Social Account in WS2
    sa_ws2 = SocialAccount.objects.create(
        workspace=ws2,
        platform="instagram",
        account_platform_id="ig-ws2",
        account_name="Account WS2",
        connection_status="connected",
    )

    # Media assets
    dummy_file = SimpleUploadedFile("sample.jpg", b"fake_jpeg_content", content_type="image/jpeg")
    img_asset = MediaAsset.objects.create(
        organization=org,
        workspace=ws1,
        file=dummy_file,
        filename="sample.jpg",
        media_type=MediaAsset.MediaType.IMAGE,
        mime_type="image/jpeg",
        file_size=1024,
        processing_status=MediaAsset.ProcessingStatus.COMPLETED,
    )

    # API key scoped to ONLY sa_a in ws1
    api_key_a = api_key_services.issue_api_key(
        workspace=ws1,
        social_accounts=[sa_a],
        issued_by=user,
        name="zafira-key-a",
        permissions=list(PERMISSION_KEYS),
    )
    client_a = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {api_key_a.plaintext_token}")

    # API key scoped to both sa_a and sa_b in ws1
    api_key_all = api_key_services.issue_api_key(
        workspace=ws1,
        social_accounts=[sa_a, sa_b],
        issued_by=user,
        name="zafira-key-all",
        permissions=list(PERMISSION_KEYS),
    )
    client_all = _SecureClient(HTTP_AUTHORIZATION=f"Bearer {api_key_all.plaintext_token}")

    return {
        "user": user,
        "org": org,
        "ws1": ws1,
        "ws2": ws2,
        "sa_a": sa_a,
        "sa_b": sa_b,
        "sa_ws2": sa_ws2,
        "img_asset": img_asset,
        "client_a": client_a,
        "client_all": client_all,
        "api_key_a": api_key_a,
    }


@pytest.mark.django_db
class TestZafiraPostsListAndExtensions:
    # A. GET /posts/ retorna somente posts do workspace correto.
    def test_a_workspace_isolation(self, env):
        # Create post in WS1
        p_ws1 = create_post(
            workspace=env["ws1"],
            social_account=env["sa_a"],
            caption="Post in WS1",
            author=env["user"],
        )
        # Create post in WS2
        p_ws2 = create_post(
            workspace=env["ws2"],
            social_account=env["sa_ws2"],
            caption="Post in WS2",
            author=env["user"],
        )

        res = env["client_all"].get("/api/v1/posts/")
        assert res.status_code == 200
        data = res.json()
        item_ids = [item["id"] for item in data["items"]]
        assert str(p_ws1.id) in item_ids
        assert str(p_ws2.id) not in item_ids

    # B. API key allowlist conta A NÃO vê post da conta B.
    def test_b_allowlist_isolation_account_b_hidden(self, env):
        p_a = create_post(
            workspace=env["ws1"],
            social_account=env["sa_a"],
            caption="Post Account A",
            author=env["user"],
        )
        p_b = create_post(
            workspace=env["ws1"],
            social_account=env["sa_b"],
            caption="Post Account B",
            author=env["user"],
        )

        res = env["client_a"].get("/api/v1/posts/")
        assert res.status_code == 200
        data = res.json()
        item_ids = [item["id"] for item in data["items"]]
        assert str(p_a.id) in item_ids
        assert str(p_b.id) not in item_ids

    # C. Post misto com filho de conta estrangeira NÃO é retornado.
    def test_c_mixed_post_with_foreign_child_hidden(self, env):
        # Post that has platform_posts for both sa_a and sa_b
        p_mixed = Post.objects.create(
            workspace=env["ws1"],
            caption="Mixed Post",
            author=env["user"],
        )
        PlatformPost.objects.create(
            post=p_mixed,
            social_account=env["sa_a"],
            status="draft",
        )
        PlatformPost.objects.create(
            post=p_mixed,
            social_account=env["sa_b"],
            status="draft",
        )

        # client_a only allows sa_a. Because sa_b is also present, it must be excluded.
        res = env["client_a"].get("/api/v1/posts/")
        assert res.status_code == 200
        data = res.json()
        item_ids = [item["id"] for item in data["items"]]
        assert str(p_mixed.id) not in item_ids

        # client_all allows both sa_a and sa_b, so it sees p_mixed
        res_all = env["client_all"].get("/api/v1/posts/")
        assert res_all.status_code == 200
        all_ids = [item["id"] for item in res_all.json()["items"]]
        assert str(p_mixed.id) in all_ids

    # D. social_account_id fora da allowlist falha fechado.
    def test_d_social_account_id_outside_allowlist_fails_closed(self, env):
        # sa_b is not in client_a allowlist
        res = env["client_a"].get(f"/api/v1/posts/?social_account_id={env['sa_b'].id}")
        assert res.status_code in (403, 404)

        # random UUID
        res_rand = env["client_a"].get(f"/api/v1/posts/?social_account_id={uuid.uuid4()}")
        assert res_rand.status_code in (403, 404)

    # E. limit/offset funcionam.
    def test_e_limit_and_offset(self, env):
        for i in range(5):
            create_post(
                workspace=env["ws1"],
                social_account=env["sa_a"],
                caption=f"Post #{i}",
                author=env["user"],
            )

        res = env["client_a"].get("/api/v1/posts/?limit=2&offset=1")
        assert res.status_code == 200
        data = res.json()
        assert data["total"] >= 5
        assert data["limit"] == 2
        assert data["offset"] == 1
        assert len(data["items"]) == 2

    # F. media_assets retornam: id, url, mime_type, media_type, position.
    def test_f_media_assets_structured_response(self, env):
        p = create_post(
            workspace=env["ws1"],
            social_account=env["sa_a"],
            caption="Post with Media",
            media_asset_ids=[env["img_asset"].id],
            author=env["user"],
        )

        res = env["client_a"].get(f"/api/v1/posts/{p.id}")
        assert res.status_code == 200
        data = res.json()
        assert "media_assets" in data
        assert len(data["media_assets"]) == 1
        ma = data["media_assets"][0]
        assert ma["id"] == str(env["img_asset"].id)
        assert "url" in ma and len(ma["url"]) > 0
        assert ma["mime_type"] == "image/jpeg"
        assert ma["media_type"] == "image"
        assert ma["position"] == 0

        # Also in list endpoint
        res_list = env["client_a"].get("/api/v1/posts/")
        assert res_list.status_code == 200
        matching = [item for item in res_list.json()["items"] if item["id"] == str(p.id)]
        assert len(matching) == 1
        assert len(matching[0]["media_assets"]) == 1
        assert matching[0]["media_assets"][0]["id"] == str(env["img_asset"].id)

    # G. post_type story/reel é retornado do platform_extra.
    def test_g_post_type_exposed_in_summary(self, env):
        payload = {
            "social_account_id": str(env["sa_a"].id),
            "caption": "Story Post",
            "media_asset_ids": [str(env["img_asset"].id)],
            "action": "draft",
            "post_type": "story",
        }
        res = env["client_a"].post("/api/v1/posts/", data=json.dumps(payload), content_type="application/json")
        assert res.status_code == 201
        data = res.json()
        assert len(data["platform_posts"]) == 1
        assert data["platform_posts"][0]["post_type"] == "story"

        # Check list endpoint
        res_list = env["client_a"].get("/api/v1/posts/")
        assert res_list.status_code == 200
        matching = [item for item in res_list.json()["items"] if item["id"] == data["id"]]
        assert len(matching) == 1
        assert matching[0]["platform_posts"][0]["post_type"] == "story"

    # H. permalink_url é persistido após publicação síncrona.
    def test_h_permalink_url_persisted_on_publish(self, env, monkeypatch):
        p = create_post(
            workspace=env["ws1"],
            social_account=env["sa_a"],
            caption="Publish Me",
            author=env["user"],
        )
        pp = p.platform_posts.first()

        # Mock the platform dispatch to return success with url
        monkeypatch.setattr(
            PublishEngine,
            "_dispatch_to_provider",
            lambda self, pp, media_cache=None: {
                "success": True,
                "platform_post_id": "remote-12345",
                "url": "https://www.instagram.com/p/Cxyz123/",
                "response": {"id": "remote-12345"},
            },
        )

        engine = PublishEngine()
        result = engine._publish_platform_post(pp)
        assert result["success"] is True

        pp.refresh_from_db()
        assert pp.status == "published"
        assert pp.platform_post_id == "remote-12345"
        assert pp.permalink_url == "https://www.instagram.com/p/Cxyz123/"

        # Verify exposed in API response
        res = env["client_a"].get(f"/api/v1/posts/{p.id}")
        assert res.status_code == 200
        data = res.json()
        assert data["platform_posts"][0]["permalink_url"] == "https://www.instagram.com/p/Cxyz123/"

    # I. post existente por ID continua funcionando.
    def test_i_retrieve_post_by_id_remains_functional(self, env):
        p = create_post(
            workspace=env["ws1"],
            social_account=env["sa_a"],
            caption="Standard Retrieve",
            author=env["user"],
        )
        res = env["client_a"].get(f"/api/v1/posts/{p.id}")
        assert res.status_code == 200
        data = res.json()
        assert data["id"] == str(p.id)
        assert data["caption"] == "Standard Retrieve"

    # J. Nenhum secret/token aparece na resposta.
    def test_j_no_secret_or_token_leaked(self, env):
        p = create_post(
            workspace=env["ws1"],
            social_account=env["sa_a"],
            caption="Check Secrets Leakage",
            author=env["user"],
        )
        assert p.id is not None
        res_list = env["client_a"].get("/api/v1/posts/")
        assert res_list.status_code == 200
        content_str = res_list.content.decode("utf-8")
        assert "super_secret_token" not in content_str
        assert "encrypted_access_token" not in content_str
        assert "plaintext_token" not in content_str
        assert "password" not in content_str
