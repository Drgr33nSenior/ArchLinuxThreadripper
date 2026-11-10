"""Offline HTTP/credit/security contracts; never contact Tavily or Kubernetes."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps/base/shared-api"))
from assistant_metrics import METRICS
from boundary import Refusal
from service_tools import ServiceTools
from web_search import WebSearch


class SearchTests(unittest.IsolatedAsyncioTestCase):
    provider = "tavily"

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.key = self.root / "api-key"
        self.key.write_text("provider-test-credential-only")
        self.mapping = self.root / "mapping.json"
        self.mapping.write_text(json.dumps({"service_key": "s" * 32, "users": {
            "alice": {"expires_at": 9999999999, "images": False}}}))
        self.policy_file = self.root / "policy.json"
        self.policy = {"provider": self.provider, "monthly_requests": 1000, "daily_requests": 100,
                       "requests_per_minute": 6, "max_results": 3, "max_content_chars": 6000}
        self.policy_file.write_text(json.dumps(self.policy))
        self.now = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)
        self.calls = []
        self.status_code = 200
        self.slow = False
        self.large = False
        self.empty_extract = False
        self.page_text = "untrusted page " * 1000
        self.highlights = ["Evidence " * 500]
        self.role = "user"
        async def upstream(request):
            self.calls.append(request)
            if request.url.host == "webui":
                self.assertEqual("/api/v1/auths/", request.url.path)
                return httpx.Response(200, json={"id": "alice", "role": self.role})
            self.assertEqual("api.exa.ai" if self.provider == "exa" else "api.tavily.com", request.url.host)
            self.assertEqual("https", request.url.scheme)
            if self.provider == "exa":
                self.assertEqual("provider-test-credential-only", request.headers["x-api-key"])
                self.assertNotIn("authorization", request.headers)
            else:
                self.assertEqual("Bearer provider-test-credential-only", request.headers["authorization"])
            self.assertNotIn("x-openwebui-user-id", request.headers)
            self.assertNotIn("cookie", request.headers)
            if self.slow:
                await asyncio.sleep(0.03)
            if self.status_code != 200:
                return httpx.Response(self.status_code, headers={"location": "https://untrusted.example/"},
                                      text="do not expose provider response")
            if self.large:
                return httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))
            if request.url.path == "/search":
                content = ({"highlights": self.highlights, "id": "exa-doc-1", "publishedDate": "2026-10-30T08:00:00.000Z"}
                           if self.provider == "exa" else
                           {"content": "Evidence " * 500, "published_date": "Fri, 30 Oct 2026 08:00:00 GMT"})
                return httpx.Response(200, json={"results": [
                    {"title": "Upstream docs", "url": "https://docs.sglang.io/", **content},
                    {"title": "Private", "url": "http://127.0.0.1/admin", "content": "private"}], "usage": {"credits": 1}})
            self.assertEqual("/contents" if self.provider == "exa" else "/extract", request.url.path)
            body = json.loads(request.content)
            if self.provider == "exa":
                # Exa applies text.maxCharacters server-side, like the real API.
                text = self.page_text[:body["text"]["maxCharacters"]]
                rows = [{"url": url, "id": url, "title": "Page title", "publishedDate": "2026-10-01", "text": text}
                        for url in body["ids"]]
            else:
                rows = [{"url": url, "raw_content": self.page_text} for url in body["urls"]]
            return httpx.Response(200, json={"results": [] if self.empty_extract else rows})
        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(upstream), trust_env=False)
        self.addAsyncCleanup(self.upstream.aclose)
        self.search = self.new_search()
        self.tools = ServiceTools(self.upstream, "http://webui", "http://swarm", "http://gateway",
            self.mapping, self.root / "state", web_search=self.search)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.tools.app), base_url="http://tools")
        self.addAsyncCleanup(self.client.aclose)
        self.headers = {"Authorization": "Bearer " + "s" * 32, "X-OpenWebUI-User-Id": "alice"}

    def new_search(self):
        return WebSearch(self.upstream, self.key, self.root / "state", self.policy_file, clock=lambda: self.now)

    async def mcp(self, name, arguments):
        return await self.client.post("/mcp", headers={"Authorization": "Bearer caller-fixture-only"}, json={
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}})

    def provider_calls(self):
        return [c for c in self.calls if c.url.host == ("api.exa.ai" if self.provider == "exa" else "api.tavily.com")]

    async def test_browser_and_mcp_share_fixed_provider_and_credit_ledger(self):
        catalog = await self.client.post("/mcp", headers={"Authorization": "Bearer caller-fixture-only"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(["retrieve_project_passage", "search_web", "read_web_page"],
                         [tool["name"] for tool in catalog.json()["result"]["tools"]])
        response = await self.client.post("/webui/search", headers=self.headers,
                                          json={"query": "SGLang docs", "count": 3})
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, len(response.json()))
        self.assertLessEqual(len(response.json()[0]["snippet"]), 1600)
        response = await self.mcp("search_web", {"query": "public Qwen documentation"})
        self.assertEqual(200, response.status_code)
        value = json.loads(response.json()["result"]["content"][0]["text"])
        self.assertEqual("untrusted-source-text", value["content_trust"])
        self.assertEqual(2, self.search.status()["monthly_reserved"])
        for call in self.provider_calls():
            body = json.loads(call.content)
            if self.provider == "exa":
                self.assertEqual("auto", body["type"])
                self.assertEqual({"highlights": {"maxCharacters": 1600}}, body["contents"])
                self.assertNotIn("additionalQueries", body)
            else:
                self.assertEqual("basic", body["search_depth"])
                self.assertFalse(body["auto_parameters"])
                self.assertFalse(body["include_answer"])
            self.assertNotIn("messages", body)
            self.assertNotIn("collection", body)
        self.assertNotIn("SGLang", self.search.database.read_bytes().decode("latin1"))

    async def test_page_reader_and_webui_loader_have_bounded_cited_content(self):
        url = "https://docs.sglang.io/"
        response = await self.mcp("read_web_page", {"url": url})
        self.assertEqual(200, response.status_code)
        pages = json.loads(response.json()["result"]["content"][0]["text"])["pages"]
        self.assertEqual(url, pages[0]["metadata"]["source"])
        self.assertTrue(pages[0]["metadata"]["truncated"])
        self.assertEqual(6000, len(pages[0]["page_content"]))
        # The pinned external loader supplies no user headers.
        response = await self.client.post("/webui/fetch", headers={"Authorization": self.headers["Authorization"]},
                                          json={"urls": [url, "https://www.kernel.org/"]})
        self.assertEqual(200, response.status_code)
        self.assertEqual(2, len(response.json()))
        self.assertEqual(2, self.search.status()["monthly_reserved"])
        self.empty_extract = True
        self.assertEqual(502, (await self.mcp("read_web_page", {"url": url})).status_code)

    async def test_read_web_page_continues_with_a_validated_offset(self):
        url = "https://docs.sglang.io/"
        first = json.loads((await self.mcp("read_web_page", {"url": url})).json()["result"]["content"][0]["text"])
        meta = first["pages"][0]["metadata"]
        self.assertEqual((0, 6000, "truncated"), (meta["offset"], meta["next_offset"], meta["completeness"]))
        response = await self.mcp("read_web_page", {"url": url, "offset": meta["next_offset"]})
        self.assertEqual(200, response.status_code)
        page = json.loads(response.json()["result"]["content"][0]["text"])["pages"][0]
        self.assertEqual(self.page_text[6000:12000], page["page_content"])
        self.assertEqual((url, 6000, 12000), (page["metadata"]["source"], page["metadata"]["offset"],
                                              page["metadata"]["next_offset"]))
        self.assertEqual(hashlib.sha256(page["page_content"].encode()).hexdigest(), page["metadata"]["content_sha256"])
        self.assertEqual("2026-11-02T10:00:00+00:00", page["metadata"]["retrieved_at"])
        self.assertEqual(2, self.search.status()["monthly_reserved"])
        # Offsets are validated before a provider request is reserved.
        for offset in (-1, True, "6000", 1.5, 42001, 48000):
            self.assertEqual(400, (await self.mcp("read_web_page", {"url": url, "offset": offset})).status_code, offset)
        self.assertEqual(400, (await self.mcp("read_web_page", {"url": url, "offset": 0, "count": 1})).status_code)
        self.assertEqual(400, (await self.mcp("read_web_page", {"offset": 6000})).status_code)
        self.assertEqual(2, self.search.status()["monthly_reserved"])
        # Past the provider text is no evidence; a provider failure stays distinct.
        self.page_text = "short"
        self.assertEqual(502, (await self.mcp("read_web_page", {"url": url, "offset": 6000})).status_code)
        self.status_code = 503
        self.assertEqual(503, (await self.mcp("read_web_page", {"url": url, "offset": 6000})).status_code)
        metrics = METRICS.render()
        for outcome in ("ok", "no_results", "provider_error", "invalid"):
            self.assertIn('workstation_assistant_web_requests_total{client="webui",outcome="%s",tool="read_continue"}'
                          % outcome, metrics)
        self.assertNotIn("docs.sglang.io", metrics)

    async def test_provider_capped_page_is_never_labelled_complete(self):
        url = "https://docs.sglang.io/"
        page = (await self.search.extract([url]))[0]
        self.assertEqual(self.page_text[:6000], page["page_content"])
        self.assertEqual(("truncated", True, 0, 6000), tuple(page["metadata"][k] for k in
                         ("completeness", "truncated", "offset", "next_offset")))
        if self.provider == "exa":
            self.assertEqual(6001, json.loads(self.provider_calls()[-1].content)["text"]["maxCharacters"])
        # A short page cannot be proven complete when the provider caps text.
        self.page_text = "short public page"
        meta = (await self.search.extract([url]))[0]["metadata"]
        expected = ("unknown", True) if self.provider == "exa" else ("complete", False)
        self.assertEqual(expected, (meta["completeness"], meta["truncated"]))
        self.assertNotIn("next_offset", meta)

    async def test_bounded_follow_up_section_of_one_page(self):
        url = "https://docs.sglang.io/"
        page = (await self.search.extract([url], offset=6000))[0]
        self.assertEqual(self.page_text[6000:12000], page["page_content"])
        self.assertEqual((6000, "truncated", 12000), (page["metadata"]["offset"],
                         page["metadata"]["completeness"], page["metadata"]["next_offset"]))
        if self.provider == "exa":
            self.assertEqual(12001, json.loads(self.provider_calls()[-1].content)["text"]["maxCharacters"])
        last = (await self.search.extract([url], offset=12000))[0]["metadata"]
        self.assertEqual("unknown" if self.provider == "exa" else "complete", last["completeness"])
        self.page_text = "x" * 60000
        self.assertNotIn("next_offset", (await self.search.extract([url], offset=42000))[0]["metadata"])
        self.assertEqual(3, self.search.status()["monthly_reserved"])
        with self.assertRaises(Refusal) as caught:
            await self.search.extract([url], offset=60000 - 6000)
        self.assertEqual("invalid_page_section", caught.exception.code)
        for offset, urls in ((-1, [url]), (True, [url]), (1.5, [url]), ("6000", [url]), (42001, [url]),
                             (6000, [url, "https://www.kernel.org/"])):
            with self.assertRaises(Refusal) as caught:
                await self.search.extract(urls, offset=offset)
            self.assertEqual("invalid_page_section", caught.exception.code)
        self.assertEqual(3, self.search.status()["monthly_reserved"])
        self.page_text = "short"
        with self.assertRaises(Refusal) as caught:
            await self.search.extract([url], offset=6000)
        self.assertEqual("web_page_not_available", caught.exception.code)

    async def test_source_identity_retrieval_time_and_whole_highlights(self):
        self.highlights = ["A" * 700, "B" * 700, "C" * 700]
        hit = (await self.search.search({"query": "SGLang docs"}))[0]
        self.assertEqual(("2026-11-02T10:00:00+00:00", "2026-10-30", self.provider, "truncated"),
                         (hit["retrieved_at"], hit["published_date"], hit["provider"], hit["completeness"]))
        self.assertEqual(hashlib.sha256(hit["snippet"].encode()).hexdigest(), hit["content_sha256"])
        if self.provider == "exa":
            self.assertEqual("A" * 700 + "\n" + "B" * 700, hit["snippet"])
            self.assertEqual("exa-doc-1", hit["provider_id"])
        meta = (await self.search.extract(["https://docs.sglang.io/"]))[0]["metadata"]
        self.assertEqual(hashlib.sha256(self.page_text[:6000].encode()).hexdigest(), meta["content_sha256"])
        self.assertEqual("2026-11-02T10:00:00+00:00", meta["retrieved_at"])
        self.assertEqual(("Page title", "2026-10-01", "https://docs.sglang.io/") if self.provider == "exa" else ("", "", ""),
                         (meta["title"], meta["published_date"], meta["provider_id"]))
        # WebUI document metadata must stay flat and scalar.
        self.assertTrue(all(isinstance(v, (str, int, bool)) for v in meta.values()))

    async def test_private_material_queries_are_refused_before_spending(self):
        for query in ('{"file_id": "abc", "snippet": "internal"}', "untrusted-source-text internal design",
                      "see https://ai.example.com/api/v1/files/abc123/content", "-----BEGIN OPENSSH PRIVATE KEY----- b3Bl",
                      "sglang.ai-home-lab.svc.cluster.local timeout", "nas.lan smb error", "build.internal failure"):
            response = await self.mcp("search_web", {"query": query})
            self.assertEqual((400, "private_material_in_query"), (response.status_code, response.json()["error"]), query)
        self.assertEqual([], self.provider_calls())
        self.assertFalse(self.search.database.exists())
        for query in ("generate an ssh private key", "mDNS .local names", "SGLang docs.sglang.io radix cache"):
            self.assertEqual(200, (await self.mcp("search_web", {"query": query})).status_code, query)

    async def test_no_unauthenticated_browser_origin_admin_or_unknown_user_spend(self):
        for headers in ({}, {**self.headers, "Origin": "https://evil.example"},
                        {**self.headers, "X-OpenWebUI-User-Id": "bob"}):
            self.assertIn((await self.client.post("/webui/search", headers=headers,
                json={"query": "test", "count": 1})).status_code, {401, 403})
        self.assertEqual(401, (await self.client.post("/webui/fetch", json={"urls": ["https://example.com/"]})).status_code)
        self.role = "admin"
        self.assertEqual(403, (await self.mcp("search_web", {"query": "test"})).status_code)
        self.assertEqual([], self.provider_calls())
        self.assertFalse(self.search.database.exists())

    async def test_query_and_url_validation_happens_before_spending(self):
        for args in ({"query": "test", "search_depth": "advanced"}, {"query": "test", "provider": "other"},
                     {"query": ""}, {"query": "x" * 513},
                     {"query": "private\ncode"}, {"query": "test", "count": True}, {"query": "test", "count": 4}):
            self.assertEqual(400, (await self.mcp("search_web", args)).status_code)
        for url in ("http://localhost/", "http://10.0.0.1/", "http://169.254.169.254/", "http://127.1/",
                    "http://0x7f.0.0.1/", "http://sglang.ai-home-lab.svc.cluster.local/", "file:///etc/passwd",
                    "https://user:password@example.com/", "https://example.com:30000/", "https://example.com/\nsecret"):
            self.assertEqual(400, (await self.mcp("read_web_page", {"url": url})).status_code, url)
        self.assertEqual([], self.provider_calls())
        self.assertFalse(self.search.database.exists())

    async def test_failures_and_restarts_preserve_conservative_reservations(self):
        self.search.policy["monthly_requests"] = 2
        self.status_code = 503
        response = await self.mcp("search_web", {"query": "docs"})
        self.assertEqual(503, response.status_code)
        self.assertNotIn("provider response", response.text)
        self.status_code = 200
        await self.search.search({"query": "docs"})
        restarted = self.new_search()
        restarted.policy["monthly_requests"] = 2
        with self.assertRaises(Refusal) as caught:
            await restarted.search({"query": "docs"})
        self.assertEqual("web_search_month_limit", caught.exception.code)
        self.assertEqual(2, len(self.provider_calls()))
        self.now = datetime(2026, 12, 1, tzinfo=timezone.utc)
        await restarted.search({"query": "docs"})
        self.assertEqual(1, restarted.status()["monthly_reserved"])

    async def test_minute_daily_limits_and_parallel_credit_reservations(self):
        self.search.policy["requests_per_minute"] = 1
        self.search.policy["daily_requests"] = 2
        await self.search.search({"query": "docs"})
        with self.assertRaises(Refusal) as caught:
            await self.search.search({"query": "docs"})
        self.assertEqual("web_search_minute_limit", caught.exception.code)
        self.now = self.now.replace(minute=1)
        await self.search.search({"query": "docs"})
        self.now = self.now.replace(minute=2)
        with self.assertRaises(Refusal) as caught:
            await self.search.search({"query": "docs"})
        self.assertEqual("web_search_day_limit", caught.exception.code)
        self.now = self.now.replace(day=3)
        self.search.policy.update(monthly_requests=5, daily_requests=100, requests_per_minute=100)
        def reserve(_):
            try:
                self.search.reserve()
                return True
            except Refusal:
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(3, sum(pool.map(reserve, range(20))))
        self.assertEqual(5, self.search.status()["monthly_reserved"])

    async def test_missing_key_corrupt_ledger_redirect_and_oversize_fail_closed(self):
        self.key.unlink()
        self.assertEqual(503, (await self.mcp("search_web", {"query": "docs"})).status_code)
        self.assertFalse(self.search.database.exists())
        self.assertEqual("web_search_credential_missing", self.search.status()["status"])
        self.key.write_text("provider-test-credential-only")
        self.status_code = 302
        self.assertEqual(503, (await self.mcp("search_web", {"query": "docs"})).status_code)
        self.assertEqual(1, len(self.provider_calls()))
        self.status_code, self.large = 200, True
        self.assertEqual(502, (await self.mcp("search_web", {"query": "docs"})).status_code)
        with sqlite3.connect(self.search.database) as db:
            db.execute("UPDATE usage SET requests = -1")
        self.assertEqual(503, (await self.mcp("search_web", {"query": "docs"})).status_code)
        self.assertEqual("web_search_accounting_unavailable", self.search.status()["status"])
        with sqlite3.connect(self.search.database) as db:
            db.execute("DROP TABLE usage")
        self.search.database.write_bytes(b"corrupt")
        self.assertEqual(503, (await self.mcp("search_web", {"query": "docs"})).status_code)
        self.search.database.write_bytes(b"")
        self.assertEqual(503, (await self.mcp("search_web", {"query": "docs"})).status_code)
        self.assertEqual(2, len(self.provider_calls()))

    async def test_provider_throttling_concurrency_and_disabled_legacy(self):
        self.status_code = 429
        self.assertEqual(429, (await self.mcp("search_web", {"query": "docs"})).status_code)
        self.status_code, self.slow = 200, True
        responses = await asyncio.gather(*(self.mcp("search_web", {"query": "docs"}) for _ in range(3)))
        self.assertEqual([200, 200, 429], sorted(r.status_code for r in responses))
        self.tools.web_search = None
        self.assertEqual(503, (await self.mcp("search_web", {"query": "docs"})).status_code)
        catalog = await self.client.post("/mcp", headers={"Authorization": "Bearer caller-fixture-only"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(["retrieve_project_passage"], [tool["name"] for tool in catalog.json()["result"]["tools"]])

    async def test_cancelled_provider_call_retains_reservation_and_releases_concurrency(self):
        self.slow = True
        task = asyncio.create_task(self.search.search({"query": "public docs"}))
        async with asyncio.timeout(1):
            while not self.provider_calls():
                if task.done():
                    await task
                await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(1, self.search.status()["monthly_reserved"])
        self.assertEqual(0, self.search.active)
        self.assertEqual(1, len(self.provider_calls()))


class ExaSearchTests(SearchTests):
    provider = "exa"


if __name__ == "__main__":
    unittest.main()
