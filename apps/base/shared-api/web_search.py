"""Bounded Exa/Tavily research with a durable, shared request ceiling.

Only explicit queries and public URLs leave the workstation. No conversations,
retrieved private passages, user identities, or provider responses are persisted.
Reservations survive failures/restarts: an uncertain request is never refunded.
"""
import asyncio
from datetime import datetime, timezone
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

import httpx

from boundary import Refusal


PROVIDERS = {"exa": "https://api.exa.ai", "tavily": "https://api.tavily.com"}
MAX_RESPONSE = 2 * 1024 * 1024
POLICY_KEYS = {"provider", "monthly_requests", "daily_requests", "requests_per_minute", "max_results", "max_content_chars"}


def public_url(value):
    """Validate a public URL; fetching always happens at the fixed provider."""
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(c.isspace() or ord(c) < 32 for c in value):
        raise Refusal(400, "public_http_url_required")
    try:
        url = urlsplit(value)
        host = (url.hostname or "").lower().rstrip(".")
        if (url.scheme not in {"http", "https"} or url.username is not None or url.password is not None
                or url.port not in {None, 80, 443} or "\\" in value or "%" in host
                or not host or "." not in host or host.endswith((".local", ".localhost", ".internal", ".lan", ".home", ".test", ".invalid", ".onion", ".arpa"))):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if (not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", host)
                    or ".." in host or host.rsplit(".", 1)[-1].isdigit()):
                raise ValueError()
        else:
            if not address.is_global:
                raise ValueError()
    except ValueError:
        raise Refusal(400, "public_http_url_required") from None
    return value


