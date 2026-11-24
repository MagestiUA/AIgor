# local-code-agent ("Igor")

*Читати українською — [README.md](README.md).*

A coding agent for **open models**, including weak ones: locally — Qwen3.8-27B via Ollama (16 GB VRAM), on a
server — any OpenAI-compatible API (vLLM on an H100, DeepSeek, etc.). The model can work with a large project and
large files because context is not kept "in the model's head": it is split between the planner and executors, moved
into files, compressed without big losses, and the original can always be fetched with a tool.

## Idea

- **Planner** — one structured call both decides the request type (answer / just run / clarify / plan) and splits
  the task into narrow steps.
- **Steps** — a separate tool-loop per step with a narrow context: the model calls tools itself, the runtime executes
  them and feeds results back. After every step the planner sees **exact facts** (command → output, changed files)
  collected by code, not the model's retelling, and adjusts the plan; at the end — verification (syntax +
  consistency) and a summary.
- **One loop** (`agent/steploop.py`) for code steps, chat, analysis and the sub-agent: the same safeguards (loop
  detection — `LoopGuard`, read-only streaks, compaction of old results, forced conclusion).
- **Context in files, compaction with the original's address.** Full command output goes to a file (only head and
  tail in context), the conversation lives in the session file (`recall_history`), session notes and chat topics are
  files (`recall`), the project map lives in the user profile. A compacted old tool result carries the exact address
  of the original (`read_file(path, start_line=…)`, the output file path). The conversation summary is "lazy": recent
  turns are kept verbatim, only what falls out of the window is compressed.
- **Search instead of reading whole files**: `search` (path:line: text) + `read_file(path, start_line, limit)`.
- **Strip/Restore** — large opaque literals (protobufs, keys) are replaced with placeholders *before* the model and
  restored byte-for-byte *after*: the model never sees them and can't corrupt them.
- **Research sub-agent** with a clean context: raw outputs stay inside it, the executor gets only the conclusion.

## Tools

| Tool | Purpose |
|---|---|
| `search` | find lines in files by text/regex (instead of grep in the shell) |
| `read_file` | read a project file or an attachment (by name); large ones in line chunks |
| `list_dir` | file structure |
| `replace_in_file` | targeted replacement (old → new), no nested model call |
| `edit_file` | change described in words — a separate model returns SEARCH/REPLACE (with strip/restore) |
| `write_file` | create / overwrite a file (indentation and line-ending style preserved) |
| `create_from_source` | a NEW file as a copy of a source (with strip/restore) |
| `run_shell` | PowerShell in the project root; long output goes to a file |
| `investigate` | research sub-agent (read-only) |
| `remember` / `recall` | session notes; `recall` also reads topics of past conversations |
| `recall_history` | search the full conversation history |
| `project_note` | a note about a file in the "Project map" |
| `web_search` | web search (DuckDuckGo via `ddgs`, no key) |

File tools are confined to the project root and added folders; service folders (command output, attachments,
topics, notes) are read-only.

## Permissions

- **edits** (`ask` / `auto`) — `ask` shows the plan and waits for approval; `auto` executes immediately (and an error
  log is treated as "fix it").
- **shell** (`smart` / `ask` / `auto` / `allowlist` / `off`): `smart` (default) — safe commands run, potentially
  destructive ones (see `shell_guard.py`) wait for confirmation.

## Layout

```
agent/
  steploop.py     the single "model → tools" loop core (hooks for UI / headless / sub-agent)
  agent_loop.py   executor prompt, step-history compaction, safeguards, run_step (headless)
  plan_runner.py  plan execution: steps, planner review, verification, summary (PlanUI — environment interface)
  headless.py     the agent without UI (HeadlessUI, CountingClient) — for task runs
  planner.py      decision: answer / shell / clarify / choose / plan; steps act / note
  intent.py       unambiguous routing without the model
  stepreview.py   step facts report, plan review, verification, execution summary
  toolkit.py      tool registry + ToolContext
  search.py       the search tool
  executor.py     edit_file / create_from_source (strip → model → restore → diff)
  patching.py     SEARCH/REPLACE with fuzzy matching and indentation alignment
  literals.py     strip / restore of large literals
  loopguard.py    loop detector
  convo.py        conversation summary (lazy), verbatim tail, digest of large attachments
  history.py      recall_history
  sessionmem.py   session notes;  topics.py — chat topics (shared between chats)
  projectmap.py   "Project map" (AST skeletons, ✓/⚠ marks)
  spill.py        large tool output → file
  limits.py       budgets scaled to the model window
  llm.py / llm_openai.py / backend.py   Ollama / OpenAI-compatible clients, backend choice
  session.py, attachments.py, settings.py, servers.py, logs.py, updater.py …
lca_web/          Reflex UI (state.py — state and adapters to the core, ui.py — layout)
scripts/          release.py (build & release), run_task.py (headless task runs with metrics)
```

## Weak-model mode

For other weak local models the settings have `weak_model_fixes`: executing tool calls "leaked" into text, nudges,
gathering without thinking + a separate synthesis. Off for Qwen3.8 and the server — it hurts there (see `CRUTCHES.md`).

## Requirements

- Python 3.12, `requests`, `reflex==0.9.5.post2`, `ddgs` (web search)
- Ollama with `hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q3_K_XL` (local) or an OpenAI-compatible server

## Ollama: q4_0 KV-cache + flash attention

`agent/llm.py` `_ensure_server` starts `ollama serve` with `OLLAMA_FLASH_ATTENTION=1` and `OLLAMA_KV_CACHE_TYPE=q4_0`
in its env — an almost-free context doubling and lower VRAM, for any model.

**Windows gotcha**: the tray app `ollama app.exe` has its own supervisor that restarts the server with its own
(stale) env block. So the `ollama app.exe` autostart is disabled — only our code starts the server (explicit `env=`
on `Popen`).

## Running

```powershell
python main.py
```

Run a task without the UI through the same path as the app (on a copy of the project), with metrics:

```powershell
.venv\Scripts\python.exe scripts\run_task.py --root D:\copy-of-project "add function X to a.py"
```

## Tests

All tests are offline (stub models, no GPU), on the same limits as the app:

```powershell
$env:PYTHONIOENCODING="utf-8"; .venv\Scripts\python.exe tests\run_all.py -j 4
```
