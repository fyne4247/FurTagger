"""Request/session fakes — tests must never call live APIs."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import requests

from furtag import Ledger, TagIntegrator
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


class _RecordingObserver:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class TestHydrusSidecarSync(unittest.TestCase):
    @staticmethod
    def _media_with_sidecar(root, name="sync.jpg", data=b"image"):
        media = root / name
        media.write_bytes(data)
        Path(str(media) + ".txt").write_text(
            "creator:test\nspecies:fox\n", encoding="utf-8")
        return media

    def test_current_hash_bypasses_reimport_and_checkpoints(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = self._media_with_sidecar(root)
            sha256 = hashlib.sha256(media.read_bytes()).hexdigest()
            session = FakeSession([
                ("GET", "get_files/search_files", FakeResponse(200, {
                    "hashes": [sha256],
                })),
                ("POST", "add_tags/add_tags", FakeResponse(200, {})),
            ])
            ti = _hydrus_ti(session)
            ti.hydrus_can_search_files = True
            observer = _RecordingObserver()
            ti._observer = observer

            attempted, failed = ti.sync_sidecars_to_hydrus(root)

            self.assertEqual((attempted, failed), (1, 0))
            self.assertFalse(any(
                "add_files/add_file" in url
                for _, url, _ in session.calls))
            tag_calls = [
                call for call in session.calls
                if "add_tags/add_tags" in call[1]]
            self.assertEqual(len(tag_calls), 1)
            self.assertEqual(tag_calls[0][2]["json"]["hash"], sha256)
            self.assertTrue(any(
                event.kind == "sidecar_sync"
                and event.current == media.name
                for event in observer.events))

            ledger = Ledger(root)
            ledger.load()
            st = media.stat()
            tags, urls = ti.read_sidecar_payload(media)
            signature = ti._sidecar_sync_signature(tags, urls)
            self.assertTrue(ledger.sidecar_sync_matches(
                media.name, st.st_size, st.st_mtime, signature))

            # A second run skips both the Hydrus search and every write.
            session.calls.clear()
            attempted, failed = ti.sync_sidecars_to_hydrus(root)
            self.assertEqual((attempted, failed), (0, 0))
            self.assertEqual(session.calls, [])

    def test_changed_sidecar_resyncs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = self._media_with_sidecar(root)
            sha256 = hashlib.sha256(media.read_bytes()).hexdigest()
            session = FakeSession([
                ("GET", "get_files/search_files", FakeResponse(200, {
                    "hashes": [sha256],
                })),
                ("POST", "add_tags/add_tags", FakeResponse(200, {})),
            ])
            ti = _hydrus_ti(session)
            ti.hydrus_can_search_files = True

            ti.sync_sidecars_to_hydrus(root)
            Path(str(media) + ".txt").write_text(
                "creator:test\nspecies:fox\nnew tag\n", encoding="utf-8")
            session.calls.clear()

            attempted, failed = ti.sync_sidecars_to_hydrus(root)

        self.assertEqual((attempted, failed), (1, 0))
        self.assertTrue(any(
            "add_tags/add_tags" in url for _, url, _ in session.calls))

    def test_failed_resume_lookup_falls_back_to_import(self):
        imported_hash = "b" * 64
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._media_with_sidecar(root)
            session = FakeSession([
                ("GET", "get_files/search_files",
                 FakeResponse(403, {}, text="missing permission")),
                ("POST", "add_files/add_file", FakeResponse(200, {
                    "status": 2,
                    "hash": imported_hash,
                })),
                ("POST", "add_tags/add_tags", FakeResponse(200, {})),
            ])
            ti = _hydrus_ti(session)
            ti.hydrus_can_search_files = True

            attempted, failed = ti.sync_sidecars_to_hydrus(root)

        self.assertEqual((attempted, failed), (1, 0))
        self.assertTrue(any(
            "add_files/add_file" in url for _, url, _ in session.calls))
        self.assertFalse(ti.hydrus_can_search_files)

    def test_failed_metadata_write_is_not_checkpointed(self):
        sha256 = "c" * 64
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = self._media_with_sidecar(root)
            session = FakeSession([
                ("POST", "add_files/add_file", FakeResponse(200, {
                    "status": 2,
                    "hash": sha256,
                })),
                ("POST", "add_tags/add_tags",
                 FakeResponse(500, {}, text="tag failure")),
            ])
            ti = _hydrus_ti(session)

            attempted, failed = ti.sync_sidecars_to_hydrus(root)
            ledger = Ledger(root)
            ledger.load()
            st = media.stat()
            tags, urls = ti.read_sidecar_payload(media)

        self.assertEqual((attempted, failed), (1, 1))
        self.assertFalse(ledger.sidecar_sync_matches(
            media.name, st.st_size, st.st_mtime,
            ti._sidecar_sync_signature(tags, urls)))


DELETED_HASH = "d" * 64
DUP_OK = "1" * 64
DUP_FAIL = "2" * 64


def _hydrus_ti(session, settings=None):
    """A TagIntegrator wired to a fake session with Hydrus already 'verified'."""
    s = settings or Settings()
    s.output.hydrus_enabled = True
    ti = TagIntegrator(settings=s, session=session)
    ti.hydrus_api_url = "http://127.0.0.1:45869"
    ti.hydrus_access_key = "test-key"
    ti.hydrus_tag_service_key = "svc123"
    ti.has_hydrus = True
    ti.hydrus_import = True
    ti.hydrus_can_edit_urls = False
    return ti


def _deleted_dup_routes(targets, failing=()):
    """add_file → status 3 (deleted); relationships → *targets*; add_tags 500
    for any hash in *failing*."""
    def add_tags(url, kwargs):
        h = (kwargs.get("json") or {}).get("hash")
        if h in failing:
            return FakeResponse(500, {}, text="nope")
        return FakeResponse(200, {})

    return [
        ("POST", "add_files/add_file", FakeResponse(200, {
            "status": 3, "hash": DELETED_HASH,
        })),
        ("GET", "get_file_relationships", FakeResponse(200, {
            "file_relationships": {DELETED_HASH: {"8": list(targets)}},
        })),
        ("POST", "add_tags/add_tags", add_tags),
    ]


class TestHydrusExactUrlEnrichment(unittest.TestCase):
    URL = "https://e621.net/posts/123"
    HASH = "a" * 64

    def _push(self, routes, *, exact_match=True, settings=None):
        session = FakeSession([
            ("POST", "add_files/add_file", FakeResponse(200, {
                "status": 2, "hash": self.HASH,
            })),
            ("POST", "add_tags/add_tags", FakeResponse(200, {})),
        ] + list(routes))
        ti = _hydrus_ti(session, settings=settings)
        ti.hydrus_can_edit_urls = True
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "exact.jpg"
            media.write_bytes(b"exact bytes")
            result = ti._hydrus_push(
                media, {"creator:test"}, {self.URL},
                exact_match=exact_match)
        return ti, session, result

    @staticmethod
    def _matching_calls(session, suffix):
        return [call for call in session.calls if suffix in call[1]]

    def test_parseable_exact_post_is_enqueued_not_associated(self):
        ti, session, result = self._push([
            ("GET", "add_urls/get_url_info", FakeResponse(200, {
                "url_type": 0,
                "url_type_string": "post url",
                "can_parse": True,
            })),
            ("POST", "add_urls/add_url", FakeResponse(200, {
                "human_result_text": "URL added successfully.",
            })),
        ])

        self.assertEqual(result, self.HASH)
        self.assertFalse(
            self._matching_calls(session, "add_urls/associate_url"))
        add_calls = self._matching_calls(session, "add_urls/add_url")
        self.assertEqual(len(add_calls), 1)
        body = add_calls[0][2]["json"]
        self.assertEqual(body["url"], self.URL)
        self.assertEqual(body["destination_page_name"], "FurTag Metadata")
        self.assertFalse(body["show_destination_page"])

        endpoints = [call[1] for call in session.calls]
        self.assertLess(
            next(i for i, value in enumerate(endpoints)
                 if "add_files/add_file" in value),
            next(i for i, value in enumerate(endpoints)
                 if "add_urls/get_url_info" in value))

    def test_unknown_url_is_only_associated(self):
        _, session, _ = self._push([
            ("GET", "add_urls/get_url_info", FakeResponse(200, {
                "url_type": 5,
                "url_type_string": "unknown url",
                "can_parse": False,
            })),
            ("POST", "add_urls/associate_url", FakeResponse(200, {})),
        ])
        self.assertFalse(self._matching_calls(session, "add_urls/add_url"))
        calls = self._matching_calls(session, "add_urls/associate_url")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2]["json"]["urls_to_add"], [self.URL])

    def test_external_source_url_is_never_enqueued(self):
        source_url = "https://www.furaffinity.net/view/999"
        session = FakeSession([
            ("POST", "add_files/add_file", FakeResponse(200, {
                "status": 2, "hash": self.HASH,
            })),
            ("POST", "add_tags/add_tags", FakeResponse(200, {})),
            ("GET", "add_urls/get_url_info", FakeResponse(200, {
                "url_type": 0, "can_parse": True,
            })),
            ("POST", "add_urls/add_url", FakeResponse(200, {})),
            ("POST", "add_urls/associate_url", FakeResponse(200, {})),
        ])
        ti = _hydrus_ti(session)
        ti.hydrus_can_edit_urls = True
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "exact.jpg"
            media.write_bytes(b"exact bytes")
            ti._hydrus_push(
                media, {"creator:test"}, {self.URL, source_url},
                exact_match=True)

        add_calls = self._matching_calls(session, "add_urls/add_url")
        self.assertEqual(len(add_calls), 1)
        self.assertEqual(add_calls[0][2]["json"]["url"], self.URL)
        info_calls = self._matching_calls(session, "add_urls/get_url_info")
        self.assertEqual(len(info_calls), 1)
        associated = self._matching_calls(
            session, "add_urls/associate_url")
        self.assertEqual(
            associated[0][2]["json"]["urls_to_add"], [source_url])

    def test_add_url_failure_falls_back_to_association(self):
        _, session, _ = self._push([
            ("GET", "add_urls/get_url_info", FakeResponse(200, {
                "url_type": 0, "can_parse": True,
            })),
            ("POST", "add_urls/add_url", FakeResponse(
                500, {}, text="queue unavailable")),
            ("POST", "add_urls/associate_url", FakeResponse(200, {})),
        ])
        self.assertEqual(
            len(self._matching_calls(session, "add_urls/add_url")), 1)
        self.assertEqual(
            len(self._matching_calls(session, "add_urls/associate_url")), 1)

    def test_perceptual_url_never_enters_downloader(self):
        _, session, _ = self._push([
            ("POST", "add_urls/associate_url", FakeResponse(200, {})),
        ], exact_match=False)
        self.assertFalse(
            self._matching_calls(session, "add_urls/get_url_info"))
        self.assertFalse(self._matching_calls(session, "add_urls/add_url"))
        self.assertEqual(
            len(self._matching_calls(session, "add_urls/associate_url")), 1)

    def test_setting_can_disable_enrichment(self):
        settings = Settings()
        settings.hydrus.exact_url_enrichment = False
        _, session, _ = self._push([
            ("POST", "add_urls/associate_url", FakeResponse(200, {})),
        ], settings=settings)
        self.assertFalse(
            self._matching_calls(session, "add_urls/get_url_info"))
        self.assertEqual(
            len(self._matching_calls(session, "add_urls/associate_url")), 1)


class TestHydrusDuplicateTaggedPage(unittest.TestCase):
    """Files tagged via a deleted file's duplicate group get their own page."""

    def _push(self, ti, name="deleted.mp4"):
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / name
            media.write_bytes(b"data")
            return ti._hydrus_push(media, {"creator:test"}, set())

    def test_tagged_members_land_on_duplicates_page(self):
        ti = _hydrus_ti(FakeSession(_deleted_dup_routes([DUP_OK, DUP_FAIL])))
        ti.hydrus_tag_deleted_duplicates = True
        ti.hydrus_can_manage_relationships = True
        for page in ti.hydrus_result_pages.values():
            page["enabled"] = True

        self.assertIsNone(self._push(ti))  # deleted hash is never returned
        self.assertEqual(sorted(ti.hydrus_result_pages["duplicates"]["hashes"]),
                         sorted([DUP_OK, DUP_FAIL]))
        # Not conflated with the ordinary "newly tagged" page.
        self.assertEqual(ti.hydrus_result_pages["updated"]["hashes"], [])
        self.assertEqual(ti.hydrus_result_pages["new"]["hashes"], [])

    def test_failed_member_is_not_added(self):
        ti = _hydrus_ti(FakeSession(
            _deleted_dup_routes([DUP_OK, DUP_FAIL], failing=[DUP_FAIL])))
        ti.hydrus_tag_deleted_duplicates = True
        ti.hydrus_can_manage_relationships = True
        for page in ti.hydrus_result_pages.values():
            page["enabled"] = True

        self._push(ti)
        hashes = ti.hydrus_result_pages["duplicates"]["hashes"]
        self.assertIn(DUP_OK, hashes)      # sorted() puts DUP_OK first
        self.assertNotIn(DUP_FAIL, hashes)

    def test_master_results_toggle_disables_the_page(self):
        session = FakeSession(routes=[
            ("GET", "verify_access_key", FakeResponse(200, {
                "basic_permissions": [0, 1, 2, 3, 4, 8],
                "permits_everything": False,
            })),
            ("GET", "get_services", FakeResponse(200, {
                "services_v2": [{"name": "downloader tags", "type": 5,
                                 "service_key": "svc123"}],
            })),
        ] + _deleted_dup_routes([DUP_OK]))
        ti = TagIntegrator(settings=Settings(), session=session)
        cfg = {
            "hydrus_api_url": "http://127.0.0.1:45869",
            "hydrus_access_key": "k" * 64,
            "hydrus_results_page": "off",
        }
        ti._init_hydrus(cfg)
        self.assertTrue(ti.has_hydrus)
        self.assertFalse(ti.hydrus_result_pages["duplicates"]["enabled"])

        ti.hydrus_can_manage_relationships = True
        ti.hydrus_tag_deleted_duplicates = True
        self._push(ti)
        self.assertEqual(ti.hydrus_result_pages["duplicates"]["hashes"], [])

    def test_page_name_comes_from_credentials_key(self):
        session = FakeSession(routes=[
            ("GET", "verify_access_key", FakeResponse(200, {
                "basic_permissions": [0, 1, 2, 3, 4, 8],
                "permits_everything": False,
            })),
            ("GET", "get_services", FakeResponse(200, {
                "services_v2": [{"name": "downloader tags", "type": 5,
                                 "service_key": "svc123"}],
            })),
        ])
        ti = TagIntegrator(settings=Settings(), session=session)
        ti._init_hydrus({
            "hydrus_api_url": "http://127.0.0.1:45869",
            "hydrus_access_key": "k" * 64,
            "hydrus_duplicate_tagged_page": "Dupe Review",
        })
        self.assertTrue(ti.hydrus_result_pages["duplicates"]["enabled"])
        self.assertEqual(ti.hydrus_result_pages["duplicates"]["name"],
                         "Dupe Review")

    def test_apply_settings_propagates_page_name(self):
        s = Settings()
        s.hydrus.duplicate_tagged_page_name = "Dupes From Settings"
        ti = TagIntegrator(settings=Settings(), session=FakeSession())
        ti.apply_settings(s)
        self.assertEqual(ti.hydrus_result_pages["duplicates"]["name"],
                         "Dupes From Settings")

    def test_flush_creates_the_page(self):
        created = []

        def new_page(url, kwargs):
            created.append(kwargs.get("json") or {})
            return FakeResponse(200, {"page_key": "pk1"})

        ti = _hydrus_ti(FakeSession([("POST", "manage_pages/new_page", new_page)]))
        ti.hydrus_result_pages["duplicates"].update(
            {"enabled": True, "name": "Dupe Review", "hashes": [DUP_OK]})
        ti._hydrus_flush_result_pages()
        self.assertEqual([c["page_name"] for c in created], ["Dupe Review"])
        self.assertEqual(created[0]["hashes"], [DUP_OK])


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
