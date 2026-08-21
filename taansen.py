"""Taansen - low-effort typing assistant for wrist-pain relief.

F9 = suggest what to type next (context-aware). F8 = Windows voice typing.
In the popup: Enter inserts suggestion 1, "2"/"3" pick others,
typing anything else + Enter regenerates with that correction, Esc dismisses.
Eye trackers / smart glasses can stream gaze JSON to UDP 127.0.0.1:5599.
Everything is logged to logs/*.jsonl and recent accept/reject feedback is
fed back into the prompt so suggestions improve with use.
"""
import ctypes
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import tkinter as tk
from collections import deque
from datetime import date, datetime
from pathlib import Path

import urllib.request

import keyboard
import mouse

APP_DIR = Path(__file__).parent
LOG_DIR = APP_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

CONFIG_FILE = APP_DIR / "config.json"
# "openai" type = OpenAI-compatible /chat/completions: Ollama, LM Studio, OpenAI,
# Groq, Mistral, Gemini-compat, vLLM... same protocol, so any of them works.
DEFAULT_CONFIG = {
    "provider": "ollama",
    "providers": {
        "ollama": {"type": "openai", "base_url": "http://localhost:11434/v1", "model": "qwen3:1.7b", "api_key": ""},
        "lmstudio": {"type": "openai", "base_url": "http://localhost:1234/v1", "model": "", "api_key": ""},
        "claude": {"type": "anthropic", "model": "claude-opus-5"},
        "openai": {"type": "openai", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "api_key": "env:OPENAI_API_KEY"},
    },
    # local speech-to-text (F8); model: tiny/base/small - bigger = slower + more accurate
    "speech": {"model": "base", "language": ""},
}


def load_config():
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
    return {**DEFAULT_CONFIG, **json.loads(CONFIG_FILE.read_text(encoding="utf-8"))}


CONFIG = load_config()
SUGGEST_KEY = "f9"
VOICE_KEY = "f8"
GAZE_PORT = 5599

SYSTEM = """You are Taansen, a typing minimizer for a user with wrist pain (RSI). \
Given a snapshot of what they are doing, predict the text they intend to type next, \
so a single keypress inserts it.

Return ONLY JSON:
{"suggestions": ["<text to insert at the caret, exactly as it should be typed>", "...up to 3, best first"],
 "next_task": "<one short line: the follow-up action they will likely want next, or null>"}

Rules:
- NEVER repeat text already present in field_text or typed_recent. Output only the NEW text to append after it. Example: field_text ends "I will send the" -> suggestion " report by Friday." (not "I will send the report...").
- Continue seamlessly from the end of the typed/field text: match language, tone and casing; include a leading space if one is needed.
- Even a single typed word is a cue: expand it into the full phrase, sentence or message they likely intend.
- Selected/highlighted text, mouse activity and gaze show what they are attending to right now.
- Prefer complete, immediately usable text (a full sentence, reply, command) over fragments.
- recent_feedback shows this user's history: imitate what they accepted, avoid what they rejected, honor corrections.
- user_profile is their learned long-term style; follow it unless the current context clearly differs.
- If user_correction is present, regenerate following it exactly."""

client = None
events = queue.Queue()
popup = None
key_events = deque(maxlen=4000)
mouse_stats = {"clicks": deque(maxlen=200), "drags": deque(maxlen=50), "_down": None}
gaze = {"last": None}
log_lock = threading.Lock()

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


# ---------- logging / learning ----------

