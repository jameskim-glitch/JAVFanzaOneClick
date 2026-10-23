"""Subprocess integration for source/EXE; all writes target an in-memory fixture."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_stashdb_rescrape import Client


class ProtocolTests(unittest.TestCase):
    def test_alias_inventory_then_confirmed_native_scrape(self):
        client = Client()
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
                try:
                    result = {"data": client.graphql(request["query"], request.get("variables"))}
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
        with tempfile.TemporaryDirectory(prefix="oneclick-alias-protocol-") as root:
            def operation(mode, **args):
                request = {"args": {"mode": mode, **args}, "server_connection": {"Endpoint": f"http://127.0.0.1:{server.server_port}/graphql", "PluginDir": root}}
                proc = subprocess.run(command, input=json.dumps(request), text=True, encoding="utf-8", capture_output=True, timeout=90,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                self.assertEqual(proc.returncode, 0, proc.stderr)
                return json.loads(proc.stdout)["output"]
            scanned = operation("stashdb_alias_scan")
            self.assertEqual(scanned["eligible"], 1)
            self.assertEqual(client.updates, [])
            result = operation("stashdb_alias_run", confirmed=True)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["renamed"], 1)
            self.assertEqual(client.item["name"], "Momo Sakura")
            self.assertIn("사쿠라 모모", client.item["alias_list"])
            self.assertEqual(len(client.updates), 1)
            self.assertNotIn("image", client.updates[0])
            self.assertNotIn("tag_ids", client.updates[0])
            self.assertTrue(Path(result["history"]).is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
