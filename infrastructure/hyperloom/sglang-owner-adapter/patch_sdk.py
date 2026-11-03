"""Explicit source adaptations for the pinned upstream SDK; no execution."""

from pathlib import Path


FILES = (
    "hyperloom/common/perf_metric.py",
    "hyperloom/inference_optimizer/cli/bootstrap.py",
    "hyperloom/orchestrator/state/shared_state.py",
    "hyperloom/orchestrator/actions/executors/benchmark_result.py",
    "hyperloom/orchestrator/actions/executors/_grid_base.py",
    "hyperloom/orchestrator/actions/executors/_workload_envs.py",
    "hyperloom/orchestrator/actions/executors/_benchmark_runtime.py",
    "hyperloom/orchestrator/measurement/integrate_performance.py",
    "hyperloom/orchestrator/kernel/request_handlers.py",
    "hyperloom/orchestrator/actions/executors/explore.py",
    "hyperloom/orchestrator/actions/executors/baseline.py",
    "hyperloom/orchestrator/loop/writeback.py",
    "hyperloom/orchestrator/actions/executors/integrate_patch.py",
    "hyperloom/orchestrator/actions/executors/report.py",
    "hyperloom/orchestrator/actions/executors/profile.py",
    "hyperloom/inference_optimizer/cli/__init__.py",
    "hyperloom/inference_optimizer/cli/preflight.py",
    "hyperloom/orchestrator/actions/executors/_grid_runner.py",
    "hyperloom/orchestrator/actions/executors/_subprocess_kill.py",
    "hyperloom/common/codex_session.py",
    "hyperloom/common/llm_config.py",
    "hyperloom/inference_optimizer/cli/backends.py",
    "hyperloom/orchestrator/roles/critic_agent.py",
    "hyperloom/orchestrator/specialists/subprocess_.py",
    "hyperloom/orchestrator/specialists/runner.py",
    "hyperloom/inference_optimizer/gpu_types.py",
    "hyperloom/inference_optimizer/cli/benchmark_config.py",
)


