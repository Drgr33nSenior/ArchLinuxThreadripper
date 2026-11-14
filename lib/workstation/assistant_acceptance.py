"""Bounded assistant acceptance runner for infrastructure/sglang/assistant-quality.json.

One invocation exercises one named boundary: ``direct`` (native SGLang),
``gateway`` (authenticated boundary/LiteLLM), ``webui-api`` (Open WebUI API)
or ``fixture`` (an in-process deterministic server used only to test this
runner). Results from different boundaries are never pooled. An unreachable
endpoint, a missing tokenizer or an unavailable sandbox is recorded as NOT RUN.

The runner sends only the synthetic menu prompts. Tool calls are answered
with the menu's synthetic results; no real tool runs. Model code runs only in
the network-free Linux sandbox from coding_eval.

``behaviour`` runs the separate tool-use menu (infrastructure/sglang/assistant-behaviour.json):
one request per case with a fixed tool list, graded on whether the model reached for a
tool, answered directly, or said what it could not verify. No tool runs. The tool-use
system prompt Open WebUI pins (apps/components/assistant/system-prompt.md) is sent as the
system message unless --no-system-prompt selects the comparison arm.
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import re
import stat
import statistics
import time
import urllib.error
import urllib.request

import assistant_api
from coding_eval import canonical, digest, execute_python, json_equal, parse_object, run_task


ROOT = Path(__file__).resolve().parents[2]
MENU = ROOT / "infrastructure/sglang/assistant-quality.json"
TASKS = ROOT / "infrastructure/sglang/assistant-tasks"
BEHAVIOUR = ROOT / "infrastructure/sglang/assistant-behaviour.json"
SYSTEM_PROMPT = ROOT / "apps/components/assistant/system-prompt.md"
BEHAVIOUR_KINDS = ("tool", "no-tool", "verify-or-disclose")
BOUNDARIES = ("fixture", "direct", "gateway", "webui-api")
NOT_RUN = "NOT RUN"
MAX_BODY = 8 << 20
FENCE = re.compile(r"```([A-Za-z0-9_./-]+)\n(.*?)```", re.S)
# A JSON answer wrapped whole in one ```json (or bare ```) fence is graded on its
# content; the wrapper is recorded as "fenced" so the habit stays visible.
JSON_FENCE = re.compile(r"\A```(?:json)?[ \t]*\n(.*?)\n?```\Z", re.S)


def json_answer(final):
    """(text to parse, fenced): strips exactly one fence enclosing the whole answer."""
    match = JSON_FENCE.match(final)
    return (match[1], True) if match else (final, False)


def load_menu(path=MENU):
    menu = json.loads(Path(path).read_text())
    if menu.get("schema") != 2 or not isinstance(menu.get("cases"), list) or set(menu.get("profiles", {})) < {"baseline"}:
        raise ValueError("unsupported assistant quality menu")
    ids = [case["id"] for case in menu["cases"]]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case id")
    for case in menu["cases"]:
        if not set(case.get("profiles", [])) <= set(menu["profiles"]):
            raise ValueError("case names an unknown profile: " + case["id"])
        if case.get("task") and not (TASKS / case["task"] / "fixture").is_dir():
            raise ValueError("missing task fixture: " + case["id"])
    return menu


def prompt(case):
    """Case prompt plus, for repository tasks, the fixture files it may read."""
    text = case.get("prompt", "")
    for name in case.get("include", []):
        source = (TASKS / case["task"] / "fixture" / name).read_text()
        text += f"\n\n```{name}\n{source}```"
    return text


def requests(menu, model, output_tokens, repetitions=None):
    """Expand the menu into concrete request plans; no network access."""
    plans = []
    count = repetitions or menu["execution"]["repetitions"]
    by_id = {case["id"]: case for case in menu["cases"]}
    for case in menu["cases"]:
        for profile in case["profiles"]:
            if case["id"] == "long-context-retrieval":
                for target in case["target_input_tokens"]:
                    for position in case["positions"]:
                        plans += [{"case": case["id"], "profile": profile, "repetition": r, "variant": f"{target}-{position}",
                                   "body": None, "not_run": "long-context bodies need the candidate tokenizer budget"}
                                  for r in range(count)]
                continue
            settings = dict(menu["profiles"][profile]["request"])
            # The follow-up case continues the search-tool conversation; its own
            # prompt is the later user question added after the tool result.
            opening = by_id["search-tool"] if case["id"] == "tool-result-followup" else case
            body = {"model": model, "messages": [{"role": "user", "content": prompt(opening)}],
                    "max_tokens": output_tokens, **settings}
            if opening.get("tools"):
                body["tools"] = opening["tools"]
            for repetition in range(count):
                plans.append({"case": case["id"], "profile": profile, "repetition": repetition,
                              "variant": None, "body": dict(body)})
    return plans


def budget(plans, tokenizer_dir, context, output_tokens, reserve):
    """Count prompt tokens with the actual tokenizer and template, when available.

    Uses the checkpoint's tokenizer.json and its tokenizer_config chat template
    with the request's template variables. Without both libraries the budget is
    NOT RUN; characters are never used as a token estimate.
    """
    try:
        import jinja2.sandbox
        import tokenizers
    except ImportError:
        return {"status": NOT_RUN, "reason": "tokenizers and jinja2 are both required"}
    directory = Path(tokenizer_dir)
    config = json.loads((directory / "tokenizer_config.json").read_text())
    template = config.get("chat_template")
    if isinstance(template, list):
        template = next(row["template"] for row in template if row.get("name") == "default")
    environment = jinja2.sandbox.ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
    environment.globals["raise_exception"] = lambda message: (_ for _ in ()).throw(ValueError(message))
    compiled = environment.from_string(template)
    tokenizer = tokenizers.Tokenizer.from_file(str(directory / "tokenizer.json"))
    rows = []
    for plan in plans:
        if plan["body"] is None or plan["repetition"]:
            continue
        body = plan["body"]
        text = compiled.render(messages=body["messages"], tools=body.get("tools"), add_generation_prompt=True,
                               **body.get("chat_template_kwargs", {}),
                               **({"reasoning_effort": body["reasoning_effort"]} if "reasoning_effort" in body else {}))
        tokens = len(tokenizer.encode(text, add_special_tokens=False).ids)
        rows.append({"case": plan["case"], "profile": plan["profile"], "prompt_tokens": tokens,
                     "fits": tokens + output_tokens + reserve <= context})
    return {"status": "measured", "tokenizer_sha256": digest((directory / "tokenizer.json").read_bytes()),
            "tokenizer_config_sha256": digest((directory / "tokenizer_config.json").read_bytes()),
            "context": context, "output_tokens": output_tokens, "reserve": reserve, "rows": rows}


def credential(path):
    if path is None:
        return None
    path = Path(path)
    meta = path.stat()
    if path.is_symlink() or meta.st_uid != os.geteuid() or stat.S_IMODE(meta.st_mode) != 0o600:
        raise ValueError("credential must be a caller-owned mode 0600 file")
    return path.read_text().strip()


class Http:
    """Minimal JSON/SSE client: no proxies, no redirects, bounded bodies."""

    def __init__(self, base, key, timeout):
        self.base, self.key, self.timeout = base.rstrip("/"), key, timeout

    def post(self, path, body):
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = "Bearer " + self.key

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), headers=headers)
        try:
            with opener.open(request, timeout=self.timeout) as response:
                return response.status, response.read(MAX_BODY + 1)
        except urllib.error.HTTPError as error:
            return error.code, error.read(65536)


def answer(case, result):
    """Grade a usable final answer against the case oracle; never an LLM judge."""
    grading, final = case["grading"], result["final"].strip()
    kind = grading["kind"]
    if kind == "text-equality":
        return final == grading["expected"], None
    if kind == "json-equality":
        text, fenced = json_answer(final)
        return json_equal(parse_object(text), grading["expected"]), {"fenced": fenced}
    if kind == "json-fields":
        text, fenced = json_answer(final)
        value = parse_object(text)
        cited = value.get("evidence")
        ok = (all(json_equal(value.get(key), expected) for key, expected in grading["expected"].items())
              and isinstance(cited, list) and set(grading["required_citations"]) <= set(cited)
              and set(cited) <= set(grading["allowed_citations"]))
        return ok, {"citations": cited if isinstance(cited, list) else None, "fenced": fenced}
    if kind == "answer-and-citation":
        ok = grading["required_fact"] in final and grading["required_url"] in final
        return ok, {"citations": [grading["required_url"]] if grading["required_url"] in final else []}
    if kind == "isolated-code-tests":
        blocks = FENCE.findall(final) or [("python", final)]
        run = execute_python(blocks[-1][1], grading["tests"])
        return run["status"] == "tests-passed" if run["status"] in ("tests-passed", "tests-failed", "timeout") else None, {"execution": run}
    if kind == "repository-tests":
        files = {name: text for name, text in FENCE.findall(final) if name in case["editable"]}
        if not files:
            return False, {"execution": {"status": "no-editable-file-returned"}}
        run = run_task(TASKS / case["task"] / "fixture", files, grading["command"], seconds=180,
                       allowed=tuple(case["editable"]))
        return run["status"] == "tests-passed" if run["status"] in ("tests-passed", "tests-failed", "timeout") else None, {"execution": run}
    if kind == "human-review":
        return None, {"review": "needs documented human rubric review"}
    raise ValueError("unsupported grading kind")


def attempt(http, path, case, plan, thinking, synthetic):
    body = dict(plan["body"])
    started = time.monotonic()
    tool_case = case["grading"]["kind"] == "tool-call" or case["id"] == "tool-result-followup"
    if tool_case:
        body["stream"] = True
    status, raw = http.post(path, body)
    elapsed = time.monotonic() - started
    record = {"case": plan["case"], "profile": plan["profile"], "repetition": plan["repetition"],
              "request_sha256": digest(canonical(body)), "http_status": status,
              "response_sha256": digest(raw), "elapsed_seconds": round(elapsed, 3)}
    if status != 200 or len(raw) > MAX_BODY:
        return {**record, "outcome": "error", "grade": "failed", "failure": f"http-{status}"}
    result = assistant_api.stream(raw.decode(), thinking) if body.get("stream") else \
        assistant_api.response(json.loads(raw), thinking)
    record.update({key: result.get(key) for key in ("outcome", "finish_reason", "segmentation", "prompt_tokens",
                                                     "completion_tokens", "reasoning_tokens")})
    record["reasoning_chars"] = result.get("reasoning_chars")
    if tool_case:
        return followup(http, path, case, body, result, record, started, thinking, synthetic)
    if result["outcome"] != "final":
        return {**record, "grade": "failed", "failure": result["outcome"]}
    passed, detail = answer(case, result)
    record["time_to_usable_seconds"] = record["elapsed_seconds"]
    if detail:
        record.update(detail)
    record["grade"] = "passed" if passed else "needs-review" if passed is None and case["grading"]["kind"] == "human-review" \
        else NOT_RUN if passed is None else "failed"
    return record


def followup(http, path, case, body, result, record, started, thinking, synthetic):
    """Complete a tool round trip with the synthetic result and grade both legs."""
    expected = {"tool": "search_web", "arguments": {"query": "SGLang prefix caching documentation", "count": 2}}
    calls = result.get("tool_calls", [])
    if result["outcome"] != "tool_calls" or len(calls) != 1:
        return {**record, "grade": "failed", "failure": "no-complete-tool-call", "tool_roundtrip": False}
    call = calls[0]
    record["tool_call_matches"] = json_equal({"tool": call["name"], "arguments": call["arguments"]}, expected)
    messages = assistant_api.followup_messages(body["messages"], result, {call["id"]: synthetic})
    if case["id"] == "tool-result-followup":
        messages.append({"role": "user", "content": case["prompt"]})
    second = {**body, "stream": False, "messages": messages}
    status, raw = http.post(path, second)
    record["followup_request_sha256"] = digest(canonical(second))
    record["followup_response_sha256"] = digest(raw)
    if status != 200:
        return {**record, "grade": "failed", "failure": f"followup-http-{status}", "tool_roundtrip": False}
    final = assistant_api.response(json.loads(raw), thinking)
    record["followup_outcome"] = final["outcome"]
    record["tool_roundtrip"] = final["outcome"] == "final"
    if final["outcome"] == "final":
        record["time_to_usable_seconds"] = round(time.monotonic() - started, 3)
    grading = case["grading"] if case["id"] == "tool-result-followup" else \
        {"kind": "answer-and-citation", "required_fact": "17", "required_url": "https://example.com/cache"}
    ok = final["outcome"] == "final" and answer({**case, "grading": grading}, final)[0]
    if case["id"] == "search-tool":
        ok = record["tool_call_matches"] and final["outcome"] == "final"
    record["grade"] = "passed" if ok else "failed"
    return record


def run(menu, boundary, base, key, model, output_tokens, timeout, repetitions=None, cases=None):
    if boundary not in BOUNDARIES:
        raise ValueError("unknown boundary")
    path = "/api/chat/completions" if boundary == "webui-api" else "/v1/chat/completions"
    http = Http(base, key, timeout)
    plans = [plan for plan in requests(menu, model, output_tokens, repetitions) if not cases or plan["case"] in cases]
    by_id = {case["id"]: case for case in menu["cases"]}
    synthetic = by_id["tool-result-followup"]["tool_result"]
    records = []
    reachable = None
    for plan in plans:
        case = by_id[plan["case"]]
        base_record = {"case": plan["case"], "profile": plan["profile"], "repetition": plan["repetition"],
                       "variant": plan["variant"]}
        if plan["body"] is None:
            records.append({**base_record, "grade": NOT_RUN, "reason": plan["not_run"]})
            continue
        if reachable is False:
            records.append({**base_record, "grade": NOT_RUN, "reason": "endpoint unreachable"})
            continue
        thinking = plan["body"].get("chat_template_kwargs", {}).get("enable_thinking", False)
        try:
            records.append({**base_record, **attempt(http, path, case, plan, thinking, synthetic)})
            reachable = True
        except (urllib.error.URLError, OSError) as error:
            if reachable is None:
                reachable = False
                records.append({**base_record, "grade": NOT_RUN, "reason": "endpoint unreachable: " + type(error).__name__})
            else:
                records.append({**base_record, "grade": "failed", "failure": "transport-" + type(error).__name__})
        except ValueError as error:
            records.append({**base_record, "grade": "failed", "failure": "protocol: " + str(error)[:120]})
    return records


def report(menu, records, boundary, settings):
    """Aggregate outcomes. A task passes only if every executed repetition passes."""
    executed = [row for row in records if row["grade"] != NOT_RUN]
    tasks = {}
    for row in records:
        tasks.setdefault((row["case"], row["profile"], row.get("variant")), []).append(row["grade"])
    unique = {key: ("passed" if grades and all(g == "passed" for g in grades) else
                    NOT_RUN if all(g == NOT_RUN for g in grades) else
                    "needs-review" if all(g in ("passed", "needs-review") for g in grades) else "failed")
              for key, grades in tasks.items()}
    category = {case["id"]: case["category"] for case in menu["cases"]}
    json_cases = {case["id"] for case in menu["cases"] if case["grading"]["kind"] in ("json-equality", "json-fields")}

    def rate(rows, predicate):
        rows = list(rows)
        return None if not rows else round(sum(map(predicate, rows)) / len(rows), 4)
    tool_rows = [row for row in executed if "tool_roundtrip" in row]
    grounded = [row for row in executed if category[row["case"]] == "grounded" and row["grade"] != "needs-review"]
    usable = [row["time_to_usable_seconds"] for row in executed if "time_to_usable_seconds" in row]
    status = NOT_RUN if not executed else "complete" if all(v != NOT_RUN for v in unique.values()) else "partial"
    return {"schema": 1, "kind": "assistant-acceptance-report", "profile": assistant_api.PROFILE,
            "menu_version": menu["version"], "menu_sha256": digest(MENU.read_bytes()) if MENU.exists() else None,
            "boundary": boundary, "status": status, "settings": settings,
            "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "unique_tasks": len({key[0] for key in unique}), "task_variants": len(unique),
            "attempts": len(records), "executed_attempts": len(executed),
            "metrics": {
                "task_pass_rate": rate((v for v in unique.values() if v != NOT_RUN), lambda v: v == "passed"),
                "tool_roundtrip_success": rate(tool_rows, lambda r: r["tool_roundtrip"]),
                "grounded_answer_accuracy": rate(grounded, lambda r: r["grade"] == "passed"),
                "abstention_correct": rate((r for r in executed if r["case"] == "project-evidence"), lambda r: r["grade"] == "passed"),
                "truncation_rate": rate(executed, lambda r: r.get("outcome") in ("truncated", "unfinished-thinking")),
                "median_time_to_usable_seconds": statistics.median(usable) if usable else None,
                "json_fenced_rate": rate((r for r in executed if r["case"] in json_cases and "fenced" in r),
                                         lambda r: r["fenced"])},
            "tasks": [{"case": k[0], "profile": k[1], "variant": k[2], "result": v} for k, v in sorted(unique.items(), key=str)],
            "records": records,
            "limitations": ["A fixture boundary tests this runner only; it is not model evidence.",
                            "Human-rubric cases are needs-review until a person records a rubric decision.",
                            "Throughput and model self-scores are not acceptance outcomes."]}


def load_behaviour(path=BEHAVIOUR):
    menu = json.loads(Path(path).read_text())
    names = {tool["function"]["name"] for tool in menu.get("tools", [])}
    cases = menu.get("cases")
    if menu.get("schema") != 1 or not names or not isinstance(cases, list) or not cases:
        raise ValueError("unsupported assistant behaviour menu")
    if len({case["id"] for case in cases}) != len(cases):
        raise ValueError("duplicate case id")
    for case in cases:
        expect = case["expect"]
        if (expect["kind"] not in BEHAVIOUR_KINDS or not isinstance(case.get("prompt"), str)
                or not set(expect.get("any_of", [])) <= names
                or (expect["kind"] != "no-tool" and not expect.get("any_of"))):
            raise ValueError("invalid behaviour case: " + case["id"])
        if expect["kind"] == "verify-or-disclose":
            re.compile(expect["disclosure"])
    return menu


def system_prompt(today=None):
    """The pinned prompt as Open WebUI sends it: its date variable filled in."""
    today = today or datetime.date.today().isoformat()
    return SYSTEM_PROMPT.read_text().strip().replace("{{CURRENT_DATE}}", today)


def behaviour_grade(expect, names, final):
    """(passed, matched disclosure text). Tool names and visible text only; never an LLM judge."""
    called = bool(set(names) & set(expect.get("any_of", [])))
    if expect["kind"] == "tool":
        return called, None
    if expect["kind"] == "no-tool":
        return not names and bool(final.strip()), None
    match = re.search(expect["disclosure"], final, re.I)
    return called or bool(match), match[0][:200] if match else None


def behaviour_attempt(http, path, menu, case, model, system):
    execution = menu["execution"]
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": case["prompt"]}]
    body = {"model": model, "messages": messages, "tools": menu["tools"], "stream": False,
            "max_tokens": case.get("max_tokens", execution["max_tokens"]), **execution["request"]}
    started = time.monotonic()
    status, raw = http.post(path, body)
    record = {"request_sha256": digest(canonical(body)), "http_status": status, "response_sha256": digest(raw),
              "elapsed_seconds": round(time.monotonic() - started, 3)}
    if status != 200 or len(raw) > MAX_BODY:
        return {**record, "grade": "failed", "failure": f"http-{status}"}
    value = json.loads(raw)
    choice = value["choices"][0]
    message = choice.get("message") or {}
    # Text beside a tool call is common ("Let me check ...") and is not an error here.
    names = [call["function"]["name"] for call in message.get("tool_calls") or []]
    final = message.get("content") or ""
    usage = value.get("usage") or {}
    passed, disclosure = behaviour_grade(case["expect"], names, final)
    record.update({"finish_reason": choice.get("finish_reason"), "tools_called": names,
                   "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                   "final_chars": len(final), "grade": "passed" if passed else "failed"})
    if disclosure:
        record["disclosure"] = disclosure
    if not passed and choice.get("finish_reason") == "length":
        record["failure"] = "truncated"
    return record


def behaviour_run(menu, boundary, base, key, model, timeout, system, repetitions=None, cases=None):
    if boundary not in BOUNDARIES:
        raise ValueError("unknown boundary")
    path = "/api/chat/completions" if boundary == "webui-api" else "/v1/chat/completions"
    http = Http(base, key, timeout)
    records, reachable = [], None
    for case in menu["cases"]:
        if cases and case["id"] not in cases:
            continue
        for repetition in range(repetitions or menu["execution"]["repetitions"]):
            row = {"case": case["id"], "repetition": repetition}
            if reachable is False:
                records.append({**row, "grade": NOT_RUN, "reason": "endpoint unreachable"})
                continue
            try:
                records.append({**row, **behaviour_attempt(http, path, menu, case, model, system)})
                reachable = True
            except (urllib.error.URLError, OSError) as error:
                if reachable is None:
                    reachable = False
                    records.append({**row, "grade": NOT_RUN, "reason": "endpoint unreachable: " + type(error).__name__})
                else:
                    records.append({**row, "grade": "failed", "failure": "transport-" + type(error).__name__})
            except (ValueError, KeyError, IndexError, TypeError, AttributeError) as error:
                records.append({**row, "grade": "failed", "failure": "protocol: " + type(error).__name__})
    return records


def behaviour_report(menu, records, boundary, settings):
    """Per-case rates. Controls pass by answering without a tool, so they guard against over-use."""
    def rate(rows, predicate):
        return None if not rows else round(sum(map(predicate, rows)) / len(rows), 4)
    executed = [row for row in records if row["grade"] != NOT_RUN]
    answered = [row for row in executed if "tools_called" in row]
    by_case = {}
    for case in menu["cases"]:
        rows = [row for row in executed if row["case"] == case["id"]]
        if not rows:
            continue
        seen = [row for row in rows if "tools_called" in row]
        by_case[case["id"]] = {
            "category": case["category"], "expect": case["expect"]["kind"], "attempts": len(rows),
            "passed": sum(row["grade"] == "passed" for row in rows),
            "pass_rate": rate(rows, lambda r: r["grade"] == "passed"),
            "tool_call_rate": rate(seen, lambda r: bool(r["tools_called"])),
            "tools_called": sorted({name for row in seen for name in row["tools_called"]})}
    prompts = [row["prompt_tokens"] for row in answered if type(row.get("prompt_tokens")) is int]
    return {"schema": 1, "kind": "assistant-behaviour-report", "menu_version": menu["version"],
            "menu_sha256": digest(BEHAVIOUR.read_bytes()), "boundary": boundary,
            "status": NOT_RUN if not executed else "complete" if len(executed) == len(records) else "partial",
            "settings": settings, "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "attempts": len(records), "executed_attempts": len(executed),
            "metrics": {"pass_rate": rate(executed, lambda r: r["grade"] == "passed"),
                        "tool_call_rate_when_expected": rate(
                            [r for r in answered if by_case[r["case"]]["expect"] != "no-tool"], lambda r: bool(r["tools_called"])),
                        "tool_call_rate_on_controls": rate(
                            [r for r in answered if by_case[r["case"]]["expect"] == "no-tool"], lambda r: bool(r["tools_called"])),
                        "median_prompt_tokens": statistics.median(prompts) if prompts else None,
                        "median_elapsed_seconds": statistics.median(r["elapsed_seconds"] for r in answered) if answered else None},
            "cases": by_case, "records": records, "limitations": menu["execution"]["limitations"]}


def write_new(path, value):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write("\n")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    p = commands.add_parser("plan", help="expand the menu and measure token budgets; no network")
    p.add_argument("output"); p.add_argument("--model", required=True)
    p.add_argument("--output-tokens", type=int, default=4096); p.add_argument("--context", type=int, default=131040)
    p.add_argument("--reserve-tokens", type=int, default=16); p.add_argument("--tokenizer-dir")
    p = commands.add_parser("run", help="run the menu against one boundary")
    p.add_argument("output"); p.add_argument("--boundary", choices=BOUNDARIES, required=True)
    p.add_argument("--base-url", required=True); p.add_argument("--model", required=True)
    p.add_argument("--key-file"); p.add_argument("--output-tokens", type=int, default=4096)
    p.add_argument("--timeout", type=int, default=180); p.add_argument("--repetitions", type=int)
    p.add_argument("--case", action="append")
    p = commands.add_parser("behaviour", help="run the tool-use behaviour menu against one boundary")
    p.add_argument("output"); p.add_argument("--boundary", choices=BOUNDARIES, required=True)
    p.add_argument("--base-url", required=True); p.add_argument("--model", required=True)
    p.add_argument("--key-file"); p.add_argument("--timeout", type=int, default=300)
    p.add_argument("--repetitions", type=int); p.add_argument("--case", action="append")
    p.add_argument("--no-system-prompt", action="store_true",
                   help="comparison arm, or webui-api (Open WebUI adds the pinned prompt itself)")
    args = parser.parse_args()
    if args.action == "behaviour":
        if not 1 <= args.timeout <= 3600 or (args.repetitions is not None and not 1 <= args.repetitions <= 20):
            raise SystemExit("invalid timeout or repetitions")
        menu = load_behaviour()
        system = None if args.no_system_prompt else system_prompt()
        settings = {"model": args.model, "timeout": args.timeout, "base_url_sha256": digest(args.base_url.encode()),
                    "request": menu["execution"]["request"], "system_prompt": system is not None,
                    "system_prompt_sha256": digest(system.encode()) if system else None}
        records = behaviour_run(menu, args.boundary, args.base_url, credential(args.key_file), args.model,
                                args.timeout, system, args.repetitions, args.case)
        result = behaviour_report(menu, records, args.boundary, settings)
        write_new(args.output, result)
        print(result["status"], json.dumps(result["metrics"]))
        for name, row in result["cases"].items():
            print(f"{name}: {row['passed']}/{row['attempts']} passed, tool calls {row['tool_call_rate']}, {row['tools_called']}")
        return 0
    menu = load_menu()
    if args.action == "plan":
        plans = requests(menu, args.model, args.output_tokens)
        budgets = budget(plans, args.tokenizer_dir, args.context, args.output_tokens, args.reserve_tokens) \
            if args.tokenizer_dir else {"status": NOT_RUN, "reason": "no tokenizer directory supplied"}
        write_new(args.output, {"schema": 1, "kind": "assistant-acceptance-plan", "menu_version": menu["version"],
                                "requests": [{**{k: v for k, v in plan.items() if k != "body"},
                                              "request_sha256": digest(canonical(plan["body"])) if plan["body"] else None}
                                             for plan in plans], "budget": budgets})
        return 0
    if not 1 <= args.timeout <= 3600 or not 128 <= args.output_tokens <= 65520:
        raise SystemExit("invalid timeout or output allowance")
    settings = {"model": args.model, "output_tokens": args.output_tokens, "timeout": args.timeout,
                "base_url_sha256": digest(args.base_url.encode()), "profiles": menu["profiles"]}
    records = run(menu, args.boundary, args.base_url, credential(args.key_file), args.model,
                  args.output_tokens, args.timeout, args.repetitions, args.case)
    result = report(menu, records, args.boundary, settings)
    write_new(args.output, result)
    print(result["status"], json.dumps(result["metrics"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
