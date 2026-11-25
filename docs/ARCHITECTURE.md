# AIgor: architecture

*Українські нотатки розробника: [docs/uk/CONCEPT.md](uk/CONCEPT.md), [docs/uk/CRUTCHES.md](uk/CRUTCHES.md).*

AIgor targets open models that are **weaker than the task**: a 27B model squeezed into 16 GB of VRAM, a 64k window, an
unreliable sense of when to stop. The design rule is to keep the model's job small at every moment. Everything else
(remembering, compressing, verifying, protecting data) is done by code and files.

## 1. Request flow

```
message
  ├─ quick_route (code, no model): "just a question" → answer; error log in auto-edit mode → plan
  ├─ "do it" after a proposal → expands into the full approved proposal → plan
  └─ planner.deliberate(route=True)  — ONE structured call (JSON schema):
       answer        → read-only loop (search / read_file / list_dir / recall / recall_history / web_search)
       shell         → a single executor step
       clarify|choose→ a question with clickable options; the typed reply continues planning
       plan          → steps (act | note) → PlanRunner
```

**PlanRunner** (`agent/plan_runner.py`, UI-independent; the UI is just a `PlanUI` adapter):

1. Each step runs in its own tool loop (`agent/steploop.py`) with a narrow context: stable parts first (project root,
   project map, conversation summary, session-note index, recent dialogue, attachments), then the task and the plan
   with the results of finished steps.
2. Every 10 tool calls the planner gets an interim report (`checkpoint`) and may redirect the step.
3. After the step the planner sees **exact facts** (`stepreview.facts_report`): each shell command with the head and
   tail of its output and exit code, changed files, files read. It may continue, revise pending steps, add steps or
   stop (at most 3 revisions per run, but user comments always get through).
4. A failed step (iteration cap, loop detected, context full) **stops the plan**. The plan is saved, and "Run"
   resumes from that step.
5. After the last step: a syntax check of changed `.py` files and an LLM consistency review, redoing at most one step
   per round for 2 rounds. Then a **final summary** of what was actually done.

You can type while a plan is running. The executor sees the comment before its next model call, and the planner sees
it at the next review.

## 2. The loop core and its safeguards

One coroutine `run_loop` drives code steps, chat/analysis and the research sub-agent. The differences live in
`LoopHooks` (dispatch with confirmations, streaming, activity cards, checkpoints) and in `LoopConfig` (profile,
thresholds, finalize text).

| Safeguard | What it does |
|---|---|
| `LoopGuard` | Detects lack of progress, not long work: the same action and result 3× in a row; 4 of the last 6 calls returning the same result; A-B-A-B cycles; 12 calls with no new information; 24 calls drawn from ≤3 distinct actions; the same action and result 5× within the window. First hit: the model is told to change approach. Second hit: the step stops with an explanation and a summary of what was done. |
| `ReadStreak` | 8 reads in a row with no action → "conclude now"; 12 → forced conclusion without tools. |
| Compaction | From 60% of the window, old tool results shrink to a 500-char head plus **the exact call that restores them** (`read_file(path, start_line=…)`, the output file, the same `search`). From 75%, the middle of the step history becomes a model-written summary; `__LIT_n__` placeholders are guaranteed to survive. |
| Context full | At 90% of the window a read-only step concludes from what it has; any other step stops with "split the task". |
| Iteration cap | `hard_iters` (60 per step) is only a fuse; progress is judged by `LoopGuard`. |

## 3. Context and memory

Everything the model may need later is a **file**. The prompt carries an index or a pointer, never the bulk.

| What | Where | How the model gets it |
|---|---|---|
| Long tool output | `~/.local-code-agent/tool-output/` | head + tail in context; `read_file` / `search` on the file |
| Full conversation | session JSON | `recall_history(query \| number)` |
| Conversation summary | session (`context_summary`, `summary_upto`) | in the prompt; recent turns after `summary_upto` stay verbatim |
| Session notes | `session-memory/<sid>/*.md` | index in the prompt; `remember` / `recall` |
| Chat topics (shared across chats) | `topics/*.txt` | list in the prompt; `recall` |
| Project map | `projects/<hash>/map.json` | in the prompt: AST skeletons with ✓ (read, unchanged) / ⚠ (changed since read) / ○ marks |
| Attachments | `attachments/<sid>/` | small ones inlined; others via `read_file(name)` (fuzzy match); huge ones in chunks with a digest |

