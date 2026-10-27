#!/usr/bin/env python3
"""
Command-line agent that answers job-search questions through the MCP server.

    python agent.py "Which applications have had no reply for 60+ days?"
    python agent.py --demo          # the five Phase 2 check tasks

The loop is written by hand (no framework, no SDK tool runner) so every step is
visible and can be traced later:
  1. launch server.py over stdio and list its tools
  2. send the task plus the tool definitions to Claude
  3. run any tool calls Claude asks for through the MCP client, return results
  4. repeat until Claude gives a final answer or the step limit is reached

Every run, failed ones included, writes a trace to traces/ (see tracing.py).

Needs ANTHROPIC_API_KEY, read from the environment or a gitignored .env file.
"""
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tracing import Tracer

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
DEFAULT_MAX_STEPS = 12
# On a safety decline the API re-runs the request on a fallback model it picks.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """You answer questions about the user's own job search using the
tools provided. They are the only source of truth: never state a fact that did not
come from a tool result. If a tool returns an error or `did_you_mean`, say so or
ask, rather than guessing. If `total` is larger than `returned`, the result is
partial: page with `offset` or tell the user the count you did not see. Call
get_today before any date reasoning. Keep the final answer short and concrete."""

DEMO_TASKS = [
    "What is my sponsorship status at Goldman Sachs, and how many applications have I sent there?",
    "How many applications are in each status?",
    "Which companies have I had interviews or assessments with in August 2026?",
    "What is my status at Micro?",
    "Find applications with no reply after 60 days where I have an accepted recruiter "
    "contact, and for the three oldest, list those recruiters by name.",
]


def tool_text(result):
    """Flatten an MCP CallToolResult into the string Claude sees."""
    parts = [c.text for c in result.content if getattr(c, "type", None) == "text"]
    return "\n".join(parts) if parts else "(no content)"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


async def call_tool(session, name, arguments):
    """Run one MCP tool call. Returns (result_text, is_error); never raises."""
    try:
        result = await session.call_tool(name, arguments)
    except Exception as e:  # server crashed or protocol error: still a result for the model
        return f"Tool call failed: {type(e).__name__}: {e}", True
    return tool_text(result), bool(result.isError)


async def run_task(client, session, tools, task, model, effort, max_steps, as_of):
    """Run one task to completion, tracing every step. Returns the finished Tracer."""
    tracer = Tracer(task, model, effort, as_of, SYSTEM_PROMPT, tools, max_steps)
    messages = [{"role": "user", "content": task}]
    # Stays "interrupted" only if something (e.g. Ctrl-C) stops the run mid-step.
    outcome, answer = "interrupted", None

    try:
        for step in range(1, max_steps + 1):
            t0 = time.monotonic()
            try:
                response = await client.beta.messages.create(
                    model=model,
                    max_tokens=16000,
                    system=SYSTEM_PROMPT,
                    tools=tools,
                    messages=messages,
                    output_config={"effort": effort},
                    betas=[FALLBACK_BETA],
                    fallbacks="default",
                )
            except anthropic.APIStatusError as e:
                outcome, answer = "errored", f"API error {e.status_code}: {e.message}"
                tracer.model_error(step, answer, (time.monotonic() - t0) * 1000)
                break
            except anthropic.APIConnectionError as e:
                outcome, answer = "errored", f"Network error: {e}"
                tracer.model_error(step, answer, (time.monotonic() - t0) * 1000)
                break
            tracer.model_call(step, response, (time.monotonic() - t0) * 1000)
            # Append the full content, not just text, so thinking blocks are preserved.
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "tool_use":
                results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    t0 = time.monotonic()
                    text, is_error = await call_tool(session, block.name, block.input)
                    ms = (time.monotonic() - t0) * 1000
                    tracer.tool_call(step, block.name, block.input, text, is_error, ms)
                    log(f"  [{step}] {block.name}({json.dumps(block.input)}) "
                        f"-> {'ERROR ' if is_error else ''}{len(text)} chars, {ms:.0f} ms")
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": text, "is_error": is_error})
                # All results go back in one user message, so parallel calls stay parallel.
                messages.append({"role": "user", "content": results})
                continue

            answer = "\n".join(b.text for b in response.content if b.type == "text")
            if response.stop_reason == "end_turn":
                outcome = "finished"
            elif response.stop_reason == "refusal":
                outcome = "refused"
            else:  # max_tokens, pause_turn, etc.
                outcome = response.stop_reason
            break
        else:
            outcome = "step_limit"
    except Exception as e:
        outcome, answer = "errored", f"{type(e).__name__}: {e}"
    finally:
        tracer.end(outcome, answer)
    return tracer


async def main(tasks, model, effort, max_steps):
    client = anthropic.AsyncAnthropic()
    # Freeze "today" for the whole run so the server and the trace agree on it.
    as_of = os.environ.get("JOBSEARCH_AS_OF") or date.today().isoformat()
    params = StdioServerParameters(command=sys.executable, args=[str(HERE / "server.py")],
                                   env={**os.environ, "JOBSEARCH_AS_OF": as_of})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = (await session.list_tools()).tools
            tools = [{"name": t.name, "description": t.description or "",
                      "input_schema": t.inputSchema} for t in listed]
            log(f"Connected: {len(tools)} tools, model {model}, effort {effort}, as_of {as_of}")

            for i, task in enumerate(tasks, 1):
                log(f"\n=== Task {i}/{len(tasks)}: {task}")
                tr = await run_task(client, session, tools, task, model, effort, max_steps, as_of)
                print(f"\n--- Task {i} answer ({tr.outcome}) ---\n{tr.answer}")
                log(f"  {tr.summary()}\n  trace: {tr.path.relative_to(HERE)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("task", nargs="?", help="the question or task for the agent")
    ap.add_argument("--demo", action="store_true", help="run the five Phase 2 check tasks")
    ap.add_argument("--model", default=os.environ.get("JOBSEARCH_MODEL", DEFAULT_MODEL))
    ap.add_argument("--effort", default=DEFAULT_EFFORT,
                    choices=["low", "medium", "high", "xhigh", "max"])
    ap.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    args = ap.parse_args()

    if not args.task and not args.demo:
        ap.error("give a task, or --demo")
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        sys.exit("No ANTHROPIC_API_KEY found. Create one in the Claude Console and put "
                 "ANTHROPIC_API_KEY=... in a .env file next to agent.py.")
    asyncio.run(main(DEMO_TASKS if args.demo else [args.task],
                     args.model, args.effort, args.max_steps))
