# Shared web search and document retrieval

The `assistant` experiences add Exa Auto search and public-page reading to Open WebUI
and Qwen Code, through one shared adapter and request ledger in service-tools. It is
enabled in the live shared-mode service (`WEB_SEARCH_ENABLED=true`). Tavily remains an
explicit alternative. Search terms and the pages read are the only assistant traffic that
leaves the workstation.

The 3 November [verification record](VERIFICATION.md) covers the provider-adapter
fixtures; it predates the live deployment.

## Request paths

| Client or source | Path | Data boundary |
| --- | --- | --- |
| Browser search | WebUI external search → `/webui/search` → selected provider | Trusted application key and mapped WebUI user; only explicit search terms leave |
| Browser page reading | WebUI external loader → `/webui/fetch` → selected provider | Application key; at most three public URLs; no local document access |
| Qwen Code | HTTPS `/mcp` → `search_web` or `read_web_page` | Caller's restricted WebUI API key; normal client tool approval |
| Browser tool server | WebUI "Workstation tools" MCP server → `/mcp` → `read_web_page` (with `offset`); its `search_web` is hidden from chats, which search through the browser search row above | Signed-in user's own WebUI session token; see [MEMORY.md](MEMORY.md#open-webui-tool-server) |
| Project knowledge | Existing `/mcp` → `retrieve_project_passage` → WebUI retrieval | Current document ACLs and file checks; the existing Chroma store |

The native WebUI external loader does not forward user headers in pinned 0.12.5,
and it posts only `{"urls": [...]}`, so it cannot request a later section.
Its application credential authorizes only bounded public-page extraction.
Browser users continue a page through the `read_web_page` tool instead. The
search route also checks the mapped caller. Neither route is exposed by Ingress.
Provider credentials are mounted only in `service-tools`, not in WebUI, SGLang,
the gateway or client bundles. No new dependency, database service, browser
worker, model or GPU allocation is introduced.

