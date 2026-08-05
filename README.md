> 中文版：[README.zh-CN.md](README.zh-CN.md)

# cassette

An AI companion that lives in your own pocket: a native iOS chat app plus a Python backend running on your own Mac, calling the model through the `claude` CLI's login session on that machine. No third-party server — your chat history lives only on your phone.

Give it a name, write it a persona, add a few stickers — and it will "wake up" on its own while you're not around, think about your recent conversation, and decide whether to message you first.

## What it does

- **Feels like a real chat app**: token-by-token streaming, Markdown rendering (code blocks with syntax highlighting and one-tap copy), edit/delete/regenerate on any message, custom avatars and display names, WeChat-style inverted scrolling with a new-message pill.
- **Wakes up on its own**: a backend scheduler wakes the model at the frequency you set. It reads the recent conversation and its own inner monologue from the last few wake-ups, then decides whether to say something or stay quiet. It can also set its own alarm ("wake me in 3 hours") — mention that you're going to bed and it will quietly schedule a wake-up for later.
- **Sends stickers**: add images from your photo library and the model writes a one-line description of each by looking at it; later it picks one that fits the mood and sends it to you (both in chat and when it wakes up), and it will rewrite a description it thinks is wrong. To ship a default set, drop PNGs into `ios/cassette/DefaultStickers/` — the filename becomes the initial description, seeded on first launch.
- **Has a sense of time**: it knows whether it's "late Wednesday night" or "Saturday morning", and that you took 3 hours to reply. All of it is injected into the prompt, so it won't wish you good night in the afternoon.
- **Doesn't lose messages**: lock your phone, background the app, or drop off the network mid-generation — the backend runs to completion, the reply goes into a pending outbox, and the app picks it up when it returns to the foreground; with [Bark](https://github.com/Finb/Bark) configured, your phone gets a push too. The reverse case is covered as well: if the request never reached the backend, the app reconciles with it and tells you to resend after about a minute; if that turn produced nothing at all, you get an explicit notice. A turn never just quietly disappears.

## Architecture

```
┌──────────────┐  HTTP + SSE   ┌──────────────────┐  subprocess  ┌──────────────┐
│   iOS app    │ ─────────────▶│  FastAPI backend │ ───────────▶ │  claude -p   │
│  (SwiftUI)   │ ◀─────────────│  (on your Mac)   │ ◀─────────── │ (CLI session)│
└──────────────┘  X-Auth key   └──────────────────┘  stream-json └──────────────┘
  owns the history   stateless + a little runtime state  one process per message
```

Three design principles:

1. **The app owns the chat history.** The backend keeps no conversation of its own — every request carries the recent history from the app (100 messages by default), and the backend assembles it into a one-shot prompt for the model. That's why editing, deleting and regenerating are purely local operations, and the history travels with the app.
2. **One throwaway `claude -p` subprocess per message.** No long-lived session; context comes from the injected history, not from a living process. The persona file is passed via `--system-prompt-file`, which **replaces** the default system prompt rather than appending to it — the model sees exactly the character you wrote and nothing else. Credentials come from the CLI login session (the subprocess environment has `ANTHROPIC_API_KEY` stripped): with a subscription account, both chat and wake-ups draw on your subscription quota and never incur metered API charges. If you'd rather use an API key, delete the line that strips it in `pipeline.py`.
3. **The backend stores only the minimum state needed to wake up** (`server/state/`, gitignored): a window snapshot of the recent conversation, the wake log, the wake schedule (its self-chosen next wake-up time, preserved across restarts), the pending outbox, the sticker catalog, and settings. When the app isn't around, this is what the model wakes up into.

## How waking up works

Saving tokens is the first principle: the scheduler ticks every 5 minutes, but **every pre-check is purely local** — roll the dice, check the active hours, check the quiet period, check the minimum interval. Only when it's really going to wake does a model process start.

On wake-up the model receives the recent conversation plus its own inner monologue from previous wake-ups (merged into a single timeline), and answers in a four-section protocol:

```
THOUGHTS: what it's actually thinking right now
ACTION:   none / message
CONTENT:  what to send (when ACTION=message)
NEXT:     when it would like to wake up again (may be "none")
```

Before anything is delivered there's a hard **interruption-control** gate: a daily message cap, a minimum interval between proactive messages, and a quiet period after you've just spoken. The gate blocks delivery, not thinking — and the wake-up prompt tells the model up front that "anything you send this round won't go out", so it never assumes a message was delivered when it wasn't. All of this is adjustable in the settings screen, or can be switched off entirely.

A few details:

