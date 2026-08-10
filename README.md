> 中文版：[README.zh-CN.md](README.zh-CN.md)

# cassette

An AI companion that lives in your own pocket: a native iOS chat app plus a Python backend running on your own Mac, calling the model through the `claude` CLI's login session on that machine. No third-party server — your chat history lives only on your phone.

Give it a name, write it a persona, add a few stickers — and it will "wake up" on its own while you're not around, think about your recent conversation, and decide whether to message you first.

## What it does

- **Feels like a real chat app**: token-by-token streaming, Markdown rendering (code blocks with syntax highlighting and one-tap copy), edit/delete/regenerate on any message, custom avatars and display names, WeChat-style inverted scrolling with a new-message pill.
- **Wakes up on its own**: a backend scheduler wakes the model at the frequency you set. It reads the recent conversation and its own inner monologue from the last few wake-ups, then decides whether to say something or stay quiet. It can also set its own alarm ("wake me in 3 hours") — mention that you're going to bed and it will quietly schedule a wake-up for later.
- **Long-term memory (optional)**: integrates [Ombre-Brain](https://github.com/P0luz/Ombre-Brain) (P0luz's open-source memory system, a self-hosted Docker service) — relevant memories surface at the start of a conversation, things worth keeping get saved on the fly, and wake-ups think with memory too; what it stored or edited shows up as gray hints in chat. Without Ombre running it falls back to plain chat, business as usual.
- **Sends stickers**: add images from your photo library and the model writes a one-line description of each by looking at it; later it picks one that fits the mood and sends it to you (both in chat and when it wakes up), and it will rewrite a description it thinks is wrong. To ship a default set, drop PNGs into `ios/cassette/DefaultStickers/` — the filename becomes the initial description, seeded on first launch.
- **Code mode (off by default)**: flip one switch in the header and the same companion moves from `claude -p` chat into a live interactive `claude` session in tmux on your Mac — same persona, the conversation carried over verbatim, and now with the whole computer in hand. A terminal panel slides up inline above the input bar so you can watch it work and answer permission prompts without leaving the chat. See [Code mode](#code-mode) — it opens a door the rest of this project keeps shut, so read that section before enabling it.
- **Plugin store**: tool families live in their own small repos, not in this one — download/enable/uninstall with one tap in the app (the backend mounts them dynamically; toggling takes effect on the next turn). The first plugin, **webpage**, lets the companion build and edit self-contained HTML pages you can open from Chat history → HTML files. **browser** hands them a real, headful Chrome on your Mac with a persistent profile — sites you log into in that window stay logged in for them (backed by a resident playwright-mcp service you set up once with the plugin repo's `setup.sh`; the chat shows a tappable "browsed N pages" note that unfolds into the URLs). The registry is a hardcoded allowlist; there is no install-from-URL.
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

## Code mode

Everywhere else in this project the model runs with a per-tool allowlist and no built-in tools.
Code mode is the deliberate exception: it starts a long-lived interactive `claude` inside a tmux
session on your Mac, with Bash, file writes, the lot. That's the point of it — and the reason it
ships **off**. Turn it on in `server/.env` (`CODE_MODE_ENABLED=1`, needs `tmux` installed);
until you do, the app doesn't even show the switch.

How it hangs together:

```
chat mode:  app → POST /chat/stream → claude -p                (one throwaway process per message)
code mode:  app → POST /code/send   → tmux send-keys → live claude session
            replies ← session hooks → POST /code/append → pending outbox → the app's normal polling
```

Memory stays continuous for free. Switching in injects the same recent history the chat path
sends, so the session opens knowing exactly what you were just talking about; and because its
replies flow back through the ordinary outbox into the app's history, switching back means the
next `claude -p` prompt contains everything said in code mode. Nothing is "synced" — the app
owns the history either way. Wake-ups see it too (code-mode turns are written to the recent
window), so it won't wake up amnesiac about the last hour of work.

- **Every switch-in is a fresh session** (old one killed, context re-injected): no drift, no
  guessing what state it's in. Leaving code mode kills the session, which keeps "session alive =
  mode on" a clean invariant — that's what the app reconciles against on return to foreground,
  and how it notices the companion switched itself in. If it's mid-task when you leave, you get
  asked first.
- **A dropped connection doesn't cost you the reply.** Replies come back through the pending
  outbox, so locking your phone or losing signal mid-task changes nothing — the work finishes on
  your Mac and the message is waiting when you return.
- **The inline terminal** sits above the input bar: tap the black strip to slide it up, tap again
  to put it away, or hit the `⅔` button for a half-height view. At either height the newest output
  stays pinned to the bottom edge and the button row stays put — the height changes how much you
  see, not where the content is. Scroll up inside it for earlier output.
- **Permission prompts are never bypassed.** They appear as buttons in that panel, one per option,
  labelled with the prompt's own wording — however many options it has. If one sits unanswered
  for five minutes and you have Bark configured, your phone gets a nudge. Messages you send while
  a prompt is waiting are refused rather than swallowed by the dialog (a message starting with a
  digit would otherwise press a button for you).
- **The session's working directory** is its turf — where Grep and Glob search, where relative
  paths resolve, whose `.claude/settings.local.json` allowlist applies. Set `CODE_CWD`; the app may
  ask for a different directory per switch-in, but only inside `CODE_CWD_ALLOW` (resolved through
  symlinks and `..`, so there's nothing to slip past).
- **Reporting hooks are scoped to that session**, passed via `claude --settings` at startup. Your
  global `~/.claude/settings.json` is left alone, and your own claude sessions never trigger them.
- **Editing and regenerating are disabled** while in code mode: those turns had real side effects,
  and replaying history would perform them again.
- **Optional: let it switch itself.** The backend also accepts a self-switch call, so the
  companion can decide mid-conversation to move to your Mac; it echoes the task it wrote into your
  chat verbatim, so a garbled restatement is something you catch immediately. Reaching it needs a
  small separate plugin, **Self-switch Code mode**, from the plugin store — **without it, that
  switch is yours alone to flip**. Even with it installed, the wake-up path never gets this tool:
  a self-initiated wake-up with nobody watching cannot open a session.

## History is editable (editing it = editing its memory)

Every message has edit and copy buttons, and a long press deletes it; your own messages also offer "edit and re-reply", which clears everything after that message and has it answer again. All of this is purely local: the backend is stateless and the history is injected whole every turn, so **editing your local history is editing its memory** — it takes effect on the very next turn, no backend involved.

Editing is more useful than it looks:

- **Fix typos**: trivial slips get corrected in place — OCD-friendly, and no need to burn a regeneration on them.
- **Fix small errors**: small factual mistakes — including ones in its own past replies — get corrected directly in the text, instead of re-generating a whole turn or correcting the model over and over; the history injected next turn is simply right.
- **Tune the voice**: rewrite its past replies into the way you'd like it to speak, and you're doing example-based fine-tuning beyond the persona file — injected history is the strongest kind of demonstration. A few edits in, you find the exact companion you were looking for.

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

### 4. Long-term memory (optional)

Self-host an [Ombre-Brain](https://github.com/P0luz/Ombre-Brain) instance (P0luz's open-source memory system, one Docker command) and point `OMBRE_MCP_URL` in `.env` at it; if Ombre has static-token auth enabled (`mcp_auth_mode: "token"`, recommended), put the same secret in `OMBRE_MCP_TOKEN`. The backend probes it before every call: if Ombre isn't there — not installed, disabled, or down — it falls back to plain chat, and the memory layer can never take the app down with it.

**On Ombre versions**: any version connects, but two seams are matched *by name*, and a mismatch **fails silently** (no error anywhere):

- **The tool allowlist** is hardcoded as `OMBRE_TOOLS` in `pipeline.py` (written against the 2.13.x tool names). Tools added by a newer Ombre aren't on the list, so `--allowedTools` won't admit them — the companion simply can't use them, and nothing says so. The cushion: Ombre's own acceptance contract pins 12 names as unchangeable (`breath / hold / grow / dream / trace / anchor / release / pulse / plan / letter_write / letter_read / I`). **The three most likely to move are the ones *not* in that contract: `breath_search`, `breath_advanced`, `source_read`.**
- **Writes from the memory page** go through Ombre's Dashboard REST (`ombre_rest.py`: editing a memory is `/api/bucket/{id}/edit`, deleting is `/forget`). Those two endpoints aren't in the contract either. If they change, browsing memories still works while editing and deleting break.

So when you upgrade Ombre, **pin by digest rather than by tag** (tags on Docker Hub can be re-pushed), and diff the `@mcp.tool()` docstrings in its `server.py` while you're at it — those docstrings *are* the tool descriptions the companion sees every single turn, and they move with the version.

Ombre also has two dependencies that **change without you touching anything**: the compression LLM used for dehydration (`OMBRE_COMPRESS_MODEL` points at a remote model alias — providers retire and re-point aliases without telling you, and a failed dehydration degrades to a truncated excerpt instead of erroring) and the embedding model (change it and your existing vectors no longer share a space with new queries). Pin both to explicit versions; avoid floating aliases.

One more of the same kind: that alias may be re-pointed at a **thinking model**, or the model you already use may switch thinking on by default one day. Hybrid-reasoning models like DeepSeek and Gemini spend output tokens reasoning first, and the reasoning shares **the same `max_tokens` budget** as the answer — with headroom that only costs you money and latency (measured: one diary digest on DeepSeek burns three thousand reasoning tokens), without headroom the reasoning eats the entire budget and the answer comes back as an empty string. On Ombre that lands as the two silent failures above: dehydration degrades to a truncated excerpt, and diary digest (`grow`) reports "empty result" — nothing from that day gets stored, and the log only says JSON parse failed, never that reasoning ate the budget. Ombre turns it off by default for providers it can identify (Gemini goes through the native `thinkingConfig`, `thinking_budget` defaults to 0), but anything can sit behind `openai_compat`, so it can't decide for you — you turn it off yourself under `dehydration.extra_body` in `config.yaml` (`thinking: {type: disabled}` for DeepSeek; the parameter is not portable across providers, so change it when you switch). Dehydration and diary digest are mechanical transforms that don't need reasoning: turning it off is cheaper and sidesteps this whole class of silent failure.

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
  code_bridge.py  # code mode: the tmux session (start/stop, send, capture, dialog parsing)
  hooks/          # the session's own reporting hook (passed via --settings, not installed globally)
  persona.example.md
  code_addendum.example.md   # working rules appended to the persona in code mode
ios/cassette/     # SwiftUI app: chat UI, sticker library, settings, local persistence
  CodeTerminalPanel.swift    # the inline terminal (two heights, output pinned to the bottom)
```

## Data and safety

- Chat history lives in the app sandbox at `Documents/chat_history.json`; the backend keeps only a shadow snapshot of the recent window (≤300 messages).
- Every endpoint except the `/health` check requires the `X-Auth` shared key; with no key configured they all refuse requests (fail closed), and the comparison is constant-time.
- The wake log at `server/state/wake_log.jsonl` is append-only. It holds the full inner monologue from every wake-up, including the text of messages that interruption control blocked from ever being sent, and it is never pruned (the pending outbox has a 7-day cleanup; this doesn't). It never leaves your Mac, but it is the single most intimate file in the project — worth knowing it exists.
- The model subprocess runs with `--tools ""` by default: conversation only. With Ombre mounted, only the memory-tool whitelist is allowed (`--strict-mcp-config` shuts out any other MCP servers on the machine, `--allowedTools` pre-approves so nothing prompts) — built-in tools like Bash and file access are never enabled, and `--dangerously-skip-permissions` is never used.
- [Code mode](#code-mode) is the one deliberate exception to the line above, which is why it ships off and has to be enabled by hand. Even there the permission prompts stay: they're answered by you, in the app. Its working directory is confined to an allowlist, and its reporting hooks are scoped to that one session rather than installed into your global claude config.
- "Install" in the plugin store means downloading and running code on your Mac, so the registry is a **hardcoded allowlist in the backend** (only plugin repos under this project's account) — there is no "install from URL" input in the app, and there never will be. Plugin tools go through the same per-tool `--allowedTools` whitelist; enabling/disabling needs no network.
- The **browser** plugin is a logged-in, headful Chrome: the model can, in principle, act as you on the sites you signed into. That is the point of the feature, and it rests on persona and trust rather than a technical fence. What *is* fenced: the dangerous upstream tools (`browser_close`, file upload, arbitrary Playwright code) are never whitelisted, and the wake-up path never gets the browser unless you flip that plugin's own "available when waking" switch in the store — off by default, and only you can flip it.
- Keep the backend on your LAN or a Tailscale network. Don't expose it raw to the public internet.

## Roadmap

- **More plugins**: more tool families landing in the plugin store one by one (each its own small repo).
- **Web client**: the client is thin enough that it deserves a version that runs in a browser.

## Credits

Long-term memory integrates [Ombre-Brain](https://github.com/P0luz/Ombre-Brain) — P0luz's open-source memory system; this project connects to it as an external service and does not vendor its code. The iOS app uses [MarkdownUI](https://github.com/gonzalezreal/swift-markdown-ui), [Highlightr](https://github.com/raspu/Highlightr) and [swift-markdown](https://github.com/swiftlang/swift-markdown); push notifications use [Bark](https://github.com/Finb/Bark).

## License

[MIT](LICENSE)
