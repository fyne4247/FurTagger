"""Request/session fakes — tests must never call live APIs."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from furtag import (
    FileItem, Ledger, LedgerManager, RetryableLookupError, TagIntegrator,
)
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


class TestDirectSourceNotes(unittest.TestCase):
    def test_permission_seven_enables_direct_note_capability(self):
        session = FakeSession([
            ("GET", "verify_access_key", FakeResponse(200, {
                "basic_permissions": [7],
                "permits_everything": False,
            })),
            ("GET", "get_services", FakeResponse(200, {
                "services_v2": [{
                    "name": "downloader tags",
                    "type": 5,
                    "service_key": "svc123",
                }],
            })),
        ])
        ti = TagIntegrator(settings=Settings(), session=session)
        ti._init_hydrus({
            "hydrus_api_url": "http://127.0.0.1:45869",
            "hydrus_access_key": "test-key",
        })
        self.assertTrue(ti.hydrus_can_edit_notes)

    def test_e621_description_has_stable_note_name(self):
        ti = TagIntegrator(settings=Settings())
        ti.has_hydrus = True
        ti.hydrus_can_edit_notes = True
        metadata = ti._parse_e6_metadata({
            "id": 123,
            "description": "  DText body  ",
            "tags": {},
        })
        self.assertEqual(
            metadata.notes,
            {"e621 description — post 123": "DText body"})

    def test_disabled_direct_notes_drops_e621_note_payload(self):
        settings = Settings()
        settings.hydrus.direct_source_notes = False
        ti = TagIntegrator(settings=settings)
        metadata = ti._parse_e6_metadata({
            "id": 123,
            "description": "DText body",
            "tags": {},
        })
        self.assertEqual(metadata.notes, {})

    def test_inkbunny_requests_and_collects_description_and_title(self):
        session = FakeSession([
            ("GET", "api_submissions.php", FakeResponse(200, {
                "submissions": [{
                    "submission_id": 456,
                    "title": "A title",
                    "description": "[b]BBCode body[/b]",
                    "keywords": [],
                }],
            })),
        ])
        ti = TagIntegrator(settings=Settings(), session=session)
        ti.has_hydrus = True
        ti.hydrus_can_edit_notes = True
        ti.pace["inkbunny"].wait = lambda: None
        metadata = ti._inkbunny_submission_metadata(["456"])

        params = session.calls[0][2]["params"]
        self.assertEqual(params["show_description"], "yes")
        self.assertEqual(metadata.notes, {
            "Inkbunny title — submission 456": "A title",
            "Inkbunny description — submission 456": "[b]BBCode body[/b]",
        })

    def test_disabled_direct_notes_uses_lightweight_inkbunny_request(self):
        session = FakeSession([
            ("GET", "api_submissions.php", FakeResponse(200, {
                "submissions": [],
            })),
        ])
        ti = TagIntegrator(settings=Settings(), session=session)
        ti.hydrus_direct_notes_enabled = False
        ti.pace["inkbunny"].wait = lambda: None
        ti._inkbunny_submission_metadata(["456"])
        self.assertEqual(
            session.calls[0][2]["params"]["show_description"], "no")

    def test_hydrus_notes_are_idempotent_upserts(self):
        file_hash = "a" * 64
        session = FakeSession([
            ("POST", "add_files/add_file", FakeResponse(200, {
                "status": 2, "hash": file_hash,
            })),
            ("POST", "add_notes/set_notes", FakeResponse(200, {})),
        ])
        ti = _hydrus_ti(session)
        ti.hydrus_can_edit_notes = True
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "notes.jpg"
            media.write_bytes(b"notes")
            result = ti._hydrus_push(
                media, set(), set(),
                notes={"e621 description — post 123": "body"})

        self.assertEqual(result, file_hash)
        calls = [call for call in session.calls
                 if "add_notes/set_notes" in call[1]]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2]["json"], {
            "hash": file_hash,
            "notes": {"e621 description — post 123": "body"},
            "merge_cleverly": False,
        })

    def test_missing_optional_metadata_permissions_do_not_force_retries(self):
        file_hash = "a" * 64
        session = FakeSession([
            ("POST", "add_files/add_file", FakeResponse(200, {
                "status": 2, "hash": file_hash,
            })),
            ("POST", "add_tags/add_tags", FakeResponse(200, {})),
        ])
        ti = _hydrus_ti(session)
        ti.hydrus_can_edit_notes = False
        ti.hydrus_can_edit_urls = False
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "limited-key.jpg"
            media.write_bytes(b"data")
            sha256, complete = ti._hydrus_push_detailed(
                media, {"creator:test"}, {"https://example.test/post/1"},
                notes={"e621 description — post 1": "body"})
        self.assertEqual(sha256, file_hash)
        self.assertTrue(complete)
        self.assertFalse(any(
            "add_notes/set_notes" in url or "add_urls/" in url
            for _method, url, _kwargs in session.calls))

    def test_deleted_hash_writes_notes_to_current_duplicate(self):
        session = FakeSession(_deleted_dup_routes([DUP_OK]) + [
            ("POST", "add_notes/set_notes", FakeResponse(200, {})),
        ])
        ti = _hydrus_ti(session)
        ti.hydrus_tag_deleted_duplicates = True
        ti.hydrus_can_manage_relationships = True
        ti.hydrus_can_edit_notes = True
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "deleted.jpg"
            media.write_bytes(b"deleted")
            # Status-3 original SHA is retained (BF-06); notes go to the live dup.
            self.assertEqual(ti._hydrus_push(
                media, set(), set(),
                notes={"e621 description — post 1": "survives"}),
                DELETED_HASH)

        calls = [call for call in session.calls
                 if "add_notes/set_notes" in call[1]]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2]["json"]["hash"], DUP_OK)

    def test_deleted_with_no_duplicate_members_is_permanent(self):
        """Successful empty relationship query → no_duplicate_targets + SHA."""
        from furtag_hydrus import HydrusImportState, HydrusMetadataState
        session = FakeSession(_deleted_dup_routes([]))
        ti = _hydrus_ti(session)
        ti.hydrus_tag_deleted_duplicates = True
        ti.hydrus_can_manage_relationships = True
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "gone.jpg"
            media.write_bytes(b"gone")
            push = ti._hydrus_push_detailed(
                media, {"creator:test"}, set())
            self.assertTrue(push.complete)
            self.assertTrue(push.hydrus_deleted)
            self.assertEqual(push.sha256, DELETED_HASH)
            self.assertEqual(
                push.import_state, HydrusImportState.PREVIOUSLY_DELETED)
            self.assertEqual(
                push.metadata_state, HydrusMetadataState.NO_DUPLICATE_TARGETS)

            outcome = ti.write_results_detailed(
                media, {"creator:test"}, set())
            self.assertTrue(outcome.complete)
            # Search status stays matched; Hydrus details are nested.
            self.assertEqual(outcome.ledger_status, "matched")
            self.assertEqual(outcome.sha256, DELETED_HASH)
            self.assertEqual(
                outcome.hydrus_output["metadata_state"], "no_duplicate_targets")
            self.assertEqual(outcome.hydrus_output["sha256"], DELETED_HASH)
            repeated = ti._repeated_issues["hydrus_deleted_no_targets"]
            self.assertEqual(repeated[0], 2)
            self.assertEqual(repeated[3], "info")

    def test_deleted_tagging_disabled_is_policy_skipped_not_permanent_seal(self):
        from furtag_hydrus import HydrusMetadataState
        session = FakeSession(_deleted_dup_routes([DUP_OK]))
        ti = _hydrus_ti(session)
        ti.hydrus_tag_deleted_duplicates = False
        ti.hydrus_can_manage_relationships = True
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "gone.jpg"
            media.write_bytes(b"gone")
            push = ti._hydrus_push_detailed(
                media, {"creator:test"}, set())
        self.assertTrue(push.complete)
        self.assertFalse(push.hydrus_deleted)  # not no_duplicate_targets
        self.assertEqual(
            push.metadata_state, HydrusMetadataState.POLICY_SKIPPED)
        self.assertEqual(push.sha256, DELETED_HASH)
        # Relationship lookup should not run when tagging is off.
        self.assertFalse(any(
            "get_file_relationships" in url for _, url, _ in session.calls))

    def test_deleted_missing_relationship_permission_is_not_sealed(self):
        """BF-01: missing permission must not write a permanent deletion seal."""
        from furtag_hydrus import HydrusMetadataState
        session = FakeSession(_deleted_dup_routes([DUP_OK]))
        ti = _hydrus_ti(session)
        ti.hydrus_tag_deleted_duplicates = True
        ti.hydrus_can_manage_relationships = False
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "gone.jpg"
            media.write_bytes(b"gone")
            push = ti._hydrus_push_detailed(
                media, {"creator:test"}, set())
        self.assertFalse(push.complete)
        self.assertFalse(push.hydrus_deleted)
        self.assertEqual(
            push.metadata_state, HydrusMetadataState.PERMISSION_MISSING)
        self.assertEqual(push.sha256, DELETED_HASH)
        self.assertFalse(any(
            "get_file_relationships" in url for _, url, _ in session.calls))

    def test_deleted_with_targets_retains_original_and_target_hashes(self):
        """BF-06: successful dup tagging keeps deleted original SHA + targets."""
        from furtag_hydrus import HydrusImportState, HydrusMetadataState
        session = FakeSession(_deleted_dup_routes([DUP_OK, DUP_FAIL]))
        ti = _hydrus_ti(session)
        ti.hydrus_tag_deleted_duplicates = True
        ti.hydrus_can_manage_relationships = True
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "gone.jpg"
            media.write_bytes(b"gone")
            push = ti._hydrus_push_detailed(
                media, {"creator:test"}, set())
        self.assertTrue(push.complete)
        self.assertEqual(push.sha256, DELETED_HASH)
        self.assertEqual(
            push.import_state, HydrusImportState.PREVIOUSLY_DELETED)
        self.assertEqual(
            push.metadata_state, HydrusMetadataState.APPLIED_DUPLICATES)
        self.assertEqual(set(push.target_hashes), {DUP_OK, DUP_FAIL})
        self.assertFalse(push.hydrus_deleted)

    def test_deleted_empty_metadata_skips_relationship_lookup(self):
        """Unmatched / no tags: previously_deleted + not_requested."""
        from furtag_hydrus import HydrusImportState, HydrusMetadataState
        session = FakeSession(_deleted_dup_routes([DUP_OK]))
        ti = _hydrus_ti(session)
        ti.hydrus_tag_deleted_duplicates = True
        ti.hydrus_can_manage_relationships = True
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "gone.jpg"
            media.write_bytes(b"gone")
            push = ti._hydrus_push_detailed(media, set(), set())
        self.assertTrue(push.complete)
        self.assertEqual(push.sha256, DELETED_HASH)
        self.assertEqual(
            push.import_state, HydrusImportState.PREVIOUSLY_DELETED)
        self.assertEqual(
            push.metadata_state, HydrusMetadataState.NOT_REQUESTED)
        self.assertFalse(any(
            "get_file_relationships" in url for _, url, _ in session.calls))

    def test_deleted_relationship_api_failure_stays_retryable(self):
        from furtag_hydrus import HydrusMetadataState
        session = FakeSession([
            ("POST", "add_files/add_file", FakeResponse(200, {
                "status": 3, "hash": DELETED_HASH,
            })),
            ("GET", "get_file_relationships", FakeResponse(
                500, {}, text="boom")),
        ])
        ti = _hydrus_ti(session)
        ti.hydrus_tag_deleted_duplicates = True
        ti.hydrus_can_manage_relationships = True
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "gone.jpg"
            media.write_bytes(b"gone")
            push = ti._hydrus_push_detailed(
                media, {"creator:test"}, set())
        self.assertFalse(push.complete)
        self.assertFalse(push.hydrus_deleted)
        self.assertEqual(
            push.metadata_state, HydrusMetadataState.RETRYABLE_FAILURE)
        self.assertEqual(push.sha256, DELETED_HASH)


class TestUnmatchedImportBF02(unittest.TestCase):
    """BF-02: typed unmatched import separate from search nomatch."""

    def test_import_off_records_not_requested_complete(self):
        ti = _hydrus_ti(FakeSession())
        ti.hydrus_import_unmatched = False
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "miss.jpg"
            media.write_bytes(b"x")
            out = ti.write_unmatched_detailed(media)
        self.assertTrue(out.complete)
        self.assertEqual(out.ledger_status, "nomatch")
        self.assertFalse(out.unmatched_import["requested"])
        self.assertTrue(out.unmatched_import["complete"])

    def test_import_success_checkpoints_complete(self):
        file_hash = "a" * 64
        session = FakeSession([
            ("POST", "add_files/add_file", FakeResponse(200, {
                "status": 1, "hash": file_hash,
            })),
        ])
        ti = _hydrus_ti(session)
        ti.hydrus_import_unmatched = True
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "miss.jpg"
            media.write_bytes(b"x")
            out = ti.write_unmatched_detailed(media)
        self.assertTrue(out.complete)
        self.assertEqual(out.sha256, file_hash)
        self.assertTrue(out.unmatched_import["requested"])
        self.assertEqual(out.unmatched_import["import_state"], "live")

    def test_import_status3_empty_metadata_is_complete_with_sha(self):
        session = FakeSession(_deleted_dup_routes([DUP_OK]))
        ti = _hydrus_ti(session)
        ti.hydrus_import_unmatched = True
        ti.hydrus_tag_deleted_duplicates = True
        ti.hydrus_can_manage_relationships = True
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "miss.jpg"
            media.write_bytes(b"x")
            out = ti.write_unmatched_detailed(media)
        self.assertTrue(out.complete)
        self.assertEqual(out.sha256, DELETED_HASH)
        self.assertEqual(
            out.unmatched_import["import_state"], "previously_deleted")
        self.assertEqual(
            out.unmatched_import["metadata_state"], "not_requested")
        self.assertFalse(any(
            "get_file_relationships" in url for _, url, _ in session.calls))

    def test_import_http_failure_is_incomplete(self):
        session = FakeSession([
            ("POST", "add_files/add_file", FakeResponse(500, {}, text="boom")),
        ])
        ti = _hydrus_ti(session)
        ti.hydrus_import_unmatched = True
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "miss.jpg"
            media.write_bytes(b"x")
            out = ti.write_unmatched_detailed(media)
        self.assertFalse(out.complete)
        self.assertTrue(out.unmatched_import["requested"])
        self.assertFalse(out.unmatched_import["complete"])

    def test_import_veto_preserves_typed_state_and_reason(self):
        from furtag_hydrus import HydrusImportState

        session = FakeSession([
            ("POST", "add_files/add_file", FakeResponse(200, {
                "status": 7,
                "hash": "f" * 64,
                "note": "blocked by import policy",
            })),
        ])
        ti = _hydrus_ti(session)
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "veto.jpg"
            media.write_bytes(b"veto")
            push = ti._hydrus_push_detailed(media, set(), set())
        self.assertEqual(push.import_state, HydrusImportState.VETOED)
        self.assertEqual(push.reason, "blocked by import policy")
        self.assertTrue(push.complete)

    def test_prior_reconciliation_removes_completed_item_from_scan_queue(self):
        file_hash = "b" * 64
        session = FakeSession([
            ("POST", "add_files/add_file", FakeResponse(200, {
                "status": 2, "hash": file_hash,
            })),
        ])
        ti = _hydrus_ti(session)
        ti.hydrus_import_unmatched = True
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "old-miss.jpg"
            media.write_bytes(b"old miss")
            st = media.stat()
            manager = LedgerManager()
            ledger = manager.get(root)
            ti.ledger_record(
                ledger, media.name, st.st_size, st.st_mtime,
                "a" * 32, "nomatch", [],
                unmatched_import={
                    "requested": False,
                    "complete": True,
                    "scope_id": None,
                })
            queue = [FileItem(
                media, media.name, st.st_size, st.st_mtime, "image",
                ledger=ledger, md5="a" * 32, mtime_ns=st.st_mtime_ns)]

            result = ti._hydrus_import_prior_nomatches(manager, queue)

            self.assertEqual(result.completed, 1)
            self.assertEqual(result.live, 1)
            self.assertEqual(queue, [])
            checkpoint = ledger.records[media.name]["unmatched_import"]
            self.assertTrue(checkpoint["complete"])
            self.assertEqual(checkpoint["scope_id"], ti._hydrus_scope_id())

    def test_failed_prior_reconciliation_stays_queued_with_checkpoint(self):
        session = FakeSession([
            ("POST", "add_files/add_file", FakeResponse(
                500, {}, text="temporary failure")),
        ])
        ti = _hydrus_ti(session)
        ti.hydrus_import_unmatched = True
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "old-miss.jpg"
            media.write_bytes(b"old miss")
            st = media.stat()
            manager = LedgerManager()
            ledger = manager.get(root)
            ti.ledger_record(
                ledger, media.name, st.st_size, st.st_mtime,
                "a" * 32, "nomatch", [])
            queue = [FileItem(
                media, media.name, st.st_size, st.st_mtime, "image",
                ledger=ledger, md5="a" * 32, mtime_ns=st.st_mtime_ns)]

            result = ti._hydrus_import_prior_nomatches(manager, queue)

            self.assertEqual(result.failed, 1)
            self.assertEqual(len(queue), 1)
            checkpoint = ledger.records[media.name]["unmatched_import"]
            self.assertFalse(checkpoint["complete"])
            self.assertEqual(
                checkpoint["import_state"], "retryable_failure")


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

    def test_deleted_no_duplicates_is_terminal_not_retried(self):
        """BF-07: status-3 + empty dups checkpoints and skips on re-sync."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = self._media_with_sidecar(root, data=b"deleted-bytes")
            session = FakeSession(_deleted_dup_routes([]))
            ti = _hydrus_ti(session)
            ti.hydrus_tag_deleted_duplicates = True
            ti.hydrus_can_manage_relationships = True

            attempted, failed = ti.sync_sidecars_to_hydrus(root)
            self.assertEqual(attempted, 1)
            self.assertEqual(failed, 0)

            ledger = Ledger(root)
            ledger.load()
            st = media.stat()
            tags, urls = ti.read_sidecar_payload(media)
            signature = ti._sidecar_sync_signature(tags, urls)
            rec = ledger.records[media.name]["sidecar_sync"]
            self.assertEqual(rec["disposition"], "deleted_no_duplicates")
            self.assertTrue(rec["complete"])
            self.assertEqual(rec["sha256"], DELETED_HASH)
            self.assertTrue(ledger.sidecar_sync_matches(
                media.name, st.st_size, st.st_mtime, signature,
                scope_id=ti._hydrus_scope_id(),
                tag_deleted_duplicates=True))

            # Second run: no new Hydrus calls.
            session.calls.clear()
            attempted2, failed2 = ti.sync_sidecars_to_hydrus(root)
            self.assertEqual((attempted2, failed2), (0, 0))
            self.assertEqual(session.calls, [])

    def test_deleted_policy_change_reopens_terminal_checkpoint(self):
        """Enabling deleted-dup tagging after a policy-skipped seal reopens."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = self._media_with_sidecar(root, data=b"deleted-bytes")
            session = FakeSession(_deleted_dup_routes([DUP_OK]))
            ti = _hydrus_ti(session)
            ti.hydrus_tag_deleted_duplicates = False
            ti.hydrus_can_manage_relationships = True

            attempted, failed = ti.sync_sidecars_to_hydrus(root)
            self.assertEqual((attempted, failed), (1, 0))
            ledger = Ledger(root)
            ledger.load()
            self.assertEqual(
                ledger.records[media.name]["sidecar_sync"]["disposition"],
                "deleted_policy_skipped")

            # Policy on → checkpoint no longer matches → re-attempt tags dups.
            ti.hydrus_tag_deleted_duplicates = True
            session.calls.clear()
            attempted2, failed2 = ti.sync_sidecars_to_hydrus(root)
            self.assertEqual((attempted2, failed2), (1, 0))
            self.assertTrue(any(
                "add_tags/add_tags" in url for _, url, _ in session.calls))
            ledger.load()
            self.assertEqual(
                ledger.records[media.name]["sidecar_sync"]["disposition"],
                "deleted_tagged_duplicates")

    def test_permission_missing_is_not_terminal_checkpoint(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = self._media_with_sidecar(root, data=b"deleted-bytes")
            session = FakeSession(_deleted_dup_routes([DUP_OK]))
            ti = _hydrus_ti(session)
            ti.hydrus_tag_deleted_duplicates = True
            ti.hydrus_can_manage_relationships = False

            attempted, failed = ti.sync_sidecars_to_hydrus(root)
            self.assertEqual((attempted, failed), (1, 1))
            ledger = Ledger(root)
            ledger.load()
            st = media.stat()
            tags, urls = ti.read_sidecar_payload(media)
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
        if settings is None:
            settings = Settings()
            # URL-downloader enrichment is now an opt-in legacy path; these
            # tests verify it remains available when explicitly enabled.
            settings.hydrus.exact_url_enrichment = True
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
        ti.hydrus_exact_url_enrichment = True
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

    def test_force_associate_url_skips_downloader(self):
        """Multi-file InkBunny URLs must associate, not queue add_url."""
        ib_url = "https://inkbunny.net/s/555"
        e6_url = "https://e621.net/posts/123"
        session = FakeSession([
            ("POST", "add_files/add_file", FakeResponse(200, {
                "status": 2, "hash": self.HASH,
            })),
            ("POST", "add_tags/add_tags", FakeResponse(200, {})),
            ("GET", "add_urls/get_url_info", FakeResponse(200, {
                "url_type": 0, "can_parse": True,
            })),
            ("POST", "add_urls/add_url", FakeResponse(200, {
                "human_result_text": "URL added successfully.",
            })),
            ("POST", "add_urls/associate_url", FakeResponse(200, {})),
        ])
        ti = _hydrus_ti(session)
        ti.hydrus_can_edit_urls = True
        ti.hydrus_exact_url_enrichment = True
        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "exact.jpg"
            media.write_bytes(b"exact bytes")
            ti._hydrus_push(
                media, {"creator:test"}, {ib_url, e6_url},
                exact_match=True,
                force_associate_urls={ib_url})

        add_calls = self._matching_calls(session, "add_urls/add_url")
        self.assertEqual(len(add_calls), 1)
        self.assertEqual(add_calls[0][2]["json"]["url"], e6_url)
        associated = self._matching_calls(session, "add_urls/associate_url")
        self.assertEqual(len(associated), 1)
        self.assertEqual(associated[0][2]["json"]["urls_to_add"], [ib_url])


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

        # Original deleted SHA is retained for diagnostics; only live dups
        # land on the duplicates results page (not the deleted original).
        self.assertEqual(self._push(ti), DELETED_HASH)
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

    def test_e621_http_failure_is_retryable_not_a_clean_miss(self):
        session = FakeSession(routes=[
            ("GET", "e621.net", FakeResponse(503, {"error": "busy"})),
        ])
        ti = TagIntegrator(settings=Settings(), session=session)
        ti.has_e621 = True
        ti.e621_username = "u"
        ti.e621_api_key = "k"
        ti.headers_e6 = {"User-Agent": "test"}
        ti.pace["e621"].wait = lambda: None
        with self.assertRaises(RetryableLookupError):
            ti.e621_lookup_by_md5("0" * 32)

    def test_inkbunny_search_http_failure_is_retryable_not_a_clean_miss(self):
        session = FakeSession(routes=[
            ("GET", "api_search.php", FakeResponse(503, {"error": "busy"})),
        ])
        ti = TagIntegrator(settings=Settings(), session=session)
        ti.has_inkbunny = True
        ti.ib_sid = "sid"
        ti.pace["inkbunny"].wait = lambda: None
        with self.assertRaises(RetryableLookupError):
            ti._inkbunny_search_md5("0" * 32)

    def test_inkbunny_login_outage_does_not_abort_credential_reload(self):
        session = FakeSession(routes=[
            ("GET", "api_login.php", FakeResponse(503, {"error": "busy"})),
        ])
        ti = TagIntegrator(settings=Settings(), session=session)
        with patch("furtag.notify"):
            ti.load_credentials({
                "inkbunny_username": "user",
                "inkbunny_password": "password",
            })
        self.assertFalse(ti.has_inkbunny)


def _auth_test_settings():
    """Offline settings with only e621 enabled as a hash source."""
    s = Settings()
    s.output.hydrus_enabled = False
    s.output.sidecars_enabled = True
    for name in ("e621", "inkbunny", "danbooru", "gelbooru",
                 "fluffle", "saucenao"):
        setattr(s.sources, f"{name}_enabled", False)
    s.sources.e621_enabled = True
    s.pdf.pdf_enabled = False
    return s


class TestAuthRejectionKeepsFilesUnresolved(unittest.TestCase):
    """HTTP 401/403 must never be laundered into a clean 'not found'.

    The ledger keys on (size, mtime); a file sealed as ``nomatch`` during a
    credential outage would stay skipped forever after the key is fixed.
    """

    def _e621_integrator(self, session):
        ti = TagIntegrator(settings=_auth_test_settings(), session=session)
        ti.has_e621 = True
        ti.enabled_e621 = True
        ti.e621_username = "u"
        ti.e621_api_key = "k"
        ti.headers_e6 = {"User-Agent": "test"}
        ti.pace["e621"].wait = lambda: None
        return ti

    def test_e621_auth_rejection_is_retryable_not_a_clean_miss(self):
        session = FakeSession(routes=[
            ("GET", "e621.net", FakeResponse(401, {"error": "denied"})),
        ])
        ti = self._e621_integrator(session)
        with patch("furtag.notify"), self.assertRaises(RetryableLookupError):
            ti.e621_lookup_by_md5("0" * 32)
        self.assertFalse(ti.has_e621)          # source disabled for the run
        self.assertIn("e621", ti.auth_rejected_sources)

    def test_danbooru_auth_rejection_is_retryable_not_a_clean_miss(self):
        session = FakeSession(routes=[
            ("GET", "danbooru.donmai.us", FakeResponse(403, {})),
        ])
        ti = TagIntegrator(settings=Settings(), session=session)
        ti.has_danbooru = True
        ti.danbooru_anon = True     # already anonymous: no fallback left
        ti.pace["danbooru"].wait = lambda: None
        with patch("furtag.notify"), self.assertRaises(RetryableLookupError):
            ti.danbooru_lookup_by_md5("0" * 32)
        self.assertFalse(ti.has_danbooru)
        self.assertIn("danbooru", ti.auth_rejected_sources)

    def test_gelbooru_auth_rejection_is_retryable_not_a_clean_miss(self):
        session = FakeSession(routes=[
            ("GET", "gelbooru.com", FakeResponse(401, {})),
        ])
        ti = TagIntegrator(settings=Settings(), session=session)
        ti.has_gelbooru = True
        ti.gelbooru_user_id = "1"
        ti.gelbooru_api_key = "k"
        ti.pace["gelbooru"].wait = lambda: None
        with patch("furtag.notify"), self.assertRaises(RetryableLookupError):
            ti.gelbooru_lookup_by_md5("0" * 32)
        self.assertFalse(ti.has_gelbooru)
        self.assertIn("gelbooru", ti.auth_rejected_sources)

    def test_later_files_are_not_sealed_after_the_source_is_disabled(self):
        """The source is gone from enabled_hash_services() — still not a miss."""
        import concurrent.futures as cf
        from furtag import FileItem

        session = FakeSession()
        ti = self._e621_integrator(session)
        ti.has_e621 = False                       # disabled by an earlier 401
        ti.auth_rejected_sources = {"e621"}
        self.assertEqual(ti.enabled_hash_services(), [])

        item = FileItem(path=Path("/tmp/later.mp4"), relpath="later.mp4",
                        size=1, mtime=0.0, kind="video", md5="0" * 32)
        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            ti.hash_tier(item, ex)
        self.assertEqual(item.lookup_errors, {"e621"})
        self.assertEqual(session.calls, [])       # no hammering of a bad key

    def test_run_with_rejecting_e621_leaves_no_file_resolved(self):
        import contextlib
        import io
        import shutil
        from furtag import RESOLVED_LEDGER_STATUSES
        from furtag_events import NullObserver
        from furtag_settings import RunOptions

        root = Path(tempfile.mkdtemp(prefix="furtag-auth-401-"))
        self.addCleanup(shutil.rmtree, root, True)
        names = ["a.mp4", "b.mp4", "c.mp4"]      # videos: hash tier only
        for i, name in enumerate(names):
            (root / name).write_bytes(b"not-real-video-%d" % i)

        session = FakeSession(routes=[
            ("GET", "e621.net", FakeResponse(401, {"error": "denied"})),
        ])
        ti = self._e621_integrator(session)
        with contextlib.redirect_stdout(io.StringIO()), patch("furtag.notify"):
            summary = ti.run(
                root,
                options=RunOptions(import_unmatched=False,
                                   result_page_limit=0,
                                   build_already_tagged_page=False,
                                   sync_sidecars=False, pdf_dpi=None),
                observer=NullObserver(), use_terminal_display=False)

        ledger = Ledger(root)
        ledger.load()
        for name in names:
            st = (root / name).stat()
            status = ledger.status_for(name, st.st_size, st.st_mtime)
            self.assertNotIn(
                status, RESOLVED_LEDGER_STATUSES,
                f"{name} was sealed as {status!r} during an auth outage")
        self.assertEqual(summary.unmatched, 0)
        # The rejecting API is asked exactly once, not once per file.
        self.assertEqual(len([c for c in session.calls if "e621" in c[1]]), 1)

    def test_credential_reload_clears_the_auth_rejection(self):
        session = FakeSession()
        ti = TagIntegrator(settings=Settings(), session=session)
        ti.auth_rejected_sources = {"e621"}
        with patch("furtag.notify"):
            ti.load_credentials({})
        self.assertEqual(ti.auth_rejected_sources, set())


if __name__ == "__main__":
    unittest.main()
