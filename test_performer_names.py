"""Name transcription and safe migration regressions, without a user database."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import jav_fanza_oneclick as backend
import performer_names as names


PROFILE = {"id": "1039157", "name": "桜空もも", "nameRuby": "さくらもも",
           "imageUrl": "https://awsimgsrc.dmm.co.jp/pics_dig/mono/actjpgs/sakura_momo.jpg"}
URL = "https://www.dmm.co.jp/digital/videoa/-/list/=/article=actress/id=1039157/"
PERSON = {"id": "1", "name": "벚꽃 하늘 모모", "alias_list": ["桜空もも"], "urls": [URL], "image_path": "/existing-photo"}


class Client:
    def __init__(self, person=None):
        self.person = copy.deepcopy(person or PERSON)
        self.updates = []

    def find_image_entity(self, kind, identifier):
        assert kind == "performer" and identifier == "1"
        return copy.deepcopy(self.person)

    def find_image_entities(self, kind, page):
        assert kind == "performer" and page == 1
        return 1, [copy.deepcopy(self.person)]

    def find_performers(self, name):
        return [copy.deepcopy(self.person)]

    def find_scenes(self, page):
        assert page == 1
        return 1, [{"id": "77", "code": "TEST-001", "title": "내 제목", "performers": [copy.deepcopy(self.person)]}]

    def update_performer(self, identifier, name, aliases):
        assert identifier == "1"
        self.updates.append({"id": identifier, "name": name, "alias_list": aliases})
        self.person.update(name=name, alias_list=list(aliases))

    def create_performer(self, person):
        raise AssertionError("Existing actor repair must not create duplicate actors")


def seed(root, legacy=True, profile=True):
    if profile:
        names.write_json(root / "performer_names" / "sources" / "1039157.json",
                         {"revision": names.REVISION, "time": time.time(), "profile": PROFILE})
    if legacy:
        names.write_json(root / "translations" / "performer.name-1039157.json", {
            "revision": 2, "source_sha256": hashlib.sha256("桜空もも".encode()).hexdigest(), "translated": "벚꽃 하늘 모모",
        })


class NameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oneclick-names-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "data"
        seed(self.root)
        self.resolver = names.Resolver(self.root)

    def test_names_in_user_screenshot_are_phonetic_not_semantic(self):
        for japanese, kana, roman, korean in [
            ("桜空もも", "さくらもも", "Sakura Momo", "사쿠라 모모"),
            ("海老咲あお", "えびさきあお", "Ebisaki Ao", "에비사키 아오"),
            ("新ありな", "あらたありな", "Arata Arina", "아라타 아리나"),
            ("三宮つばき", "さんのみやつばき", "Sannomiya Tsubaki", "산노미야 츠바키"),
        ]:
            with self.subTest(japanese=japanese):
                result = names.transcribe(kana, japanese)
                self.assertEqual((result["roman"], result["korean"]), (roman, korean))

    def test_verified_filename_supplies_spacing_only(self):
        result = names.transcribe("みうらのあ", "三浦乃愛", "https://pics.dmm.co.jp/mono/actjpgs/miura_noa.jpg")
        self.assertEqual(result["korean"], "미우라 노아")
        wrong = names.transcribe("みうらのあ", "三浦乃愛", "https://pics.dmm.co.jp/mono/actjpgs/sakura_momo.jpg")
        self.assertEqual(wrong["korean"], "미우라노아")
        untrusted = names.transcribe("みうらのあ", "三浦乃愛", "https://evil.test/actjpgs/miura_noa.jpg")
        self.assertEqual(untrusted["korean"], "미우라노아")

    def test_kunrei_photo_spelling_cannot_change_hepburn_or_pronunciation(self):
        result = names.transcribe("さんのみやつばき", "三宮つばき", "https://pics.dmm.co.jp/mono/actjpgs/sannomiya_tubaki.jpg")
        self.assertEqual(result["roman"], "Sannomiya Tsubaki")
        self.assertEqual(result["korean"], "산노미야 츠바키")

    def test_katakana_small_kana_nasal_and_long_mark(self):
        self.assertEqual(names.transcribe("ミホ ナナ")["korean"], "미호 나나")
        self.assertEqual(names.transcribe("しんいち")["roman"], "Shin'ichi")
        self.assertEqual(names.transcribe("しんいち")["korean"], "신이치")
        self.assertEqual(names.transcribe("いっか")["korean"], "잇카")
        self.assertEqual(names.transcribe("メロディー")["korean"], "메로디")
        self.assertEqual(names.transcribe("きゃりー")["korean"], "캬리")

    def test_unsupported_reading_does_not_guess_or_translate(self):
        for reading in ["", "桜空もも", "Sakura Momo", "Unknown 1", "っ", "ん", "ー", "ぁ" * 101]:
            with self.subTest(reading=reading), self.assertRaises(ValueError):
                names.transcribe(reading)

    def test_parenthetical_former_name_readings_are_separate(self):
        person = names.transcribe_profile({"name": "似鳥（似鳥日菜）", "nameRuby": "にとり（にとりひな）",
                                          "imageUrl": "https://pics.dmm.co.jp/mono/actjpgs/nitori_hina.jpg"})
        self.assertEqual(person["korean"], "니토리")
        self.assertIn("니토리 히나", person["aliases"])
        self.assertIn("Nitori Hina", person["aliases"])
        self.assertEqual(person["source_names"], ["似鳥", "似鳥日菜"])

    def test_multiple_former_names_keep_current_name_clean(self):
        person = names.transcribe_profile({"name": "真田さな（真田美樹、真田みづ稀）", "nameRuby": "さなださな（さなだみき、さなだみづき）",
                                          "imageUrl": "https://pics.dmm.co.jp/mono/actjpgs/sanada_mizuki.jpg"})
        self.assertEqual(person["korean"], "사나다 사나")
        self.assertIn("사나다 미즈키", person["aliases"])

    def test_unaligned_or_nested_alias_readings_are_not_guessed(self):
        for original, reading in [("現在(旧名)", "げんざい"), ("現在(旧名,別名)", "げんざい(きゅうめい)"), ("現在(旧名(別名))", "げんざい(きゅうめい(べつめい))")]:
            with self.subTest(original=original), self.assertRaises(ValueError):
                names.transcribe_profile({"name": original, "nameRuby": reading})

    def test_manually_selected_confirmed_alias_stays_user_owned(self):
        self.resolver.mark_applied("1", "1039157", "선택한 이름", manual=True)
        self.assertEqual(self.resolver.applied_name("1", "1039157"), "")

    def test_legacy_translation_of_confirmed_former_name_is_repaired(self):
        profile = {"id": "1078559", "name": "似鳥（似鳥日菜）", "nameRuby": "にとり（にとりひな）",
                   "imageUrl": "https://pics.dmm.co.jp/mono/actjpgs/nitori_hina.jpg"}
        names.write_json(self.root / "translations" / "performer.name-1078559.json",
                         {"revision": 2, "source_sha256": hashlib.sha256("似鳥日菜".encode()).hexdigest(), "translated": "닮은 햇살"})
        client = Client({**PERSON, "name": "닮은 햇살", "urls": [URL.replace("1039157", "1078559")]})
        with patch.object(self.resolver.provider, "query", return_value={"actress": profile}):
            person = self.resolver.profile("1078559")
        self.assertTrue(backend.apply_performer_name(client, client.person, person, self.resolver))
        self.assertEqual(client.person["name"], "니토리")
        self.assertIn("니토리 히나", client.person["alias_list"])
        self.assertIn("닮은 햇살", client.person["alias_list"])

    def test_offline_name_cache_has_separate_revision(self):
        with patch.object(self.resolver.provider, "query") as query:
            person = self.resolver.profile("1039157")
            self.assertEqual(person["name_ko"], "사쿠라 모모")
            query.assert_not_called()
        self.assertEqual(self.resolver.legacy_name("1039157", "桜空もも"), "벚꽃 하늘 모모")
        self.assertEqual(self.resolver.legacy_name("1039157", "other"), "")

    def test_old_revision_source_cache_is_refetched(self):
        path = self.root / "performer_names" / "sources" / "1039157.json"
        names.write_json(path, {"revision": -1, "time": time.time(), "profile": PROFILE})
        with patch.object(self.resolver.provider, "query", return_value={"actress": PROFILE}) as query:
            self.assertEqual(self.resolver.profile("1039157")["name_roman"], "Sakura Momo")
            query.assert_called_once()

    def test_wrong_identity_cannot_enter_cache(self):
        with patch.object(self.resolver.provider, "query", return_value={"actress": PROFILE}):
            with self.assertRaises(ValueError):
                self.resolver.profile("999")
        self.assertFalse((self.root / "performer_names" / "sources" / "999.json").exists())

    def test_legacy_translation_is_repaired_with_original_aliases_kept(self):
        client = Client()
        result = backend.run_name_bulk(client, backend.StateStore(Path(self.tmp.name)))
        self.assertEqual(result["renamed"], 1)
        self.assertEqual(client.person["name"], "사쿠라 모모")
        self.assertEqual(set(client.person["alias_list"]), {"桜空もも", "Sakura Momo", "벚꽃 하늘 모모"})
        self.assertEqual(client.person["image_path"], "/existing-photo")
        self.assertEqual(self.resolver.applied_name("1", "1039157"), "사쿠라 모모")

    def test_manual_korean_name_is_preserved(self):
        client = Client({**PERSON, "name": "내가 고른 모모"})
        result = backend.run_name_bulk(client, backend.StateStore(Path(self.tmp.name)))
        self.assertEqual(result["preserved"], 1)
        self.assertEqual(result["renamed"], 0)
        self.assertEqual(client.person["name"], "내가 고른 모모")
        self.assertIn("Sakura Momo", client.person["alias_list"])
        self.assertEqual(self.resolver.applied_name("1", "1039157"), "")

    def test_missing_provenance_does_not_rename_korean_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            seed(Path(tmp) / "data", legacy=False)
            client = Client()
            result = backend.run_name_bulk(client, backend.StateStore(Path(tmp)))
            self.assertEqual(result["renamed"], 0)
            self.assertEqual(client.person["name"], PERSON["name"])

    def test_japanese_name_is_converted_without_legacy_cache(self):
        client = Client({**PERSON, "name": "桜空もも", "alias_list": []})
        result = backend.run_name_bulk(client, backend.StateStore(Path(self.tmp.name)))
        self.assertEqual(result["renamed"], 1)
        self.assertEqual(client.person["name"], "사쿠라 모모")

    def test_name_repair_is_idempotent(self):
        client = Client()
        backend.run_name_bulk(client, backend.StateStore(Path(self.tmp.name)))
        result = backend.run_name_bulk(client, backend.StateStore(Path(self.tmp.name)))
        self.assertEqual(result["renamed"], 0)
        self.assertEqual(len(client.updates), 1)

    def test_user_edit_during_source_lookup_wins(self):
        client = Client({**PERSON, "name": "사용자가 바꾼 이름"})
        backend.ensure_performer(client, self.resolver.profile("1039157"), [PERSON], self.resolver)
        self.assertEqual(client.person["name"], "사용자가 바꾼 이름")

    def test_ambiguous_stash_names_do_not_create_duplicate_actor(self):
        person = self.resolver.profile("1039157")
        with self.assertRaises(ValueError):
            backend.match_performer([PERSON, {**PERSON, "id": "2"}], person)

    def test_scene_source_recovers_actor_without_profile_link(self):
        client = Client({**PERSON, "urls": []})
        self.resolver.provider.scene_sources = Mock(return_value={"actresses": [{"id": "1039157", "name": "桜空もも"}]})
        with patch.object(backend.performer_names, "Resolver", return_value=self.resolver):
            result = backend.run_name_bulk(client, backend.StateStore(Path(self.tmp.name)))
        self.assertEqual(result["renamed"], 1)
        self.assertEqual(client.person["name"], "사쿠라 모모")

    def test_ambiguous_cast_does_not_repair_from_guess(self):
        client = Client({**PERSON, "urls": []})
        self.resolver.provider.scene_sources = Mock(return_value={"actresses": [
            {"id": "1039157", "name": "桜空もも"}, {"id": "9999", "name": "桜空もも"}]})
        with patch.object(backend.performer_names, "Resolver", return_value=self.resolver):
            result = backend.run_name_bulk(client, backend.StateStore(Path(self.tmp.name)))
        self.assertEqual(result["unmatched"], 1)
        self.assertEqual(client.updates, [])

    def test_english_custom_name_is_preserved(self):
        client = Client({**PERSON, "name": "Momo (favorite)"})
        backend.run_name_bulk(client, backend.StateStore(Path(self.tmp.name)))
        self.assertEqual(client.person["name"], "Momo (favorite)")

    def test_previous_generated_name_can_be_refined_but_manual_change_cannot(self):
        client = Client({**PERSON, "name": "사쿠라모모"})
        self.resolver.mark_applied("1", "1039157", "사쿠라모모")
        # Whitespace normalization deliberately avoids a pointless cosmetic rename.
        backend.run_name_bulk(client, backend.StateStore(Path(self.tmp.name)))
        self.assertEqual(client.person["name"], "사쿠라모모")
        self.resolver.mark_applied("1", "1039157", "자동 발음")
        client.person["name"] = "직접 고친 발음"
        backend.run_name_bulk(client, backend.StateStore(Path(self.tmp.name)))
        self.assertEqual(client.person["name"], "직접 고친 발음")

    def test_metadata_never_calls_general_translator_for_names(self):
        person = {"name": "桜空もも", "remote_site_id": "1039157", "urls": [URL]}
        with patch.object(backend.fanza_core, "fetch_metadata", return_value={"title": "작품", "details": "내용", "performers": [person]}), \
                patch.object(backend, "korean_text", side_effect=lambda code, source, store, purpose, minimum: source) as translate:
            result = backend.fetch_metadata("TEST-001", backend.StateStore(Path(self.tmp.name)))
            self.assertEqual(result["performers"][0]["name_ko"], "사쿠라 모모")
            self.assertEqual([c.args[3] for c in translate.call_args_list], ["title", "details"])

    def test_reading_failure_keeps_original_and_does_not_translate_name(self):
        with patch.object(backend.fanza_core, "fetch_metadata", return_value={"title": "작품", "performers": [{"name": "漢字", "remote_site_id": "999"}]}), \
                patch.object(backend.performer_names.Resolver, "profile", side_effect=ValueError("no reading")), \
                patch.object(backend, "korean_text", return_value="작품") as translate:
            result = backend.fetch_metadata("TEST-001", backend.StateStore(Path(self.tmp.name)))
            self.assertEqual(result["performers"][0]["name_ja"], "漢字")
            self.assertNotIn("name_ko", result["performers"][0])
            self.assertEqual(translate.call_count, 2)


@unittest.skipUnless(os.environ.get("ONECLICK_LIVE_NAMES") == "1", "Opt-in FANZA pronunciation lookup")
class LiveNames(unittest.TestCase):
    def test_actual_profiles(self):
        with tempfile.TemporaryDirectory() as tmp:
            resolver = names.Resolver(Path(tmp))
            for identifier, expected in [("1039157", "사쿠라 모모"), ("1092426", "에비사키 아오"),
                                         ("1078619", "아라타 아리나"), ("1066895", "미우라 노아")]:
                result = resolver.profile(identifier)
                self.assertEqual(result["name_ko"], expected)

    def test_backend_repairs_existing_actor_using_real_fanza_reading(self):
        client = Client()
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append(payload)
                query, variables = payload["query"], payload.get("variables") or {}
                if "findScenes(" in query:
                    total, scenes = client.find_scenes(1)
                    data = {"findScenes": {"count": total, "scenes": scenes}}
                elif "findPerformers(" in query:
                    data = {"findPerformers": {"count": 1, "performers": [copy.deepcopy(client.person)]}}
                elif "findPerformer(" in query:
                    data = {"findPerformer": copy.deepcopy(client.person)}
                elif "performerUpdate(" in query:
                    item = variables["input"]
                    client.update_performer(item["id"], item["name"], item["alias_list"])
                    data = {"performerUpdate": {"id": item["id"]}}
                else:
                    data = {}
                raw = json.dumps({"data": data}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        with tempfile.TemporaryDirectory(prefix="oneclick-names-live-") as tmp:
            seed(Path(tmp) / "data", profile=False)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                command = [os.environ["ONECLICK_BACKEND_EXE"]] if os.environ.get("ONECLICK_BACKEND_EXE") else [sys.executable, str(Path(__file__).with_name("jav_fanza_oneclick.py"))]
                request = {"args": {"mode": "names_bulk"}, "server_connection": {"PluginDir": tmp,
                           "Endpoint": f"http://127.0.0.1:{server.server_port}/graphql"}}
                run = subprocess.run(command, input=json.dumps(request), text=True, encoding="utf-8", capture_output=True,
                                     timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(run.returncode, 0, run.stderr)
                result = json.loads(run.stdout)["output"]
                self.assertEqual(result["renamed"], 1, result)
                self.assertEqual(client.person["name"], "사쿠라 모모")
                mutations = [p for p in requests if "mutation" in p["query"]]
                self.assertEqual(len(mutations), 1)
                self.assertEqual(set(mutations[0]["variables"]["input"]), {"id", "name", "alias_list"})
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
