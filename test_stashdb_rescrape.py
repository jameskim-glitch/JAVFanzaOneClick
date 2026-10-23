"""Check mass renaming/rescraping against a synthetic Stash, never the library."""
import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import image_upgrade as hq
import stashdb_lookup as lookup
import stashdb_rescrape as scrape
from test_image_upgrade import picture, SID, URL


class Client:
    def __init__(self):
        self.item = {"id": "1", "name": "사쿠라 모모", "alias_list": ["Sakura Momo", "桜空もも", "사용자 별명"],
                     "urls": [], "stash_ids": [], "image_path": "http://local/image", "updated_at": "1",
                     "birthdate": None, "gender": "FEMALE", "details": "직접 입력한 소개", "favorite": True, "rating100": 90,
                     "tags": [{"id": "5", "name": "Keep"}], "height_cm": None}
        self.source = {"name": "Momo Sakura", "aliases": "Sakura Momo, 桜空もも", "remote_site_id": SID,
                       "urls": ["https://example.test/profile"], "images": [URL], "gender": "FEMALE", "birthdate": "1996-12-03",
                       "height": "160", "details": "Source bio"}
        self.rows = [self.item]
        self.results = [self.source]
        self.old = picture((80, 120), "red")
        self.new = picture((160, 240), "blue")
        self.picture = self.old
        self.queries, self.updates, self.backups = [], [], 0
        self.backup_response = {"backupDatabase": None}
        self.lose_response = False

    def graphql(self, query, variables=None, **kwargs):
        self.queries.append((query, variables))
        if "OneClickHQSources" in query:
            return {"configuration": {"general": {"stashBoxes": [{"endpoint": lookup.ENDPOINT}]}}}
        if "OneClickAliasPeople" in query:
            return {"findPerformers": {"count": len(self.rows), "performers": copy.deepcopy(self.rows)}}
        if "OneClickAliasPerson" in query:
            return {"findPerformer": copy.deepcopy(next(p for p in self.rows if p["id"] == variables["id"]))}
        if "scrapeSinglePerformer(" in query:
            term = variables["input"]["query"]
            rows = [self.source] if term == SID else self.results
            return {"scrapeSinglePerformer": copy.deepcopy(rows)}
        if "OneClickAliasBackup" in query:
            self.backups += 1
            self.assert_backup = variables["input"] == {"download": False, "includeBlobs": True}
            if isinstance(self.backup_response, Exception):
                raise self.backup_response
            return self.backup_response
        if "OneClickAliasUpdate" in query:
            values = copy.deepcopy(variables["input"])
            self.updates.append(values)
            item = next(p for p in self.rows if p["id"] == values["id"])
            item.update({k: v for k, v in values.items() if k != "image"})
            item["updated_at"] += "x"
            if "image" in values:
                import base64
                self.picture = hq.inspect_bytes(base64.b64decode(values["image"].split(",", 1)[1]))
            if self.lose_response:
                raise TimeoutError("response lost after commit")
            return {"performerUpdate": {"id": values["id"]}}
        raise AssertionError(query)

    def entity_image_data(self, kind, identifier):
        return self.picture["data"]


class RescrapeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oneclick-alias-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.client = Client()
        self.downloader = patch.object(hq, "download", return_value=self.client.new).start()
        self.addCleanup(patch.stopall)

    def run_bulk(self, **args):
        return scrape.run(self.client, self.root, {"confirmed": True, **args})

    def test_inventory_reads_aliases_without_search_or_changes(self):
        result = scrape.scan(self.client, {})
        self.assertEqual(result["eligible"], 1)
        self.assertEqual(result["items"][0]["english_aliases"], ["Sakura Momo"])
        self.assertEqual(self.client.updates, [])
        self.assertFalse(any("scrapeSingle" in query for query, _ in self.client.queries))

    def test_alias_promoted_in_stashdb_order_then_profile_saved(self):
        result = self.run_bulk()
        self.assertTrue(result["ok"])
        self.assertEqual((result["renamed"], result["scraped"]), (1, 1))
        self.assertEqual(self.client.item["name"], "Momo Sakura")
        self.assertIn("사쿠라 모모", self.client.item["alias_list"])
        self.assertIn("사용자 별명", self.client.item["alias_list"])
        self.assertEqual(self.client.item["birthdate"], "1996-12-03")
        self.assertEqual(self.client.item["height_cm"], 160)
        self.assertEqual(self.client.item["details"], "직접 입력한 소개")
        self.assertEqual(hq.remote_id(self.client.item), SID)
        self.assertTrue(self.client.assert_backup)
        self.assertFalse(set(self.client.updates[0]) & {"tag_ids", "favorite", "rating100"})
        self.assertEqual(self.client.picture, self.client.old)

    def test_loose_first_hit_is_ignored(self):
        wrong = {**self.client.source, "name": "Momo", "aliases": None, "remote_site_id": "a" + SID[1:]}
        self.client.results = [wrong, self.client.source]
        self.assertEqual(self.run_bulk()["renamed"], 1)
        self.assertEqual(hq.remote_id(self.client.item), SID)

    def test_ambiguous_scrape_keeps_metadata_but_promotes_existing_alias(self):
        self.client.results += [{**self.client.source, "remote_site_id": "a" + SID[1:]}]
        result = self.run_bulk()
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["results"][0]["status"], "renamed_only")
        self.assertEqual(self.client.item["name"], "Sakura Momo")
        self.assertEqual(set(self.client.updates[0]), {"id", "name", "alias_list"})
        self.assertEqual(self.client.item["stash_ids"], [])

    def test_no_english_alias_does_not_generate_one(self):
        self.client.item["alias_list"] = ["桜空もも", "사용자 별명"]
        result = self.run_bulk()
        self.assertEqual(result["results"][0]["status"], "no_english_alias")
        self.assertEqual(self.client.item["name"], "사쿠라 모모")
        self.assertEqual(self.client.updates, [])

    def test_conflicting_search_profile_promotes_alias_without_overwriting_profile(self):
        self.client.item["birthdate"] = "1990-01-01"
        self.assertEqual(self.run_bulk()["skipped"], 1)
        self.assertEqual(set(self.client.updates[0]), {"id", "name", "alias_list"})
        self.assertEqual(self.client.item["birthdate"], "1990-01-01")

    def test_multiple_stashdb_ids_are_not_overwritten(self):
        self.client.item["stash_ids"] = [{"endpoint": lookup.ENDPOINT, "stash_id": SID}, {"endpoint": lookup.ENDPOINT, "stash_id": "a" + SID[1:]}]
        self.assertEqual(self.run_bulk()["results"][0]["status"], "conflict")
        self.assertEqual(self.client.updates, [])

    def test_existing_uuid_uses_exact_source_without_name_search(self):
        self.client.item["stash_ids"] = [{"endpoint": lookup.ENDPOINT, "stash_id": SID}]
        self.run_bulk()
        terms = [v["input"]["query"] for q, v in self.client.queries if "scrapeSinglePerformer" in q]
        self.assertEqual(terms, [SID])

    def test_backup_missing_field_refuses_all_changes(self):
        self.client.backup_response = {}
        result = self.run_bulk()
        self.assertFalse(result["ok"])
        self.assertIn("stop_reason", result)
        self.assertEqual(self.client.updates, [])

    def test_backup_graphql_error_refuses_changes(self):
        self.client.backup_response = RuntimeError("GraphQL error")
        self.assertFalse(self.run_bulk()["ok"])
        self.assertEqual(self.client.updates, [])

    def test_upgrade_keeps_original_and_saves_larger_photo(self):
        result = self.run_bulk(photo_policy="upgrade")
        self.assertEqual(result["photos"], 1)
        import json
        record = json.loads(Path(result["results"][0]["history"]).read_text(encoding="utf-8"))
        self.assertEqual(record["old_image"]["sha256"], self.client.old["sha256"])
        self.assertEqual(self.client.picture["sha256"], self.client.new["sha256"])

    def test_smaller_photo_is_preserved_during_profile_rescrape(self):
        self.downloader.return_value = picture((40, 60), "blue")
        self.assertEqual(self.run_bulk(photo_policy="upgrade")["photos"], 0)
        self.assertNotIn("image", self.client.updates[0])

    def test_missing_photo_is_filled(self):
        self.client.item["image_path"] = "http://local/image?default=true"
        # A real Stash clears the placeholder flag after imageUpdate.
        original = self.client.graphql
        def graphql(q, v=None, **kw):
            response = original(q, v, **kw)
            if "OneClickAliasUpdate" in q:
                self.client.item["image_path"] = "http://local/image"
            return response
        self.client.graphql = graphql
        self.assertEqual(self.run_bulk()["photos"], 1)

    def test_lost_update_response_stops_batch_without_retry(self):
        self.client.rows.append({**copy.deepcopy(self.client.item), "id": "2"})
        self.client.lose_response = True
        result = self.run_bulk()
        self.assertEqual((result["needs_check"], result["checked"]), (1, 1))
        self.assertEqual(len(self.client.updates), 1)
        self.assertEqual(self.client.rows[1]["name"], "사쿠라 모모")

    def test_no_confirmation_does_not_query_or_change(self):
        with self.assertRaises(ValueError):
            scrape.run(self.client, self.root, {})
        self.assertEqual(self.client.queries, [])

    def test_disk_failure_does_not_change_stash(self):
        with patch.object(scrape, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): self.run_bulk()
        self.assertEqual(self.client.updates, [])

    def test_single_short_stage_name_needs_additional_identity(self):
        self.client.item["alias_list"] = ["Momo"]
        self.client.source.update(name="Momo", aliases="")
        self.assertEqual(self.run_bulk()["skipped"], 1)
        self.assertEqual(set(self.client.updates[0]), {"id", "name", "alias_list"})
        self.assertEqual(self.client.item["stash_ids"], [])

    def test_nullable_search_result_is_an_empty_result(self):
        client = type("Empty", (), {"graphql": lambda *args, **kw: {"scrapeSinglePerformer": None}})()
        self.assertEqual(lookup.fetch(client, "performer", "Momo Sakura", lookup.ENDPOINT), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