- The model's own NEXT only guarantees "wake once at that time"; it doesn't suppress the random rhythm. The scheduler polls every 5 minutes, so "on time" can be up to 5 minutes late.
- If you happen to send a message during the tens of seconds a wake-up takes to generate, that proactive message is dropped entirely (it's talking about a world that no longer exists) and only logged.
- If a chat turn is in flight, the wake-up yields to the next tick rather than talking nonsense from a stale context.
- Repeated failures (an expired CLI login, say) trigger a 30-minute backoff instead of burning a doomed subprocess every tick.

## Getting started

You'll need: a Mac (the backend runs here), an iPhone (the frontend is an iOS app), the [claude CLI](https://claude.com/claude-code) installed and logged in (subscription quota only by default; an API key works too, but takes deleting one line — see Architecture, point 2), Python 3, and Xcode.

### 1. Backend

```bash
cd server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env        # at minimum, set CASSETTE_AUTH_KEY (any random string you choose)
.venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

`.env` also configures: the model (`CLAUDE_MODEL`, defaults to opus), timezone, persona file path, Bark push URL, and the wake scheduler's on/off switch and tick interval. Every personal setting lives only in `.env` and never enters the repo.

### 2. Persona

```bash
cp persona.example.md persona.md   # then rewrite it however you like
```

This file is its entire personality. It supports `{{AGENT_NAME}}` / `{{USER_NAME}}` placeholders (filled with the names you set in the app). It's read fresh on every call — no backend restart needed after an edit.

### 3. iOS app

First copy the connection config from its template (`Config.swift` is gitignored, so your real address and key stay out of the repo):

```bash
cd ios/cassette
cp Config.swift.example Config.swift
```

Then open `ios/cassette.xcodeproj` in Xcode and set two values in `Config.swift`:

- `authKey`: must match `CASSETTE_AUTH_KEY` in `.env`.
- `baseURL`: the simulator can use `127.0.0.1:8000` directly; on a real device use your Mac's LAN IP (same Wi-Fi), or a [Tailscale](https://tailscale.com) IP if you want it to work away from home.

Run it, name the two of you in the first-launch flow, and start talking.

## Project layout

```
server/
  app.py          # FastAPI routes (/health /chat /chat/stream /chat/active /pending
                  #   /pending/ack /settings /describe_sticker) + stream heartbeat,
                  #   disconnect-rescue guard
  config.py       # config loading: .env → constants; name resolution (app settings first,
                  #   env as fallback)
  pipeline.py     # prompt assembly, claude -p subprocess, time awareness, inline markers
  sse.py          # stream-json → SSE translation (marker filtering, idle-timeout detection)
  wake.py         # wake scheduler: local pre-gates → four-section protocol → interruption control
  state_store.py  # runtime state (plain files, atomic writes + locks)
  notify.py       # Bark push + timestamped error logging
  persona.example.md
ios/cassette/     # SwiftUI app: chat UI, sticker library, settings, local persistence
```

## Data and safety

- Chat history lives in the app sandbox at `Documents/chat_history.json`; the backend keeps only a shadow snapshot of the recent window (≤300 messages).
- Every endpoint except the `/health` check requires the `X-Auth` shared key; with no key configured they all refuse requests (fail closed), and the comparison is constant-time.
- The wake log at `server/state/wake_log.jsonl` is append-only. It holds the full inner monologue from every wake-up, including the text of messages that interruption control blocked from ever being sent, and it is never pruned (the pending outbox has a 7-day cleanup; this doesn't). It never leaves your Mac, but it is the single most intimate file in the project — worth knowing it exists.
- The model subprocess runs with `--tools ""`: no tool access at all, conversation only.
- Keep the backend on your LAN or a Tailscale network. Don't expose it raw to the public internet.

## Roadmap

- **Seamless chat ⇄ coding mode**: switch from `claude -p` chat mode straight into a coding session inside tmux — same persona, continuous memory and awareness — so you can have the Claude Code on your Mac run tasks for you from your phone.
- **Long-term memory**: integrate [Ombre-Brain](https://github.com/P0luz/Ombre-Brain) (P0luz's open-source memory system, mounted as an external service over MCP) — memories surfacing at the start of a session, saved on the fly, continuous across sessions.
- **Plugin ecosystem**: tool families (browser, web generation, and so on) as standalone plugin repos, mounted dynamically by the backend and installable from inside the app; the registry is a hardcoded allowlist, and install will never accept an arbitrary URL.
- **Web client**: the client is thin enough that it deserves a version that runs in a browser.

## Credits

The iOS app uses [MarkdownUI](https://github.com/gonzalezreal/swift-markdown-ui), [Highlightr](https://github.com/raspu/Highlightr) and [swift-markdown](https://github.com/swiftlang/swift-markdown); push notifications use [Bark](https://github.com/Finb/Bark).

## License

[MIT](LICENSE)
