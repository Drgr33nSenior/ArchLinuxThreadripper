"""Tokenize an owner-selected coding corpus with the staged server tokenizer.

Run with the selected SGLang image's Python/Transformers, offline. No model
weights are loaded and no download or trust_remote_code execution is allowed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import os
import re


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model_directory")
    parser.add_argument("model_revision")
    parser.add_argument("corpus")
    parser.add_argument("output")
    parser.add_argument("profile", choices=("interactive", "batch"))
    parser.add_argument("--contexts", default="4096,8192,32768")
    args = parser.parse_args()
    if not re.fullmatch("[a-f0-9]{40}", args.model_revision):
        parser.error("provide the staged model's exact revision")
    from transformers import AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(args.model_directory, local_files_only=True, trust_remote_code=False)
    raw = Path(args.corpus).read_bytes()
    ids = tokenizer.encode(raw.decode(), add_special_tokens=False)
    output_tokens = 128 if args.profile == "interactive" else 512
    # The pinned TP worker caps max_req_len at context_len - 1; its scheduler
    # caps generation at max_req_len - input_len - 1. Keep the output budget
    # intact instead of submitting a request that the server silently clips.
    context_reserve_tokens = 2
    cases = []
    for context in map(int, args.contexts.split(",")):
        input_tokens = context - output_tokens - context_reserve_tokens
        if context not in (4096, 8192, 32768) or len(ids) < input_tokens:
            parser.error("corpus must supply enough actual coding tokens; no synthetic padding is added")
        cases.append({"context_tokens": context, "output_tokens": output_tokens,
                      "context_reserve_tokens": context_reserve_tokens,
                      "input_ids": ids[:input_tokens], "source_sha256": hashlib.sha256(raw).hexdigest()})
    result = {"schema": 1, "profile": args.profile, "model_revision": args.model_revision,
              "prefix_state": "warm-prefix", "concurrency": [1, 2] if args.profile == "interactive" else [1, 2, 4],
              "repetitions": 3, "requests_per_worker": 2, "cases": cases,
              "provenance": "fixed-length raw coding completion, not a chat/tool harness quality score"}
    os.umask(0o077)
    with open(args.output, "x") as output:
        json.dump(result, output, indent=2)
        output.write("\n")


if __name__ == "__main__":
    main()
