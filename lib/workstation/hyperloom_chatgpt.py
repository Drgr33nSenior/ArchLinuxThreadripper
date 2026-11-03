"""Explicit ChatGPT authentication through the installed Codex SDK.

Codex alone reads and refreshes its credential store. This module never reads,
copies or logs auth.json, and never sends a subscription token to an API client.
The owner's native execution and qualification gates remain independent.
"""

import asyncio
from contextlib import contextmanager
import os
import json
from pathlib import Path
import stat
import time

from hyperloom_sglang import require


AUTH_MODE_ENV = "HYPERLOOM_AUTH_MODE"
AUTH_HOME_ENV = "HYPERLOOM_CHATGPT_AUTH_HOME"
CONFIG = ('model_provider = "openai"\nforced_login_method = "chatgpt"\n'
          'cli_auth_credentials_store = "file"\nfeatures.memories = false\n')
OVERRIDES = ('model_provider="openai"', 'forced_login_method="chatgpt"',
             'cli_auth_credentials_store="file"', 'features.memories=false')
CONFLICTS = ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_CUSTOM_HEADERS",
             "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
             "ANTHROPIC_CUSTOM_HEADERS", "CLAUDE_CODE_OAUTH_TOKEN", "CODEX_API_KEY",
             "CODEX_ACCESS_TOKEN", "DEEPSEEK_API_KEY")


def enabled(source=None):
    source = os.environ if source is None else source
    return source.get(AUTH_MODE_ENV, "") == "chatgpt"


def environment(source=None):
    source = os.environ if source is None else source
    require(enabled(source), "explicit ChatGPT authentication mode required")
    require(source.get("HYPERLOOM_BENCHMARK_BACKEND") == "workstation_owner",
            "ChatGPT adapter requires the finite workstation owner")
    require(not any(source.get(key, "").strip() for key in CONFLICTS),
            "ChatGPT mode cannot mix API credentials or gateway overrides")
    return source


def auth_home(source=None, *, writable_roots=()):
    source = environment(source)
    path = Path(source.get(AUTH_HOME_ENV, ""))
    require(path.is_absolute() and path.resolve() == path and path.is_dir()
            and not path.is_symlink(), "dedicated canonical ChatGPT sign-in directory required")
    info = path.stat()
    require(info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
            "ChatGPT sign-in directory must be private to its owner")
    for value in writable_roots:
        root = Path(value).resolve()
        require(path != root and not path.is_relative_to(root) and not root.is_relative_to(path),
                "ChatGPT sign-in directory must be outside agent writable roots")
    config = path / "config.toml"
    require(config.is_file() and not config.is_symlink() and config.stat().st_size == len(CONFIG)
            and config.read_text() == CONFIG, "dedicated ChatGPT configuration changed")
    return path


@contextmanager
def persistent_home(*, source, cwd, writable_roots):
    # Deliberately retain the user-managed login across roles and campaigns.
    # The upstream ephemeral gateway-home cleanup must never delete it.
    yield auth_home(source, writable_roots=(cwd, *writable_roots))


def account_is_chatgpt(response):
    account = getattr(response, "account", None)
    return getattr(getattr(account, "root", account), "type", None) == "chatgpt"


async def account_catalog(*, source=None, sdk=None):
    source = dict(environment(source))
    path = auth_home(source)
    if sdk is None:
        import openai_codex as sdk
    source["CODEX_HOME"] = str(path)
    config = sdk.CodexConfig(env=source, config_overrides=OVERRIDES,
                             client_name="hyperloom", client_title="Hyperloom")
    async with sdk.AsyncCodex(config) as client:
        response = await asyncio.wait_for(client.account(), timeout=20)
        require(account_is_chatgpt(response), "Complete ChatGPT sign-in for Hyperloom before running it")
        models = await asyncio.wait_for(client.models(), timeout=20)
        rows = []
        for model in models.data:
            name = getattr(model, "model", None)
            require(isinstance(name, str) and name.strip() and len(name) <= 256, "Codex model identity missing")
            rows.append(dict(model=name, is_default=bool(getattr(model, "is_default", False))))
        require(rows and len(rows) <= 256, "Codex returned an empty or oversized model catalog")
        # No email, account/workspace ID, tokens or raw account response leaves
        # this function. Model names and authentication method are sufficient.
        return dict(authentication="chatgpt", models=rows)


