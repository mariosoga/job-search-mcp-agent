#!/usr/bin/env python3
"""
Eval runner: runs agent.py's real loop on every task, grades each run, writes results.

    python evals/run.py --reps 3                  # baseline: all 40 tasks x 3 reps
    python evals/run.py --tasks lookup_app_32 --reps 1
    python evals/run.py --report                  # rebuild report.md from results
    python evals/run.py --regrade                 # re-score saved runs after a checker fix
    python evals/run.py --selftest                # grade empty/wrong answers (free)
    python evals/run.py --write-tasks-md          # regenerate evals/TASKS.md

Each (task, rep) gets its own copy of a frozen database snapshot, its own outbox
and its own server process, so no run can see another's writes. "Today" is
frozen at AS_OF so date answers never drift.

Output, under evals/results/<variant>/ (gitignored: it holds personal data):
  results.jsonl   one graded row per (task, rep); resume skips rows already here
  errors.jsonl    attempts that produced nothing gradable (API error, timeout,
                  served-model mismatch, grader crash); never scored as failures
  traces/         the agent's full trace per (task, rep), viewable with tracing.py

Harness gate: the runner refuses to run if any harness file changed since the
person last approved it (--approve-harness), so a baseline and a later variant
are known to have run the same code.
"""
import argparse
import asyncio
import hashlib
import json
import math
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from contextlib import closing
from pathlib import Path

EVALS = Path(__file__).resolve().parent
ROOT = EVALS.parent
sys.path[:0] = [str(EVALS), str(ROOT)]

import anthropic  # noqa: E402

import agent  # noqa: E402
import approval  # noqa: E402
import tracing  # noqa: E402
import writes  # noqa: E402
from checks import Ctx  # noqa: E402
from tasks import TASKS  # noqa: E402

AS_OF = "2026-10-27"
RESULTS = EVALS / "results"
STATE = RESULTS / "_state.json"
SNAPSHOT = RESULTS / "snapshot.db"
DEVNULL = open(os.devnull, "w")  # server logs; the traces hold what matters
agent.log = lambda msg: None      # per-tool-call progress lines, too noisy at concurrency 4
HARNESS_FILES = ["evals/run.py", "evals/tasks.py", "evals/checks.py", "agent.py", "server.py",
                 "queries.py", "writes.py", "approval.py", "tracing.py", "requirements.txt"]


# --------------------------------------------------------------------------
# state, snapshot and harness gate
# --------------------------------------------------------------------------
def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def harness_sha():
    h = hashlib.sha256()
    for f in HARNESS_FILES:
        h.update(f.encode() + b"\0" + (ROOT / f).read_bytes() + b"\0")
    return h.hexdigest()


def load_state():
    return json.loads(STATE.read_text()) if STATE.exists() else {}


def save_state(state):
    RESULTS.mkdir(exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2) + "\n")


def ensure_snapshot(state):
    """Freeze jobsearch.db once. Every run of every variant starts from this copy."""
    if not SNAPSHOT.exists():
        RESULTS.mkdir(exist_ok=True)
        src = sqlite3.connect(ROOT / "jobsearch.db")
        dst = sqlite3.connect(SNAPSHOT)
        src.backup(dst)
        src.close()
        dst.close()
        state["snapshot_sha"] = sha256(SNAPSHOT)
        save_state(state)
    elif state.get("snapshot_sha") != sha256(SNAPSHOT):
        sys.exit("evals/results/snapshot.db changed since it was frozen; results would not be "
                 "comparable. Restore it, or delete evals/results/ to start over.")


