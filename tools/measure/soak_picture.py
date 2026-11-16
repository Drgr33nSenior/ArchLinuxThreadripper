"""One picture through the LIVE service-tools endpoint, run inside the service-tools pod (tools/measure).

Reads the service key and a user allowed to make pictures from /run/browser/mapping.json; neither is
printed. Prints one JSON line: HTTP status, seconds, image bytes, and the server's kv_tail state before
and after (from /health). Run: kubectl exec -i deploy/service-tools -c tools -- python3 - < soak_picture.py
"""
import json
import time
import urllib.error
import urllib.request

with open("/run/browser/mapping.json") as handle:
    mapping = json.load(handle)
user = next((uid for uid, e in mapping.get("users", {}).items()
             if e.get("images") and e.get("expires_at", 0) > time.time()), None)


def health():
    with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=10) as r:
        return json.load(r).get("kv_tail")


before = health()
record = {"before": {k: (before or {}).get(k) for k in ("backed", "jobs")}}
if user is None:
    record.update(ok=False, error="no user with picture permission in the mapping")
else:
    body = json.dumps({"model": "workstation-qwen-image", "prompt": "A lighthouse on a rocky coast at dawn, photo",
                       "size": "1328x1328", "n": 1}).encode()
    req = urllib.request.Request("http://127.0.0.1:8000/webui/images/generations", data=body, method="POST",
                                 headers={"content-type": "application/json",
                                          "authorization": "Bearer " + mapping["service_key"],
                                          "x-openwebui-user-id": user})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=320) as r:
            data = json.load(r)
            record.update(ok=r.status == 200, status=r.status, image_bytes=len(data["data"][0]["b64_json"]))
    except urllib.error.HTTPError as e:
        record.update(ok=False, status=e.code, error=(e.read() or b"")[:200].decode(errors="replace"))
    record["seconds"] = round(time.monotonic() - started, 1)
after = health()
record["after"] = {k: (after or {}).get(k) for k in ("backed", "jobs", "last")}
print(json.dumps(record), flush=True)
