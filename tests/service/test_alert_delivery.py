"""Real Alertmanager, loopback receiver; no owner destination or credentials."""
import datetime
import http.server
import json
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request

import yaml

ROOT = Path(__file__).resolve().parents[2]


class AlertDelivery(unittest.TestCase):
    def test_selected_binary_routing_inhibition_and_resolution(self):
        binary = shutil.which("alertmanager")
        self.assertIsNotNone(binary, "pinned Alertmanager binary is a required CI tool")
        self.assertIn("0.34.1", subprocess.check_output([binary, "--version"], stderr=subprocess.STDOUT).decode())
        subprocess.run(["promtool", "check", "rules", str(ROOT / "infrastructure/service/alerts.yaml")], check=True)
        delivered = []
        class Receiver(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                delivered.extend(a["labels"]["alertname"] for a in data["alerts"])
                self.send_response(200)
                self.end_headers()
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = next(o["data"]["alertmanager.yaml"] for o in yaml.safe_load_all(
                (ROOT / "infrastructure/service/alertmanager.yaml").read_text()) if o["kind"] == "ConfigMap")
            config = yaml.safe_load(config)
            receiver_file = root / "receiver-url"
            receiver_file.write_text(f"http://127.0.0.1:{server.server_port}/notify")
            config["receivers"][1]["webhook_configs"][0]["url_file"] = str(receiver_file)
            config["route"].update(group_wait="1s", group_interval="1s")
            config_path = root / "config.yaml"
            config_path.write_text(yaml.safe_dump(config))
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
            process = subprocess.Popen([binary, "--config.file=" + str(config_path), "--storage.path=" + str(root / "state"),
                f"--web.listen-address=127.0.0.1:{port}", "--cluster.listen-address=", "--log.level=error"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                base = f"http://127.0.0.1:{port}"
                for _ in range(100):
                    try:
                        with urllib.request.urlopen(base + "/-/ready", timeout=1):
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    self.fail("Alertmanager startup")
                now = datetime.datetime.now(datetime.timezone.utc)
                def alert(name):
                    return {"labels": {"alertname": name, "cluster": "workstation", "severity": "warning"},
                            "startsAt": now.isoformat(), "endsAt": (now + datetime.timedelta(minutes=5)).isoformat()}
                maintenance = alert("WorkstationServiceMaintenance")
                model = alert("WorkstationModelUnavailable")
                backup = alert("WorkstationBackupFailed")
                local_backup = alert("WorkstationLocalBackupFailed")
                def send(alerts):
                    req = urllib.request.Request(base + "/api/v2/alerts", data=json.dumps(alerts).encode(),
                                                 headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=2):
                        pass
                send([maintenance, model, backup, local_backup])
                for _ in range(100):
                    if {"WorkstationBackupFailed", "WorkstationLocalBackupFailed"} <= set(delivered):
                        break
                    time.sleep(0.1)
                self.assertIn("WorkstationBackupFailed", delivered)
                self.assertIn("WorkstationLocalBackupFailed", delivered)
                self.assertNotIn("WorkstationModelUnavailable", delivered)
                self.assertNotIn("WorkstationServiceMaintenance", delivered)
                maintenance["endsAt"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                send([maintenance])
                for _ in range(100):
                    if "WorkstationModelUnavailable" in delivered:
                        break
                    time.sleep(0.1)
                self.assertIn("WorkstationModelUnavailable", delivered)
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
