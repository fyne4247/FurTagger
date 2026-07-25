"""Request/session fakes — tests must never call live APIs."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import requests

from furtag import TagIntegrator
from furtag_settings import Settings


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class FakeSession:
    """Minimal requests.Session stand-in for unit tests."""

    def __init__(self, routes=None):
        # routes: list of (method, url_substr, FakeResponse or callable)
        self.routes = list(routes or [])
        self.calls = []

    def get(self, url, **kwargs):
        return self._dispatch("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._dispatch("POST", url, **kwargs)

    def _dispatch(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for m, substr, resp in self.routes:
            if m.upper() == method.upper() and substr in url:
                if callable(resp):
                    return resp(url, kwargs)
                return resp
        return FakeResponse(404, {"error": "no fake route"})


class TestHydrusRouting(unittest.TestCase):
    def test_result_page_routing_new_vs_updated(self):
        session = FakeSession(routes=[
            ("GET", "verify_access_key", FakeResponse(200, {
                "basic_permissions": [0, 1, 2, 3, 4, 8],
                "permits_everything": False,
            })),
            ("GET", "get_services", FakeResponse(200, {
                "services_v2": [{
                    "name": "downloader tags",
                    "type": 5,
                    "service_key": "svc123",
                }],
            })),
            ("POST", "add_files/add_file", FakeResponse(200, {
                "status": 1, "hash": "aaa" * 16 + "aa",
            })),
            ("POST", "add_tags/add_tags", FakeResponse(200, {})),
            ("POST", "add_urls/associate_url", FakeResponse(200, {})),
        ])
        s = Settings()
        s.output.hydrus_enabled = True
        ti = TagIntegrator(settings=s, session=session)
        ti.hydrus_api_url = "http://127.0.0.1:45869"
        ti.hydrus_access_key = "test-key"
        ti.hydrus_tag_service_key = "svc123"
        ti.has_hydrus = True
        ti.hydrus_import = True
        ti.hydrus_can_edit_urls = True
        ti.hydrus_result_pages["new"]["enabled"] = True
        ti.hydrus_result_pages["updated"]["enabled"] = True

        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "x.jpg"
            media.write_bytes(b"data")
            h = ti._hydrus_push(media, {"creator:test"}, {"https://e621.net/posts/1"})
            self.assertIsNotNone(h)
            # status 1 → new imports page
            self.assertIn(h, ti.hydrus_result_pages["new"]["hashes"])

        # status 2 → already in hydrus → newly tagged
        session.routes = [
            ("POST", "add_files/add_file", FakeResponse(200, {
                "status": 2, "hash": "bbb" * 16 + "bb",
            })),
            ("POST", "add_tags/add_tags", FakeResponse(200, {})),
            ("POST", "add_urls/associate_url", FakeResponse(200, {})),
        ]
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "y.jpg"
            media.write_bytes(b"data2")
            h2 = ti._hydrus_push(media, {"creator:test2"}, set())
            self.assertIn(h2, ti.hydrus_result_pages["updated"]["hashes"])


class TestNoLiveCalls(unittest.TestCase):
    def test_hash_lookup_uses_session(self):
        session = FakeSession(routes=[
            ("GET", "e621.net", FakeResponse(200, {"posts": []})),
        ])
        ti = TagIntegrator(settings=Settings(), session=session)
        ti.has_e621 = True
        ti.enabled_e621 = True
        ti.e621_username = "u"
        ti.e621_api_key = "k"
        ti.headers_e6 = {"User-Agent": "test"}
        tags, urls = ti.e621_lookup_by_md5("0" * 32)
        self.assertEqual(tags, set())
        self.assertTrue(any("e621" in c[1] for c in session.calls))


if __name__ == "__main__":
    unittest.main()
