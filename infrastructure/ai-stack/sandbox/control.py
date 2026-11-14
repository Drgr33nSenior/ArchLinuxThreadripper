"""Deployment policy and group gate around the native Terminals ASGI application.

Lifecycle, filesystem and execution remain upstream. No request bodies are inspected,
stored or logged here. Labels contain only the three reviewed group categories.
"""
import asyncio
from collections import Counter
import json
import logging
import os
from pathlib import Path
import secrets

import httpx


def group_for(user, groups, config):
    if user.get("role") == "admin":
        return "Admin"
    if user.get("role") != "user":
        return None
    ids = {g.get("id") for g in groups if isinstance(g, dict)}
    # A Family membership wins over Parents so accidental dual membership cannot
    # bypass the owner's kids switch.
    if config["family_group_id"] in ids:
        return "Family" if config.get("family_enabled", False) else None
    return "Parents" if config["parents_group_id"] in ids else None


class Gate:
    def __init__(self, app, config, spec, client=None):
        self.app, self.config, self.spec = app, config, spec
        self.client = client
        self.counts = Counter()

    async def identity(self, headers):
        auth = headers.get(b"authorization", b"").decode("latin1")
        uid = headers.get(b"x-user-id", b"").decode("latin1")
        if not auth.startswith("Bearer ") or len(auth) > 8192 or not uid:
            return None
        async def check(client):
            base = os.environ["TERMINALS_OPEN_WEBUI_URL"].rstrip("/")
            # Only caller-owned records; never a service/admin credential.
            user = await client.get(base + "/api/v1/auths/", headers={"Authorization": auth})
            if user.status_code != 200:
                return None
            user = user.json()
            if not isinstance(user, dict) or user.get("id") != uid:
                return None
            if user.get("role") == "admin":
                return "Admin"
            groups = await client.get(base + "/api/v1/groups/", headers={"Authorization": auth})
            if groups.status_code != 200 or not isinstance(groups.json(), list):
                return None
            return group_for(user, groups.json(), self.config)
        try:
            if self.client:
                return await check(self.client)
            async with httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False) as client:
                return await check(client)
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            return None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        path, headers = scope["path"], dict(scope.get("headers", []))
        async def reply(status, body, content_type=b"application/json"):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", content_type)]})
            await send({"type": "http.response.body", "body": body})
        if scope["type"] == "http" and scope["method"] == "GET":
            if path == "/health":
                return await reply(200, b'{"status":true}')
            if path == "/metrics":
                body = "# TYPE workstation_sandbox_executions_total counter\n"
                for group in ("Admin", "Parents", "Family"):
                    body += f'workstation_sandbox_executions_total{{group="{group}"}} {self.counts[group]}\n'
                return await reply(200, body.encode(), b"text/plain; version=0.0.4")
            # Discovery must not provision a privileged 'system' workspace.
            if path == "/p/sandbox/openapi.json":
                return await reply(200, self.spec)
        if not path.startswith("/p/sandbox/") or ".." in path or "%" in path:
            return await reply(403, b'{"error":"sandbox_route_denied"}')
        # The gate and upstream must authenticate the same identity. Reject
        # ambiguous duplicate headers rather than relying on first/last wins.
        for name in (b"authorization", b"x-user-id"):
            if sum(k.lower() == name for k, _ in scope.get("headers", [])) != 1:
                return await reply(403, b'{"error":"sandbox_access_denied"}')
        group = await self.identity(headers)
        if not group:
            return await reply(403, b'{"error":"sandbox_access_denied"}')
        scope = dict(scope)
        # Force native per-user scope, never caller-chosen contexts or policies.
        scope["headers"] = [(k, v) for k, v in scope.get("headers", [])
                            if k.lower() not in {b"x-terminal-context-id", b"x-terminal-policy-id"}]
        if path == "/p/sandbox/execute" and scope.get("method") == "POST":
            # Counts authorised execution attempts, including terminal failures.
            self.counts[group] += 1
        if scope["type"] == "websocket":
            # A long-lived socket must periodically reauthenticate and observe revocations.
            try:
                await asyncio.wait_for(self.app(scope, receive, send), timeout=60)
            except asyncio.TimeoutError:
                await send({"type": "websocket.close", "code": 1008})
            return
        return await self.app(scope, receive, send)


def main():
    # A process-local admin key closes upstream's open mode; no admin route is exposed.
    os.environ["TERMINALS_API_KEY"] = secrets.token_urlsafe(32)
    from contextlib import asynccontextmanager
    from terminals.main import app
    from terminals.db.session import init_db, async_session
    from terminals.models.policy import Policy
    import uvicorn
    config = json.loads(Path("/config/config.json").read_text())
    policy = json.loads(Path("/config/policy.json").read_text())
    original_lifespan = app.router.lifespan_context
    @asynccontextmanager
    async def lifespan(application):
        init_db()
        async with async_session() as session:
            await session.merge(Policy(id="sandbox", data=policy))
            await session.commit()
        async with original_lifespan(application):
            # Upstream may attach request URLs or exception details to logs.
            logging.disable(logging.CRITICAL)
            from loguru import logger
            logger.remove()
            yield
    app.router.lifespan_context = lifespan
    gate = Gate(app, config, Path("/opt/sandbox/openapi.json").read_bytes())
    uvicorn.run(gate, host="0.0.0.0", port=3000, access_log=False, log_level="critical")


if __name__ == "__main__":
    main()
