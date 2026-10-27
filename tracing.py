#!/usr/bin/env python3
"""
Run traces: one JSON Lines file per agent run in traces/, plus a viewer.

    python tracing.py                 # list recent runs, one line each
    python tracing.py --last          # show the most recent run step by step
    python tracing.py traces/<id>.jsonl [--full]

Every record has a "type":
  run_start   run id, task, model, effort, frozen date, system prompt, tool definitions
  model_call  step, stop reason, served model, full content blocks, usage, latency
  model_error step, API or network error, latency
  tool_call   step, tool name, arguments, result text, is_error, latency
  run_end     outcome, final answer, totals (steps, tool calls, tokens, cost, seconds)

Each record is flushed as soon as it is written, so a run that crashes still
leaves everything up to the crash. traces/ holds personal data and is gitignored.
"""
import argparse
import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

TRACE_DIR = Path(__file__).with_name("traces")

# USD per million tokens (input, output). Unknown models report cost as None.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


def cost_usd(model, tokens_in, tokens_out):
    if model not in PRICES:
        return None
    p_in, p_out = PRICES[model]
    return round((tokens_in * p_in + tokens_out * p_out) / 1_000_000, 4)


def _jsonable(obj):
    """SDK and MCP objects are Pydantic models; everything else falls back to str."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json", exclude_none=True)
    return str(obj)


# --------------------------------------------------------------------------
# writer
# --------------------------------------------------------------------------
class Tracer:
    def __init__(self, task, model, effort, as_of, system, tools, max_steps):
        TRACE_DIR.mkdir(exist_ok=True)
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
        self.path = TRACE_DIR / f"{self.run_id}.jsonl"
        self._f = self.path.open("w", encoding="utf-8")
        self.model = model
        self.started = time.monotonic()
        self.steps = self.tool_calls = self.tool_errors = 0
        self.tokens_in = self.tokens_out = 0
        self._write("run_start", task=task, model=model, effort=effort, as_of=as_of,
                    max_steps=max_steps, system=system, tools=tools,
                    started_at=datetime.now().isoformat(timespec="seconds"))

    def _write(self, type_, **fields):
        record = {"type": type_, "run_id": self.run_id, **fields}
        self._f.write(json.dumps(record, default=_jsonable, ensure_ascii=False) + "\n")
        self._f.flush()

    def model_call(self, step, response, latency_ms):
        self.steps = step
        self.tokens_in += response.usage.input_tokens
        self.tokens_out += response.usage.output_tokens
        self._write("model_call", step=step, stop_reason=response.stop_reason,
                    served_model=response.model, request_id=getattr(response, "_request_id", None),
                    latency_ms=round(latency_ms), usage=response.usage,
                    content=response.content)

    def model_error(self, step, error, latency_ms):
        self.steps = step
        self._write("model_error", step=step, error=error, latency_ms=round(latency_ms))

    def tool_call(self, step, name, arguments, result, is_error, latency_ms):
        self.tool_calls += 1
        self.tool_errors += bool(is_error)
        self._write("tool_call", step=step, name=name, arguments=arguments,
                    result=result, is_error=bool(is_error), latency_ms=round(latency_ms, 1))

    def end(self, outcome, answer):
        self.totals = {"steps": self.steps, "tool_calls": self.tool_calls,
                       "tool_errors": self.tool_errors, "input_tokens": self.tokens_in,
                       "output_tokens": self.tokens_out,
                       "cost_usd": cost_usd(self.model, self.tokens_in, self.tokens_out),
                       "seconds": round(time.monotonic() - self.started, 1)}
        self._write("run_end", outcome=outcome, answer=answer, totals=self.totals)
        self._f.close()
        self.outcome, self.answer = outcome, answer

    def summary(self):
        return summary_line(self.run_id, self.outcome, self.totals)


def summary_line(run_id, outcome, t):
    cost = f"${t['cost_usd']:.4f}" if t.get("cost_usd") is not None else "$?"
    return (f"{run_id}  {outcome:<11} {t['steps']} steps, {t['tool_calls']} tool calls"
            f" ({t['tool_errors']} errors), {t['input_tokens']}+{t['output_tokens']} tokens,"
            f" {cost}, {t['seconds']} s")


# --------------------------------------------------------------------------
# viewer
# --------------------------------------------------------------------------
def load(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _clip(text, n, full):
    text = str(text)
    return text if full or len(text) <= n else text[:n] + f" … (+{len(text) - n} chars)"


def show(path, full=False):
    records = load(path)
    start = records[0]
    print(f"run {start['run_id']}  model {start['model']}  effort {start['effort']}"
          f"  as_of {start['as_of']}")
    print(f"task: {start['task']}\n")
    for r in records[1:]:
        if r["type"] == "model_call":
            u = r["usage"]
            print(f"[{r['step']}] model -> {r['stop_reason']}  "
                  f"({u.get('input_tokens')}+{u.get('output_tokens')} tokens, {r['latency_ms']} ms"
                  + (f", served by {r['served_model']}" if r['served_model'] != start['model'] else "")
                  + ")")
            for b in r["content"]:
                if b.get("type") == "text" and b.get("text", "").strip():
                    print("      says: " + _clip(b["text"].strip().replace("\n", " "), 160, full))
                elif b.get("type") == "fallback":
                    print(f"      fallback: {b}")
        elif r["type"] == "model_error":
            print(f"[{r['step']}] model -> ERROR {r['error']}  ({r['latency_ms']} ms)")
        elif r["type"] == "tool_call":
            flag = "ERROR " if r["is_error"] else ""
            print(f"    {r['name']}({json.dumps(r['arguments'])})  {r['latency_ms']} ms")
            print(f"      -> {flag}" + _clip(r["result"].replace("\n", " "), 200, full))
        elif r["type"] == "run_end":
            print(f"\nanswer:\n{r['answer'] or '(no final answer)'}\n")
            print(summary_line(r["run_id"], r["outcome"], r["totals"]))
            return
    print("\n(no run_end record: the run was killed before it could finish)")


def list_runs(limit=20):
    paths = sorted(TRACE_DIR.glob("*.jsonl"))[-limit:]
    if not paths:
        print("No traces yet. Run agent.py first.")
    for p in paths:
        records = load(p)
        end = next((r for r in records if r["type"] == "run_end"), None)
        task = records[0]["task"] if records else "?"
        line = (summary_line(end["run_id"], end["outcome"], end["totals"]) if end
                else f"{p.stem}  incomplete")
        print(f"{line}\n    {_clip(task, 100, False)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="View agent run traces.")
    ap.add_argument("trace", nargs="?", help="path to a trace .jsonl file")
    ap.add_argument("--last", action="store_true", help="show the most recent run")
    ap.add_argument("--full", action="store_true", help="do not shorten long text")
    args = ap.parse_args()
    if args.last:
        paths = sorted(TRACE_DIR.glob("*.jsonl"))
        if not paths:
            sys.exit("No traces yet.")
        show(paths[-1], args.full)
    elif args.trace:
        show(args.trace, args.full)
    else:
        list_runs()