Service folders are **read-only** for the model; writes are confined to the project root and folders the user added.
Budgets (`agent/limits.py`) scale with the model window, so the same rules apply to a 64k local model and a 256k
server.

**Lazy summary.** After each request nothing is summarized while the verbatim tail is small. When the tail outgrows
its limit, its oldest turns are folded into the summary in **one** call and the newest turns stay verbatim. The
summary itself is compressed only when it exceeds its budget.

## 4. Editing safely

- `replace_in_file(path, old, new)`: exact unique match, else whole-line match ignoring indentation (with hint
  "similar place: …"). Indentation of the replacement is re-aligned to the file. `.py` must still compile.
- `edit_file(path, instruction)`: a separate call returns SEARCH/REPLACE blocks that are applied deterministically,
  with one retry that explains what didn't match. If blocks still don't apply, files under 12k chars fall back to a full rewrite.
- `create_from_source(target, source, instruction)`: copies a file with changes. Literals are copied byte-for-byte.
- **Strip/restore** (Python files): string constants and commented data lines over 1000 chars become `__LIT_n__` placeholders
  before the model and are restored after. The model never retypes a key or a blob.
- Style is preserved (tabs vs spaces, CRLF vs LF). Every write leaves a backup in `~/.local-code-agent/backups/`.

## 5. Weak-model mode

`weak_model_fixes` (a setting) turns on workarounds that helped older local models (gemma4, Qwen3-Coder) but hurt a
strong one:

- executing tool calls the model "leaked" into plain text;
- nudging when the model only announces an intent;
- gathering with thinking off, then a separate synthesis;
- retrying on leaked self-talk.

Live tests on the server showed why they are off by default. In 12 "show me a JSON example" runs, the
leaked-call parser would have **executed 11** of them as `write_file`. The single-phase loop with thinking and tools
read all files correctly in 12/12 runs. Details are in [docs/uk/CRUTCHES.md](uk/CRUTCHES.md).

## 6. Backends

- **Ollama:** one model for every role, `num_ctx` from settings, flash attention + q4_0 KV cache when AIgor starts
  the server itself. Role profiles only switch thinking, temperature and DRY sampling.
- **OpenAI-compatible:**
  - vLLM extras (`chat_template_kwargs.enable_thinking`, `top_k`, `min_p`, `json_schema`);
  - a "strict" mode for plain APIs (DeepSeek: `thinking` param, `json_object` plus the schema in the prompt),
    switched on automatically after an HTTP 400;
  - patient retries while a rented server wakes from hibernation.

## 7. Measurements

**Context order vs vLLM prefix cache** (`scripts/bench_context_order.py`, H100, Qwen3.8-27B FP8, 6 steps, medians of
3 runs, prefill only):

| Scenario | Steps 2–6, old order (plan first) | New order (plan last) | Cached tokens |
|---|---|---|---|
| Typical prompt ≈ 9.7k tokens | 7.77 s | 5.00 s (−36%) | 23% → 79% |
| Long conversation ≈ 17k tokens | 12.38 s | 7.03 s (−43%) | 13% → 72% |

Only the first call of each step benefits: calls inside a step share their prefix either way.

**Local GPU** (RTX 5070 Ti 16 GB, Qwen3.8-27B UD-Q3_K_XL): fully on GPU up to a 64k window. Larger windows spill to
CPU and slow down sharply.

**End-to-end** (`scripts/run_task.py`, local model): "add `mul()` to calc.py, write a test and run it" became a
4-step plan, ran with 17 model calls in 48 s, and all steps were verified.

## 8. Known limitations

- The UI is Ukrainian-only. English localization is planned: model-facing texts first, then a uk/en switch for the
  UI and chat messages.
- Windows-first: `run_shell` is PowerShell 5.1.
- The verbatim tail is conservative (about 12 messages, each clipped), and compressing an oversized summary can be
  far more aggressive than the requested 50%. Both are being tuned.
- Not tested on macOS or Linux.
