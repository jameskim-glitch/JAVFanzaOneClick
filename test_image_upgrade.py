"""Image replacement and per-person name correction use synthetic data only."""
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image

import image_upgrade as hq


SID = "b2a5febd-6b92-4bab-9aea-22143fc3b487"
URL = "https://stashdb.org/images/69eb6b4b-d3a9-4fdf-b603-410c10379a00"


def picture(size=(160, 240), color="blue"):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "PNG")
    return hq.inspect_bytes(out.getvalue())


class Client:
    def __init__(self):
        self.item = {"id": "1", "name": "User name", "stash_ids": [{"endpoint": hq.ENDPOINT, "stash_id": SID}],
                     "image_path": "http://local/image", "updated_at": "1", "alias_list": [], "urls": [], "birthdate": None, "gender": "FEMALE"}
        self.picture = picture((40, 60), "red")
        self.source = {"remote_site_id": SID, "name": "Source name", "images": [URL], "image": URL, "urls": [], "gender": "FEMALE"}
        self.boxes = [{"endpoint": hq.ENDPOINT}]
        self.updates, self.queries = [], []
        self.fail_mutation = False

    def graphql(self, query, variables=None, **kwargs):
        self.queries.append((query, variables))
        if "OneClickHQSources" in query:
            return {"configuration": {"general": {"stashBoxes": self.boxes}}}
        if "OneClickHQSource(" in query:
            self.assert_source = variables["input"] == {"query": SID}
            key = "scrapeSinglePerformer" if "scrapeSinglePerformer(" in query else "scrapeSingleStudio"
            return {key: [copy.deepcopy(self.source)]}
        if "OneClickHQCatalog" in query:
            key, rows = ("findPerformers", "performers") if "findPerformers(" in query else ("findStudios", "studios")
            return {key: {"count": 1, rows: [copy.deepcopy(self.item)]}}
        if "OneClickHQEntity" in query:
            key = "findPerformer" if "findPerformer(" in query else "findStudio"
            return {key: copy.deepcopy(self.item)}
        raise AssertionError(query)

    def entity_image_data(self, kind, identifier):
        return self.picture["data"]

    def update_entity_image(self, kind, identifier, data):
        self.updates.append({"kind": kind, "id": identifier, "image": data})
        import base64
        self.picture = hq.inspect_bytes(base64.b64decode(data.split(",", 1)[1]))
        self.item["updated_at"] += "1"
        if self.fail_mutation:
            raise TimeoutError("response lost after commit")

    def update_performer(self, identifier, name, aliases):
        self.updates.append({"id": identifier, "name": name, "alias_list": aliases})
        self.item.update(name=name, alias_list=aliases, updated_at="changed")


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oneclick-hq-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.client = Client()
        self.new = picture()
        self.downloader = patch.object(hq, "download", return_value=self.new).start()
        self.addCleanup(patch.stopall)

    def preview(self, kind="performer"):
        found = hq.candidates(self.client, self.root, kind, "1")
        return hq.preview(self.client, self.root, {"ticket": found["ticket"], "url": found["urls"][0]})

    def apply(self, plan=None, **args):
        plan = plan or self.preview()
        return hq.apply(self.client, self.root, {"token": plan["token"], "confirmed": True, **args})

    def test_catalog_and_preview_never_write_stash(self):
        result = hq.catalog(self.client, "performer")
        self.assertEqual(result["linked"], 1)
        self.preview()
        self.assertEqual(self.client.updates, [])
        self.assertTrue(self.client.assert_source)
        self.assertTrue(all("api_key" not in q.lower() for q, _ in self.client.queries))

    def test_unlinked_source_refused_without_search_by_name(self):
        self.client.item["stash_ids"] = []
        self.assertEqual(hq.catalog(self.client, "performer")["linked"], 0)
        with self.assertRaises(ValueError): self.preview()
        self.assertFalse(any("OneClickHQSource(" in q for q, _ in self.client.queries))

    def test_conflicting_remote_ids_refused(self):
        self.client.item["stash_ids"].append({"endpoint": hq.ENDPOINT, "stash_id": "a" + SID[1:]})
        with self.assertRaises(ValueError): self.preview()

    def test_foreign_endpoint_cannot_supply_identity(self):
        self.client.item["stash_ids"][0]["endpoint"] = "https://stashdb.org.evil.test/graphql"
        with self.assertRaises(ValueError): self.preview()

    def test_unconfigured_stashdb_is_explained(self):
        self.client.boxes = []
        with self.assertRaisesRegex(ValueError, "StashDB 연결"): self.preview()

    def test_wrong_remote_record_refused(self):
        self.client.source["remote_site_id"] = "a" + SID[1:]
        with self.assertRaises(ValueError): self.preview()

    def test_profile_conflicts_refused(self):
        self.client.item["birthdate"] = "1990-01-01"
        self.client.source["birthdate"] = "1991-01-01"
        with self.assertRaises(ValueError): self.preview()

    def test_untrusted_source_image_is_not_presented(self):
        self.client.source["images"] = ["http://127.0.0.1/private", "https://evil.test/image"]
        result = hq.candidates(self.client, self.root, "performer", "1")
        self.assertEqual(result["urls"], [])
        self.assertEqual(result["rejected"], 2)

    def test_tampered_url_or_ticket_is_refused(self):
        found = hq.candidates(self.client, self.root, "performer", "1")
        with self.assertRaises(ValueError): hq.preview(self.client, self.root, {"ticket": found["ticket"], "url": URL + "?other"})
        with self.assertRaises(ValueError): hq.load_ticket(self.root, "../history", "preview")

    def test_confirmation_is_required(self):
        plan = self.preview()
        with self.assertRaises(ValueError): hq.apply(self.client, self.root, {"token": plan["token"]})
        self.assertEqual(self.client.updates, [])

    def test_existing_original_preserved_and_restore_works(self):
        original = copy.deepcopy(self.client.picture)
        result = self.apply()
        self.assertEqual(result["status"], "updated")
        record = json.loads(Path(result["history"]).read_text(encoding="utf-8"))
        self.assertEqual(record["old"], original)
        self.assertEqual(set(self.client.updates[0]), {"kind", "id", "image"})
        restored = hq.restore(self.client, self.root, {"token": result["restore_token"], "confirmed": True})
        self.assertTrue(restored["ok"])
        self.assertEqual(self.client.picture["data"], original["data"])

    def test_studio_uses_same_original_preservation(self):
        result = self.apply(self.preview("studio"))
        self.assertEqual(result["kind"], "studio")
        self.assertEqual(self.client.updates[0]["kind"], "studio")

    def test_smaller_image_requires_second_consent(self):
        self.downloader.return_value = picture((20, 20))
        plan = self.preview()
        self.assertTrue(plan["smaller"])
        with self.assertRaises(ValueError): self.apply(plan)
        self.assertEqual(self.client.updates, [])
        self.assertTrue(self.apply(plan, allow_smaller=True)["ok"])

    def test_identical_image_not_replaced(self):
        self.downloader.return_value = self.client.picture
        plan = self.preview()
        self.assertTrue(plan["same"])
        self.assertEqual(self.apply(plan)["status"], "unchanged")
        self.assertEqual(self.client.updates, [])

    def test_manual_entity_change_after_preview_wins(self):
        plan = self.preview()
        self.client.item["name"] = "User changed name"
        with self.assertRaises(ValueError): self.apply(plan)
        self.assertEqual(self.client.updates, [])

    def test_manual_picture_change_without_timestamp_wins(self):
        plan = self.preview()
        self.client.picture = picture((80, 100), "green")
        with self.assertRaises(ValueError): self.apply(plan)
        self.assertEqual(self.client.updates, [])

    def test_source_image_change_after_preview_refused(self):
        plan = self.preview()
        self.downloader.return_value = picture(color="green")
        with self.assertRaises(ValueError): self.apply(plan)
        self.assertEqual(self.client.updates, [])

    def test_original_preservation_failure_prevents_update(self):
        plan = self.preview()
        with patch.object(hq, "write_json", side_effect=OSError("disk full")):
            with self.assertRaises(OSError): self.apply(plan)
        self.assertEqual(self.client.updates, [])

    def test_expired_preview_requires_new_comparison(self):
        plan = self.preview()
        with patch.object(hq.time, "time", return_value=10**12):
            with self.assertRaises(ValueError): self.apply(plan)

    def test_lost_response_is_not_retried(self):
        self.client.fail_mutation = True
        result = self.apply()
        self.assertEqual(result["status"], "needs_check")
        self.assertEqual(len(self.client.updates), 1)

    def test_reusing_consumed_preview_is_blocked(self):
        plan = self.preview()
        self.apply(plan)
        with self.assertRaises(ValueError): self.apply(plan)
        self.assertEqual(len(self.client.updates), 1)

    def test_later_manual_image_cannot_be_overwritten_by_restore(self):
        result = self.apply()
        self.client.picture = picture(color="green")
        with self.assertRaises(ValueError): hq.restore(self.client, self.root, {"token": result["restore_token"], "confirmed": True})
        self.assertEqual(len(self.client.updates), 1)

    def test_name_preview_and_explicit_selection_preserve_old_aliases(self):
        self.client.item["urls"] = ["https://www.dmm.co.jp/digital/videoa/-/list/=/article=actress/id=1078559/"]
        self.client.item["name"] = "닮은 햇살"
        profile = {"name_ja": "似鳥（似鳥日菜）", "name_ko": "니토리", "name_reading": "にとり", "remote_site_id": "1078559",
                   "name_aliases": ["似鳥", "似鳥日菜", "Nitori", "Nitori Hina", "니토리", "니토리 히나"]}
        with patch.object(hq, "Resolver") as resolver:
            resolver.return_value.profile.return_value = profile
            plan = hq.name_preview(self.client, self.root, {"id": "1"})
            self.assertEqual(self.client.updates, [])
            self.assertEqual(plan["options"], ["니토리", "니토리 히나"])
            with self.assertRaises(ValueError): hq.name_apply(self.client, self.root, {"token": plan["token"], "confirmed": True, "name": "Arbitrary"})
            result = hq.name_apply(self.client, self.root, {"token": plan["token"], "confirmed": True, "name": "니토리 히나"})
            self.assertTrue(result["ok"])
            self.assertIn("닮은 햇살", self.client.item["alias_list"])
            self.assertIn("似鳥日菜", self.client.item["alias_list"])
            resolver.return_value.mark_applied.assert_called_once_with("1", "1078559", "니토리 히나", manual=True)
            self.assertNotIn("image", self.client.updates[0])


class ImageSafetyTests(unittest.TestCase):
    def test_allowlisted_original_urls_only(self):
        self.assertEqual(hq.image_url(URL), URL)
        for value in [URL + "?key=secret", URL + "#hash", "http" + URL[5:], URL.replace("stashdb.org", "stashdb.org.evil.test"),
                      URL.replace("stashdb.org", "user:secret@stashdb.org"), "https://stashdb.org/private", "file:///test", "http://127.0.0.1/image"]:
            with self.subTest(value=value), self.assertRaises(ValueError): hq.image_url(value)

    def test_redirect_cannot_leak_to_another_host(self):
        with self.assertRaises(ValueError): hq.ImageRedirect().redirect_request(Mock(), None, 302, "", {}, "http://127.0.0.1/private")

    def test_corrupt_image_rejected(self):
        for data in [b"", b"<html>Not an image</html>", b"GIF89a"]:
            with self.subTest(data=data), self.assertRaises(Exception): hq.inspect_bytes(data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