def resolve_models(args):
    catalog = asyncio.run(account_catalog())
    available = {row["model"] for row in catalog["models"]}
    chosen = getattr(args, "codex_model", "")
    specialist = getattr(args, "specialist_model", None) or chosen
    require(chosen in available and specialist in available,
            "Requested coordinator or specialist model is unavailable to this ChatGPT sign-in")
    require(getattr(args, "critic_protocol", "auto") in {"auto", "openai"},
            "ChatGPT mode cannot route the critic through Anthropic")
    args.claude_model = chosen  # Upstream's provider-neutral role field has this historical name.
    return catalog


def permission_overrides(source, mode, writable_roots=()):
    """Deny tool reads of the login store and deny tool network access."""
    require(mode in {"read-only", "workspace-write"}, "ChatGPT agents require sandbox containment")
    home = auth_home(source, writable_roots=writable_roots)
    profile = "hyperloom_read" if mode == "read-only" else "hyperloom_write"
    parent = ":read-only" if mode == "read-only" else ":workspace"
    options = ["default_permissions=" + json.dumps(profile),
        f"permissions.{profile}.extends=" + json.dumps(parent),
        f"permissions.{profile}.filesystem={{" + json.dumps(str(home)) + '="deny"}',
        f"permissions.{profile}.network.enabled=false", 'approval_policy="never"']
    if mode == "workspace-write":
        roots = ",".join(json.dumps(str(Path(root).resolve())) + "=true" for root in writable_roots)
        options.append(f"permissions.{profile}.workspace_roots={{" + roots + "}")
    return tuple(options)


def specialist_launch(dispatcher, *, workspace, worktree, system_prompt, base_env, probe_sandbox):
    """Use the same sign-in without copying credentials into an agent's workspace."""
    from hyperloom.orchestrator.specialists.subprocess_ import (
        SpecialistAgentUnavailableError, resolve_codex_executable,
        resolve_codex_sandbox_mode, probe_codex_sandbox_capability)

    environment(base_env)
    cfg = dispatcher.config
    roots = dispatcher._writable_dirs(workspace, worktree)
    path = auth_home(base_env, writable_roots=roots)
    executable = resolve_codex_executable(cfg.codex_executable)
    require(executable and cfg.model, "explicit installed Codex runtime and model required")
    require(not cfg.extra_codex_args and not cfg.mcp_config_path,
            "ChatGPT campaign specialist cannot override the runtime or add MCP services")
    mode = resolve_codex_sandbox_mode(env=dict(base_env))
    require(mode == "workspace-write", "ChatGPT specialist requires its bounded writable workspace")
    if probe_sandbox and not probe_codex_sandbox_capability(env=dict(base_env)):
        raise SpecialistAgentUnavailableError("Codex workspace sandbox probe failed")
    require(len(system_prompt.encode()) <= 120000, "bounded specialist instructions required")
    command = [executable, "exec", "--json", "--strict-config", "--skip-git-repo-check",
               "-C", str(worktree or workspace)]
    for extra in roots[1:]:
        command.extend(["--add-dir", extra])
    for option in (*OVERRIDES, *permission_overrides(base_env, mode, roots),
                   "developer_instructions=" + json.dumps(system_prompt)):
        command.extend(["-c", option])
    command.extend(["-m", cfg.model, "-"])
    return command, {"CODEX_HOME": str(path)}


async def review(backend, *, system_prompt, user_prompt, max_tokens, call_id=None):
    """Keep the real critic runtime, parser and verdict gates; change transport."""
    from hyperloom.common.codex_session import CodexSessionConfig, CodexTurnRequest, run_codex_turn

    environment()
    root = backend.session_dir / "runtime" / "chatgpt-critic"
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    started = time.perf_counter()
    try:
        result = await run_codex_turn(
            config=CodexSessionConfig(model=backend._review_model, cwd=root, sandbox_mode="read-only"),
            request=CodexTurnRequest(user_prompt, timeout_sec=300),
            developer_instructions=system_prompt or "Review the supplied evidence. Do not edit any files.",
            component="critic", operation="review")
        require(not result.error and bool(result.text), "ChatGPT critic returned an incomplete result")
    except Exception as error:
        raise backend._llm_call_failed("ChatGPT critic failed: " + type(error).__name__,
            latency_ms=int((time.perf_counter() - started) * 1000), call_id=call_id) from error
    backend._trace_critic_llm_call(result.usage,
        latency_ms=int((time.perf_counter() - started) * 1000), call_id=call_id)
    # The SDK reports a completed turn, not a chat-completions token cap.
    # The existing critic parser still refuses malformed or incomplete JSON.
    return result.text, None
