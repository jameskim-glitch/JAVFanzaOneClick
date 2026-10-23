"""Offline regression tests plus opt-in FANZA / packaged-backend smoke tests."""
import base64
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
from unittest.mock import Mock, patch

from PIL import Image

import entity_images as images
import jav_fanza_oneclick as backend


PHOTO_URL = "https://awsimgsrc.dmm.co.jp/pics_dig/mono/actjpgs/sannomiya_tubaki.jpg"
LOGO_URL = "https://p.dmm.co.jp/p/maker_logo/attackers.gif"
ACTOR_URL = "https://www.dmm.co.jp/digital/videoa/-/list/=/article=actress/id=1062909/"
STUDIO_URL = "https://www.dmm.co.jp/digital/videoa/-/list/=/article=maker/id=1227/"
PERSON = {"id": "1", "name": "산노미야 츠바키", "alias_list": ["三宮つばき"], "urls": [],
          "image_path": "http://localhost:9999/performer/1/image?t=1&default=true"}
STUDIO = {"id": "2", "name": "アタッカーズ", "aliases": [], "url": None,
          "image_path": "http://localhost:9999/studio/2/image?t=1&default=true"}
PERSON_SOURCE = {"id": "1062909", "name": "三宮つばき", "imageUrl": PHOTO_URL}
STUDIO_SOURCE = {"id": "1227", "name": "アタッカーズ", "imageUrl": LOGO_URL}
SCENE = {"id": "77", "code": "ADN-414", "title": "내 제목", "details": "내 메모", "files": [],
         "paths": {"screenshot": None},
         "urls": ["https://video.dmm.co.jp/av/content/?id=adn00414"],
         "performers": [PERSON], "studio": STUDIO}


class Client:
    def __init__(self):
        self.entities = {("performer", "1"): copy.deepcopy(PERSON), ("studio", "2"): copy.deepcopy(STUDIO)}
        self.updates = []

    def find_image_entity(self, kind, identifier):
        return copy.deepcopy(self.entities.get((kind, identifier)))

    def update_entity_image(self, kind, identifier, image):
        self.updates.append((kind, identifier, image))
        self.entities[(kind, identifier)]["image_path"] = f"/{kind}/{identifier}/image?t=2"


