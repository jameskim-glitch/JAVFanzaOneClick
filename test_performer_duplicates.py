"""Duplicate detection and destructive-operation guards, using synthetic records only."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from PIL import Image

import jav_fanza_oneclick as backend
import performer_duplicates as duplicates


def person(identifier, name="사쿠라 모모", remote="1039157", **extra):
    result = {"id": str(identifier), "name": name, "alias_list": [], "urls": [], "stash_ids": [],
              "birthdate": None, "gender": "FEMALE", "favorite": False, "ignore_auto_tag": False,
              "tags": [], "custom_fields": {}, "image_path": None, "scene_count": 1,
              "image_count": 0, "gallery_count": 0, "updated_at": "2026-10-01T00:00:00Z"}
    if remote:
        result["urls"] = [f"https://www.dmm.co.jp/digital/videoa/-/list/=/article=actress/id={remote}/"]
    return {**result, **extra}


class Client:
    def __init__(self, people=None):
        self.people = {p["id"]: copy.deepcopy(p) for p in people or [person(1), person(2, "Momo Sakura")]}
        self.requests = []
        self.backup_error = False
        # The real Stash 0.31.1 resolver returns null for download=false success.
        self.backup_response = {"backupDatabase": None}
        self.merge_error = False
        self.after_backup = None
        self.links = {"1": {"scene-a"}, "2": {"scene-a", "scene-b"}}

    def graphql(self, query, variables=None, **kwargs):
        variables = variables or {}
        self.requests.append({"query": query, "variables": copy.deepcopy(variables)})
        if "findPerformers(" in query:
            page, size = variables["filter"]["page"], variables["filter"]["per_page"]
            people = sorted(self.people.values(), key=lambda p: int(p["id"]))
            return {"findPerformers": {"count": len(people), "performers": copy.deepcopy(people[(page-1)*size:page*size])}}
        if "findPerformer(" in query:
            return {"findPerformer": copy.deepcopy(self.people.get(variables["id"]))}
        if "backupDatabase(" in query:
            if self.backup_error:
                raise RuntimeError("backup failed")
            if self.after_backup:
                self.after_backup()
            return copy.deepcopy(self.backup_response)
        if "performerMerge(" in query:
            if self.merge_error:
                raise TimeoutError("ambiguous response")
            item = variables["input"]
            destination = item["destination"]
            self.people[destination].update({k: v for k, v in item["values"].items() if k != "custom_fields"})
            for source in item["source"]:
                self.links.setdefault(destination, set()).update(self.links.pop(source, set()))
                del self.people[source]
            return {"performerMerge": {"id": destination}}
        raise AssertionError(query)

    def performer_image_data(self, identifier):
        return "data:image/png;base64,synthetic"

    def mutations(self):
        return [p for p in self.requests if p["query"].startswith("mutation")]


class DetectionTests(unittest.TestCase):
    def test_same_source_matches_unrelated_names(self):
        result = duplicates.discover([person(1, "벚꽃 하늘 모모"), person(2, "Momo Sakura")])
        self.assertEqual(result["identifier_matches"], 1)

    def test_different_fanza_identity_blocks_name_match(self):
        result = duplicates.classify(person(1), person(2, remote="99999"))
        self.assertEqual(result["confidence"], "blocked")

    def test_stash_ids_are_scoped_to_endpoint(self):
        sid = "12345678-1234-1234-1234-123456789abc"
        a = person(1, remote=None, stash_ids=[{"endpoint": "https://box.test/graphql/", "stash_id": sid}])
        b = person(2, name="Different Name", remote=None, stash_ids=[{"endpoint": "https://BOX.test/graphql", "stash_id": sid}])
        self.assertEqual(duplicates.classify(a, b)["confidence"], "identifier")
        b["stash_ids"][0]["endpoint"] = "https://other.test/graphql"
        self.assertIsNone(duplicates.classify(a, b))

    def test_conflicting_stash_ids_block_even_same_fanza(self):
        a, b = person(1), person(2)
        for p, tail in [(a, "a"), (b, "b")]:
            p["stash_ids"] = [{"endpoint": "https://box.test/graphql", "stash_id": "12345678-1234-1234-1234-123456789ab" + tail}]
        self.assertEqual(duplicates.classify(a, b)["confidence"], "blocked")

    def test_birthdate_conflict_blocks_shared_id(self):
        self.assertEqual(duplicates.classify(person(1, birthdate="1995-01-01"), person(2, birthdate="1996-01-01"))["confidence"], "blocked")

    def test_partial_birthdate_not_false_conflict(self):
        self.assertEqual(duplicates.classify(person(1, birthdate="1995"), person(2, birthdate="1995-01-01"))["confidence"], "identifier")

    def test_roman_order_and_whitespace_are_review_only(self):
        self.assertEqual(duplicates.classify(person(1, "Momo Sakura", None), person(2, "sakura momo", None))["confidence"], "review")
        self.assertEqual(duplicates.classify(person(1, "사쿠라 모모", None), person(2, "사쿠라모모", None))["confidence"], "review")

    def test_short_alias_and_fuzzy_spelling_not_used(self):
        self.assertIsNone(duplicates.classify(person(1, "유이", None), person(2, "유이", None)))
        self.assertIsNone(duplicates.classify(person(1, "Momo Sakura", None), person(2, "Momo Sakurai", None)))

    def test_parenthetical_old_names_are_discovery_aliases_not_renamed(self):
        a, b = person(1, "新名（旧名前、昔の名前）", None), person(2, "旧名前", None)
        self.assertEqual(duplicates.classify(a, b)["confidence"], "review")
        self.assertEqual(a["name"], "新名（旧名前、昔の名前）")

    def test_no_transitive_grouping(self):
        people = [person(1, "Alpha Name", None), person(2, "Alpha Name", None, alias_list=["Gamma Name"]), person(3, "Gamma Name", None)]
        self.assertEqual([p["ids"] for p in duplicates.discover(people)["candidates"]], [["1", "2"], ["2", "3"]])

    def test_ambiguous_single_record_id_blocks(self):
        a, b = person(1), person(2)
        a["urls"] += person(3, remote="99999")["urls"]
        self.assertEqual(duplicates.classify(a, b)["confidence"], "blocked")

    def test_common_name_buckets_are_limited(self):
        result = duplicates.discover([person(i, "Common Name", None) for i in range(1, 23)])
        self.assertEqual(result["total_candidates"], 0)
        self.assertGreater(result["ignored_common_keys"], 0)

    def test_pagination_reads_every_actor_without_mutation(self):
        client = Client([person(i, f"Actor {i}", str(i)) for i in range(1, 228)])
        self.assertEqual(duplicates.scan(client)["checked"], 227)
        self.assertEqual(len(client.requests), 3)
        self.assertEqual(client.mutations(), [])

    def test_duplicate_page_ids_refused(self):
        with self.assertRaises(ValueError): duplicates.discover([person(1), person(1)])


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oneclick-merge-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.client = Client()
        self.args = {"ids": ["1", "2"], "destination": "1"}

    def confirmed(self):
        preview = duplicates.prepare(self.client, self.args)
        return {**self.args, "token": preview["token"], "confirmed": True, "reviewed_identity": True}

    def test_preview_has_no_mutations_or_history(self):
        result = duplicates.prepare(self.client, self.args)
        self.assertEqual(result["source"], "2")
        self.assertEqual(self.client.mutations(), [])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_confirmation_required(self):
        with self.assertRaises(ValueError): duplicates.merge(self.client, self.root, self.args)
        self.assertEqual(self.client.mutations(), [])

    def test_stale_manual_edit_blocks_before_backup(self):
        args = self.confirmed()
        self.client.people["1"]["name"] = "My own edit"
        with self.assertRaises(ValueError): duplicates.merge(self.client, self.root, args)
        self.assertEqual(self.client.mutations(), [])

    def test_backup_failure_prevents_merge(self):
        args = self.confirmed(); self.client.backup_error = True
        result = duplicates.merge(self.client, self.root, args)
        self.assertEqual(result["status"], "not_merged")
        self.assertEqual(result["backup_status"], "unconfirmed")
        self.assertEqual(len(self.client.people), 2)
        self.assertEqual(len(self.client.mutations()), 1)

    def test_missing_backup_result_prevents_merge(self):
        self.client.backup_response = {}
        result = duplicates.merge(self.client, self.root, self.confirmed())
        self.assertEqual(result["status"], "not_merged")
        self.assertEqual(result["backup_status"], "unconfirmed")
        self.assertEqual(len(self.client.people), 2)
        self.assertEqual(len(self.client.mutations()), 1)

    def test_wrong_backup_types_and_empty_strings_prevent_merge(self):
        for value in [True, False, 0, 1, {}, [], "", "  "]:
            with self.subTest(value=value):
                self.client.backup_response = {"backupDatabase": value}
                result = duplicates.merge(self.client, self.root, self.confirmed())
                self.assertEqual(result["status"], "not_merged")
                self.assertEqual(result["backup_status"], "unconfirmed")
                self.assertEqual(len(self.client.people), 2)
        self.assertFalse(any("performerMerge" in req["query"] for req in self.client.requests))

    def test_backup_path_response_remains_supported(self):
        self.client.backup_response = {"backupDatabase": "C:/test-only/stash-before-merge.zip"}
        result = duplicates.merge(self.client, self.root, self.confirmed())
        self.assertEqual(result["status"], "merged")
        self.assertEqual(result["backup_status"], "completed")
        self.assertEqual(result["backup"], "C:/test-only/stash-before-merge.zip")

    def test_manual_change_during_backup_prevents_merge(self):
        args = self.confirmed()
        self.client.after_backup = lambda: self.client.people["2"].update(name="Changed")
        result = duplicates.merge(self.client, self.root, args)
        self.assertEqual(result["status"], "not_merged")
        self.assertEqual(result["backup_status"], "completed")
        self.assertEqual(len(self.client.people), 2)
        self.assertEqual(len(self.client.mutations()), 1)

    def test_history_failure_prevents_any_mutations(self):
        args = self.confirmed()
        with patch.object(duplicates, "write_json", side_effect=OSError("disk full")):
            result = duplicates.merge(self.client, self.root, args)
        self.assertEqual(result["status"], "not_merged")
        self.assertEqual(result["backup_status"], "not_requested")
        self.assertEqual(self.client.mutations(), [])

    def test_photo_failure_after_backup_is_definite_non_merge(self):
        self.client.people["2"]["image_path"] = "http://local/performer/2/image"
        with patch.object(self.client, "performer_image_data", side_effect=OSError("photo unavailable")):
            result = duplicates.merge(self.client, self.root, self.confirmed())
        self.assertEqual(result["status"], "not_merged")
        self.assertEqual(result["backup_status"], "completed")
        self.assertEqual(len(self.client.people), 2)
        self.assertEqual(len(self.client.mutations()), 1)

    def test_weak_match_requires_additional_identity_review(self):
        self.client.people["1"]["urls"] = []
        self.client.people["2"].update(name="사쿠라모모", urls=[])
        args = {**self.confirmed(), "reviewed_identity": False}
        with self.assertRaises(ValueError): duplicates.merge(self.client, self.root, args)
        self.assertEqual(self.client.mutations(), [])

    def test_backup_then_one_atomic_merge_and_history(self):
        result = duplicates.merge(self.client, self.root, self.confirmed())
        self.assertTrue(result["ok"])
        self.assertIsNone(result["backup"])
        self.assertEqual(result["backup_status"], "completed")
        mutations = self.client.mutations()
        self.assertIn("backupDatabase", mutations[0]["query"])
        self.assertEqual(mutations[0]["variables"]["input"], {"download": False, "includeBlobs": True})
        self.assertIn("performerMerge", mutations[1]["query"])
        self.assertEqual(mutations[1]["variables"]["input"]["source"], ["2"])
        self.assertEqual(self.client.links["1"], {"scene-a", "scene-b"})
        self.assertEqual(self.client.people["1"]["name"], "사쿠라 모모")
        self.assertIn("Momo Sakura", self.client.people["1"]["alias_list"])
        history = json.loads(Path(result["history"]).read_text(encoding="utf-8"))
        self.assertEqual(history["status"], "merged")
        self.assertEqual(history["backup_status"], "completed")
        self.assertIsNone(history["backup"])
        self.assertEqual(len(history["before"]), 2)

    def test_no_automatic_retry_on_unknown_merge_result(self):
        args = self.confirmed(); self.client.merge_error = True
        result = duplicates.merge(self.client, self.root, args)
        self.assertEqual(result["status"], "needs_check")
        self.assertEqual(len(self.client.mutations()), 2)
        self.assertIsNone(result["backup"])
        self.assertEqual(result["backup_status"], "completed")

    def test_double_submission_cannot_merge_again(self):
        args = self.confirmed()
        duplicates.merge(self.client, self.root, args)
        with self.assertRaises(ValueError): duplicates.merge(self.client, self.root, args)
        self.assertEqual(len(self.client.mutations()), 2)

    def test_conflicting_fields_preserve_destination_and_show_other(self):
        a, b = self.client.people.values()
        a.update(height_cm=160, details="User memo", custom_fields={"style": "mine"})
        b.update(height_cm=165, favorite=True, details="Other memo", custom_fields={"style": "other", "note": "keep"})
        preview = duplicates.prepare(self.client, self.args)
        self.assertNotIn("height_cm", preview["values"])
        self.assertNotIn("name", preview["values"])
        self.assertTrue(preview["values"]["favorite"])
        self.assertIn("User memo", preview["values"]["details"])
        self.assertIn("Other memo", preview["values"]["details"])
        self.assertEqual(preview["values"]["custom_fields"]["full"], {"style": "mine", "note": "keep"})
        self.assertEqual(len(preview["field_conflicts"]), 2)

    def test_source_image_only_copied_if_destination_missing(self):
        self.client.people["2"]["image_path"] = "http://local/performer/2/image"
        preview = duplicates.prepare(self.client, self.args)
        self.assertEqual(preview["image_from"], "2")
        result = duplicates.merge(self.client, self.root, self.confirmed())
        self.assertTrue(result["ok"])
        self.assertEqual(self.client.mutations()[1]["variables"]["input"]["values"]["image"], "data:image/png;base64,synthetic")

    def test_existing_destination_image_is_not_replaced(self):
        for p in self.client.people.values(): p["image_path"] = "http://local/image"
        self.assertIsNone(duplicates.prepare(self.client, self.args)["image_from"])

    def test_invalid_pair_and_non_candidate_rejected(self):
        for args in [{"ids": ["1", "1"], "destination": "1"}, {"ids": ["1", "2", "3"], "destination": "1"},
                     {"ids": ["../1", "2"], "destination": "2"}, {"ids": ["1", "2"], "destination": "3"}]:
            with self.subTest(args=args), self.assertRaises(ValueError): duplicates.prepare(self.client, args)
        self.client.people["2"] = person(2, "Unrelated Person", "999")
        with self.assertRaises(ValueError): duplicates.prepare(self.client, self.args)


class ProtocolTests(unittest.TestCase):
    def start_server(self):
        client = Client()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                try:
                    result = {"data": client.graphql(payload["query"], payload.get("variables"))}
                except Exception as exc:
                    result = {"errors": [{"message": str(exc)}]}
                    if "backupDatabase(" in payload["query"]:
                        # HTTP 200 and a null value are not success if errors exist.
                        result["data"] = {"backupDatabase": None}
                raw = json.dumps(result).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
            def do_GET(self):
                assert self.path == "/performer/2/image"
                output = io.BytesIO(); Image.new("RGB", (24, 32), "blue").save(output, "PNG")
                raw = output.getvalue()
                self.send_response(200); self.end_headers(); self.wfile.write(raw)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        return client, server

    def operation(self, server, tmp, args):
        command = [os.environ["ONECLICK_BACKEND_EXE"]] if os.environ.get("ONECLICK_BACKEND_EXE") else [sys.executable, str(Path(__file__).with_name("jav_fanza_oneclick.py"))]
        raw = {"args": args, "server_connection": {"Endpoint": f"http://127.0.0.1:{server.server_port}/graphql", "PluginDir": tmp}}
        result = subprocess.run(command, input=json.dumps(raw), text=True, encoding="utf-8", capture_output=True,
                                timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["output"]

    def test_source_or_packaged_backend_read_scan_and_confirmed_merge(self):
        client, server = self.start_server()
        with tempfile.TemporaryDirectory(prefix="oneclick-duplicate-protocol-") as tmp:
            def operation(args):
                return self.operation(server, tmp, args)
            result = operation({"mode": "duplicates_scan"})
            self.assertEqual(result["identifier_matches"], 1)
            self.assertEqual(client.mutations(), [])
            client.people["2"]["image_path"] = f"http://127.0.0.1:{server.server_port}/performer/2/image"
            args = {"ids": ["1", "2"], "destination": "1"}
            preview = operation({"mode": "duplicates_prepare", **args})
            result = operation({"mode": "duplicates_merge", **args, "token": preview["token"], "confirmed": True})
            self.assertEqual(result["status"], "merged")
            self.assertEqual(result["backup_status"], "completed")
            self.assertIsNone(result["backup"])
            self.assertEqual(len(client.mutations()), 2)
            self.assertTrue(client.mutations()[1]["variables"]["input"]["values"]["image"].startswith("data:image/png;base64,"))

    def test_graphql_error_with_null_and_http_200_never_merges(self):
        client, server = self.start_server()
        client.backup_error = True
        with tempfile.TemporaryDirectory(prefix="oneclick-backup-error-") as tmp:
            args = {"ids": ["1", "2"], "destination": "1"}
            preview = duplicates.prepare(client, args)
            result = self.operation(server, tmp, {"mode": "duplicates_merge", **args, "token": preview["token"], "confirmed": True})
            self.assertEqual(result["status"], "not_merged")
            self.assertEqual(result["backup_status"], "unconfirmed")
            self.assertIn("backup failed", result["error"])
            self.assertEqual(len(client.people), 2)
            self.assertEqual(len(client.mutations()), 1)

    def test_http_200_with_missing_backup_field_never_merges(self):
        client, server = self.start_server()
        client.backup_response = {}
        with tempfile.TemporaryDirectory(prefix="oneclick-backup-missing-") as tmp:
            args = {"ids": ["1", "2"], "destination": "1"}
            preview = duplicates.prepare(client, args)
            result = self.operation(server, tmp, {"mode": "duplicates_merge", **args, "token": preview["token"], "confirmed": True})
            self.assertEqual(result["status"], "not_merged")
            self.assertEqual(result["backup_status"], "unconfirmed")
            self.assertEqual(len(client.people), 2)
            self.assertEqual(len(client.mutations()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
