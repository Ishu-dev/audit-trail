"""Run an audit from the command line.

  .venv/bin/python app/run_audit.py sample_models/halcyra_model_planted.xlsx
  .venv/bin/python app/run_audit.py FILE --provider vultr --sandbox docker
  .venv/bin/python app/run_audit.py FILE --attack-probe     # also run the containment test

Output goes to runs/<timestamp>_<file>/ : workbook.json, events.jsonl, report.json, report.md
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent import Audit, write_report_md  # noqa: E402
from parser import parse_workbook  # noqa: E402
from sandbox import ATTACK_PROBE, make_sandbox  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent


def audit_file(path: Path, provider: str, sandbox_kind: str, model: str | None, max_steps: int,
               attack_probe: bool, out_root: Path = ROOT / "runs", on_event=None) -> Path:
    out = out_root / f"{time.strftime('%Y%m%d_%H%M%S')}_{path.stem}"
    out.mkdir(parents=True, exist_ok=True)
    dump = parse_workbook(path)
    (out / "workbook.json").write_text(json.dumps(dump, default=str))
    sb = make_sandbox(sandbox_kind, out / "workbook.json")
    events = (out / "events.jsonl").open("a")

    def emit(ev):
        events.write(json.dumps(ev, default=str) + "\n")
        events.flush()
        if on_event:
            on_event(ev)

    audit = Audit(dump=dump, sandbox=sb, provider=provider, model=model, max_steps=max_steps, on_event=emit)
    audit.run()
    if attack_probe:
        r = sb.run(ATTACK_PROBE)
        audit.log("attack_probe", run_id=r.run_id, stdout=r.stdout, stderr=r.stderr[-1000:],
                  detail="Labelled test: runs what the hidden instruction asked for, inside the sandbox.")
        if r.network_attempt:
            audit.log("containment", run_id=r.run_id, source="attack probe",
                      detail="POST of workbook and env to the attacker URL failed: no network.",
                      blocked=r.blocked)
    rep = audit.report()
    rep["attack_probe"] = [e for e in audit.events if e["type"] == "attack_probe"]
    (out / "report.json").write_text(json.dumps(rep, indent=2, default=str))
    write_report_md(rep, out / "report.md")
    events.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", type=Path)
    ap.add_argument("--provider", default="gemini", choices=["gemini", "vultr", "claude"])
    ap.add_argument("--model")
    ap.add_argument("--sandbox", default="auto", choices=["auto", "local", "docker"])
    ap.add_argument("--max-steps", type=int, default=20)
    ap.add_argument("--attack-probe", action="store_true")
    a = ap.parse_args()

    def show(ev):
        t = ev["type"]
        if t == "llm_turn":
            print(f"[step {ev['step']}] tools={ev['tool_calls']} tokens={ev['tokens']}")
        elif t == "sandbox_run":
            print(f"  {ev['run_id']} exit={ev['exit_code']} {ev['seconds']}s: {ev['purpose']}")
        elif t == "finding_accepted":
            f = ev["finding"]
            print(f"  ACCEPTED {f['id']} {f['location']}: {f['title']}")
        elif t == "finding_rejected":
            print(f"  REJECTED {ev['finding'].get('location')}: {ev['reasons']}")
        elif t == "model_switch":
            print(f"  MODEL SWITCH: {ev['old']} -> {ev['new']}")
        elif t in ("injection_flagged", "containment", "llm_retry", "llm_unavailable"):
            print(f"  {t.upper()}: {ev.get('location', '')} {ev.get('what_it_asked', ev.get('detail', ev.get('error', '')))}")

    out = audit_file(a.file, a.provider, a.sandbox, a.model, a.max_steps, a.attack_probe, on_event=show)
    print("\nreport:", out / "report.md")


if __name__ == "__main__":
    main()