class EntityImages(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oneclick-images-")
        self.addCleanup(self.tmp.cleanup)
        self.client = Client()
        self.provider = Mock()
        self.provider.scene_sources.return_value = {"id": "adn00414", "maker": STUDIO_SOURCE, "actresses": [PERSON_SOURCE]}
        self.provider.profile.side_effect = lambda kind, identifier: copy.deepcopy(
            PERSON_SOURCE if (kind, identifier) == ("performer", "1062909") else
            STUDIO_SOURCE if (kind, identifier) == ("studio", "1227") else None)
        self.download = Mock(return_value="data:image/png;base64,dGVzdA==")
        self.enricher = images.Enricher(self.client, Path(self.tmp.name), self.provider, self.download)

    def test_stash_placeholder_is_not_a_saved_photo(self):
        self.assertTrue(images.missing_image(PERSON))
        self.assertTrue(images.missing_image({"image_path": None}))
        self.assertFalse(images.missing_image({}))
        self.assertFalse(images.missing_image({"image_path": "/image?default=false"}))
        self.assertFalse(images.missing_image({"image_path": "/default=true/image"}))

    def test_name_and_japanese_alias_match_fanza_identity(self):
        result = self.enricher.scene(copy.deepcopy(SCENE), "ADN-414")
        self.assertEqual((result["portraits"], result["logos"]), (1, 1))
        self.assertEqual(len(self.client.updates), 2)
        self.assertEqual(self.client.entities[("performer", "1")]["name"], PERSON["name"])

    def test_existing_images_make_no_external_requests(self):
        scene = copy.deepcopy(SCENE)
        for entity in [scene["studio"], *scene["performers"]]:
            entity["image_path"] = "http://localhost/saved-image"
        result = self.enricher.scene(scene, "ADN-414")
        self.assertEqual(result["existing"], 2)
        self.provider.scene_sources.assert_not_called()
        self.provider.profile.assert_not_called()
        self.download.assert_not_called()

    def test_unknown_image_state_is_not_treated_as_missing(self):
        person = dict(PERSON)
        del person["image_path"]
        self.assertEqual(self.enricher.fill("performer", person, [PERSON_SOURCE])["status"], "unknown")
        self.download.assert_not_called()

    def test_new_image_during_download_wins(self):
        self.client.entities[("performer", "1")]["image_path"] = "/saved-user-image"
        result = self.enricher.fill("performer", PERSON, [PERSON_SOURCE])
        self.assertEqual(result["status"], "existing")
        self.assertEqual(self.client.updates, [])

    def test_identity_change_during_download_does_not_overwrite(self):
        self.client.entities[("performer", "1")].update(name="다른 배우", alias_list=[])
        result = self.enricher.fill("performer", PERSON, [PERSON_SOURCE])
        self.assertEqual(result["status"], "unmatched")
        self.assertEqual(self.client.updates, [])

    def test_stored_source_id_works_without_original_name(self):
        person = {**PERSON, "name": "사용자 이름", "alias_list": [], "urls": [ACTOR_URL]}
        self.client.entities[("performer", "1")] = copy.deepcopy(person)
        result = self.enricher.fill("performer", person)
        self.assertEqual(result["status"], "filled")

    def test_conflicting_profile_link_overrides_name_match(self):
        person = {**PERSON, "urls": [ACTOR_URL.replace("1062909", "9999")]}
        result = self.enricher.fill("performer", person, [PERSON_SOURCE])
        self.assertEqual(result["status"], "unmatched")
        self.download.assert_not_called()

    def test_multiple_profile_ids_and_duplicate_names_are_ambiguous(self):
        person = {**PERSON, "urls": [ACTOR_URL, ACTOR_URL.replace("1062909", "9999")]}
        self.assertEqual(self.enricher.fill("performer", person, [PERSON_SOURCE])["status"], "unmatched")
        self.assertEqual(self.enricher.fill("performer", PERSON, [PERSON_SOURCE, {**PERSON_SOURCE, "id": "9999"}])["status"], "unmatched")
        self.download.assert_not_called()

    def test_missing_photo_is_not_replaced_with_cover(self):
        result = self.enricher.fill("performer", PERSON, [{**PERSON_SOURCE, "imageUrl": None}])
        self.assertEqual(result["status"], "no_image")
        self.download.assert_not_called()

    def test_download_error_is_reported_and_non_destructive(self):
        self.download.side_effect = OSError("download failed")
        result = self.enricher.fill("performer", PERSON, [PERSON_SOURCE])
        self.assertEqual(result["status"], "failed")
        self.assertFalse(self.enricher.summary()["ok"])
        self.assertEqual(self.client.updates, [])
        self.enricher.fill("performer", PERSON, [PERSON_SOURCE])
        self.download.assert_called_once()

    def test_repeated_scene_deduplicates_and_empty_scene_counts_zero(self):
        self.enricher.scene(copy.deepcopy(SCENE), "ADN-414")
        again = self.enricher.scene(copy.deepcopy(SCENE), "ADN-414")
        self.assertEqual((again["filled"], again["existing"]), (0, 2))
        self.assertEqual(self.enricher.scene({}, None)["checked"], 0)
        self.assertEqual(self.enricher.summary()["filled"], 2)
        self.assertEqual(len(self.client.updates), 2)

    def test_invalid_source_and_profile_urls_are_rejected(self):
        for url in ["http://pics.dmm.co.jp/a.jpg", "https://localhost/a.jpg", "file:///etc/passwd",
                    "https://p.dmm.co.jp.evil.test/a.jpg", "https://user:secret@p.dmm.co.jp/a.jpg",
                    "https://p.dmm.co.jp:9999/a.jpg", "https://p.dmm.co.jp/noimage.jpg"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                images.image_url(url)
        self.assertEqual(images.source_ids({"urls": [ACTOR_URL.replace("www.dmm.co.jp", "evil.test")]}, "performer"), set())
        self.assertEqual(images.source_ids({"urls": [ACTOR_URL]}, "performer"), {"1062909"})

    def test_redirect_checked_before_requesting_external_host(self):
        with self.assertRaises(ValueError):
            images.ImageRedirect().redirect_request(Mock(), None, 302, "", {}, "http://127.0.0.1/secret")

    def test_bound_and_decode_downloaded_image(self):
        output = io.BytesIO()
        Image.new("RGB", (67, 40), "white").save(output, "GIF")
        good = output.getvalue()
        for body, valid in [(good, True), (b"<html>error</html>", False), (good[:30], False), (b"x" * (images.MAX_IMAGE_BYTES + 1), False)]:
            response = Mock()
            response.headers = {}
            response.geturl.return_value = LOGO_URL
            response.read.return_value = body
            opener = Mock()
            opener.open.return_value.__enter__ = Mock(return_value=response)
            opener.open.return_value.__exit__ = Mock(return_value=False)
            with patch.object(images.urllib.request, "build_opener", return_value=opener):
                if valid:
                    value = images.download_image(LOGO_URL)
                    self.assertTrue(value.startswith("data:image/gif;base64,"))
                    self.assertEqual(base64.b64decode(value.split(",", 1)[1]), good)
                else:
                    with self.assertRaises((ValueError, OSError)):
                        images.download_image(LOGO_URL)

    def test_profile_response_must_match_requested_id(self):
        provider = images.FanzaProfiles()
        with patch.object(provider, "query", return_value={"actress": {**PERSON_SOURCE, "id": "9999"}}):
            with self.assertRaises(ValueError):
                provider.profile("performer", "1062909")

    def test_provider_outage_does_not_retry_for_every_scene(self):
        provider = images.FanzaProfiles()
        with patch.object(images.fanza_core.HTTP, "post_json", return_value=None) as post:
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    provider.profile("performer", "1062909")
            post.assert_called_once()

    def test_unavailable_profile_does_not_abort_other_candidates(self):
        provider = images.FanzaProfiles()
        with patch.object(provider, "query", side_effect=[{"ppvContent": None}, {"ppvContent": {"id": "adn00414", "maker": STUDIO_SOURCE}}]), \
                patch.object(images.fanza_core, "make_cid_candidates", return_value=["wrongadn00414", "adn00414"]):
            self.assertEqual(provider.scene_sources({}, "ADN-414")["maker"]["id"], "1227")

    def test_content_response_must_match_requested_cid(self):
        provider = images.FanzaProfiles()
        with patch.object(provider, "query", return_value={"ppvContent": {"id": "sone00458"}}):
            with self.assertRaises(ValueError):
                provider.content("adn00414")

    def test_unrelated_product_url_is_not_used(self):
        provider = images.FanzaProfiles()
        with patch.object(images.fanza_core, "make_cid_candidates", return_value=["adn00414"]), \
                patch.object(provider, "content", return_value={}) as get:
            provider.scene_sources({"urls": ["https://video.dmm.co.jp/av/content/?id=sone00458"]}, "ADN-414")
            get.assert_called_once_with("adn00414")
        with patch.object(provider, "content") as get:
            self.assertEqual(provider.scene_sources({"urls": ["https://video.dmm.co.jp/av/content/?id=sone00458",
                                                               "https://video.dmm.co.jp/av/content/?id=adn00414"]}, None), {})
            get.assert_not_called()

    def test_entity_lock_blocks_duplicate_writer(self):
        with images.entity_lock(Path(self.tmp.name), "performer", "1"):
            with self.assertRaises((OSError, RuntimeError)):
                with images.entity_lock(Path(self.tmp.name), "performer", "1"):
                    self.fail("Second writer acquired the lock")

    def test_1_4_1_metadata_completion_survives_update(self):
        store = backend.StateStore(Path(self.tmp.name))
        backend.atomic_write(store.scene_path("77"), {"version": "1.4.1", "code": "ADN-414", "complete": True})
        self.assertTrue(store.scene_complete("77", "ADN-414"))

    def test_image_mutations_contain_no_other_fields(self):
        client = backend.StashClient({})
        with patch.object(client, "graphql", side_effect=[{"performerUpdate": {"id": "1"}}, {"studioUpdate": {"id": "2"}}]) as graphql:
            client.update_entity_image("performer", "1", "data:image/png;base64,test")
            query, variables = graphql.call_args.args
            self.assertIn("PerformerUpdateInput!", query)
            self.assertEqual(set(variables["input"]), {"id", "image"})
            client.update_entity_image("studio", "2", "data:image/gif;base64,test")
            self.assertIn("studioUpdate", graphql.call_args.args[0])

    def test_image_update_missing_or_wrong_response_is_not_success(self):
        client = backend.StashClient({})
        for result in [{}, {"performerUpdate": None}, {"performerUpdate": {"id": "2"}}]:
            with patch.object(client, "graphql", return_value=result), self.assertRaises(RuntimeError):
                client.update_entity_image("performer", "1", "data:image/png;base64,test")

    def test_bulk_does_not_page_over_shrinking_missing_filter(self):
        client = backend.StashClient({})
        with patch.object(client, "graphql", return_value={"findPerformers": {"count": 0, "performers": []}}) as graphql:
            client.find_image_entities("performer", 2)
            query, variables = graphql.call_args.args
            self.assertNotIn("is_missing", query)
            self.assertEqual(variables["filter"], {"page": 2, "per_page": 100, "sort": "id", "direction": "ASC"})

    def test_image_only_operation_never_fetches_translation_or_updates_scene(self):
        client = Mock()
        client.find_scene.return_value = copy.deepcopy(SCENE)
        args = {"mode": "images_scene", "scene_id": "77"}
        with patch.object(backend, "read_input", return_value={"args": args, "server_connection": {"PluginDir": self.tmp.name}}), \
                patch.object(backend, "StashClient", return_value=client), \
                patch.object(backend.entity_images, "Enricher", return_value=self.enricher), \
                patch.object(backend, "fetch_metadata") as metadata, patch.object(backend, "emit") as emit:
            self.assertEqual(backend.main(), 0)
            self.assertEqual(emit.call_args.args[0]["filled"], 2)
            metadata.assert_not_called()
            client.update_scene.assert_not_called()

    def test_scene_metadata_success_survives_image_failure(self):
        client = Mock()
        client.find_scene.return_value = copy.deepcopy(SCENE)
        metadata = {"image": "https://example.test/cover.jpg", "title": "作品", "title_ko": "작품",
                    "details_ko": "한국어 설명", "performers": []}
        failed_images = Mock()
        failed_images.scene.side_effect = RuntimeError("image-only failure")
        with patch.object(backend, "fetch_metadata", return_value=metadata):
            result = backend.process_scene(client, copy.deepcopy(SCENE), backend.StateStore(Path(self.tmp.name)), images=failed_images)
        self.assertTrue(result["ok"])
        self.assertTrue(result["complete"])
        self.assertIn("사진·로고는 처리하지 못했습니다", result["warning"])
        client.update_scene.assert_called_once()

    def test_scene_metadata_automatically_fills_images(self):
        client = Mock()
        client.find_scene.return_value = copy.deepcopy(SCENE)
        metadata = {"image": "https://example.test/cover.jpg", "title": "作品", "title_ko": "작품",
                    "details_ko": "한국어 설명", "performers": []}
        with patch.object(backend, "fetch_metadata", return_value=metadata):
            result = backend.process_scene(client, copy.deepcopy(SCENE), backend.StateStore(Path(self.tmp.name)), images=self.enricher)
        self.assertEqual(result["entity_images"]["filled"], 2)
        self.assertIn("배우 사진 1개", result["changes"])
        self.assertIn("제작사 로고 1개", result["changes"])

    def test_bulk_covers_multiple_pages_and_standalone_entities(self):
        self.client.find_scenes_for_images = Mock(side_effect=[(2, [copy.deepcopy(SCENE)]), (2, [{**copy.deepcopy(SCENE), "id": "78"}])])
        standalone = {**copy.deepcopy(PERSON), "id": "3", "urls": [ACTOR_URL]}
        self.client.entities[("performer", "3")] = standalone
        def listing(kind, page):
            if kind == "performer":
                return (2, [self.client.entities[("performer", "1" if page == 1 else "3")]])
            return (1, [self.client.entities[("studio", "2")]])
        self.client.find_image_entities = Mock(side_effect=listing)
        with patch.object(backend.entity_images, "Enricher", return_value=self.enricher), patch.object(backend, "PAGE_SIZE", 1):
            result = backend.run_image_bulk(self.client, backend.StateStore(Path(self.tmp.name)))
        self.assertEqual((result["filled"], result["portraits"], result["logos"]), (3, 2, 1))
        self.assertEqual(self.client.find_scenes_for_images.call_count, 2)
        self.assertEqual(self.client.find_image_entities.call_count, 3)
        self.assertEqual(self.download.call_count, 2, "Shared portrait downloads are reused")


@unittest.skipUnless(os.environ.get("ONECLICK_LIVE_IMAGES") == "1", "Opt-in real FANZA image downloads")
class LiveImages(unittest.TestCase):
    def test_profile_and_scene_sources(self):
        provider = images.FanzaProfiles()
        source = provider.scene_sources(SCENE, "ADN-414")
        self.assertEqual(source["maker"]["id"], "1227")
        self.assertIn("1062909", [p["id"] for p in source["actresses"]])
        for kind, identifier in [("performer", "1062909"), ("studio", "1227")]:
            profile = provider.profile(kind, identifier)
            value = images.download_image(profile["imageUrl"])
            self.assertTrue(value.startswith("data:image/"))
            self.assertGreater(len(value), 100)

    def test_backend_protocol_with_real_images(self):
        client = Client()
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(payload)
                q, variables = payload["query"], payload.get("variables") or {}
                if "findScene(" in q:
                    data = {"findScene": copy.deepcopy(SCENE)}
                elif "findPerformer(" in q:
                    data = {"findPerformer": client.find_image_entity("performer", variables["id"])}
                elif "findStudio(" in q:
                    data = {"findStudio": client.find_image_entity("studio", variables["id"])}
                elif "performerUpdate(" in q or "studioUpdate(" in q:
                    kind = "performer" if "performerUpdate(" in q else "studio"
                    item = variables["input"]
                    client.update_entity_image(kind, item["id"], item["image"])
                    data = {f"{kind}Update": {"id": item["id"]}}
                else:
                    data = {}
                raw = json.dumps({"data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        with tempfile.TemporaryDirectory(prefix="oneclick-images-live-") as tmp:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                command = [os.environ["ONECLICK_BACKEND_EXE"]] if os.environ.get("ONECLICK_BACKEND_EXE") else [sys.executable, str(Path(__file__).with_name("jav_fanza_oneclick.py"))]
                request = {"args": {"mode": "images_scene", "scene_id": "77"},
                           "server_connection": {"Endpoint": f"http://127.0.0.1:{server.server_port}/graphql", "PluginDir": tmp}}
                result = subprocess.run(command, input=json.dumps(request), text=True, encoding="utf-8",
                                        capture_output=True, timeout=90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(result.returncode, 0, result.stderr)
                data = json.loads(result.stdout)["output"]
                self.assertEqual((data["portraits"], data["logos"]), (1, 1), data)
                self.assertEqual(len(client.updates), 2)
                for payload in requests:
                    if "mutation" in payload["query"]:
                        self.assertEqual(set(payload["variables"]["input"]), {"id", "image"})
                for kind, identifier, value in client.updates:
                    with Image.open(io.BytesIO(base64.b64decode(value.split(",", 1)[1]))) as decoded:
                        decoded.load()
                        print(f"Verified {kind}: {decoded.format} {decoded.size}")
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