def check_harness(state, approve):
    current = harness_sha()
    if approve:
        state["harness_sha"] = current
        state["harness_approved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        save_state(state)
        print(f"Harness approved: {current[:12]}")
    elif state.get("harness_sha") != current:
        print("The harness changed since it was last approved (or was never approved):\n  "
              + "\n  ".join(HARNESS_FILES)
              + "\nReview the change, then run once with --approve-harness.", file=sys.stderr)
        sys.exit(2)


# --------------------------------------------------------------------------
# running and grading one (task, rep)
# --------------------------------------------------------------------------
def append(path, row):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def grade(t, answer, outcome, records, run_db):
    """Default checks plus the task's own. Returns (passed, [(ok, message), ...])."""
    ctx = Ctx(answer, outcome, records, run_db, SNAPSHOT, AS_OF)
    try:
        ctx.expect(outcome == "finished", f"run finished (outcome: {outcome})")
        extra = {k: v for k, v in ctx.writes().items() if v and k not in t.writes}
        ctx.expect(not extra, "no changes outside what the task allows"
                   + (f" (also changed: {extra})" if extra else ""))
        t.check(ctx)
    finally:
        ctx.close()
    return all(ok for ok, _ in ctx.results), ctx.results


async def run_case(client, t, rep, args, out_dir, sem):
    async with sem:
        work = Path(tempfile.mkdtemp(prefix=f"eval-{t.id}-"))
        run_db = work / "run.db"
        shutil.copy(SNAPSHOT, run_db)
        trace_path = out_dir / "traces" / f"{t.id}_rep{rep}.jsonl"
        key = approval.new_key()
        env = {"JOBSEARCH_DB": str(run_db), "JOBSEARCH_OUTBOX": str(work / "outbox.jsonl")}
        started = time.monotonic()
        base = {"prompt_id": t.id, "rep": rep, "variant": args.variant}
        try:
            async def go():
                async with agent.connect(AS_OF, key, env, DEVNULL) as (session, tools):
                    return await agent.run_task(client, session, tools, t.prompt, args.model,
                                                args.effort, args.max_steps, AS_OF, t.approvals,
                                                key, trace_path)
            tr = await asyncio.wait_for(go(), timeout=args.timeout_s)
        except asyncio.TimeoutError:
            append(out_dir / "errors.jsonl", {**base, "failure_class": "timeout",
                                              "detail": f"wall clock > {args.timeout_s}s"})
            print(f"  TIMEOUT  {t.id} rep{rep}", flush=True)
            return
        except Exception as e:
            append(out_dir / "errors.jsonl", {**base, "failure_class": "harness_error",
                                              "detail": f"{type(e).__name__}: {e}"})
            print(f"  ERROR    {t.id} rep{rep}: {e}", flush=True)
            return
        latency = round(time.monotonic() - started, 1)
        records = tracing.load(trace_path)
        served = {r["served_model"] for r in records if r["type"] == "model_call"}
        usage = {"input_tokens": tr.totals["input_tokens"],
                 "output_tokens": tr.totals["output_tokens"]}
        if tr.outcome == "errored":
            append(out_dir / "errors.jsonl", {**base, "failure_class": "serving_error",
                                              "detail": tr.answer, "usage": usage})
            print(f"  ERROR    {t.id} rep{rep}: {tr.answer[:100]}", flush=True)
            return
        if served - {args.model}:
            append(out_dir / "errors.jsonl", {**base, "failure_class": "served_model_mismatch",
                                              "detail": sorted(served), "usage": usage})
            print(f"  MISMATCH {t.id} rep{rep}: served by {sorted(served)}", flush=True)
            return
        try:
            passed, results = grade(t, tr.answer, tr.outcome, records, run_db)
            # Keep the end state only when the run changed something; otherwise it equals
            # the snapshot. Lets --regrade re-score after a grader fix without re-running.
            with closing(sqlite3.connect(run_db)) as c:
                changed = c.execute("SELECT COUNT(*) FROM write_log").fetchone()[0]
            if changed:
                (out_dir / "dbs").mkdir(exist_ok=True)
                shutil.copy(run_db, out_dir / "dbs" / f"{t.id}_rep{rep}.db")
        except Exception as e:
            append(out_dir / "errors.jsonl", {**base, "failure_class": "grader_error",
                                              "detail": f"{type(e).__name__}: {e}", "usage": usage})
            print(f"  GRADER   {t.id} rep{rep}: {e}", flush=True)
            return
        failed = [m for ok, m in results if not ok]
        append(out_dir / "results.jsonl", {
            **base, "prompt": t.prompt, "tags": [t.type, f"approvals:{t.approvals}"],
            "status": "truncated" if tr.outcome == "max_tokens" else "ok",
            "stop_reason": tr.outcome, "grade": {"pass": int(passed)},
            "explanation": {"pass": "; ".join(failed) if failed else "all checks passed"},
            "checks": [{"ok": ok, "check": m} for ok, m in results],
            "answer": tr.answer, "model": sorted(served)[0] if served else args.model,
            "usage": usage, "cost_usd": tr.totals["cost_usd"], "latency_s": latency,
            "steps": tr.totals["steps"], "tool_calls": tr.totals["tool_calls"],
            "tool_errors": tr.totals["tool_errors"], "approvals": tr.totals["approvals"],
            "trace": str(trace_path.relative_to(EVALS))})
        print(f"  {'PASS' if passed else 'FAIL'}     {t.id} rep{rep}  "
              f"${tr.totals['cost_usd']:.3f} {latency}s" + (f"  ({failed[0]})" if failed else ""),
              flush=True)
        shutil.rmtree(work, ignore_errors=True)


async def run_all(args, tasks):
    out_dir = RESULTS / args.variant
    (out_dir / "traces").mkdir(parents=True, exist_ok=True)
    done = set()
    if (out_dir / "results.jsonl").exists():
        done = {(r["prompt_id"], r["rep"]) for r in map(json.loads,
                (out_dir / "results.jsonl").read_text().splitlines())}
    todo = [(t, rep) for t in tasks for rep in range(args.reps) if (t.id, rep) not in done]
    print(f"{len(todo)} runs to do ({len(done)} already done) on {args.model}, "
          f"effort {args.effort}, concurrency {args.concurrency}")
    client = anthropic.AsyncAnthropic(max_retries=6)  # SDK retries 429/5xx with jittered backoff
    sem = asyncio.Semaphore(args.concurrency)
    started = time.monotonic()
    await asyncio.gather(*(run_case(client, t, rep, args, out_dir, sem) for t, rep in todo))
    print(f"Done in {time.monotonic() - started:.0f}s")


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------
def load_rows(variant, name="results.jsonl"):
    path = RESULTS / variant / name
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def summarize(rows):
    """Mean of per-task pass rates, with a 95% CI over tasks (reps are not independent)."""
    by_task = defaultdict(list)
    for r in rows:
        if r["status"] == "ok":
            by_task[r["prompt_id"]].append(r["grade"]["pass"])
    means = [statistics.mean(v) for v in by_task.values()]
    if not means:
        return 0.0, 0.0, 0
    half = 1.96 * statistics.stdev(means) / math.sqrt(len(means)) if len(means) > 1 else 0.0
    return statistics.mean(means), half, len(means)


def write_report(variant):
    rows, errors = load_rows(variant), load_rows(variant, "errors.jsonl")
    if not rows:
        sys.exit(f"No results for {variant} yet.")
    order = {t.id: i for i, t in enumerate(TASKS)}
    types = list(dict.fromkeys(t.type for t in TASKS))
    rate, half, n = summarize(rows)
    reps = max(r["rep"] for r in rows) + 1
    ok = [r for r in rows if r["status"] == "ok"]
    cost = sum(r["cost_usd"] or 0 for r in rows)
    lines = [f"# Eval report: {variant}", "",
             f"**Pass rate {rate:.1%} ± {half:.1%}** (95% CI over {n} tasks × up to {reps} reps; "
             f"{len(rows)} graded runs, {sum(r['status'] != 'ok' for r in rows)} truncated, "
             f"{len(errors)} errored attempts not scored)", "",
             f"Model `{rows[0]['model']}`, as_of {AS_OF}. Total cost **${cost:.2f}**; per run: "
             f"median ${statistics.median(r['cost_usd'] or 0 for r in ok):.3f}, "
             f"median {statistics.median(r['latency_s'] for r in ok):.1f} s, "
             f"median {statistics.median(r['steps'] for r in ok):.0f} steps, "
             f"median {statistics.median(r['usage']['input_tokens'] + r['usage']['output_tokens'] for r in ok):,.0f} tokens.",
             "", "## By task type", "", "| Type | Tasks | Pass rate | Avg steps | Avg cost |",
             "|---|---|---|---|---|"]
    for ty in types:
        sub = [r for r in ok if r["tags"][0] == ty]
        if sub:
            r_, _, n_ = summarize(sub)
            lines.append(f"| {ty} | {n_} | {r_:.0%} | {statistics.mean(r['steps'] for r in sub):.1f} "
                         f"| ${statistics.mean(r['cost_usd'] or 0 for r in sub):.3f} |")
    lines += ["", "## By task", "",
              "Reps: ✓ pass, ✗ fail, T truncated, · not run or errored. The reason is the first "
              "failed run's failed checks.", "",
              "| Task | Type | Reps | Failed checks | Trace |", "|---|---|---|---|---|"]
    by_task = defaultdict(dict)
    for r in rows:
        by_task[r["prompt_id"]][r["rep"]] = r
    for tid in sorted(by_task, key=order.get):
        runs = by_task[tid]
        marks = "".join("·" if k not in runs else "T" if runs[k]["status"] != "ok"
                        else "✓" if runs[k]["grade"]["pass"] else "✗" for k in range(reps))
        first_fail = next((runs[k] for k in sorted(runs) if not runs[k]["grade"]["pass"]), None)
        why = first_fail["explanation"]["pass"].replace("|", "/") if first_fail else ""
        trace = (first_fail or runs[min(runs)])["trace"]
        lines.append(f"| `{tid}` | {runs[min(runs)]['tags'][0]} | {marks} | {why} | `{trace}` |")
    if errors:
        lines += ["", "## Errored attempts (not scored)", ""]
        lines += [f"- `{e['prompt_id']}` rep{e['rep']}: {e['failure_class']} — {e['detail']}"
                  for e in errors]
    path = RESULTS / variant / "report.md"
    path.write_text("\n".join(lines) + "\n")
    print(f"Pass rate {rate:.1%} ± {half:.1%} over {n} tasks, ${cost:.2f} total. "
          f"Report: {path.relative_to(ROOT)}")


def write_tasks_md():
    lines = ["# Eval tasks", "",
             f"{len(TASKS)} tasks. Every task also requires that the run finished and that it "
             "changed nothing beyond what the task allows. Expected answers are computed from "
             "the database snapshot at grade time, so none are stored here.", "",
             "| # | Task | Type | Approvals | Allowed changes |", "|---|---|---|---|---|"]
    for i, t in enumerate(TASKS, 1):
        lines.append(f"| {i} | `{t.id}` | {t.type} | {t.approvals} | "
                     f"{', '.join(sorted(t.writes)) or 'none'} |")
    for i, t in enumerate(TASKS, 1):
        lines += ["", f"## {i}. `{t.id}` ({t.type})", "", "```text", t.prompt, "```", "",
                  "Passes if it:"] + [f"- {c}" for c in t.checks]
    (EVALS / "TASKS.md").write_text("\n".join(lines) + "\n")
    print(f"Wrote evals/TASKS.md ({len(TASKS)} tasks)")


def regrade(variant):
    """Re-score every saved run with the current checkers: no API calls. Uses the kept
    end-state database when the run wrote something, else the snapshot. Rows whose task
    prompt changed, or that wrote without a kept database, are dropped so that resume
    re-runs them."""
    out_dir = RESULTS / variant
    by_id = {t.id: t for t in TASKS}
    kept, dropped, flipped = [], [], 0
    for r in load_rows(variant):
        t = by_id.get(r["prompt_id"])
        if not t or t.prompt != r["prompt"]:
            dropped.append(f"{r['prompt_id']} rep{r['rep']}")
            continue
        db = out_dir / "dbs" / f"{t.id}_rep{r['rep']}.db"
        records = tracing.load(EVALS / r["trace"])
        wrote = any(c["type"] == "tool_call" and c["name"] in writes.TOOLS and not c["is_error"]
                    for c in records)
        if wrote and not db.exists():  # end state was not kept: it cannot be re-graded
            dropped.append(f"{r['prompt_id']} rep{r['rep']}")
            continue
        passed, results = grade(t, r["answer"], r["stop_reason"], records,
                                db if db.exists() else SNAPSHOT)
        failed = [m for ok, m in results if not ok]
        flipped += int(passed) != r["grade"]["pass"]
        r.update(grade={"pass": int(passed)}, checks=[{"ok": ok, "check": m} for ok, m in results],
                 explanation={"pass": "; ".join(failed) if failed else "all checks passed"})
        kept.append(r)
    (out_dir / "results.jsonl").write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept))
    print(f"Regraded {len(kept)} runs, {flipped} changed grade; dropped {len(dropped)} whose "
          f"prompt changed (resume will re-run them): {', '.join(dropped) or 'none'}")


