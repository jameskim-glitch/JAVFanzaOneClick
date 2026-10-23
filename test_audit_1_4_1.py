"""Regression cases reproduced during the 1.4.1 reliability audit."""
import json
import os
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import Mock, patch

import fanza_core
import installer
import jav_fanza_oneclick as backend
import subtitles
from test_subtitles import SRT, Client


class MetadataAudit(unittest.TestCase):
    def test_code_comes_from_video_not_parent_folder(self):
        self.assertEqual(backend.extract_code({"files": [{"path": "D:/ADN-414/SONE-458.mp4", "basename": "SONE-458.mp4"}]}), "SONE-458")
        self.assertEqual(backend.extract_code({"files": [{"path": "D:\\ADN-414\\SONE-458.mp4"}]}), "SONE-458")

    def test_notes_and_related_actor_names_do_not_supply_code(self):
        self.assertIsNone(backend.extract_code({"title": "ordinary video", "details": "also TEST-001",
                                              "performers": [{"name": "TEST-002"}]}))

    def test_conflicting_files_require_explicit_code(self):
        scene = {"files": [{"path": "D:/SONE-458.mp4"}, {"path": "D:/ADN-414.mp4"}]}
        with self.assertRaises(ValueError):
            backend.extract_code(scene)
        self.assertEqual(backend.extract_code({**scene, "code": "SONE-458"}), "SONE-458")

    def test_malformed_translation_response_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(backend, "request_json", return_value=[]), \
                patch.object(backend, "translate_mymemory", return_value="정상 번역 결과") as fallback:
            result = backend.korean_text("TEST-001", "短い文章", backend.StateStore(Path(tmp)), "details")
            self.assertEqual(result, "정상 번역 결과")
            fallback.assert_called_once()

    def test_mymemory_chunks_fit_utf8_limit(self):
        chunks = []
        def request(url):
            chunk = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["q"][0]
            chunks.append(chunk)
            return {"responseStatus": 200, "responseData": {"translatedText": "정상적인 번역입니다"}}
        source = "日記です。" * 140
        with patch.object(backend, "request_json", side_effect=request):
            self.assertTrue(backend.translate_mymemory(source))
        self.assertEqual("".join(chunks), source)
        self.assertTrue(all(len(chunk.encode("utf-8")) <= 500 for chunk in chunks))
        with patch.object(backend, "request_json", return_value={"responseStatus": 429, "responseData": {"translatedText": "요청 한도 초과"}}):
            self.assertIsNone(backend.translate_mymemory("test"))

    def test_bad_cache_shapes_are_misses(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = backend.StateStore(Path(tmp))
            for value in ([], None, 42, "wrong"):
                for path in (store.scene_path("1"), store.translation_path("TEST-001", "title")):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(value), encoding="utf-8")
                self.assertFalse(store.scene_complete("1", "TEST-001"))
                self.assertIsNone(store.cached_translation("TEST-001", "source", "title"))

    def test_short_title_and_custom_korean_text_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = backend.StateStore(Path(tmp))
            client = Mock()
            scene = {"id": "1", "code": "TEST-001", "title": "나의 ○ 제목", "details": "<br>내가 적은 이야기입니다.<br>", "files": []}
            metadata = {"image": "https://example.test/cover.jpg", "title": "作品", "title_ko": "작품",
                        "details_ko": "사이트에서 가져온 설명입니다", "performers": []}
            with patch.object(backend, "fetch_metadata", return_value=metadata):
                result = backend.process_scene(client, scene, store)
            update = client.update_scene.call_args.args[0]
            self.assertEqual(update["title"], "나의 제목")
            self.assertEqual(update["details"], "내가 적은 이야기입니다.")
            self.assertTrue(result["complete"])
            with patch.object(backend, "request_json", return_value=[[["작품", "作品"]]]):
                self.assertEqual(backend.korean_text("TEST-002", "作品", store, "title", minimum=1), "작품")
            self.assertFalse(backend.title_needs_translation({"title": "추억", "files": []}, "TEST-001"))

    def test_story_about_campaign_is_not_always_reprocessed(self):
        self.assertFalse(backend.details_need_refresh("첫 방문 캠페인 전단지를 보고 시작되는 충분한 한국어 줄거리입니다."))

    def test_atomic_cache_failure_preserves_old_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(fanza_core, "CACHE_DIR", root):
                fanza_core.write_cache("TEST-001", {"title": "old"})
                with patch.object(fanza_core.os, "replace", side_effect=PermissionError("test")):
                    fanza_core.write_cache("TEST-001", {"title": "new"})
                self.assertEqual(json.loads((root / "TEST-001.json").read_text())["title"], "old")
                self.assertEqual(len(list(root.iterdir())), 1)

    def test_unknown_mode_never_changes_scene(self):
        with patch.object(backend, "read_input", return_value={"args": {"mode": "subtitle_typo"}}), \
                patch.object(backend, "StashClient") as client:
            with self.assertRaises(ValueError):
                backend.main()
            client.assert_not_called()


