"""Open WebUI reasoning presets: rendered from the contract, applied to named groups only."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "apps/base/shared-api"), str(ROOT / "lib/workstation")]
import service
import webui_presets

PROFILE = ROOT / "infrastructure/ai-stack/serving/qwen3.8-27b-mxfp4-tp2.json"


class FakeWebUI(BaseHTTPRequestHandler):
    groups = [{"id": "g-parents", "name": "Parents"}, {"id": "g-kids", "name": "Kids"}]
    imported = []
    auth = []
    answer = True

    def _send(self, value):
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.auth.append(self.headers.get("Authorization"))
        self._send(self.groups if self.path == "/api/v1/groups/" else None)

    def do_POST(self):
        self.auth.append(self.headers.get("Authorization"))
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path == "/api/v1/models/import":
            self.imported.append(body)
        self._send(self.answer)

    def log_message(self, *args):
        pass


class PresetTests(unittest.TestCase):
    def setUp(self):
        FakeWebUI.imported, FakeWebUI.auth, FakeWebUI.answer = [], [], True
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeWebUI)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def test_presets_follow_the_contract(self):
        contract = service.serving_contract(PROFILE)
        rows = webui_presets.presets(contract)
        alias = contract["model"]["alias"]
        self.assertEqual([alias, alias], [r["base_model_id"] for r in rows])
        think, fast = rows
        self.assertEqual(("qwen3.8-27b-mxfp4-think", "qwen3.8-27b-mxfp4-fast"), (think["id"], fast["id"]))
        # Think: thinking on at the template's deepest accepted effort; Fast: thinking off.
        self.assertIn(think["params"]["reasoning_effort"], contract["chat_template"]["reasoning_efforts"])
        self.assertEqual("xhigh", think["params"]["reasoning_effort"])
        self.assertEqual({"enable_thinking": True}, think["params"]["custom_params"]["chat_template_kwargs"])
        self.assertEqual({"enable_thinking": False}, fast["params"]["custom_params"]["chat_template_kwargs"])
        self.assertNotIn("reasoning_effort", fast["params"])
        # Both stay inside what the gateway admits.
        self.assertIn(think["params"]["reasoning_effort"], contract["reasoning"]["client_efforts"])
        self.assertEqual(["enable_thinking"], contract["reasoning"]["client_template_kwargs"])
        self.assertFalse(any("access_grants" in r for r in rows))  # granted at apply time only
        unverified = dict(contract, reasoning=dict(contract["reasoning"], request_defaults=None))
        self.assertEqual([], webui_presets.presets(unverified))

    def test_apply_upserts_with_read_access_for_named_groups_only(self):
        rows = webui_presets.presets(service.serving_contract(PROFILE))
        self.assertEqual([r["id"] for r in rows], webui_presets.apply(rows, self.url, "sk-admin", ["Parents"]))
        models = FakeWebUI.imported[-1]["models"]
        self.assertEqual([r["id"] for r in rows], [m["id"] for m in models])
        for model in models:
            self.assertEqual([{"principal_type": "group", "principal_id": "g-parents", "permission": "read"}],
                             model["access_grants"])
        self.assertEqual({"Bearer sk-admin"}, set(FakeWebUI.auth))
        for groups, message in ((["Nobody"], "unknown Open WebUI group"), ([], "never public")):
            with self.assertRaisesRegex(ValueError, message):
                webui_presets.apply(rows, self.url, "sk-admin", groups)
        with self.assertRaisesRegex(ValueError, "unverified"):
            webui_presets.apply([], self.url, "sk-admin", ["Parents"])
        FakeWebUI.answer = False
        with self.assertRaisesRegex(ValueError, "refused"):
            webui_presets.apply(rows, self.url, "sk-admin", ["Parents"])

    def test_cli_refuses_plain_http_and_a_shared_key_file(self):
        with tempfile.TemporaryDirectory() as directory:
            key = Path(directory) / "key"
            key.write_text("sk-admin\n")
            key.chmod(0o644)
            presets = Path(directory) / "presets.json"
            presets.write_text("[]")
            for argv in (["apply", str(presets), "--url", self.url, "--key-file", str(key), "--group", "Parents"],
                         ["apply", str(presets), "--url", "https://chat.example", "--key-file", str(key),
                          "--group", "Parents"]):
                with self.assertRaises(SystemExit):
                    webui_presets.main(argv)
        self.assertEqual([], FakeWebUI.imported)

    def test_render_writes_the_presets_beside_the_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            site = root / "site.json"
            site.write_text(json.dumps({"context": "fixture", "node": "fixture", "node_address": "192.168.77.9",
                "management_cidr": "192.168.77.0/24", "client_cidrs": ["192.168.77.0/24"],
                "webui_host": "chat.fixture.test", "api_host": "api.fixture.test", "tls_secret": "fixture-tls"}))
            output = root / "shared"
            service.render(SimpleNamespace(site=site, output=output, mode="shared", experience="assistant-memory",
                                           serving_profile=PROFILE, arm="UQS", runtime_image=None, output_tokens=4096))
            rendered = json.loads((output / "webui-presets.json").read_text())
            self.assertEqual(["qwen3.8-27b-mxfp4-think", "qwen3.8-27b-mxfp4-fast"], [r["id"] for r in rendered])
            evidence = json.loads((output / "evidence.json").read_text())
            self.assertIn("webui-presets.json", evidence["files"])


if __name__ == "__main__":
    unittest.main()