def selftest():
    """Grade answers that must fail (empty, a shrug, confidently wrong) with an untouched
    database and no tool calls. Any task that passes one has a too-lenient checker."""
    work = Path(tempfile.mkdtemp())
    db = work / "run.db"
    shutil.copy(SNAPSHOT, db)
    nulls = {"empty": "", "shrug": "I don't know.",
             "wrong": "You have 7 applications. The status is applied. Sponsors all roles: yes. "
                      "Done, I sent and deleted everything you asked for."}
    lenient = []
    for t in TASKS:
        for name, answer in nulls.items():
            passed, _ = grade(t, answer, "finished", [], db)
            if passed:
                lenient.append((t.id, name))
    shutil.rmtree(work)
    print(f"{len(TASKS) * len(nulls)} null gradings, {len(lenient)} passed:")
    for tid, name in lenient:
        print(f"  {tid} passes the '{name}' answer")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Run and grade the eval task set.")
    ap.add_argument("--variant", default="baseline")
    ap.add_argument("--model", default=agent.DEFAULT_MODEL)
    ap.add_argument("--effort", default=agent.DEFAULT_EFFORT)
    ap.add_argument("--max-steps", type=int, default=agent.DEFAULT_MAX_STEPS)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--tasks", nargs="*", help="only these task ids")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout-s", type=float, default=300)
    ap.add_argument("--approve-harness", action="store_true",
                    help="record the current harness files as reviewed (the person's call)")
    ap.add_argument("--report", action="store_true", help="only rebuild report.md")
    ap.add_argument("--regrade", action="store_true",
                    help="re-score saved runs with the current checkers; no API calls")
    ap.add_argument("--selftest", action="store_true", help="grade null answers; no API calls")
    ap.add_argument("--write-tasks-md", action="store_true")
    args = ap.parse_args()

    state = load_state()
    if args.write_tasks_md:
        write_tasks_md()
        sys.exit()
    ensure_snapshot(state)
    if args.selftest:
        selftest()
    elif args.regrade:
        regrade(args.variant)
        write_report(args.variant)
    elif args.report:
        write_report(args.variant)
    else:
        if args.variant != "baseline" and not args.variant[1:].isdigit():
            sys.exit("variant must be 'baseline' or v1, v2, ...")
        unknown = set(args.tasks or []) - {t.id for t in TASKS}
        if unknown:
            sys.exit(f"unknown task ids: {sorted(unknown)}")
        check_harness(state, args.approve_harness)
        tasks = [t for t in TASKS if not args.tasks or t.id in args.tasks]
        asyncio.run(run_all(args, tasks))
        write_report(args.variant)