def adapt(destination):
    destination = Path(destination)
    sources = {name: (destination / name).read_text() for name in FILES}

    def edit(name, old, new, count=1):
        if sources[name].count(old) != count:
            raise ValueError("upstream SDK patch drift: " + name + ": " + old[:90])
        sources[name] = sources[name].replace(old, new)

    # The optimizer has no devices. Its explicit R9700 target is qualified by
    # the independent owner, not a driver probe or framework import here.
    edit("hyperloom/inference_optimizer/gpu_types.py",
         '    """Return a canonical supported board name, or ``None`` if undetectable."""\n',
         '    """Return a canonical supported board name, or ``None`` if undetectable."""\n'
         '    from hyperloom_sdk_adapter import active\n'
         '    if active():\n'
         '        return None\n')

    # Subscription authentication stays inside Codex. API-mode behavior is
    # unchanged, and the real critic's review parser and verdict policy remain.
    session = "hyperloom/common/codex_session.py"
    edit(session, '    """Resolve provider config from one already-effective environment."""\n',
         '    """Resolve provider config from one already-effective environment."""\n'
         '    import hyperloom_chatgpt as chatgpt\n'
         '    if chatgpt.enabled(source):\n'
         '        chatgpt.environment(source)\n'
         '        return CodexProviderConfig(overrides=chatgpt.OVERRIDES)\n')
    edit(session, '    """Create and deterministically clean one private mode-0700 state directory."""\n',
         '    """Create and deterministically clean one private mode-0700 state directory."""\n'
         '    import hyperloom_chatgpt as chatgpt\n'
         '    if chatgpt.enabled(source):\n'
         '        with chatgpt.persistent_home(source=source, cwd=cwd, writable_roots=writable_roots) as home:\n'
         '            yield home\n'
         '        return\n')
    edit(session, '            client = await stack.enter_async_context(sdk.AsyncCodex(config))\n',
         '            client = await stack.enter_async_context(sdk.AsyncCodex(config))\n'
         '            import hyperloom_chatgpt as chatgpt\n'
         '            if chatgpt.enabled(effective_env):\n'
         '                account = await asyncio.wait_for(client.account(), timeout=20)\n'
         '                if not chatgpt.account_is_chatgpt(account):\n'
         '                    raise CodexSessionUnavailableError("Hyperloom ChatGPT sign-in is required")\n')
    edit(session, '        # The client is entered inside the CODEX_HOME context so unwinding closes the client first and only then\n',
         '        import hyperloom_chatgpt as chatgpt\n'
         '        if chatgpt.enabled(effective_env):\n'
         '            sandbox = None  # Explicit permission profile; legacy presets would override its deny rules.\n'
         '            config_overrides = (*provider_config.overrides, *chatgpt.permission_overrides(\n'
         '                effective_env, resolved_sandbox_mode, self.config.writable_roots))\n'
         '        # The client is entered inside the CODEX_HOME context so unwinding closes the client first and only then\n')
    edit(session, '        """Return the open conversation, opening one on first use."""\n',
         '        """Return the open conversation, opening one on first use."""\n'
         '        import hyperloom_chatgpt as chatgpt\n')
    edit(session, '                model_provider=CODEX_PROVIDER_NAME,\n',
         '                model_provider=("openai" if chatgpt.enabled(\n'
         '                    _effective_env(self.config.env)) else CODEX_PROVIDER_NAME),\n')
    edit("hyperloom/common/llm_config.py",
         '    source = env if env is not None else os.environ\n    ranks = {\n',
         '    source = env if env is not None else os.environ\n'
         '    import hyperloom_chatgpt as chatgpt\n'
         '    if chatgpt.enabled(source):\n'
         '        chatgpt.environment(source)\n'
         '        return AGENT_BACKEND_CODEX\n'
         '    ranks = {\n')
    edit("hyperloom/inference_optimizer/cli/backends.py",
         '    if requested not in CRITIC_PROTOCOL_CHOICES:\n',
         '    import hyperloom_chatgpt as chatgpt\n'
         '    if chatgpt.enabled():\n'
         '        chatgpt.environment()\n'
         '        if requested not in {"auto", "openai"}:\n'
         '            raise ValueError("ChatGPT mode requires the Codex critic transport")\n'
         '        return "openai"\n'
         '    if requested not in CRITIC_PROTOCOL_CHOICES:\n')
    critic = "hyperloom/orchestrator/roles/critic_agent.py"
    edit(critic, '        if self.protocol == "anthropic":\n            self._require_anthropic_transport()\n',
         '        import hyperloom_chatgpt as chatgpt\n'
         '        if chatgpt.enabled():\n'
         '            chatgpt.environment()\n'
         '            if self.protocol != "openai":\n'
         '                raise BackendError("ChatGPT mode requires the Codex critic transport")\n'
         '        elif self.protocol == "anthropic":\n            self._require_anthropic_transport()\n')
    edit(critic, '        """Issue one Codex chat-completions call and return ``(text, finish_reason)``."""\n',
         '        """Issue one review call using the explicitly selected authentication."""\n'
         '        import hyperloom_chatgpt as chatgpt\n'
         '        if chatgpt.enabled():\n'
         '            return await chatgpt.review(self, system_prompt=system_prompt, user_prompt=user_prompt,\n'
         '                                        max_tokens=max_tokens, call_id=call_id)\n')
    specialist = "hyperloom/orchestrator/specialists/subprocess_.py"
    edit(specialist, '_SPECIALIST_ENV_ALLOWLIST: frozenset[str] = frozenset(\n    {\n',
         '_SPECIALIST_ENV_ALLOWLIST: frozenset[str] = frozenset(\n    {\n'
         '        "HYPERLOOM_AUTH_MODE", "HYPERLOOM_CHATGPT_AUTH_HOME", "HYPERLOOM_BENCHMARK_BACKEND",\n')
    edit(specialist, '            env["CODEX_HOME"] = str(codex_home)\n',
         '            import hyperloom_chatgpt as chatgpt\n'
         '            if not chatgpt.enabled(env):\n'
         '                env["CODEX_HOME"] = str(codex_home)\n')
    edit(specialist, '        cfg = self.config\n        executable = resolve_codex_executable(cfg.codex_executable)\n',
         '        import hyperloom_chatgpt as chatgpt\n'
         '        if chatgpt.enabled(base_env):\n'
         '            try:\n'
         '                return chatgpt.specialist_launch(self, workspace=workspace, worktree=worktree,\n'
         '                    system_prompt=system_prompt, base_env=base_env, probe_sandbox=probe_sandbox)\n'
         '            except ValueError as error:\n'
         '                raise SpecialistAgentUnavailableError(str(error)) from error\n'
         '        cfg = self.config\n        executable = resolve_codex_executable(cfg.codex_executable)\n')
    edit(specialist, '        if worktree is not None and (worktree / ".git").exists():\n',
         '        from hyperloom_sdk_adapter import active, collect_specialist_patch\n'
         '        if active():\n'
         '            return collect_specialist_patch(worktree, workspace, worktree_base, targets)\n'
         '        if worktree is not None and (worktree / ".git").exists():\n')
    edit("hyperloom/orchestrator/specialists/runner.py",
         '        if workspace is None:\n            return None, None, ""\n',
         '        from hyperloom_sdk_adapter import active, prepare_specialist_tree\n'
         '        if active():\n'
         '            return prepare_specialist_tree(ctx, workspace=workspace, profile=profile,\n'
         '                                           integration_pending=self._integrate_in_flight)\n'
         '        if workspace is None:\n            return None, None, ""\n')

    grid_runtime = "hyperloom/orchestrator/actions/executors/_grid_runner.py"
    edit("hyperloom/inference_optimizer/cli/__init__.py",
         '    """Validate the session\'s compute-partition shape and publish it."""\n',
         '    """Validate the session\'s compute-partition shape and publish it."""\n'
         '    from hyperloom_sdk_adapter import active, owner_partition\n'
         '    if active():\n'
         '        return owner_partition(request, framework=framework, gpu_type=gpu_type, nodes=nodes, model=model)\n')
    edit(grid_runtime,
         '    return env_flag("INFERENCE_OPTIMIZER_RUN_GRID_WARMUP", default=not os.environ.get("PYTEST_CURRENT_TEST"))',
         '    from hyperloom_sdk_adapter import active\n'
         '    if active():\n'
         '        return False  # Each finite owner already executes its frozen warmup menu.\n'
         '    return env_flag("INFERENCE_OPTIMIZER_RUN_GRID_WARMUP", default=not os.environ.get("PYTEST_CURRENT_TEST"))')
    edit(grid_runtime,
         '    """Give Magpie and bypass the same cap as the enclosing benchmark process."""\n',
         '    """Give Magpie and bypass the same cap as the enclosing benchmark process."""\n'
         '    from hyperloom_sdk_adapter import active, sync_owner_timeout\n'
         '    if active():\n'
         '        return sync_owner_timeout(config_path, timeout_sec)\n')
    edit("hyperloom/orchestrator/actions/executors/_subprocess_kill.py",
         '    from hyperloom.common.agentx_workload import is_mlperf_backend, mlperf_benchmark_timeout_sec\n',
         '    from hyperloom_sdk_adapter import active, owner_timeouts\n'
         '    if active():\n'
         '        return owner_timeouts(env)\n'
         '    from hyperloom.common.agentx_workload import is_mlperf_backend, mlperf_benchmark_timeout_sec\n')

    metric = FILES[0]
    edit(metric, 'GRADED_OUTPUT = "output_throughput"',
         'GRADED_OUTPUT = "output_throughput"\nGRADED_BALANCED = "balanced_c1_c2_e2e_latency"')
    edit(metric, '    if not isinstance(source, Mapping):\n        return None\n    intvty =',
         '    if not isinstance(source, Mapping):\n        return None\n'
         '    if "workstation_latency" in source:\n'
         '        from hyperloom_sdk_adapter import snapshot\n\n'
         '        return snapshot(dict(source))\n    intvty =')
    edit(metric, '    if objective in INTVTY_OBJECTIVES:\n        value = axis_of',
         '    if objective == GRADED_BALANCED:\n'
         '        snap = getattr(state, _BASELINE_PERF_ATTR, None) or {}\n'
         '        return 1.0 if isinstance(snap.get("workstation_latency"), dict) else None\n'
         '    if objective in INTVTY_OBJECTIVES:\n        value = axis_of')
    edit(metric, '    axes: dict[str, float] = {}\n    intvty =',
         '    axes: dict[str, Any] = {}\n'
         '    if "workstation_latency" in source:\n'
         '        from hyperloom_sdk_adapter import snapshot\n\n'
         '        owner = snapshot(dict(source))\n'
         '        if owner is not None:\n'
         '            axes["workstation_latency"] = owner["workstation_latency"]\n'
         '            axes["workstation_latency_control"] = owner["workstation_latency_control"]\n'
         '    intvty =')
    edit(metric, 'CONSTRAINT_FIELDS = (\n', 'CONSTRAINT_FIELDS = (\n    "workstation_latency",\n    "workstation_latency_control",\n')
    edit(metric, '    @property\n    def graded_on_intvty(self) -> bool:',
         '    @property\n    def graded_on_balanced(self) -> bool:\n'
         '        return self.objective == GRADED_BALANCED\n\n'
         '    @property\n    def graded_on_latency(self) -> bool:\n'
         '        return self.graded_on_balanced or self.graded_on_intvty\n\n'
         '    @property\n    def graded_on_intvty(self) -> bool:')
    # These snapshots now carry a structured owner latency series as well as
    # physical scalar metrics. The public output-throughput helpers stay intact.
    edit(metric, 'def perf_snapshot_from_mapping(source: Mapping[str, Any] | None) -> dict[str, float] | None:',
         'def perf_snapshot_from_mapping(source: Mapping[str, Any] | None) -> dict[str, Any] | None:')
    edit(metric, 'def resolve_grading_anchor_perf(state: Any) -> tuple[dict[str, float] | None, str]:',
         'def resolve_grading_anchor_perf(state: Any) -> tuple[dict[str, Any] | None, str]:')
    sources[metric] = sources[metric].replace('def graded_axes_of(source: Mapping[str, Any] | None) -> dict[str, float]:',
                                            'def graded_axes_of(source: Mapping[str, Any] | None) -> dict[str, Any]:')

    state = FILES[2]
    edit(state, '    on_intvty, noise_pct = resolved_grading(state)\n',
         '    if (getattr(state, "grading", None) or {}).get("objective") == "balanced_c1_c2_e2e_latency":\n'
         '        from hyperloom_sdk_adapter import grade\n\n'
         '        return grade(state, measurement, against_baseline=against_baseline,\n'
         '                     anchor_perf=anchor_perf, keep_threshold_pct=keep_threshold_pct)\n'
         '    on_intvty, noise_pct = resolved_grading(state)\n')

    bootstrap = FILES[1]
    anchor = '    on_intvty = intvty_serving_grading_enabled(\n'
    edit(bootstrap, anchor,
         '    from hyperloom_sdk_adapter import active, seed_grading as seed_owner_grading\n\n'
         '    if active():\n'
         '        return seed_owner_grading()\n' + anchor)

    result = FILES[3]
    edit(result, '        "reported_success": report.get("success") if report else None,\n',
         '        "benchmark_backend": report.get("benchmark_backend"),\n'
         '        "benchmark_valid": report.get("benchmark_valid"),\n'
         '        "diagnostic_only": report.get("diagnostic_only"),\n'
         '        "workload_identity": report.get("workload_identity"),\n'
         '        "workstation_latency": report.get("workstation_latency"),\n'
         '        "workstation_latency_control": report.get("workstation_latency_control"),\n'
         '        "reported_success": report.get("success") if report else None,\n')
    edit(result, '    if result.get("benchmark_backend") == "magpie_agentx":\n',
         '    if result.get("benchmark_backend") == "workstation_owner":\n'
         '        return (result.get("benchmark_valid") is True and result.get("diagnostic_only") is False\n'
         '                and (result.get("quality_gate") or {}).get("passed") is True\n'
         '                and isinstance(result.get("workstation_latency"), dict)\n'
         '                and isinstance(result.get("workstation_latency_control"), dict))\n'
         '    if result.get("benchmark_backend") == "magpie_agentx":\n')

    grid = FILES[4]
    edit(grid, '    workload_identity: str | None = None\n',
         '    workload_identity: str | None = None\n    workstation_latency: dict[str, Any] | None = None\n'
         '    workstation_latency_control: dict[str, Any] | None = None\n')
    edit(grid, '            "workload_identity": self.workload_identity,\n',
         '            "workload_identity": self.workload_identity,\n'
         '            "workstation_latency": self.workstation_latency,\n'
         '            "workstation_latency_control": self.workstation_latency_control,\n')

    workload = FILES[5]
    edit(workload, '    server_args = (extra_server_args or "").strip()\n',
         '    from hyperloom_sdk_adapter import active, materialize_config\n\n'
         '    if active():\n'
         '        return materialize_config(config_path, output_dir, extra_server_args=extra_server_args,\n'
         '            extra_envs=extra_envs, remove_args=remove_args, unset_envs=unset_envs,\n'
         '            model_path=model_path, gpu_type=gpu_type, benchmark_script=benchmark_script, out_name=out_name)\n'
         '    server_args = (extra_server_args or "").strip()\n')
    runtime = FILES[6]
    edit(runtime, '    agentx_mode = True if is_agentx_client_script(str(bench.get("benchmark_script") or "")) else None\n',
         '    from hyperloom_sdk_adapter import active, runtime_overrides\n\n'
         '    if active():\n'
         '        return runtime_overrides(bench, model_path=model_path, gpu_type=gpu_type,\n'
         '                                 benchmark_script=benchmark_script, conc=conc)\n'
         '    agentx_mode = True if is_agentx_client_script(str(bench.get("benchmark_script") or "")) else None\n')

    # Every existing non-throughput decision branch must recognize balanced
    # latency. Keep the interactivity-only property for AgentX-specific math.
    for name in FILES[7:13]:
        if '.graded_on_intvty' not in sources[name]:
            raise ValueError("expected decision consumer missing: " + name)
        sources[name] = sources[name].replace('.graded_on_intvty', '.graded_on_latency')

    integrate = FILES[12]
    edit(integrate, '    return "interactivity p50" if graded.graded_on_latency else "throughput"\n',
         '    if graded.graded_on_balanced:\n'
         '        return "balanced C1/C2 latency"\n'
         '    return "interactivity p50" if graded.graded_on_intvty else "throughput"\n')
    edit(integrate, '    if graded.comparable and graded.graded_on_latency:\n        refusals.extend(_intvty_guard_refusals(shared_state, bench_result))\n',
         '    if graded.graded_on_balanced:\n'
         '        if not graded.comparable or graded.verdict != VERDICT_KEEP:\n'
         '            refusals.append("balanced C1/C2 gates cannot be relaxed for a correctness-fix label")\n'
         '    elif graded.comparable and graded.graded_on_intvty:\n'
         '        refusals.extend(_intvty_guard_refusals(shared_state, bench_result))\n')

    writeback = FILES[11]
    # An owner latency snapshot has no invented total-throughput or AgentX
    # interactivity value. Preserve absence through the real baseline promote.
    for key in ("total_throughput", "e2e_norm_intvty_p90"):
        edit(writeback, '                current_best["' + key + '"] = snap["' + key + '"]\n',
             '                current_best["' + key + '"] = snap.get("' + key + '")\n')
    edit(writeback, '            correctness_fix = bv.get("keep_reason") == KEEP_REASON_CORRECTNESS_FIX\n',
         '            correctness_fix = (bv.get("keep_reason") == KEEP_REASON_CORRECTNESS_FIX\n'
         '                               and not graded.graded_on_balanced)\n')
    report = FILES[13]
    edit(report, 'INTVTY_V1 if graded_on_intvty else GRADED_OUTPUT',
         "INTVTY_V1 if graded_on_intvty else comparison['objective']")

    baseline = FILES[10]
    edit(baseline, '        """Run the Magpie baseline; an eval-rooted failure fails the round, a MoE-runner failure retries once."""\n',
         '        """Run one owner pass, or the upstream backend retry policy."""\n'
         '        from hyperloom_sdk_adapter import active\n'
         '        if active():\n'
         '            return await self._run_pass(ctx, recorder=recorder, attempt_reason=RUN_INITIAL)\n')
    edit(baseline, '        """Run the Magpie baseline benchmark and parse its result."""\n',
         '        """Run the selected benchmark backend and parse its result."""\n'
         '        from hyperloom_sdk_adapter import active\n'
         '        if active():\n'
         '            if not _should_establish_quality_ref(getattr(ctx.task, "kind", ""), ctx.task.params or {}):\n'
         '                raise ValueError("owner baseline only accepts a genuine baseline task")\n'
         '            from hyperloom_owner_baseline import run\n'
         '            return await run(self, ctx)\n')

    profile = FILES[14]
    edit(profile, '        """Run the profiling action for the given context."""\n',
         '        """Run the profiling action for the given context."""\n'
         '        from hyperloom_sdk_adapter import active\n\n'
         '        if active():\n'
         '            from hyperloom_owner_profile import run\n\n'
         '            return await run(self, ctx)\n')

    cli = FILES[15]
    edit("hyperloom/inference_optimizer/cli/benchmark_config.py",
         '    """Validate the requested workload before resolving the ordinary CLI defaults."""\n',
         '    """Validate the requested workload before resolving the ordinary CLI defaults."""\n'
         '    from hyperloom_sdk_adapter import active, configure_owner_benchmark\n'
         '    if active():\n'
         '        return configure_owner_benchmark(args)\n')
    edit(cli, '    return llm_config.is_openai_only()\n',
         '    import hyperloom_chatgpt as chatgpt\n'
         '    return chatgpt.enabled() or llm_config.is_openai_only()\n')
    edit(cli, '    """Resolve both model ids against the gateway before any session work."""\n',
         '    """Resolve both model ids through the selected authentication before session work."""\n'
         '    import hyperloom_chatgpt as chatgpt\n'
         '    if chatgpt.enabled():\n'
         '        chatgpt.resolve_models(args)\n'
         '        return\n')
    edit(cli, '    """Bring the workspace\'s Experience KB service to serving, then check its Experiences can be collected."""\n',
         '    """Bring the workspace\'s Experience KB service to serving, then check its Experiences can be collected."""\n'
         '    from hyperloom_sdk_adapter import active\n'
         '    if active():\n'
         '        return  # This campaign uses the authenticated, read-only research bundle.\n')
    edit(cli, '    require_runner_framework(gpu_type or "", framework)\n',
         '    from hyperloom_sdk_adapter import active, require_qualified\n\n'
         '    if active():\n'
         '        require_qualified(gpu_type or "", framework)\n'
         '    else:\n'
         '        require_runner_framework(gpu_type or "", framework)\n')

    preflight = FILES[16]
    edit(preflight, '    """Auto-install missing runtime deps and export auth aliases."""\n',
         '    """Validate the selected local or workstation-owner runtime."""\n'
         '    from hyperloom_sdk_adapter import active, owner_preflight\n'
         '    if active():\n'
         '        return owner_preflight(args)\n')
    edit(preflight, '    """Fail fast when the selected serving framework is not importable here."""\n',
         '    """Validate either the local framework or its qualified workstation owner."""\n'
         '    from hyperloom_sdk_adapter import active, require_qualified\n\n'
         '    if active():\n'
         '        proof = require_qualified(getattr(args, "gpu_type", None) or "",\n'
         '                                  getattr(args, "framework", None) or "")\n'
         '        return {"status": "checked", "target": "sglang",\n'
         '                "scope": "qualified workstation owner; optimizer has no GPU devices",\n'
         '                "selection_sha256": proof["selection_sha256"],\n'
         '                "runtime_sha256": proof["runtime_sha256"],\n'
         '                "controller_sha256": proof["controller_sha256"]}\n')

    grid_runner = FILES[17]
    edit(grid_runner, '    """Materialize a per-variant Magpie YAML on disk."""\n',
         '    """Materialize a per-variant benchmark configuration on disk."""\n'
         '    from hyperloom_sdk_adapter import active, materialize_variant_config\n\n'
         '    if active():\n'
         '        return materialize_variant_config(base_yaml_path, base_extra_args, variant,\n'
         '            output_subdir=output_subdir, model_path=model_path, gpu_type=gpu_type,\n'
         '            benchmark_script=benchmark_script, server_lifecycle=server_lifecycle,\n'
         '            base_args_mode=base_args_mode, base_extra_envs=base_extra_envs,\n'
         '            base_remove_args=base_remove_args, base_unset_envs=base_unset_envs)\n')

    for name, text in sources.items():
        compile(text, name, 'exec', dont_inherit=True)
        (destination / name).write_text(text)
    return FILES
