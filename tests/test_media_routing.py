"""Routing tests for user-uploaded media (``MEDIA_URL`` -> ``MEDIA_ROOT``).

Two separate regressions are pinned here.

The first is issue #130: ``config/urls.py`` used to mount media only under
``settings.DEBUG``, so a ``config.settings.production`` deployment with
``STORAGE_BACKEND=local`` returned 404 for every upload. That breaks more than
the thumbnails in the UI — ``apps/publisher/engine.py`` builds each attachment's
outbound URL as ``APP_URL + asset.file.url``, and Instagram, Threads, Facebook,
Pinterest, Google Business and dev.to fetch that URL server-side with no
byte-upload fallback, so publishing to them fails too.

The second is the shape of the route. ``MEDIA_URL`` is only assigned on the
local-storage branch of ``config/settings/base.py``; under ``STORAGE_BACKEND=s3``
Django normalises the unset default to ``"/"``, and a ``"/"`` prefix compiles to
``^(?P<path>.*)$`` — a catch-all appended after every real route, serving
whatever it matches out of the process CWD. That is how the dev server came to
answer ``GET /.env`` with the repo's own environment file.
"""

import tempfile
from pathlib import Path

import pytest
from django.conf import settings
from django.test import Client, override_settings
from django.urls import Resolver404, resolve
from django.views.static import serve

from config.urls import media_urlpatterns


def test_helper_builds_one_prefixed_route():
    with tempfile.TemporaryDirectory() as media_root:
        with override_settings(MEDIA_URL="/media/", MEDIA_ROOT=media_root):
            patterns = media_urlpatterns()

        assert len(patterns) == 1
        assert patterns[0].callback is serve
        assert patterns[0].default_args["document_root"] == media_root


@pytest.mark.parametrize("media_url", ["", "/"])
def test_helper_refuses_to_build_a_catch_all(media_url):
    """An empty prefix would match every unrouted URL — build nothing instead."""
    with (
        tempfile.TemporaryDirectory() as media_root,
        override_settings(MEDIA_URL=media_url, MEDIA_ROOT=media_root),
    ):
        assert media_urlpatterns() == []


def test_helper_needs_a_document_root():
    """Without MEDIA_ROOT the route would serve relative to the process CWD."""
    with override_settings(MEDIA_URL="/media/", MEDIA_ROOT=""):
        assert media_urlpatterns() == []


def test_media_is_routed_with_debug_off():
    """The regression from #130: DEBUG is False here, the route still exists."""
    assert settings.DEBUG is False

    match = resolve("/media/media_library/2026/09/example.png")

    assert match.func is serve
    assert match.kwargs["path"] == "media_library/2026/09/example.png"


@pytest.mark.parametrize("path", ["/.env", "/requirements.txt", "/config/settings/base.py"])
def test_urlconf_has_no_catch_all(path):
    """Nothing outside /media/ may fall through to the static file server."""
    with pytest.raises(Resolver404):
        resolve(path)


@pytest.mark.django_db
def test_media_file_is_served():
    probe = Path(settings.MEDIA_ROOT) / "routing-probe.txt"
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_bytes(b"probe-bytes")

    try:
        response = Client().get("/media/routing-probe.txt")

        assert response.status_code == 200
        assert b"".join(response.streaming_content) == b"probe-bytes"
    finally:
        probe.unlink(missing_ok=True)
