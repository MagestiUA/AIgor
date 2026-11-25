# Contributing to AIgor

Thanks for looking! AIgor is a small personal project, so keep changes focused and explain the *why* in the PR.

## Setup

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python main.py
```

You need a model: local [Ollama](https://ollama.com) (default `hf.co/unsloth/Qwen3.8-27B-GGUF:UD-Q3_K_XL`) or any
OpenAI-compatible server. Set it in Settings → LLM connection.

## Tests

```powershell
$env:PYTHONIOENCODING="utf-8"; .venv\Scripts\python tests\run_all.py -j 4
```

- Tests are plain scripts (`tests/test_*.py`) with stub models, so they need no GPU or network.
- **Never touch the real profile** (`~/.local-code-agent`): point storage paths (sessions, attachments, topics,
  project map) at a temp dir, as the existing tests do.
- Tests run on `limits.production()`, the limits the app really uses. Use `limits.LOCAL` only to test weak-model mode.

To check behaviour on a real model without the UI, run a task through the same code path as the app:

```powershell
.venv\Scripts\python scripts\run_task.py --root D:\copy-of-project "your task"
```

## Where things live

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) first. In short:

- `agent/` is the core and must stay UI-free.
- `lca_web/` is the Reflex UI plus thin adapters.
- Prefer improving the shared loop (`agent/steploop.py`) and tools (`agent/toolkit.py`) over adding mode-specific code.

## Style

- Comments and docstrings are mostly Ukrainian. English is fine in new code; don't mix languages within one comment.
- Workarounds for weak models go behind `weak_model_fixes` and need a live test that shows they help
  (see [docs/uk/CRUTCHES.md](docs/uk/CRUTCHES.md)).
- Keep line endings and indentation of files you touch.

## Issues

For bugs, attach a diagnostics archive (Settings → Diagnostics). It contains logs and settings **without** API keys
or chat contents. Also include the version from the right panel and what you expected versus what happened.
