"""Offline native-tool game smoke; run inside an unprivileged, network-none container."""
import json
import importlib.metadata
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
import urllib.request

GAME = '''<!doctype html><html lang="en"><meta charset="utf-8"><title>Star catcher</title>
<style>body{font:24px sans-serif;background:#10213b;color:white;text-align:center}
button{font-size:48px;padding:24px;border-radius:16px;cursor:pointer}</style>
<h1>Star catcher</h1><p>Catch three stars to win.</p><button aria-label="Catch star">★</button>
<p role="status">Score: 0</p><script>let score=0;document.querySelector('button').onclick=()=>{
score++;document.querySelector('[role=status]').textContent=score===3?'You win!':'Score: '+score;
if(score===3)document.querySelector('button').disabled=true;};</script></html>'''


def main():
    assert os.getuid() == 1000
    assert not Path("/var/run/secrets/kubernetes.io/serviceaccount/token").exists()
    assert not Path("/dev/kfd").exists()
    try:
        Path("/opt/sandbox/write-probe").write_text("probe")
    except OSError:
        pass
    else:
        raise AssertionError("root filesystem is writable")
    key = secrets.token_urlsafe(32)
    env = {**os.environ, "OPEN_TERMINAL_API_KEY": key,
           "OPEN_TERMINAL_ENABLE_DESKTOP": "false", "OPEN_TERMINAL_BROWSER_NO_SANDBOX": "true"}
    process = subprocess.Popen([sys.executable, "/opt/sandbox/terminal.py"], env=env,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    def request(path, payload=None):
        req = urllib.request.Request("http://127.0.0.1:8000" + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as response:
            return json.load(response)
    try:
        for _ in range(100):
            try:
                request("/health")
                break
            except OSError:
                time.sleep(.1)
        else:
            raise AssertionError("terminal did not become ready")
        spec = request("/openapi.json")
        assert spec["paths"]["/execute"]["post"]["operationId"] == "run_command"
        # The assistant writes the game through the same native tool the model receives.
        command = "python - <<'PY'\nfrom pathlib import Path\nPath('index.html').write_text(" + repr(GAME) + ")\nPY"
        assert request("/execute?wait=10", {"command": command})["exit_code"] == 0
        server = request("/execute?wait=1", {"command": "python -m http.server 8765 --bind 127.0.0.1"})
        assert server["status"] == "running"
        # Playwright executes inside the workspace; its debugging pipe has no network listener.
        browser = '''from playwright.sync_api import sync_playwright
import json
from pathlib import Path
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto('http://127.0.0.1:8765')
    assert page.title() == 'Star catcher'
    for _ in range(3): page.get_by_role('button', name='Catch star').click()
    assert page.get_by_role('status').inner_text() == 'You win!'
    assert page.get_by_role('button').is_disabled()
    assert not errors
    page.screenshot(path='/home/user/game.png')
    Path('/home/user/game-result.json').write_text(json.dumps({'title':page.title(),
        'snapshot':page.locator('body').aria_snapshot(), 'page_errors':len(errors)}))
    browser.close()
'''
        command = "python - <<'PY'\n" + browser + "\nPY"
        assert request("/execute?wait=60", {"command": command})["exit_code"] == 0
        assert Path("/home/user/game.png").stat().st_size > 1000
        req = urllib.request.Request("http://127.0.0.1:8000/files/read?path=/home/user/game.png",
                                     headers={"Authorization": "Bearer " + key})
        with urllib.request.urlopen(req, timeout=10) as response:
            assert response.headers["Content-Type"].startswith("image/png")
            assert response.read(8) == b"\x89PNG\r\n\x1a\n"
        print(json.dumps({"status": "passed", "native_execute": True, "playwright_chromium": True,
                          "playwright_version": importlib.metadata.version("playwright"), "native_image_read": True,
                          "game": "three clicks reach the win state", "page_errors": 0,
                          "network": "none", "uid": os.getuid()}))
    finally:
        process.terminate()
        process.wait(timeout=10)


if __name__ == "__main__":
    main()