def log_event(event, data=None):
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), "event": event, **(data or {})}
    with log_lock:
        with open(LOG_DIR / f"{date.today()}.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def iter_feedback(last_files=None):
    files = sorted(LOG_DIR.glob("*.jsonl"))
    if last_files:
        files = files[-last_files:]
    for f in files:
        for line in f.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("event") in ("accepted", "rejected", "corrected"):
                yield {k: e[k] for k in ("event", "app", "typed_recent", "text", "correction") if k in e}


def recent_feedback(n=8):
    """Last accepted/rejected/corrected entries -> few-shot signal in the prompt."""
    return list(iter_feedback(last_files=2))[-n:]


# ---------- long-term learning: distilled style profile ----------

PROFILE_FILE = APP_DIR / "profile.json"
DISTILL_EVERY = 25  # new feedback entries between profile refreshes

DISTILL_PROMPT = """You maintain the long-term style profile of one user for a typing predictor.
Rewrite the profile using the current profile plus new evidence (their accepted/rejected/corrected
suggestions). Max 20 bullet lines. Cover only what the evidence supports: tone and phrasing per app
(email vs chat vs code), languages used, sign-offs, recurring names/projects/tasks, patterns in what
they reject or correct. Output ONLY the bullet lines, no preamble."""


def load_profile():
    try:
        return json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"profile": "", "trained_on": 0}


def distill_if_due():
    """Re-distill the style profile from logs when enough new feedback accumulated."""
    entries = list(iter_feedback())
    prof = load_profile()
    if len(entries) - prof["trained_on"] < DISTILL_EVERY:
        return
    try:
        text = chat_raw(DISTILL_PROMPT, json.dumps(
            {"current_profile": prof["profile"], "evidence": entries[-300:]},
            ensure_ascii=False), max_tokens=500).strip()
        if text:
            PROFILE_FILE.write_text(json.dumps(
                {"profile": text, "trained_on": len(entries),
                 "updated": datetime.now().isoformat(timespec="seconds")},
                ensure_ascii=False, indent=1), encoding="utf-8")
            log_event("profile_distilled", {"trained_on": len(entries)})
    except Exception as ex:
        log_event("profile_distill_failed", {"error": str(ex)[:200]})


# ---------- context capture ----------

def foreground_window():
    hwnd = user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(256)
    user32.GetWindowTextW(hwnd, buf, 256)
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    exe = ""
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        size = ctypes.c_ulong(512)
        pbuf = ctypes.create_unicode_buffer(512)
        if kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
            exe = os.path.basename(pbuf.value)
        kernel32.CloseHandle(h)
    return hwnd, buf.value, exe


def read_focused_text():
    """Field text + current selection via UI Automation; empty strings when the app doesn't expose them."""
    try:
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread():
            ctrl = auto.GetFocusedControl()
            field, sel = "", ""
            tp = ctrl.GetPattern(auto.PatternId.TextPattern)
            if tp:
                try:
                    field = tp.DocumentRange.GetText(4000) or ""
                except Exception:
                    pass
                try:
                    ranges = tp.GetSelection()
                    if ranges:
                        sel = ranges[0].GetText(2000) or ""
                except Exception:
                    pass
            if not field:
                vp = ctrl.GetPattern(auto.PatternId.ValuePattern)
                if vp:
                    field = vp.Value or ""
            return field[-4000:], sel[:2000]
    except Exception:
        return "", ""


def on_key(e):
    if e.event_type == "down":
        key_events.append(e)


def typed_tail():
    """Reconstruct recently typed text from the key log (shift/backspace aware)."""
    try:
        parts = list(keyboard.get_typed_strings(list(key_events), allow_backspace=True))
    except Exception:
        parts = []
    return " ".join(p for p in parts if p.strip())[-1500:]


def on_mouse(e):
    # ponytail: only button events processed; moves ignored, position read on demand
    if isinstance(e, mouse.ButtonEvent) and e.button == "left":
        pos = mouse.get_position()
        if e.event_type == "down":
            mouse_stats["_down"] = (pos, e.time)
        elif e.event_type == "up":
            mouse_stats["clicks"].append(pos)
            d = mouse_stats["_down"]
            if d and abs(pos[0] - d[0][0]) + abs(pos[1] - d[0][1]) > 40:
                mouse_stats["drags"].append({"from": d[0], "to": pos})
                log_event("drag_highlight", {"from": d[0], "to": pos})