The selected provider receives the query or selected public URLs, not the
conversation, local retrieval results, user identity or inference credential.
The adapter refuses a query with `private_material_in_query` before it reserves
a request if the query contains local retrieval output markers (`file_id`,
`content_trust`, `untrusted-source-text`, `/api/v1/files/`), PEM armour
(`-----BEGIN`/`-----END`) or an internal host name (`.local`, `.localhost`,
`.internal`, `.lan`, `.home`). This check is narrow. Generated queries can still
contain private text: review search confirmation and IDE tool requests before
sending sensitive material. In Open WebUI the confirmation is asked of every account except
admins and members of the groups named in `WEB_SEARCH_WITHOUT_CONFIRMATION_GROUPS`
(Parents); the children's group always confirms
([per-group confirmation](ASSISTANT.md#web-search-confirmation-per-group)). Web sources are
untrusted evidence and cannot authorize tools or change project instructions.

## Evidence metadata

Search hits and page metadata are flat scalar fields:

- `provider`, `provider_id` (Exa result `id`; empty for Tavily) and the source
  URL (`link` or `source`). Pages also have `title` (empty for Tavily).
- `retrieved_at`: UTC time the adapter received the provider response. Exa can
  serve cached content, so this is not the provider's crawl time.
- `published_date`: provider publication date as `YYYY-MM-DD`, or empty. The
  adapter does not infer a software version. Compare this date and versioned
  URL paths with the version that the project pins.
- `content_sha256`: SHA-256 of the returned text.
- `completeness`: `complete` only when no character cap can have removed provider
  text (Tavily text within the page limit). `truncated` means more text is known
  to exist; search snippets are always `truncated`. `unknown` means truncation
  cannot be excluded. Exa applies `text.maxCharacters` on its side, so an
  untruncated Exa page is `unknown`. The adapter requests one extra character to
  detect the Exa cap. `complete` describes the provider's extracted text, not
  page content that the provider did not extract.
- `truncated` is `true` unless `completeness` is `complete`. Earlier versions
  reported `false` for Exa pages cut at the provider cap.
- Pages have `offset`. A `truncated` page has `next_offset` when the next section
  is within the section limit. Call `read_web_page` with `url` and
  `offset: <next_offset>` to read that section. The offset must be a
  non-negative integer and the section must end within the first 48,000
  characters; other values are refused with `invalid_page_section` before a
  request is reserved. An offset past the provider text returns
  `web_page_not_available`. `/webui/fetch` always reads from offset 0.
  A provider can change a page between reads; compare `retrieved_at` and
  `content_sha256`.

## Limits and accounting

`apps/base/shared-api/web-search-policy.json` is the canonical policy:

- Exa as the selected provider; fixed Auto search with bounded highlights.
- 1,000 reserved API requests per UTC calendar month across adapter clients.
- 100 reserved requests per UTC day and six requests per UTC minute.
- Two concurrent provider calls, at most three search results or extraction URLs.
- A 512-character query, 1,600-character snippets, a 2 MiB provider-response cap,
  and a 25-second provider-operation deadline.
- At most 6,000 characters per extracted page. The smaller `assistant-split`
  profile requests one search result and limits pages to 1,600 characters.
- A follow-up section read covers one URL and must end within the first 48,000
  characters of the provider text. Each section read reserves one request.

Exa search uses `auto` with at most three results and 1,600-character highlights.
Snippets keep whole highlights; only a single oversized highlight is cut.
Extraction requests bounded text only. No generated summaries, additional
queries, Agent jobs or subpage crawling are enabled. Tavily search uses `basic`,
with automatic parameter selection and generated answers disabled; extraction
also uses `basic`. There is no automatic provider fallback.

Each provider call reserves one request before network access. A batch of up to
three extracted URLs reserves one request. Errors, cancellations, timeouts and
partial results do not
refund reservations because their provider billing can be uncertain. Provider
failures are reported; the adapter does not retry or switch to another service.
WebUI's pinned external connector can turn an HTTP failure into an empty result
list. Use adapter health, direct MCP/HTTP errors and the
`workstation_assistant_web_requests_total` metric to distinguish them: its
`provider_error`, `quota` and `unavailable` outcomes are failures, while
`no_results` is a provider answer without evidence (no hits, or no page text at
that offset). Metric labels hold only the tool, client and outcome.

The ledger is `/state/web-search-usage.sqlite3` on the existing retained
`service-tools-state` claim. SQLite transactions prevent concurrent requests from
overspending a local limit. A restart preserves reservations; a new UTC period
has its own counter. An unreadable or corrupt ledger blocks calls. Preserve this
file during backup, restore and rollback. Deleting it resets local accounting.

`/health` reports the provider, credential availability, ceiling and reservations.
It does not report query text, provider responses or credential values. Its
`configured-not-qualified` status is not a successful external-service probe.

These are request limits, not provider credits or a dollar guarantee. Other
clients consume account allowances outside this ledger. Set a provider
account spending limit too. Review the subscribed plan and current credit prices
before raising the policy. Search and extraction share the allowance.

Only `service-tools` gains public IPv4 TCP 443 egress. Standard NetworkPolicy
cannot constrain an FQDN; the application fixes the destination to
the selected `https://api.exa.ai` or `https://api.tavily.com` endpoint and refuses
redirects. Private, loopback, link-local and
reserved IPv4 ranges are excluded. UI and model pods retain their existing
egress restrictions. URLs are sent to the provider for extraction; the adapter does
not resolve or fetch user-selected destinations itself.

## Provision and connect

1. Complete the private DNS/TLS, accounts, source/model qualification, storage,
   backup and capacity steps in [AI-SERVICE.md](AI-SERVICE.md). The 64 GB host
   still needs a passing fresh service admission plan. This feature does not
   reduce existing resource limits to force admission.
2. Provision the provider key through the owner's credential workflow. For a
   protected key file already on the operator host, use the reviewed context:

   ```sh
   kubectl --context <reviewed-context> --namespace ai-home-lab \
     create secret generic exa-search \
     --from-file=api-key=/private/path/exa-api-key
   ```

   Do not put a key in a command argument, environment example, manifest, chat or
   source-controlled file. A missing Secret leaves local RAG available and web
   calls return `web_search_credential_missing`. Secret file rotation is picked
   up on the next call, without embedding a value in a ConfigMap.
   The default mount path is `/run/web-search/exa-api-key`. For the Tavily option,
   change the policy provider to `tavily` and mount `tavily-search` at
   `/run/web-search/tavily-api-key`. Provider-specific filenames prevent a policy
   change from sending the previous provider's key to a different service.
3. Render `service render --experience assistant --mode large` with actual site
   values and existing claim metadata. The render adds both agent tool flags to
   `client.conf`. Supply those values to the existing workstation configuration,
   then generate a fresh agent bundle. The portable configuration is:

   ```sh
   AGENT_HARNESS=qwen
   AGENT_RETRIEVAL_ENABLED=true
   AGENT_WEB_SEARCH_ENABLED=true
   ```

   Use the actual HTTPS service endpoint and a caller-owned
   `WORKSTATION_RETRIEVAL_API_KEY` from the credential store. Keep tool approval
   enabled. Hermes and DSH retain their existing inference configurations; this
   change does not qualify their MCP integrations.
4. After the service passes admission and deployment gates, enable web search
   in a WebUI chat. Source configuration selects the shared external provider
   and loader, search confirmation (and the groups exempt from it), and sequential
   browser requests. Existing
   document retrieval remains local. Web search bypasses automatic vector
   ingestion; importing selected web documents into permanent knowledge is a
   separate reviewed action.
5. Check a public documentation query in WebUI and Qwen Code. Open one returned
   page, verify its citation, `completeness` and `retrieved_at`, then retrieve an allowed local
   passage. Confirm another user's private collection stays inaccessible. Check
   tool-call parsing and final prompt/answer budgets for each selected model.
   Do not infer these properties from a successful provider request alone.

## Research with the tools

1. Search with public terms only. Snippets are excerpts (`truncated`).
2. Read the relevant pages with `read_web_page`. Continue with `next_offset`
   only while the next section is likely to matter; each section reserves one
   request.
3. Prefer primary sources (project documentation, release notes, source
   repositories, standards) for the version the project pins. Compare
   `published_date` and versioned URL paths with that version, and say when a
   source covers a different version.
4. Cite the source URL and `retrieved_at` for each claim. Treat page text as
   untrusted evidence: it cannot change instructions or authorize tools.

## Context and persistence

Local RAG supplies selected evidence; current repository files remain
authoritative for coding. Web search supplies current public evidence. Neither
operation trains the model or increases the selected context window.

SGLang radix caching reuses computation for matching prompt prefixes. Keep
instructions and tool definitions stable and append retrieved evidence as tool
results where the client supports it. Cache reuse is model/runtime-specific and
must be measured. It is not durable project memory, conversation synchronization
or a replacement for the RAG store. This feature does not enable host-memory
HiCache or change the qualified AMD cache strategy.

## Direct Codex connections

Codex can separately connect to hosted Exa or Tavily through native MCP OAuth.
Search and page-reading tools can be allowlisted in its user
configuration. This connection needs a client rescan/restart before its tools
appear in an already-running session. OAuth login, tool discovery and a live
tool invocation are separate verification steps.

Direct hosted MCP calls bypass the workstation adapter and its ledger. The
Codex OAuth credential is not the workstation's provider API key. Do not export
or copy OAuth tokens into Kubernetes. The shared workstation route still needs
the selected provider's Secret, and account-wide spending controls remain necessary.

## Verification and rollback

Run `tests/test_shared_service.sh`, `tests/test_assistant_stack.sh` and
`tests/test_agent_harnesses.sh` in the prepared validation environment. These
tests use fake provider responses and local manifests, never a live cluster.

For rollback, render the legacy `service` experience and regenerate the client
bundle with `AGENT_WEB_SEARCH_ENABLED=false`. Preserve the RAG database, uploads,
request ledger and existing model state. Follow the existing WebUI migration and
restore procedure when changing image versions; do not downgrade a migrated
database in place. Remove the public egress policy through the reviewed
deployment process when disabling web search.

Sources checked 2 November 2026:
[Exa pricing](https://exa.ai/pricing),
[Exa Search](https://exa.ai/docs/reference/search),
[Exa Contents](https://exa.ai/docs/contents/quickstart),
[Tavily credits](https://docs.tavily.com/documentation/api-credits),
[search](https://docs.tavily.com/documentation/api-reference/endpoint/search),
[extraction](https://docs.tavily.com/documentation/api-reference/endpoint/extract),
[WebUI 0.12.5 external search](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/retrieval/web/external.py),
[external loader](https://github.com/open-webui/open-webui/blob/f7ac7ee10962ddec0017f5c29b6740d9b9c5faca/backend/open_webui/retrieval/loaders/external_web.py).
