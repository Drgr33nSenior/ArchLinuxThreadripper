"""Start native Open Terminal without host integration or application content logs."""
import asyncio
import json
import logging
import os
import urllib.request

# The workspace's egress proxy answers this name itself with the requests from this
# workspace that it refused (infrastructure/ai-stack/egress/main.go). Chromium reports
# a refused HTTPS tunnel only as ERR_TUNNEL_CONNECTION_FAILED; curl and Python's
# clients report a 403 without the reason header unless asked.
STATUS_URL = "http://sandbox-egress.invalid/blocked"
REFUSED = (b"ERR_TUNNEL_CONNECTION_FAILED", b"CONNECT tunnel failed, response 403",
           b"Tunnel connection failed: 403")
# Tool calls whose JSON result can carry a browser or command error.
EXPLAINED = {("POST", "/execute"), ("POST", "/browser/action"), ("GET", "/browser/snapshot")}
LIMIT = 4 << 20  # larger results pass through untouched
RECENT_SECONDS = 60  # a refusal this recent is shown even if its host is not named


def refusals(timeout=3):
    """This workspace's recent refusals from its proxy, newest first.

    The request goes through the workspace's configured proxy (HTTP_PROXY), which knows
    the workspace by its pod address and lists only that workspace's refusals.
    """
    with urllib.request.build_opener().open(STATUS_URL, timeout=timeout) as response:
        blocked = json.load(response)["blocked"]
    if not isinstance(blocked, list):
        raise ValueError("unexpected status page")
    return blocked


def explanation(blocked, text):
    """What to add to a tool result that shows a refused tunnel.

    `text` is the tool call and its result: a refusal whose host appears in either is
    shown; otherwise refusals from the last minute are.
    """
    if blocked is None:
        return {"note": "The sandbox's egress proxy probably refused this site, but it could not be asked why.",
                "check": "curl -s " + STATUS_URL}
    named = [entry for entry in blocked if isinstance(entry, dict) and isinstance(entry.get("host"), str)
             and entry["host"] and entry["host"].encode() in text]
    shown = named or [entry for entry in blocked if isinstance(entry, dict)
                      and isinstance(entry.get("seconds_ago"), int) and entry["seconds_ago"] <= RECENT_SECONDS]
    if not shown:
        return {"note": "The sandbox's egress proxy has refused nothing from this workspace recently, so this "
                        "failure is something else: the name may not exist or the site may not have answered.",
                "check": "curl -s " + STATUS_URL}
    return {"note": "The sandbox's egress proxy refused these sites. This is the sandbox's policy, not a "
                    "network fault: retrying will not help. Use another source or tell the user why.",
            "refused": shown[:5], "check": "curl -s " + STATUS_URL}


class BlockedReason:
    """Adds the proxy's reason to a tool result that shows only a refused tunnel.

    Only JSON results of the command and browser tools are read, and only when they
    carry one of the REFUSED markers; everything else passes through unchanged.
    """

    def __init__(self, app, lookup=refusals):
        self.app, self.lookup = app, lookup

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or (scope.get("method"), scope.get("path")) not in EXPLAINED:
            return await self.app(scope, receive, send)
        start, chunks, size, passing, request = None, [], 0, False, []

        async def recording():
            # The call's own arguments (a browser action's URL) help name the refused host.
            message = await receive()
            if message["type"] == "http.request" and sum(map(len, request)) < LIMIT:
                request.append(message.get("body", b""))
            return message

        async def capture(message):
            nonlocal start, size, passing
            if passing:
                return await send(message)
            if message["type"] == "http.response.start":
                headers = dict(message.get("headers", []))
                if not headers.get(b"content-type", b"").startswith(b"application/json"):
                    passing = True
                    return await send(message)
                start = message
                return None
            if message["type"] != "http.response.body":
                return await send(message)
            chunks.append(message.get("body", b""))
            size += len(chunks[-1])
            if size > LIMIT:
                passing = True
                await send(start)
                return await send({**message, "body": b"".join(chunks)})
            if message.get("more_body"):
                return None
            body = b"".join(chunks)
            if any(marker in body for marker in REFUSED):
                body = await self.explain(body, b"".join(request))
            headers = [(k, v) for k, v in start.get("headers", []) if k.lower() != b"content-length"]
            await send({**start, "headers": headers + [(b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, recording, capture)

    async def explain(self, body, call):
        try:
            value = json.loads(body)
        except ValueError:
            return body
        if not isinstance(value, dict):
            return body
        try:
            blocked = await asyncio.wait_for(asyncio.to_thread(self.lookup), timeout=5)
        except Exception:  # noqa: BLE001 - any failure falls back to telling the model where to look
            blocked = None
        value["sandbox_egress"] = explanation(blocked, call + b"\n" + body)
        return json.dumps(value).encode()


def main():
    os.chdir("/home/user")
    logging.disable(logging.CRITICAL)
    import uvicorn
    from open_terminal.main import app
    uvicorn.run(BlockedReason(app), host="0.0.0.0", port=8000,
                access_log=False, log_level="critical")


if __name__ == "__main__":
    main()
