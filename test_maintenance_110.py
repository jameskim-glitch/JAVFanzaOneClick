import copy
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import entity_images
import image_upgrade as hq
import jav_fanza_oneclick as plugin
import maintenance
import stashdb_rescrape as rescrape
import studio_sync as sync
from test_image_upgrade import picture, SID, URL
from test_entity_images import SCENE
from test_stashdb_rescrape import Client as ActorClient


SOURCE = {"id": SID, "name": "Attackers", "aliases": ["アタッカーズ"], "urls": []}


class StudioClient:
    def __init__(self):
        common = {"urls": [], "stash_ids": [{"endpoint": hq.ENDPOINT, "stash_id": SID}], "image_path": "x",
                  "updated_at": "1", "parent_studio": None, "child_studios": [], "tags": [], "custom_fields": {},
                  "details": "", "rating100": None, "favorite": False, "ignore_auto_tag": False, "organized": False}
        self.rows = {"1": {**copy.deepcopy(common), "id": "1", "name": "Attackers", "aliases": []},
                     "2": {**copy.deepcopy(common), "id": "2", "name": "アタッカーズ", "aliases": ["custom alias"], "favorite": True}}
        self.refs = {kind: {"10": "2"} for kind in ("Scene", "Image", "Gallery", "Group")}
        self.backups = 0
        self.deleted = []
        self.updates = []
        self.fail_move = False
        self.change_source = False

    def graphql(self, query, variables=None, **kwargs):
        variables = variables or {}
        if "OneClickStudios(" in query:
            return {"findStudios": {"count": len(self.rows), "studios": copy.deepcopy(list(self.rows.values()))}}
        if "OneClickStudio(" in query:
            return {"findStudio": copy.deepcopy(self.rows.get(variables["id"]))}
        if "OneClickMaintenanceBackup" in query:
            self.backups += 1
            return {"backupDatabase": None}
        if "OneClickStudioReferences" in query:
            kind = next(k for k in self.refs if k + "FilterType" in query)
            collection = {"Scene":"scenes", "Image":"images", "Gallery":"galleries", "Group":"groups"}[kind]
            identifier = variables["filter"]["studios"]["value"][0]
            rows = [{"id": i, "studio": {"id": sid}} for i, sid in self.refs[kind].items() if sid == identifier]
            return {"find" + collection.capitalize(): {"count": len(rows), collection: rows}}
        if "OneClickStudioMove" in query:
            if self.fail_move:
                raise RuntimeError("simulated lost connection")
            kind = next(k for k in self.refs if "Bulk" + k in query)
            values = variables["input"]
            for identifier in values["ids"]:
                self.refs[kind][identifier] = values["studio_id"]
            if self.change_source:
                self.rows["2"]["details"] = "new manual note"
            return {"bulk" + kind + "Update": [{"id": i, "studio": {"id": values["studio_id"]}} for i in values["ids"]]}
        if "studioUpdate" in query:
            values = copy.deepcopy(variables["input"])
            other_claims = {sync.key(n) for r in self.rows.values() if r["id"] != values["id"] for n in [r["name"], *(r.get("aliases") or [])]}
            candidate = {**self.rows[values["id"]], **values}
            if other_claims & {sync.key(n) for n in [candidate["name"], *(candidate.get("aliases") or [])]}:
                raise ValueError("studio name or alias already exists")
            self.updates.append(values)
            target = self.rows[values["id"]]
            for k, v in values.items():
                if k == "parent_id":
                    target["parent_studio"] = {"id":v} if v else None
                elif k == "tag_ids":
                    target["tags"] = [{"id": i} for i in v]
                else:
                    target[k] = v
            target["updated_at"] += "x"
            return {"studioUpdate": {"id": target["id"]}}
        if "studioDestroy" in query:
            identifier = variables["input"]["id"]
            assert all(identifier not in refs.values() for refs in self.refs.values())
            self.deleted.append(identifier)
            del self.rows[identifier]
            return {"studioDestroy": True}
        raise AssertionError(query)


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_punctuation_cleanup_is_stable(self):
        for value in ("한국어 설명입니다!! 정말 좋습니다!!!", "한국어 설명?! 다음 문장!!"):
            cleaned = plugin.clean_existing_text(value, "details")
            self.assertEqual(cleaned, plugin.clean_existing_text(cleaned, "details"))
            self.assertFalse(plugin.details_need_refresh(cleaned))

    def test_existing_cover_survives_force_and_missing_cover_fills(self):
        for missing, force, replace in ((False, False, False), (False, True, False), (False, True, True), (True, False, False)):
            with self.subTest(missing=missing, force=force, replace=replace):
                scene = copy.deepcopy(SCENE)
                scene["paths"] = {"screenshot": "http://local/scene/77/screenshot?t=1"}
                client = Mock()
                client.find_scene.return_value = scene
                client.scene_cover_missing.return_value = missing
                with patch.object(plugin, "needs_processing", return_value=True), patch.object(plugin, "fetch_metadata", return_value={"image": "https://example.test/cover.jpg", "performers": []}):
                    plugin.process_scene(client, scene, plugin.StateStore(self.root), force=force, replace_cover=replace, images=Mock())
                writes = client.update_scene.call_args_list
                self.assertEqual(any("cover_image" in call.args[0] for call in writes), missing or replace)

    def test_verified_bridge_requires_correct_uuid_and_domain(self):
        sid, (domain, aliases) = next(iter(sync.OFFICIAL_ALIASES.items()))
        source = {"id": sid, "name": "Wanz Factory", "urls": [{"url":"https://" + domain}], "aliases": []}
        local = {"name": aliases[0]}
        self.assertEqual(sync.resolve(local, [source])["status"], "matched")
        source["urls"] = []
        self.assertEqual(sync.resolve(local, [source])["status"], "unmatched")

    def test_ambiguous_names_never_merge(self):
        self.assertEqual(sync.resolve({"name": "アタッカーズ"}, [SOURCE, {**SOURCE, "id":"different"}])["status"], "ambiguous")

    def test_parent_suffix_requires_verified_parent(self):
        local = {"name": "アタッカーズ/妄想族"}
        self.assertEqual(sync.resolve(local, [SOURCE])["status"], "unmatched")
        self.assertEqual(sync.resolve(local, [{**SOURCE, "parent": {"id":"04e2f54d-12db-490b-91fe-460cf334f70c"}}])["status"], "matched")

    def test_merge_preserves_all_relationship_kinds(self):
        client = StudioClient()
        with patch.object(hq, "source_record", return_value=SOURCE):
            result = sync.merge_group(client, self.root, ["1", "2"], maintenance.Backup(client))
        self.assertEqual(result["status"], "merged")
        self.assertEqual(client.deleted, ["2"])
        self.assertTrue(all(refs["10"] == "1" for refs in client.refs.values()))
        self.assertTrue(client.rows["1"]["favorite"])
        self.assertIn("custom alias", client.rows["1"]["aliases"])
        self.assertIn("アタッカーズ", client.rows["1"]["aliases"])
        self.assertEqual(client.backups, 1)

    def test_connection_loss_never_deletes_source(self):
        client = StudioClient()
        client.fail_move = True
        with patch.object(hq, "source_record", return_value=SOURCE), self.assertRaises(RuntimeError):
            sync.merge_group(client, self.root, ["1", "2"], maintenance.Backup(client))
        self.assertEqual(client.deleted, [])

    def test_manual_edit_during_merge_never_deletes_source(self):
        client = StudioClient()
        client.change_source = True
        with patch.object(hq, "source_record", return_value=SOURCE), self.assertRaises(RuntimeError):
            sync.merge_group(client, self.root, ["1", "2"], maintenance.Backup(client))
        self.assertEqual(client.deleted, [])

    def test_identity_mismatch_blocks_before_backup(self):
        client = StudioClient()
        client.rows["2"]["stash_ids"] = []
        with self.assertRaises(ValueError):
            sync.merge_group(client, self.root, ["1", "2"], maintenance.Backup(client))
        self.assertEqual(client.backups, 0)
        self.assertEqual(client.updates, [])

    def test_unchanged_studios_skip_backup(self):
        client = StudioClient()
        del client.rows["2"]
        client.rows["1"]["aliases"] = SOURCE["aliases"][:]
        with patch.object(sync.Directory, "load", return_value=[SOURCE]):
            result = sync.run(client, self.root, {"confirmed": True})
        self.assertTrue(result["ok"])
        self.assertEqual(client.backups, 0)
        self.assertEqual(client.updates, [])

    def test_sync_links_duplicates_without_conflicting_aliases(self):
        client = StudioClient()
        client.rows["2"]["stash_ids"] = []
        with patch.object(sync.Directory, "load", return_value=[SOURCE]):
            result = sync.run(client, self.root, {"confirmed":True})
        self.assertTrue(result["ok"])
        self.assertEqual(client.rows["2"]["name"], "アタッカーズ")
        self.assertEqual(hq.remote_id(client.rows["2"]), SID)

    def test_image_metric_cache_avoids_repeat_downloads(self):
        client = ActorClient()
        with patch.object(hq, "download", return_value=client.old) as download:
            for _ in range(2):
                best, _, errors = rescrape.best_image(client, client.item, {"images":[URL]}, "upgrade", self.root)
                self.assertIsNone(best)
                self.assertEqual(errors, [])
        self.assertEqual(download.call_count, 1)

    def test_noop_actor_run_skips_backup(self):
        client = ActorClient()
        with patch.object(hq, "download", return_value=client.new):
            rescrape.run(client, self.root, {"confirmed": True})
            client.backups = 0
            result = rescrape.run(client, self.root, {"confirmed": True})
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(client.backups, 0)

    def test_stashdb_fills_only_missing_image(self):
        client = Mock()
        entity = {"id":"1", "name":"Example", "stash_ids":[{"endpoint":hq.ENDPOINT, "stash_id":SID}], "image_path":"x?default=true"}
        with patch.object(hq, "entity", return_value=entity), patch.object(hq, "source_record", return_value={"images":[URL]}), patch.object(hq, "download", return_value=picture((80,120), "red")):
            result = entity_images.Enricher(client, self.root).fill("performer", entity)
        self.assertEqual(result["status"], "filled")
        client.update_entity_image.assert_called_once()

    def test_stashdb_preserves_picture_added_while_downloading(self):
        client = Mock()
        entity = {"id":"1", "name":"Example", "stash_ids":[{"endpoint":hq.ENDPOINT, "stash_id":SID}], "image_path":"x?default=true"}
        with patch.object(hq, "entity", side_effect=[entity, {**entity, "image_path":"saved"}]), patch.object(hq, "source_record", return_value={"images":[URL]}), patch.object(hq, "download", return_value=picture((80,120), "red")):
            result = entity_images.Enricher(client, self.root).fill("performer", entity)
        self.assertEqual(result["status"], "existing")
        client.update_entity_image.assert_not_called()


if __name__ == "__main__":
    unittest.main()
