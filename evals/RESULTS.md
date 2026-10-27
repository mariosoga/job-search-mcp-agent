# Phase 6: guardrails, before and after

Same task set, same model (`claude-haiku-5-5`, effort `medium`), same frozen database
snapshot and date for every variant; 3 reps per task. Each variant changes exactly one
thing on top of the previous one. The baseline numbers are from [BASELINE.md](BASELINE.md)
plus 6 held-out tasks.

**Held-out tasks.** Before any fix was written, 6 new tasks were added (3 bulk-write
requests, 3 paging questions; tasks 41-46 in [TASKS.md](TASKS.md)) and run on the
baseline code. They were never used to design a fix, so they show whether a fix
generalizes beyond the task that exposed the failure.

## Variants

| Variant | Change | Files |
|---|---|---|
| baseline | Phase 5 agent and server | |
| v1 | `find_stale_applications` gains `offset`; the server rejects arguments a tool does not declare instead of silently dropping them | `queries.py`, `server.py` |
| v2 | v1 + one sentence in the `update_status` and `delete_application` descriptions: for a request covering more than one application, state how many would change and ask to confirm before calling | `server.py` |

## Results

| | baseline | v1 | v2 |
|---|---|---|---|
| **All 46 tasks** | 87.0% ± 9.4 | 90.6% ± 8.3 | **99.3% ± 1.4** |
| Original 40 tasks | 96.7% | 97.5% | 99.2% |
| 6 held-out tasks | 22.2% | 44.4% | **100.0%** |
| Cost per run | $0.0042 | $0.0027 | $0.0027 |
| Latency per run | 12.4 s | 8.6 s | 9.2 s |
| Tokens per run | 36.2k | 22.6k | 23.5k |
| Tool calls per run | 6.2 | 5.5 | 3.4 |
| Steps per run | 3.0 | 2.8 | 2.7 |
| Total cost (138 runs) | $0.58 | $0.37 | $0.38 |

Pass rates are means of per-task pass rates with a 95% CI over tasks.

## Failures by category (runs out of 138)

| Category | baseline | v1 | v2 | Fixed by |
|---|---|---|---|---|
| Mass action before confirming | 12 | 12 | **0** | v2 |
| Tool gap: no paging | 4 | **0** | 0 | v1 |
| Hand-counting error | 2 | 1 | 1 | not fixed |
| **Total** | 18 | 13 | 1 | |

- **Mass action before confirming.** Asked to "reject all January applications" or
  "delete every BuiltIn application", the baseline fired every write at once (8, 9, 33
  and 44 approval requests) and relied on the approval gate to stop it. After v2 it
  makes zero write attempts on these: it lists the scope ("27 January applications, 19
  already rejected, so 8 would change: ...") and asks. Single-item writes were
  unaffected: the 4 single-write tasks passed 12 of 12 in v2.
- **Tool gap: no paging.** `find_stale_applications` capped at 100 rows with no
  `offset`. The model passed `offset` anyway and the server silently ignored it, so it
  either reported a floor ("at least 45 companies") or rebuilt the missing rows by hand
  (about 33 tool calls). v1 fixed both: `trunc_stale_companies` now takes 3 tool calls.
  This is also where v1's cost (-36%) and latency (-31%) savings come from.
- **Hand-counting error (not fixed).** With every row in hand, the model sometimes
  counts distinct companies wrong by one or two (190 vs 191; 178 vs 179). It moves
  between tasks from run to run (`hold_page_stale90_companies` in baseline and v1,
  `trunc_sept_companies` in v2), so the 2 -> 1 -> 1 trend is noise, not a fix. The
  likely fix is a tool that counts distinct companies for a filter, so the model never
  counts by hand; it was not tried here.

## What each guardrail cost

| Guardrail | Pass rate effect | Cost effect | Latency effect |
|---|---|---|---|
| v1: paging + strict arguments | +3.6 pts overall, +22 pts held-out | -36% per run | -31% per run |
| v2: confirm before bulk writes | +8.7 pts overall, +56 pts held-out | none | +0.6 s (within noise) |

## Caveats

- One user's data, one model, 46 tasks. The held-out set is small (6 tasks x 3 reps).
- Graders are code checks. Each failing run in every variant was read by hand, and the
  grader was right each time after the two Phase 5 fixes.
- v2's rule was tested only where the eval stops after one turn. The eval does not
  check that the agent goes ahead once the user confirms.
- The approval-gate denials count as tool errors in the traces, which is why tool
  errors fall from 285 to 3 in v2. That reflects fewer denied attempts, not fewer bugs.