class SubtitleAudit(unittest.TestCase):
    def test_utf16_korean_srt(self):
        self.assertEqual(subtitles.validate_srt(SRT.encode("utf-16"))[0], SRT)

    def test_foreign_unlabelled_subtitle_allows_separate_korean_track(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "TEST-001.mp4"
            video.touch()
            original = video.with_suffix(".srt")
            original.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello!\n", encoding="utf-8")
            file = subtitles.scene_files({"files": [{"path": str(video)}]})[0]
            self.assertEqual(file["existing"], [])
            self.assertEqual(file["other"], [str(original)])
            self.assertIn("Hello", original.read_text())

    def test_rescan_uses_only_registered_scene_sidecar(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "TEST-001.mp4"
            video.touch()
            video.with_suffix(".ko.srt").write_text(SRT, encoding="utf-8")
            client = Client()
            scene = {"files": [{"path": str(video)}]}
            result = subtitles.rescan_scene(scene, {"path": "C:/ignored.srt"}, client)
            self.assertEqual(result["scan_job_ids"], ["123"])
            self.assertEqual(client.calls[0][1]["input"]["paths"], [str(video.with_suffix(".ko.srt"))])
            with self.assertRaises(RuntimeError):
                subtitles.rescan_scene(scene, {"file_path": "C:/unrelated.mp4"}, client)

    def test_duration_mismatch_does_not_modify_subtitle_timing(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "TEST-001.mp4"
            video.touch()
            provider = Mock()
            provider.download.return_value = (SRT, {"last_end_seconds": 1000, "cues": 2})
            scene = {"files": [{"path": str(video), "duration": 10}]}
            result = subtitles.download_scene(scene, "TEST-001", {"page_url": subtitles.ORIGIN + "/subs/1/TEST-001.html"}, Client(), provider)
            self.assertIn("영상 길이", result["warning"])
            self.assertEqual(video.with_suffix(".ko.srt").read_text(encoding="utf-8"), SRT)


class InstallerAudit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oneclick-update-audit-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source, self.target = self.root / "payload", self.root / "installed"
        self.source.mkdir()
        self.target.mkdir()
        for name in installer.PAYLOAD_FILES:
            (self.source / name).write_text("new " + name)
            (self.target / name).write_text("old " + name)
        (self.target / "data").mkdir()
        (self.target / "data" / "user.json").write_text("user data")

    def install(self):
        with patch.object(installer, "bundled_plugin_dir", return_value=self.source), \
                patch.object(installer, "install_target", return_value=self.target):
            return installer.install()

    def test_mid_update_failure_rolls_back_every_file(self):
        replace = os.replace
        def fail(src, dst):
            if str(src).endswith("jav_fanza_oneclick.js.new"):
                raise PermissionError("simulated locked file")
            replace(src, dst)
        with patch.object(installer.os, "replace", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "복원"):
                self.install()
        for name in installer.PAYLOAD_FILES:
            self.assertEqual((self.target / name).read_text(), "old " + name)
        self.assertEqual((self.target / "data" / "user.json").read_text(), "user data")
        self.assertFalse(list(self.root.glob(".oneclick-update-*")))
        self.install()  # Previous failed attempt must not leave the lock held.

    def test_rollback_failure_keeps_recovery_backup(self):
        replace = os.replace
        def fail(src, dst):
            if str(src).endswith(("jav_fanza_oneclick.js.new", "jav_fanza_oneclick.exe.previous")):
                raise PermissionError("simulated lock")
            replace(src, dst)
        with patch.object(installer.os, "replace", side_effect=fail):
            with self.assertRaisesRegex(RuntimeError, "보존한 위치"):
                self.install()
        backups = list(self.root.glob(".oneclick-update-*/jav_fanza_oneclick.exe.previous"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "old jav_fanza_oneclick.exe")

    def test_concurrent_install_is_blocked_and_lock_is_reusable(self):
        with installer.installation_lock(self.target):
            with self.assertRaises(RuntimeError):
                self.install()
        self.install()
        self.install()
        for name in installer.PAYLOAD_FILES:
            self.assertEqual((self.target / name).read_text(), "new " + name)


if __name__ == "__main__":
    unittest.main(verbosity=2)
