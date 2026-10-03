"""Record Phase 1 evidence separately from the still-blocked production promotion gate."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

from dictionary_pipeline import Factory, ROOT, atomic_json, now, read_json


def command(args):
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(" ".join(args) + "\n" + result.stdout + result.stderr)
    return {"command": args, "exit_code": result.returncode, "output": (result.stdout + result.stderr).strip()}


def main():
    audit = read_json(ROOT / ".codex/artifacts/legacy-audit.json")
    for relative, expected in audit["immutable_inventory"].items():
        actual = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        if actual != expected["sha256"]:
            raise RuntimeError("Immutable input changed: " + relative)
    production_diff = command(["git", "diff", "650ffb950c875318d27085b5f48567c550b98165", "--", "data"])
    if production_diff["output"]:
        raise RuntimeError("Production data was changed")
    checks = [command([sys.executable, "-X", "utf8", "-m", "unittest", "discover", "-s", "scripts", "-p", "test_dictionary_pipeline.py", "-q"]),
              command(["node", "scripts/test_reading_report.cjs"]),
              command(["node", "scripts/test_interpretation.cjs"]),
              command(["node", "scripts/test_dictionary_runtime.cjs", ".codex/staging-sample-final"])]
    runtime = json.loads(checks[-1]["output"])
    evaluation = read_json(ROOT / ".codex/sample-work/evaluation.json")
    if not evaluation["resume_no_resubmission"] or evaluation["real_source_records"] != 50:
        raise RuntimeError("Representative evaluation is incomplete")
    full = Factory(ROOT / ".codex/dictionary-work")
    live = Factory(ROOT / ".codex/transport-work")
    sample = Factory(ROOT / ".codex/sample-work")
    try:
        stats = full.qa_all()
        live_stats = live.qa_all()
        sample_stats = sample.qa_all()
        if stats["entries"] != audit["source_meanings"] or stats["phase"] != "phase1":
            raise RuntimeError("Full preparation count or phase mismatch")
        if stats["llm_processed_count"] != 0 or live_stats["states"]["complete"] != 4:
            raise RuntimeError("Unexpected full dispatch or incomplete transport verification")
        sample.random_audit(per_language=5)
        full.export_master()
        report = {"recorded_at": now(), "phase1_infrastructure_gate": "PASS",
            "production_ready": False, "immutable_files_verified": len(audit["immutable_inventory"]),
            "production_data_unchanged": True, "tests": checks, "full_preparation": stats,
            "representative_evaluation": evaluation, "sample_current_qa": sample_stats,
            "live_transport": live_stats, "frontend": runtime,
            "required_phase2_fixes": ["Connect rebuilt dictionary meanings to substantive diagnostic prose; require --require-diagnosis before promotion",
                "Review or retain quarantined oversized source records", "Luna full processing, flagged second pass and stratified random audit",
                "Production build, mobile/desktop validation and deployment remain unexecuted"]}
        atomic_json(ROOT / ".codex/artifacts/phase1-verification.json", report)
        print(json.dumps({"phase1_infrastructure_gate": "PASS", "production_ready": False,
            "source_meanings": stats["entries"], "pending": stats["states"]["pending"],
            "diagnosis_ready": runtime["diagnosisReady"], "artifact": ".codex/artifacts/phase1-verification.json"}))
    finally:
        full.close()
        live.close()
        sample.close()


if __name__ == "__main__":
    main()