class WebSearch:
    def __init__(self, client, key_file, state_dir, policy_file, *, clock=None, content_limit=None):
        self.client = client
        self.key_file = Path(key_file)
        self.database = Path(state_dir) / "web-search-usage.sqlite3"
        self.policy = json.loads(Path(policy_file).read_text())
        if (not isinstance(self.policy, dict) or set(self.policy) != POLICY_KEYS
                or self.policy.get("provider") not in PROVIDERS
                or any(type(n) is not int or not 1 <= n <= 100000 for k, n in self.policy.items() if k != "provider")
                or self.policy["max_results"] > 3 or self.policy["max_content_chars"] > 12000):
            raise ValueError("invalid web search policy")
        if content_limit is not None:
            if not 1 <= content_limit <= self.policy["max_content_chars"]:
                raise ValueError("invalid web content limit")
            self.policy["max_content_chars"] = content_limit
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.active = 0

    def status(self):
        status = {"provider": self.policy["provider"], "status": "configured-not-qualified",
                  "monthly_request_limit": self.policy["monthly_requests"], "monthly_reserved": 0}
        try:
            self.credential()
        except Refusal as exc:
            status["status"] = exc.code
        if self.database.exists():
            try:
                with sqlite3.connect(self.database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2) as db:
                    row = db.execute("SELECT requests FROM usage WHERE period = ?",
                        (self.clock().astimezone(timezone.utc).strftime("month:%Y-%m"),)).fetchone()
                    if row and (type(row[0]) is not int or row[0] < 0):
                        raise sqlite3.DatabaseError("invalid request counter")
                    status["monthly_reserved"] = row[0] if row else 0
            except sqlite3.Error:
                status["status"] = "web_search_accounting_unavailable"
        return status

    def credential(self):
        # Projected Kubernetes Secret symlinks are intentional. Never log values.
        try:
            with self.key_file.open() as source:
                key = source.read(4097).strip()
        except OSError:
            raise Refusal(503, "web_search_credential_missing") from None
        if not 16 <= len(key) <= 4096 or any(c.isspace() or ord(c) < 32 for c in key):
            raise Refusal(503, "web_search_credential_invalid")
        return key

    def reserve(self):
        """Each search or extraction of <=3 URLs reserves one API request."""
        now = self.clock().astimezone(timezone.utc)
        periods = [(now.strftime("month:%Y-%m"), self.policy["monthly_requests"]),
                   (now.strftime("day:%Y-%m-%d"), self.policy["daily_requests"]),
                   (now.strftime("minute:%Y-%m-%dT%H:%M"), self.policy["requests_per_minute"])]
        self.database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.database.is_symlink():
            raise Refusal(503, "web_search_accounting_unavailable")
        existed = self.database.exists()
        try:
            with sqlite3.connect(self.database, timeout=2) as db:
                db.execute("BEGIN IMMEDIATE")
                if not existed:
                    db.execute("CREATE TABLE IF NOT EXISTS usage (period TEXT PRIMARY KEY, requests INTEGER NOT NULL)")
                for period, limit in periods:
                    row = db.execute("SELECT requests FROM usage WHERE period = ?", (period,)).fetchone()
                    if row and (type(row[0]) is not int or row[0] < 0):
                        raise Refusal(503, "web_search_accounting_unavailable")
                    if row and row[0] >= limit:
                        raise Refusal(429, "web_search_" + period.split(":")[0] + "_limit")
                for period, _ in periods:
                    db.execute("INSERT INTO usage VALUES (?, 1) ON CONFLICT(period) DO UPDATE SET requests = requests + 1", (period,))
        except sqlite3.Error:
            raise Refusal(503, "web_search_accounting_unavailable") from None

    async def request(self, endpoint, data):
        key = self.credential()
        if self.active >= 2:
            raise Refusal(429, "web_search_busy")
        self.active += 1
        try:
            self.reserve()
            # No caller headers, cookies, user identifiers, or arbitrary URL target.
            provider = self.policy["provider"]
            headers = {"x-api-key": key} if provider == "exa" else {"Authorization": "Bearer " + key}
            async with asyncio.timeout(25):
                async with self.client.stream("POST", PROVIDERS[provider] + endpoint,
                        headers=headers, json=data, timeout=20,
                        follow_redirects=False) as response:
                    if response.status_code == 429:
                        raise Refusal(429, "web_search_provider_limit")
                    if response.status_code != 200:
                        raise Refusal(503, "web_search_provider_unavailable")
                    raw = bytearray()
                    async for part in response.aiter_bytes():
                        raw.extend(part)
                        if len(raw) > MAX_RESPONSE:
                            raise Refusal(502, "web_search_response_too_large")
                    result = json.loads(raw)
                    if not isinstance(result, dict):
                        raise Refusal(502, "web_search_invalid_response")
                    return result
        except (httpx.HTTPError, TimeoutError, ValueError, TypeError):
            raise Refusal(503, "web_search_provider_unavailable") from None
        finally:
            self.active -= 1

    async def search(self, args):
        if not isinstance(args, dict) or set(args) - {"query", "count"}:
            raise Refusal(400, "invalid_tool_arguments")
        query, count = args.get("query", ""), args.get("count", 3)
        if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 512
                or any(ord(c) < 32 for c in query) or type(count) is not int or not 1 <= count <= 3):
            raise Refusal(400, "invalid_search_query_or_count")
        count = min(count, self.policy["max_results"])
        if self.policy["provider"] == "exa":
            # Auto search includes content for <=10 results. No generated
            # summaries, Agent runs, extra queries or subpage crawling.
            data = {"query": query.strip(), "numResults": count, "type": "auto",
                    "contents": {"highlights": {"maxCharacters": 1600}}}
        else:
            data = {"query": query.strip(), "max_results": count,
                "search_depth": "basic", "auto_parameters": False, "include_answer": False,
                "include_raw_content": False, "include_images": False, "include_usage": True}
        result = await self.request("/search", data)
        rows = result.get("results")
        if not isinstance(rows, list):
            raise Refusal(502, "web_search_invalid_response")
        hits = []
        for row in rows[:count]:
            if not isinstance(row, dict):
                continue
            try:
                url = public_url(row.get("url"))
            except Refusal:
                continue
            content = row.get("content")
            if self.policy["provider"] == "exa":
                highlights = row.get("highlights")
                content = "\n".join(h for h in highlights if isinstance(h, str)) if isinstance(highlights, list) else None
            if not isinstance(content, str) or not isinstance(row.get("title"), str):
                continue
            hits.append({"link": url, "title": row["title"][:240], "snippet": content[:1600]})
        return hits

    async def extract(self, urls):
        if not isinstance(urls, list) or not 1 <= len(urls) <= 3:
            raise Refusal(400, "one_to_three_public_urls_required")
        urls = list(dict.fromkeys(public_url(url) for url in urls))
        if self.policy["provider"] == "exa":
            result = await self.request("/contents", {"ids": urls,
                "text": {"maxCharacters": self.policy["max_content_chars"]}})
            content_field = "text"
        else:
            result = await self.request("/extract", {"urls": urls, "extract_depth": "basic",
                "format": "text", "include_images": False, "include_usage": True})
            content_field = "raw_content"
        rows = result.get("results")
        if not isinstance(rows, list):
            raise Refusal(502, "web_search_invalid_response")
        pages = []
        for row in rows[:len(urls)]:
            if not isinstance(row, dict) or row.get("url") not in urls or not isinstance(row.get(content_field), str):
                continue
            text = row[content_field]
            if text.strip():
                pages.append({"page_content": text[:self.policy["max_content_chars"]],
                    "metadata": {"source": row["url"], "content_trust": "untrusted-source-text",
                                 "truncated": len(text) > self.policy["max_content_chars"]}})
        if not pages:
            raise Refusal(502, "web_page_not_available")
        return pages
