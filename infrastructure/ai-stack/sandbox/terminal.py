"""Start native Open Terminal without host integration or application content logs."""
import asyncio
import json
import logging
import os
import re
import urllib.request

# The workspace's egress proxy answers this name itself with the requests from this
# workspace that it refused (infrastructure/ai-stack/egress/main.go). Chromium reports
# a refused HTTPS tunnel only as ERR_TUNNEL_CONNECTION_FAILED; curl and Python's
# clients report a 403 without the reason header unless asked. Chromium also reports a
# 502 (a name that does not exist) that way, so the marker alone is not a refusal.
STATUS_URL = "http://sandbox-egress.invalid/blocked"
REFUSED = (b"ERR_TUNNEL_CONNECTION_FAILED", b"CONNECT tunnel failed, response 403",
           b"Tunnel connection failed: 403", b"Proxy tunneling failed: Forbidden",
           b"(403) !== 200 when HTTP Tunneling")
# Tool calls whose JSON result can carry a browser or command error.
EXPLAINED = re.compile(r"(POST /execute|GET /execute/[^/]+/status|POST /browser/action|GET /browser/snapshot)")
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


def mentions(text, host):
    """Whether `text` names `host` as a whole host name: not ample.com in example.com."""
    pattern = rb"(?<![A-Za-z0-9.-])" + re.escape(host.encode()) + rb"(?![A-Za-z0-9-]|\.[A-Za-z0-9])"
    return re.search(pattern, text, re.IGNORECASE) is not None


def explanation(blocked, text):
    """What to add to a tool result that shows a failed tunnel.

    `text` is the tool call and its result. A refusal whose host it names is the
    reason. Otherwise refusals from the last minute are offered as possibly related:
    the same browser error also covers a name that does not exist.
    """
    check = "curl -s " + STATUS_URL
    if blocked is None:
        return {"note": "The sandbox's egress proxy may have refused this site, but it could not be asked why.",
                "check": check}
    entries = [entry for entry in blocked if isinstance(entry, dict) and isinstance(entry.get("host"), str)
               and entry["host"] and isinstance(entry.get("seconds_ago"), int)]
    named = [entry for entry in entries if mentions(text, entry["host"])]
    if named:
        return {"note": "The sandbox's egress proxy refused these sites. This is the sandbox's policy, not a "
                        "network fault: retrying will not help. Use another source or tell the user why.",
                "refused": named[:5], "check": check}
    recent = [entry for entry in entries if entry["seconds_ago"] <= RECENT_SECONDS]
    if recent:
        return {"note": "The proxy has no refusal on record for a site named here. It refused the sites below "
                        "from this workspace in the last minute; they may be related, for example a page's "
                        "redirect or resource. Otherwise the name may not exist or the site may not have answered.",
                "possibly_related": recent[:5], "check": check}
    return {"note": "The sandbox's egress proxy has refused nothing from this workspace recently, so this "
                    "failure is something else: the name may not exist or the site may not have answered.",
            "check": check}


class BlockedReason:
    """Adds the proxy's reason to a tool result that shows only a refused tunnel.

    Only JSON results of the command and browser tools are read, and only when they
    carry one of the REFUSED markers; everything else passes through unchanged.
    """

    def __init__(self, app, lookup=refusals):
        self.app, self.lookup = app, lookup

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not EXPLAINED.fullmatch(f'{scope.get("method")} {scope.get("path")}'):
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
