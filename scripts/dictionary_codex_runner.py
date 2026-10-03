"""Sequential ChatGPT-authenticated Codex transport. Never uses a paid API key.

Bounded invocations let the supervising agent check account usage between batches.
An uncertain dispatch is never retried automatically.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from dictionary_pipeline import Factory, PipelineError, PROMPT, ROOT, atomic_json, dumps, read_json, response_schema


def codex_executable():
    executable = shutil.which("codex.exe") or shutil.which("codex")
    if not executable:
        raise PipelineError("Codex CLI is unavailable")
    # Windows npm wrappers are not executables for shell=False; use their Node entry.
    if Path(executable).suffix.lower() in {".cmd", ".ps1"}:
        entry = Path(executable).parent / "node_modules/@openai/codex/bin/codex.js"
        node = shutil.which("node")
        if not node or not entry.exists():
            raise PipelineError("Cannot resolve the installed Codex CLI entry point")
        return [node, str(entry)]
    return [executable]


def invoke(factory, request, model, timeout=900):
    response_path = factory.root / "responses" / (request["batch_id"] + ".json")
    response_path.parent.mkdir(exist_ok=True)
    schema_path = factory.root / "response-schema.json"
    atomic_json(schema_path, response_schema())
    executable = codex_executable()
    env = {k: v for k, v in os.environ.items() if k not in {"OPENAI_API_KEY", "OPENAI_BASE_URL"}}
    login = subprocess.run(executable + ["login", "status"], capture_output=True, text=True,
                           encoding="utf-8", env=env, timeout=30)
    if login.returncode or "ChatGPT" not in login.stdout + login.stderr:
        raise PipelineError("ChatGPT authentication required; API credentials are not accepted")
    log_dir = factory.root / "transport"
    log_dir.mkdir(exist_ok=True)
    payload = PROMPT + "\n\nINPUT JSON:\n" + dumps({"pass": request["pass"], "items": request["items"]})
    with tempfile.TemporaryDirectory(prefix="dream-translation-") as isolated:
        command = executable + ["exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
            "--sandbox", "read-only", "-C", isolated, "-m", model,
            "-c", 'model_reasoning_effort="medium"', "--output-schema", str(schema_path),
            "--output-last-message", str(response_path), "--json", "-"]
        atomic_json(log_dir / (request["batch_id"] + ".dispatch.json"),
                    {"model_requested": model, "reasoning_requested": "medium", "command": command,
                     "transport": "codex-chatgpt", "input_characters": len(payload)})
        with (log_dir / (request["batch_id"] + ".events.jsonl")).open("w", encoding="utf-8") as stdout, \
             (log_dir / (request["batch_id"] + ".stderr.log")).open("w", encoding="utf-8") as stderr:
            try:
                completed = subprocess.run(command, input=payload, text=True, encoding="utf-8",
                    env=env, stdout=stdout, stderr=stderr, timeout=timeout)
            except subprocess.TimeoutExpired as error:
                raise PipelineError("UNCERTAIN_DISPATCH timed out; inspect saved transport logs before recovery") from error
        if completed.returncode:
            raise PipelineError(f"TRANSPORT_FAILURE exit={completed.returncode}; inspect {log_dir}; no automatic retry")
    if not response_path.exists():
        raise PipelineError("UNCERTAIN_DISPATCH missing final response")
    return factory.ingest(request["batch_id"], read_json(response_path), model)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--model", choices=["gpt-6-astra", "gpt-6-luna"], required=True)
    parser.add_argument("--limit", type=int, default=4)
    parser.add_argument("--max-batches", type=int, default=1)
    parser.add_argument("--pass-kind", choices=["translate", "review"], default="translate")
    parser.add_argument("--usage-checked", action="store_true", help="Supervisor has just checked shared account limits")
    args = parser.parse_args()
    if not args.usage_checked or not 1 <= args.max_batches <= 5:
        raise PipelineError("Check actual account usage first; max-batches must be 1..5")
    factory = Factory(args.work)
    try:
        phase = factory.meta("phase")
        expected = "gpt-6-astra" if phase == "phase1" else "gpt-6-luna" if phase == "phase2" else None
        if args.model != expected:
            raise PipelineError("PHASE_MODEL_MISMATCH; no automatic model substitution")
        for _ in range(args.max_batches):
            request = factory.export_batch(args.limit, pass_kind=args.pass_kind)
            if request is None:
                break
            print(dumps({"batch_id": request["batch_id"], "items": len(request["items"]), "state": "dispatching"}), flush=True)
            metrics = invoke(factory, request, args.model)
            print(dumps(metrics), flush=True)
            if factory.meta("halt"):
                raise PipelineError("QUALITY_HALT: investigate before another batch")
    finally:
        factory.close()


if __name__ == "__main__":
    try:
        main()
    except (PipelineError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