def gaze_listener():
    """Smart glasses / eye trackers stream JSON like {"x":512,"y":300} to UDP 5599."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("127.0.0.1", GAZE_PORT))
    except OSError:
        return
    while True:
        try:
            data, _ = s.recvfrom(4096)
            gaze["last"] = {**json.loads(data.decode("utf-8")), "t": time.time()}
        except Exception:
            pass


def gather_context():
    hwnd, title, exe = foreground_window()
    field, sel = read_focused_text()
    g = gaze["last"]
    return {
        "_hwnd": hwnd,
        "app": exe,
        "window_title": title,
        "field_text": field,
        "typed_recent": typed_tail(),
        "selection": sel,
        "mouse": {
            "pos": list(mouse.get_position()),
            "recent_clicks": len(mouse_stats["clicks"]),
            "recent_highlights": list(mouse_stats["drags"])[-3:],
        },
        "gaze": g if g and time.time() - g["t"] < 5 else None,
        "time": datetime.now().strftime("%A %H:%M"),
        "recent_feedback": recent_feedback(),
        "user_profile": load_profile()["profile"],
    }


# ---------- Claude ----------

def parse_response(text):
    m = re.search(r"\{.*\}", text.strip(), re.S)
    if m:
        try:
            d = json.loads(m.group(0))
            subs = [s for s in d.get("suggestions", []) if isinstance(s, str) and s]
            return {"suggestions": subs[:3], "next_task": d.get("next_task")}
        except json.JSONDecodeError:
            pass
    t = text.strip()
    return {"suggestions": [t] if t else [], "next_task": None}


def resolve_key(spec):
    return os.environ.get(spec[4:], "") if spec.startswith("env:") else spec


def ollama_default_model(base_url):
    """Ollama with no model configured -> use the first locally pulled model."""
    tags_url = base_url.rsplit("/v1", 1)[0] + "/api/tags"
    with urllib.request.urlopen(tags_url, timeout=3) as r:
        models = json.loads(r.read()).get("models", [])
    return models[0]["name"] if models else ""


def post_json(url, payload, api_key=""):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + api_key} if api_key else {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def chat_raw(system, user, max_tokens=700):
    """One text completion on the configured provider. Raises on failure."""
    p = CONFIG["providers"][CONFIG["provider"]]
    if p["type"] == "anthropic":
        global client
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        resp = client.beta.messages.create(
            model=p["model"],
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "low"},  # ponytail: latency over depth; raise if quality lacks
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if resp.stop_reason == "refusal":
            raise RuntimeError("model declined this context")
        return "".join(b.text for b in resp.content if b.type == "text")

    is_ollama = "11434" in p["base_url"]
    if not p["model"] and is_ollama:
        p["model"] = ollama_default_model(p["base_url"])
    if not p["model"]:
        raise RuntimeError(f"no model set for provider (edit {CONFIG_FILE.name})")
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if is_ollama:
        # native API so we can turn thinking off - a reasoning model musing for
        # a minute defeats the point of a typing suggester
        data = post_json(p["base_url"].rsplit("/v1", 1)[0] + "/api/chat", {
            "model": p["model"], "messages": messages, "stream": False,
            "think": False, "options": {"num_predict": max_tokens}})
        return data["message"]["content"] or ""
    data = post_json(p["base_url"].rstrip("/") + "/chat/completions",
                     {"model": p["model"], "max_tokens": max_tokens, "messages": messages},
                     resolve_key(p.get("api_key", "")))
    return data["choices"][0]["message"]["content"] or ""


def ask_llm(ctx):
    # ponytail: 300 tokens fits 3 suggestions + next_task; benchmarks showed
    # output length, not model size, is the main local-latency lever
    return parse_response(chat_raw(SYSTEM, json.dumps(ctx, ensure_ascii=False), max_tokens=300))


def suggest_worker(correction=None, prev_ctx=None):
    t0 = time.time()
    ctx = prev_ctx or gather_context()
    if correction:
        ctx["user_correction"] = correction
    sendable = {k: v for k, v in ctx.items() if not k.startswith("_")}
    try:
        result = ask_llm(sendable)
    except Exception as ex:
        result = {"suggestions": [], "next_task": None,
                  "error": f"{CONFIG['provider']}: {str(ex)[:180]}"}
    result["latency_s"] = round(time.time() - t0, 2)
    log_event("suggested", {
        "app": ctx.get("app"), "typed_recent": ctx.get("typed_recent", "")[-200:],
        "correction": correction,
        **{k: result.get(k) for k in ("suggestions", "next_task", "error", "latency_s")},
    })
    events.put(lambda: show_popup(ctx, result))


# ---------- local voice typing (F8) ----------

rec = {"stream": None, "chunks": [], "model": None, "indicator": None}


def get_whisper():
    if rec["model"] is None:
        from faster_whisper import WhisperModel
        rec["model"] = WhisperModel(CONFIG["speech"]["model"], device="cpu", compute_type="int8")
    return rec["model"]


def show_indicator(text):
    hide_indicator()
    w = tk.Toplevel(root)
    rec["indicator"] = w
    w.overrideredirect(True)
    w.attributes("-topmost", True)
    tk.Label(w, text=text, bg="#1e1e2e", fg="#f38ba8",
             font=("Segoe UI", 10, "bold"), padx=10, pady=4).pack()
    w.geometry(f"+{w.winfo_screenwidth() - 280}+{w.winfo_screenheight() - 120}")


def hide_indicator():
    if rec["indicator"]:
        rec["indicator"].destroy()
        rec["indicator"] = None


def voice_start():
    import sounddevice as sd
    rec["chunks"] = []
    rec["stream"] = sd.InputStream(
        samplerate=16000, channels=1, dtype="float32",
        callback=lambda data, *a: rec["chunks"].append(data.copy()))
    rec["stream"].start()
    log_event("voice_start")
    events.put(lambda: show_indicator("REC - F8 to stop"))


def voice_stop():
    stream, chunks = rec["stream"], rec["chunks"]
    rec["stream"] = None
    stream.stop()
    stream.close()
    events.put(lambda: show_indicator("transcribing..."))

    def work():
        try:
            import numpy as np
            audio = np.concatenate(chunks)[:, 0] if chunks else None
            if audio is None or len(audio) < 8000:  # <0.5s = accidental tap
                return
            segments, _ = get_whisper().transcribe(
                audio, beam_size=1, vad_filter=True,
                language=CONFIG["speech"]["language"] or None)
            text = " ".join(s.text.strip() for s in segments).strip()
            if text:
                keyboard.write(text + " ", delay=0.005)
            log_event("voice_typed", {"text": text, "seconds": round(len(audio) / 16000, 1)})
        except Exception as ex:
            log_event("voice_failed", {"error": str(ex)[:200]})
        finally:
            events.put(hide_indicator)
    threading.Thread(target=work, daemon=True).start()


def toggle_voice():
    if rec["stream"]:
        voice_stop()
        return
    try:
        voice_start()
    except Exception as ex:
        # ponytail: no mic / missing deps -> Windows' own voice typing (Win+H) still works
        log_event("voice_fallback", {"error": str(ex)[:200]})
        keyboard.send("windows+h")


# ---------- UI ----------

def show_popup(ctx, result):
    global popup
    if popup:
        popup.destroy()
        popup = None
    p = tk.Toplevel(root)
    popup = p
    p.overrideredirect(True)
    p.attributes("-topmost", True)
    x, y = root.winfo_pointerxy()
    p.geometry(f"+{max(0, min(x, p.winfo_screenwidth() - 580))}+{min(y + 18, p.winfo_screenheight() - 250)}")
    frame = tk.Frame(p, bg="#1e1e2e", bd=1, relief="solid")
    frame.pack()

    subs = result["suggestions"]
    if result.get("error"):
        tk.Label(frame, text="! " + result["error"], bg="#1e1e2e", fg="#f38ba8",
                 wraplength=540, justify="left").pack(anchor="w", padx=8, pady=4)
    for i, s in enumerate(subs):
        disp = s if len(s) <= 220 else s[:217] + "..."
        tk.Label(frame, text=f"[{i + 1}] {disp}", bg="#1e1e2e", fg="#cdd6f4",
                 wraplength=540, justify="left", font=("Segoe UI", 10)).pack(anchor="w", padx=8, pady=2)
    if result.get("next_task"):
        tk.Label(frame, text="-> next: " + str(result["next_task"]), bg="#1e1e2e",
                 fg="#89b4fa", wraplength=540, justify="left").pack(anchor="w", padx=8, pady=2)
    entry = tk.Entry(frame, bg="#313244", fg="#cdd6f4", insertbackground="#cdd6f4", relief="flat")
    entry.pack(fill="x", padx=8, pady=6)
    tk.Label(frame, text="Enter=insert #1 - 2/3=pick - type a fix+Enter - Esc=close",
             bg="#1e1e2e", fg="#6c7086", font=("Segoe UI", 8)).pack(pady=(0, 4))

    def close(reason=None):
        global popup
        p.destroy()
        popup = None
        if reason:
            log_event(reason, {"app": ctx.get("app"),
                               "typed_recent": ctx.get("typed_recent", "")[-200:],
                               "text": subs[0] if subs else None})

    def accept(i):
        if i >= len(subs):
            return
        text = subs[i]
        close()
        log_event("accepted", {"app": ctx.get("app"),
                               "typed_recent": ctx.get("typed_recent", "")[-200:], "text": text})
        threading.Thread(target=distill_if_due, daemon=True).start()
        hwnd = ctx.get("_hwnd")

        def type_it():
            # ponytail: SetForegroundWindow can be denied by Windows focus rules; works
            # in practice since we just held focus - add AttachThreadInput dance if it fails
            if hwnd:
                user32.SetForegroundWindow(hwnd)
            time.sleep(0.15)
            keyboard.write(text, delay=0.005)
        threading.Thread(target=type_it, daemon=True).start()

    def on_enter(_):
        v = entry.get().strip()
        if v in ("", "1"):
            accept(0)
        elif v in ("2", "3"):
            accept(int(v) - 1)
        else:
            close()
            log_event("corrected", {"app": ctx.get("app"), "correction": v})
            threading.Thread(target=suggest_worker, args=(v, ctx), daemon=True).start()

    entry.bind("<Return>", on_enter)
    p.bind("<Escape>", lambda e: close("rejected"))
    p.after(50, lambda: (p.focus_force(), entry.focus_set()))


def pump():
    try:
        while True:
            events.get_nowait()()
    except queue.Empty:
        pass
    root.after(50, pump)


# ---------- selftest ----------

def selftest():
    assert parse_response('{"suggestions": ["hi"], "next_task": null}') == {"suggestions": ["hi"], "next_task": None}
    assert parse_response('```json\n{"suggestions": ["a","b","c","d"], "next_task": "x"}\n```')["suggestions"] == ["a", "b", "c"]
    assert parse_response("plain text")["suggestions"] == ["plain text"]
    assert parse_response("")["suggestions"] == []
    assert parse_response('{"suggestions": [1, "ok"]}')["suggestions"] == ["ok"]
    assert "speech" in CONFIG and CONFIG["speech"]["model"]
    assert load_profile()["trained_on"] >= 0
    assert isinstance(recent_feedback(), list)
    log_event("selftest", {})
    assert (LOG_DIR / f"{date.today()}.jsonl").exists()
    print("selftest OK")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        selftest()
        sys.exit(0)
    root = tk.Tk()
    root.withdraw()
    keyboard.hook(on_key)
    mouse.hook(on_mouse)
    keyboard.add_hotkey(SUGGEST_KEY, lambda: threading.Thread(target=suggest_worker, daemon=True).start(), suppress=True)
    keyboard.add_hotkey(VOICE_KEY, toggle_voice, suppress=True)
    threading.Thread(target=gaze_listener, daemon=True).start()
    def preload_stt():
        try:
            get_whisper()
        except Exception as ex:
            log_event("stt_preload_failed", {"error": str(ex)[:200]})
    threading.Thread(target=preload_stt, daemon=True).start()
    threading.Thread(target=distill_if_due, daemon=True).start()
    log_event("started", {"provider": CONFIG["provider"]})
    print(f"Taansen running [{CONFIG['provider']}]. {SUGGEST_KEY.upper()}=suggest  "
          f"{VOICE_KEY.upper()}=local voice typing  gaze UDP on 127.0.0.1:{GAZE_PORT}  "
          f"logs in {LOG_DIR}  Ctrl+C to quit.")
    pump()
    root.mainloop()
