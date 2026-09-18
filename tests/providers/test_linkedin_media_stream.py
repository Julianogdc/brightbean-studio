"""``_media_handle`` must never materialize media as ``bytes``.

The local-path branch is bounded by the 20 MB image cap, but the HTTP(S)
branch fetched an arbitrary URL with ``resp.content`` — an unbounded read
straight into the worker's heap, on a dyno with 512 MB total.
"""

import httpx
import pytest

from providers.linkedin import LinkedInProvider


class TestLocalPathBranch:
    def test_yields_a_readable_file_object(self, tmp_path):
        media = tmp_path / "image.png"
        media.write_bytes(b"pixels" * 100)
        with LinkedInProvider._media_handle(str(media)) as handle:
            assert handle.read() == b"pixels" * 100

    def test_closes_the_handle_on_exit(self, tmp_path):
        media = tmp_path / "image.png"
        media.write_bytes(b"x")
        with LinkedInProvider._media_handle(str(media)) as handle:
            pass
        assert handle.closed


class TestUrlBranch:
    def _transport(self, body, status=200):
        return httpx.MockTransport(lambda request: httpx.Response(status, content=body))

    def test_spools_a_url_to_disk_and_yields_a_file(self, monkeypatch):
        body = b"remote-bytes" * 5000
        transport = self._transport(body)
        original = httpx.Client

        def client(*args, **kwargs):
            kwargs["transport"] = transport
            return original(*args, **kwargs)

        monkeypatch.setattr(httpx, "Client", client)

        with LinkedInProvider._media_handle("https://cdn.example/photo.jpg") as handle:
            # A real file on disk, not an in-memory buffer.
            assert handle.fileno() > 0
            assert handle.read() == body

    def test_starts_at_offset_zero(self, monkeypatch):
        """The spool is written then rewound; forgetting the seek uploads nothing."""
        body = b"abcdef"
        transport = self._transport(body)
        original = httpx.Client
        monkeypatch.setattr(httpx, "Client", lambda *a, **k: original(*a, **{**k, "transport": transport}))

        with LinkedInProvider._media_handle("https://cdn.example/photo.jpg") as handle:
            assert handle.tell() == 0

    def test_raises_on_an_http_error(self, monkeypatch):
        transport = self._transport(b"", status=404)
        original = httpx.Client
        monkeypatch.setattr(httpx, "Client", lambda *a, **k: original(*a, **{**k, "transport": transport}))

        with (
            pytest.raises(httpx.HTTPStatusError),
            LinkedInProvider._media_handle("https://cdn.example/missing.jpg"),
        ):
            pass
