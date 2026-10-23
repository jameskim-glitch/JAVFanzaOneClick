"""Opt-in EXE integration: real public source reads, synthetic local Stash writes.

Does not connect to the user's Stash. The only GraphQL server is a fresh local
fixture on an OS-selected port. Image/name changes happen in memory only.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from test_image_upgrade import Client


@unittest.skipUnless(os.environ.get("ONECLICK_LIVE_HQ_PROTOCOL") == "1", "Opt-in public original/reading download")
class ProtocolTests(unittest.TestCase):
    def test_source_or_exe_original_replace_restore_and_name(self):
        client = Client()
        client.item["name"] = "닮은 햇살"
        client.item["urls"] = ["https://www.dmm.co.jp/digital/videoa/-/list/=/article=actress/id=1078559/"]

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass

            def do_GET(self):
                if self.path != "/performer/1/image":
                    self.send_error(404)
                    return
                body = base64.b64decode(client.picture["data"].split(",", 1)[1])
                self.send_response(200)
                self.send_header("Content-Type", client.picture["mime"])
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                query, variables = request["query"], request.get("variables") or {}
                try:
                    if "performerUpdate(" in query:
                        item = variables["input"]
                        assert item["id"] == "1"
                        if "image" in item:
                            assert set(item) == {"id", "image"}
                            client.update_entity_image("performer", "1", item["image"])
                        else:
                            assert set(item) == {"id", "name", "alias_list"}
                            client.update_performer("1", item["name"], item["alias_list"])
                        data = {"performerUpdate": {"id": "1"}}
                    else:
                        data = client.graphql(query, variables)
                    result = {"data": data}
                except Exception as exc:
                    result = {"errors": [{"message": str(exc)}]}
                body = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        command = [os.environ["ONECLICK_BACKEND_EXE"]] if os.environ.get("ONECLICK_BACKEND_EXE") else [sys.executable, str(Path(__file__).with_name("jav_fanza_oneclick.py"))]
        with tempfile.TemporaryDirectory(prefix="oneclick-hq-protocol-") as tmp:
            def operation(mode, **args):
                request = {"args": {"mode": mode, **args}, "server_connection": {
                    "Endpoint": f"http://127.0.0.1:{server.server_port}/graphql", "PluginDir": tmp}}
                response = subprocess.run(command, input=json.dumps(request), text=True, encoding="utf-8", capture_output=True,
                                          timeout=90, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(response.returncode, 0, response.stderr)
                result = json.loads(response.stdout)["output"]
                self.assertTrue(result["ok"], {k: v for k, v in result.items() if k not in {"old", "new"}})
                return result

            original = client.picture["sha256"]
            found = operation("images_candidates", kind="performer", id="1")
            plan = operation("images_preview", ticket=found["ticket"], url=found["urls"][0])
            self.assertEqual(client.updates, [])
            saved = operation("images_apply", token=plan["token"], confirmed=True)
            self.assertEqual(saved["status"], "updated")
            self.assertNotEqual(client.picture["sha256"], original)
            self.assertTrue(Path(saved["history"]).is_file())
            operation("images_restore", token=saved["restore_token"], confirmed=True)
            self.assertEqual(client.picture["sha256"], original)
            name = operation("name_preview", id="1")
            self.assertIn("니토리 히나", name["options"])
            operation("name_apply", token=name["token"], name="니토리 히나", confirmed=True)
            self.assertEqual(client.item["name"], "니토리 히나")
            self.assertIn("닮은 햇살", client.item["alias_list"])
            self.assertEqual(len(client.updates), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
