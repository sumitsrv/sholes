# Taansen

Context-aware typing minimizer for Windows — built to reduce wrist strain.
Press one key; it reads what you're doing (focused text field, what you typed,
what you highlighted with the mouse, where you're looking) and offers the text
you were about to type. One more key inserts it.

## Setup

```
pip install -r requirements.txt
python taansen.py
```

Default brain is a **local model via Ollama** — `qwen3:1.7b` (1.4 GB, ~4 s per
suggestion; benchmarked as the smallest model that stays coherent). Speech uses
**Whisper base** (142 MB) fully on-device. No data leaves your machine unless
you pick a commercial provider in config.json.

## Keys

| Key | Action |
|-----|--------|
| **F9** | Suggest what to type next (popup appears at the mouse) |
| **Enter** in popup | Insert suggestion 1 |
| **2 / 3** + Enter | Insert that suggestion |
| type a fix + Enter | Regenerate ("shorter", "in Dutch", "more formal", …) |
| **Esc** | Dismiss (logged as rejected — the app learns from it) |
| **F8** | Local voice typing: tap to record, tap again to stop — Whisper transcribes on-device and types it. Falls back to Windows voice typing (Win+H) if mic/deps missing. |

## Providers — `config.json`

Set `"provider"` to any entry under `"providers"`:

- `ollama` (default, local), `lmstudio` (local) — model auto-detected for Ollama,
  set `"model"` for LM Studio.
- `claude` — needs `ANTHROPIC_API_KEY` env var (`setx ANTHROPIC_API_KEY sk-ant-...`).
- `openai` — needs `OPENAI_API_KEY`.
- **Any other service** that speaks the OpenAI `/chat/completions` protocol
  (Groq, Mistral, Gemini compat, vLLM, …): add an entry with
  `"type": "openai"`, its `base_url`, `model`, and `"api_key": "env:YOUR_VAR"`.

## Eye tracking / smart glasses

Stream gaze samples as JSON to **UDP 127.0.0.1:5599**, e.g. `{"x": 812, "y": 430}`.
When fresh samples (<5 s old) exist they're included in the context.

## Speech

F8 records locally and transcribes with faster-whisper (`"speech"` in
config.json: model `tiny`/`base`/`small`, optional fixed `language` like `"en"`).
Nothing leaves the machine. Windows' own voice typing stays available via Win+H.

## Logging & learning

Everything (suggestions, accepts, rejects, corrections, highlight-drags, voice)
goes to `logs/YYYY-MM-DD.jsonl`. Two learning loops:

1. **Immediate** — the last few accepted/rejected/corrected entries ride along
   in every prompt.
2. **Long-term** — every ~25 feedback events the app re-distills all history
   into a persistent style profile (`profile.json`: your tone per app,
   languages, sign-offs, recurring names/tasks) which is included in every
   prompt. So it keeps improving the more you use it.
Note: reconstructed recent typing is included in these logs — they stay local,
but treat the `logs/` folder as private.

## Sanity check

```
python taansen.py --selftest
```
