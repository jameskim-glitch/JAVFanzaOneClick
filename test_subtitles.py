"""Offline regressions plus an opt-in real SubtitleCat release smoke test."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import jav_fanza_oneclick as backend
import subtitles as sub

CODE = "SONE-458"
PAGE = sub.ORIGIN + "/subs/981/SONE-458%20test.html"
SRT_URL = sub.ORIGIN + "/subs/982/SONE-458%20test-ko.srt"
SRT = "1\n00:00:01,000 --> 00:00:03,000\n안녕하세요.\n\n2\n00:01:00,000 --> 00:01:02,000\n두 번째 대사입니다.\n"
SEARCH = b'<input name="search"><a href="/subs/981/SONE-458%20test.html">SONE-458 test</a>'
DETAIL = '<h2>All language subtitles for SONE-458 test</h2><a id="download_ko" href="/subs/982/SONE-458 test-ko.srt">Download</a>'
NO_KO = '<h2>All language subtitles for SONE-458</h2><button id="ko" onclick="translate_from_server_folder()">Translate</button>'


class Client:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def graphql(self, document, variables=None):
        self.calls.append((document, variables))
        if self.fail:
            raise RuntimeError("test scan failed")
        return {"metadataScan": "123"}


class SubtitleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="oneclick-subtitle-test-")
        self.addCleanup(self.tmp.cleanup)
        self.video = Path(self.tmp.name) / "SONE-458_1080p.mp4"
        self.video.write_bytes(b"fixture, not a real video")
        self.scene = {"id": "77", "code": CODE, "files": [{"path": str(self.video)}]}
        self.client = Client()
        self.requests = []
        self.detail = DETAIL

        def fetch(url):
            self.requests.append(url)
            if "index.php?" in url:
                return SEARCH
            if url == PAGE:
                return self.detail.encode()
            if url == SRT_URL:
                return SRT.encode("utf-8-sig")
            raise AssertionError("unexpected network call: " + url)

        self.provider = sub.SubtitleCat(fetch)

    def download(self, **args):
        return sub.download_scene(self.scene, CODE, {"page_url": PAGE, **args}, self.client, self.provider)

    def test_exact_code_search(self):
        html = SEARCH + b'<a href="/subs/1/SONE-4580.html">other</a><a href="https://evil.example/SONE-458.html">other</a>'
        found = sub.SubtitleCat(lambda _: html).search(CODE)
        self.assertEqual([item["page_url"] for item in found["candidates"]], [PAGE])
        self.assertTrue(sub.code_matches("sone00458.zh", CODE))
        self.assertFalse(sub.code_matches("ASONE-458", CODE))
        self.assertFalse(sub.code_matches("SONE-4580", CODE))
        self.assertTrue(sub.code_matches("FC2-PPV-12345", "FC2-PPV-12345"))

    def test_no_results_and_failed_page_are_different(self):
        provider = sub.SubtitleCat(lambda _: b'<input name="search"><p>No results</p>')
        self.assertEqual(sub.search_scene(self.scene, CODE, provider)["candidates"], [])
        with self.assertRaises(RuntimeError):
            sub.SubtitleCat(lambda _: b"<html>Access denied</html>").search(CODE)

    def test_download_rename_and_exact_file_scan(self):
        result = self.download()
        path = self.video.with_suffix(".ko.srt")
        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(result["scan_job_id"], "123")
        self.assertEqual(path.read_text(encoding="utf-8"), SRT)
        self.assertEqual(self.video.read_bytes(), b"fixture, not a real video")
        self.assertEqual(self.client.calls[0][1]["input"]["paths"], [str(path)])
        self.assertFalse(self.client.calls[0][1]["input"]["scanGenerateCovers"])
        self.assertEqual(result["cues"], 2)
        self.assertEqual(sorted(p.name for p in self.video.parent.iterdir()), [path.name, self.video.name])

    def test_existing_subtitle_never_overwritten(self):
        for suffix in (".ko.srt", ".ko.vtt", ".kr.srt", ".kr.vtt", ".srt", ".vtt"):
            path = self.video.with_suffix(suffix)
            path.write_bytes(b"user-owned subtitle")
            self.assertEqual(self.download()["status"], "existing")
            self.assertEqual(path.read_bytes(), b"user-owned subtitle")
            path.unlink()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.client.calls, [])

    def test_search_existing_no_remote_requests(self):
        self.video.with_suffix(".ko.srt").write_bytes(b"existing")
        result = sub.search_scene(self.scene, CODE, self.provider)
        self.assertEqual(result["status"], "existing")
        self.assertEqual(self.requests, [])

    def test_translate_only_makes_no_file(self):
        self.detail = NO_KO
        result = self.download()
        self.assertEqual(result["status"], "not_found")
        self.assertEqual(self.requests, [PAGE])
        self.assertEqual(self.client.calls, [])
        self.assertEqual(list(self.video.parent.iterdir()), [self.video])

    def test_scan_failure_keeps_download(self):
        self.client.fail = True
        result = self.download()
        self.assertEqual(result["status"], "downloaded")
        self.assertIn("선택 스캔", result["warning"])
        self.assertEqual(self.video.with_suffix(".ko.srt").read_text(encoding="utf-8"), SRT)

    def test_multi_file_requires_explicit_selection(self):
        second = self.video.with_name("SONE-458_4K.mp4")
        second.write_bytes(b"second")
        self.scene["files"].append({"path": str(second)})
        with self.assertRaises(RuntimeError):
            self.download()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.download(file_index=1)["path"], str(second.with_suffix(".ko.srt")))
        self.assertFalse(self.video.with_suffix(".ko.srt").exists())

    def test_untrusted_url_rejected_before_fetch(self):
        for url in ("http://www.subtitlecat.com/subs/1/SONE-458.html",
                    "https://127.0.0.1/subs/1/SONE-458.html", "file:///C:/SONE-458.html",
                    "https://www.subtitlecat.com:9999/subs/1/SONE-458.html",
                    "https://www.subtitlecat.com/subs/1/SONE-458.html?next=evil",
                    "https://www.subtitlecat.com/subs/1/SONE-459.html",
                    "https://www.subtitlecat.com/subs/1/../SONE-458.html"):
            with self.assertRaises(ValueError):
                sub.page_url(url, CODE)
        self.assertEqual(self.requests, [])

    def test_changed_file_selection_rejected_before_fetch(self):
        with self.assertRaises(RuntimeError):
            self.download(file_path="D:/some-other-video.mp4")
        self.assertEqual(self.requests, [])

    def test_invalid_srt_no_writes(self):
        for raw in (b"<html>Access denied</html>", b"not a subtitle", b"",
                    b"1\n00:00:02,000 --> 00:00:01,000\nhello\n",
                    b"1\n00:00:01,000 --> 00:00:02,000\nEnglish only\n"):
            with self.assertRaises(RuntimeError):
                sub.validate_srt(raw)
        self.assertEqual(sub.validate_srt(SRT.encode("cp949"))[0], SRT)

    def test_publication_race_does_not_overwrite(self):
        path = self.video.with_suffix(".ko.srt")
        def race(src, dest):
            Path(dest).write_bytes(b"a user added this during download")
            raise FileExistsError("user already wrote subtitle")

        with patch.object(sub.os, "rename" if os.name == "nt" else "link", race):
            self.assertEqual(sub.save_subtitle(self.video, SRT), (path, False))
        self.assertEqual(path.read_bytes(), b"a user added this during download")

    def test_backend_dispatch_does_not_touch_metadata(self):
        request = {"args": {"mode": "subtitle_download", "scene_id": "77", "page_url": PAGE},
                   "server_connection": {"PluginDir": self.tmp.name}}
        output = io.StringIO()
        with patch.object(backend, "read_input", return_value=request), \
                patch.object(backend, "StashClient", return_value=self.client), \
                patch.object(self.client, "find_scene", return_value=self.scene, create=True), \
                patch.object(sub, "SubtitleCat", return_value=self.provider), \
                patch.object(backend, "process_scene", side_effect=AssertionError("metadata called")), \
                contextlib.redirect_stdout(output):
            self.assertEqual(backend.main(), 0)
        self.assertEqual(json.loads(output.getvalue())["output"]["status"], "downloaded")


@unittest.skipUnless(os.environ.get("ONECLICK_LIVE_SUBTITLES") == "1", "opt-in live download")
class ReleaseSmoke(unittest.TestCase):
    def test_real_site_with_backend(self):
        with tempfile.TemporaryDirectory(prefix="oneclick-subtitle-release-") as tmp:
            video = Path(tmp) / "SONE-458_1080p.mp4"
            video.write_bytes(b"workspace smoke fixture")
            scene = {"id": "77", "title": CODE, "code": CODE, "files": [{"path": str(video)}]}
            calls = []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass

                def do_POST(self):
                    request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                    calls.append(request)
                    data = {"findScene": scene} if "findScene" in request["query"] else {"metadataScan": "123"}
                    raw = json.dumps({"data": data}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                exe = os.environ.get("ONECLICK_BACKEND_EXE")
                command = [exe] if exe else [sys.executable, str(Path(backend.__file__))]

                def run(args):
                    request = {"server_connection": {"PluginDir": tmp,
                               "Endpoint": f"http://127.0.0.1:{server.server_port}/graphql"},
                               "args": {"scene_id": "77", **args}}
                    process = subprocess.run(command, input=json.dumps(request), encoding="utf-8",
                                             capture_output=True, timeout=65)
                    self.assertEqual(process.returncode, 0, process.stderr)
                    payload = json.loads(process.stdout)
                    self.assertNotIn("error", payload)
                    return payload["output"]

                found = run({"mode": "subtitle_search"})
                self.assertTrue(found["candidates"])
                result = run({"mode": "subtitle_download", "page_url": found["candidates"][0]["page_url"]})
                self.assertEqual(result["status"], "downloaded")
                path = video.with_suffix(".ko.srt")
                self.assertTrue(path.is_file())
                self.assertGreater(sub.validate_srt(path.read_bytes())[1]["cues"], 100)
                self.assertEqual(video.read_bytes(), b"workspace smoke fixture")
                self.assertEqual(calls[-1]["variables"]["input"]["paths"], [str(path)])
                self.assertEqual(run({"mode": "subtitle_download", "page_url": found["candidates"][0]["page_url"]})["status"], "existing")
                print(json.dumps({"live": True, "compiled": bool(exe), "saved_name": path.name,
                                  "cues": result["cues"], "scan_job_id": result["scan_job_id"]}))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
