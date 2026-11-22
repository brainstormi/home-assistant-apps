# Asterisk Realtime Bridge for Home Assistant

[![Add this add-on repository to your Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fbrainstormi%2Fhome-assistant-apps)

A Home Assistant add-on (called an *app* since Home Assistant 2026.2; this project keeps the classic add-on naming in its files) that connects a phone call handled by **Asterisk** (the `AudioSocket` application) to the **OpenAI Realtime API** (speech-to-speech), so you can talk to your home over the phone and **interrupt the assistant while it is speaking**.

```
Phone ──► SIP trunk ──► Asterisk ──AudioSocket (TCP)──► this add-on ──wss──► OpenAI Realtime API
                                                             │
                                                             └─► Home Assistant conversation agent (exposed entities only)
```

* **Nothing inbound from the internet.** The add-on only makes an outbound HTTPS/WebSocket connection to `api.openai.com`. AudioSocket listens on `127.0.0.1` by default.
* **No resampling:** `audio/pcmu` (G.711 µ-law, 8 kHz) end to end, implemented in pure Python (no `audioop`, which is gone in Python 3.13).
* **Real barge-in:** the assistant's audio waits in a local queue that is emptied the instant the caller speaks, and the model is told what the caller actually heard (`conversation.item.truncate`).
* **Security enforced in code:** with the PIN enabled, the tool that touches your home is rejected until the PIN is verified (spoken or typed on the keypad).
* **The model never gets a Home Assistant token.** It sends natural-language requests to a conversation agent, which only acts on the entities **exposed to Assist**.
* **Outbound notice calls:** Home Assistant can have the assistant call a phone, read a notice and answer questions, with `QueryOnly` / `Management` profiles, per-call confirmation and a result event (`no_answer`, `answered`, `confirmed`, `failed`). See *Outbound calls* in DOCS.md.

## Why AudioSocket and not SIP or WebRTC directly?

The Realtime API has no phone number: a call has to come from a telephony stack. Asterisk already terminates the SIP trunk, filters callers and can also place outbound calls. Pointing a SIP trunk directly at OpenAI requires a public webhook, and WebRTC targets browsers and mobile apps. AudioSocket keeps everything on your side of the network and needs only one outbound connection.

## Which AI does what

1. **The OpenAI Realtime model** holds the spoken conversation: it listens, understands, decides when an action is needed and answers with its voice.
2. **A Home Assistant conversation agent** (`conversation_agent`) carries out the request. By default this is the built-in **Assist** agent (`conversation.home_assistant`): it matches sentence patterns, is fast, free and deterministic, but it is **not an LLM**. The Realtime model rephrases what the caller says into a simple request ("turn on the living room light").
3. **Optional fallback agent** (`fallback_agent`): if the main agent cannot handle a request, it is retried with another one, for example `conversation.openai_conversation` or any other LLM-based agent you have configured. This mirrors "prefer handling commands locally" in an Assist pipeline: cheap and fast first, LLM only when needed. It adds latency (an LLM inside another one) and cost. If most of your requests end up in the fallback, set that agent as `conversation_agent` directly and leave the fallback empty.

Any conversation entity of your Home Assistant can be used. LLM-based agents need *Control Home Assistant* enabled in their options to act on devices. The assistant that is "preferred" in your Assist pipelines is **not** used implicitly: the bridge only uses the agents you configure.

Tip: if requests are often not understood, describe how your areas and devices are named in `extra_instructions`, so the model uses the same words.

## Realtime model

`model` accepts any **Realtime API** model (`gpt-realtime-*`):

| Model | When to use it |
|---|---|
| `gpt-realtime-2.1` (default) | Most capable: best with instructions, tools and non-English languages |
| `gpt-realtime-2.1-mini` | Cheaper and faster; a bit less reliable when reasoning |
| `gpt-realtime-2` | Previous generation, in case 2.1 gives you trouble |

* The older `gpt-realtime` and `gpt-realtime-mini` models retire on **2027-01-20**; the add-on warns if you use them.
* **GPT-Live (`gpt-live-*`) is not supported yet.** It is a different API (`/v1/live/sessions`) with a different protocol (`session.start`, 24 kHz PCM audio, work delegated to a backend model). If you set it, the add-on stops with a message explaining why. It is a candidate for a future version.

## Language

`language` (default `en`) controls the language the assistant speaks and the language used for Home Assistant conversation agents and transcription. The default prompts are in English and tell the model to speak the configured language (any ISO code, e.g. `es`, `de`, `fr`).

### Prompt

The prompt that makes the assistant introduce itself and behave is the `prompt` option, pre-filled with a template you can edit. Values that already have their own option are **not** repeated in it; they are filled in through placeholders:

| Placeholder | Replaced by |
|---|---|
| `{assistant_name}` | the `assistant_name` option |
| `{language}` / `{language_name}` | the `language` option, as a code (`es`) and as a name (`Spanish`) |
| `{accent_line}` | the `accent` option as a line of text; the line is dropped when `accent` is empty (keep it on a line of its own) |
| `{pin_rules}` | the PIN instructions; dropped when `pin_enabled` is off (keep it on a line of its own) |

Anything else in braces is left untouched. Notes:

* **Empty = built-in template.** If you clear the field, the version shipped with the add-on is used, including any future improvements. If you keep your own copy, it will not change when the add-on is updated.
* **Multi-line editing.** The options form may show the field as a single line. For comfortable editing use the three-dot menu → *Edit in YAML*; a literal `\n` is also accepted as a line break.
* **Tools.** The assistant has the tools `home`, `hang_up` and, with the PIN enabled, `verify_pin`. If you rewrite the prompt, mention them by name.
* **The PIN is enforced in code**, not by the prompt: even if you remove `{pin_rules}`, the `home` tool is rejected until the PIN is verified.
* `extra_instructions` is still appended after the prompt, for short additions.

The current default template, for reference:

```
You are {assistant_name}, the voice assistant of this home. You are speaking on the phone, so keep your replies short and natural: no lists, no long explanations.

Language: always speak {language_name} (language code "{language}"), starting with your very first words, unless the caller clearly speaks another language.
{accent_line}

Security:
{pin_rules}
- For locks, alarms, doors, gates and garage doors always ask for explicit confirmation ("do you really want me to open it?") before acting.

Home:
- For any action or question about the home, use the home tool with ONE clear request in natural language, for example "turn on the living room light" or "is the garage door open?". Write the request in {language_name} (language code "{language}"), because that is the language Home Assistant understands.
- Tell the caller the result in your own words. If the tool returns an error, say so honestly. Never invent states or actions.

Call:
- If the caller says goodbye or needs nothing else, say a brief goodbye and call the hang_up tool.
- If you are interrupted, stop talking and listen.
```

### Accent and voice

The Realtime voices are a fixed list (`marin`, `cedar`, `alloy`, `ash`, `ballad`, `coral`, `echo`, `sage`, `shimmer`, `verse`) and the accent is not a parameter. The `accent` option appends a line to the instructions to steer it, for example:

```
Spanish as spoken in Spain (Castilian), standard peninsular accent: pronounce "c" before e/i and "z" as in Spain, use "vosotros" and Spain vocabulary (ordenador, móvil, vale). Avoid Latin American accent and vocabulary.
```

The model follows this to a degree, and the result depends on the model and the voice, so try a few voices. If the accent matters more than speech-to-speech speed, a classic speech-to-text → LLM → text-to-speech pipeline lets you pick a regional TTS voice, at the cost of latency and barge-in.

## Requirements

* Home Assistant OS or Supervised: this is a Supervisor add-on/app, so it is not available on Home Assistant Container or Core (if your *Settings* menu has no *Apps* entry, that is your case).
* Asterisk 18 or newer with the AudioSocket modules loaded (`module show like audiosocket` in the Asterisk CLI). If Asterisk runs as another add-on with host networking, the default `127.0.0.1` binding just works; otherwise see `bind_address`.
* An OpenAI API key with access to the Realtime API. **Create a key just for this**, in a project with a spending limit.

## Installation

1. In Home Assistant: *Settings → Apps → App Store → ⋮ (top right) → Repositories*, paste `https://github.com/brainstormi/home-assistant-apps`, then *Add* and *Close* (or use the button at the top). On versions before 2026.2 the path is *Settings → Add-ons → Add-on store*. Note that the *Marketplace* (HACS-style) is for integrations, cards and themes, not for this.
2. Find **Asterisk Realtime Bridge**, install it, open **Configuration**, fill in `openai_api_key` and `pin` (digits only) and start it.

The log should show:

```
Asterisk Realtime Bridge 0.7.0 listening on 127.0.0.1:9092 (model gpt-realtime-2.1, PIN enabled)
```

The image is built on your own machine from the `Dockerfile`, with no external registry involved. Reinstalling on a clean system means repeating the two steps above.

**Local alternative (no GitHub):** copy the `asterisk_realtime_bridge/` folder to `/addons/` of your Home Assistant and it will show up under *Local apps* (*Local add-ons* before 2026.2).

### Publishing a new version

1. Bump `version` in `asterisk_realtime_bridge/config.yaml` and add an entry to `CHANGELOG.md`.
2. Commit and push. Home Assistant detects the update when the store is refreshed.

## PIN

* `pin_enabled: true` (default) requires a PIN: set it in `pin` (digits only). The caller can **say it** or **type it on the phone keypad** (keypad entry depends on your Asterisk version and trunk forwarding DTMF over AudioSocket; if it does not work, speak the PIN).
* `pin_enabled: false` disables it. Then any call that reaches the bridge can control your home, and your only barrier is your Asterisk dialplan (caller ID is a weak factor on its own). A warning is logged at startup.
* The PIN and the OpenAI key are stored in the add-on configuration, visible to Home Assistant administrators.

## Asterisk setup

### Try it in isolation first

Append new contexts to your `extensions.conf`; they do not touch any of your existing contexts:

```
[realtime-test]
; Dial 9902 from any internal phone or softphone
exten => 9902,1,NoOp(Realtime test)
 same => n,Answer()
 same => n,Set(UUID=${UUID()})
 same => n,AudioSocket(${UUID},127.0.0.1:9092)
 same => n,Hangup()

; Optional: have Asterisk call a phone and connect it to the assistant when answered
[realtime-test-answer]
exten => s,1,Answer()
 same => n,Set(UUID=${UUID()})
 same => n,AudioSocket(${UUID},127.0.0.1:9092)
 same => n,Hangup()

[realtime-test-dialer]
exten => _X.,1,NoOp(Realtime test: calling ${EXTEN})
 ; Some trunks require a specific caller ID: Set(CALLERID(all)=...)
 same => n,Dial(PJSIP/${EXTEN}@<your-trunk-endpoint>,30)
 same => n,Hangup()
```

Reload the dialplan (`dialplan reload`) and, for the outbound variant, run from the Asterisk CLI:

```
channel originate Local/<PHONE_NUMBER>@realtime-test-dialer extension s@realtime-test-answer
```

Answer, say or type the PIN, ask for something like "turn on the living room light" and try **talking over** the assistant while it answers.

### Routing real calls

Once you are happy with the test, send real calls to the same three lines (`Answer()`, `Set(UUID=${UUID()})`, `AudioSocket(${UUID},127.0.0.1:9092)`) from the context that handles your calls. Authorize callers in the dialplan first (for example, compare `${CALLERID(num)}` with an allowed number and hang up otherwise) and keep the PIN enabled as a second barrier. Calls originated by Asterisk can be connected the same way.

## Configuration

| Option | Default | Purpose |
|---|---|---|
| `openai_api_key` | – | OpenAI API key (required) |
| `model` | `gpt-realtime-2.1` | Realtime API model (see table above) |
| `voice` | `marin` | Assistant voice |
| `language` | `en` | Spoken language and Home Assistant request language |
| `assistant_name` | `Assistant` | How the assistant introduces itself |
| `accent` | – | Optional accent/dialect hint (see above) |
| `prompt` | template | Assistant prompt with placeholders (see above); empty = built-in |
| `pin_enabled` / `pin` | `true` / – | Require a PIN and its value |
| `max_pin_attempts` | `3` | Failed PIN attempts before hanging up |
| `conversation_agent` | `conversation.home_assistant` | Agent that executes requests |
| `fallback_agent` | – | Agent used when the main one cannot handle a request |
| `max_call_seconds` | `300` | Hard limit on call length (cost control) |
| `max_concurrent_calls` | `1` | Simultaneous calls |
| `vad`, `vad_threshold`, `vad_silence_ms` | `server_vad`, `0.5`, `500` | Turn detection |
| `noise_reduction` | `near_field` | `near_field`, `far_field` or `off` |
| `port` / `bind_address` | `9092` / `127.0.0.1` | Where AudioSocket listens |
| `extra_instructions` | – | Appended after the prompt |
| `log_transcripts` | `false` | Log what is said (private; for debugging only) |

## Security

* You do not need to open any port. Keep your router free of forwards to Asterisk, Home Assistant or this add-on.
* AudioSocket has **no authentication**. It listens on `127.0.0.1` by default, so only processes on the same host can use it. If you change `bind_address` (for example because Asterisk runs on another machine), restrict access with a firewall.
* Expose to Assist **only** what you want to control by voice. Consider not exposing locks, alarm panels and garage doors.
* Set a spending limit on the OpenAI project, and use `max_call_seconds` and `max_concurrent_calls`.

## Troubleshooting

| Symptom | What to check |
|---|---|
| The add-on does not start and the log mentions configuration | Missing key or PIN, or the model is not a `gpt-realtime-*` one |
| Asterisk hangs up immediately | Is the add-on running? `Connection refused` in the Asterisk log means nothing listens on the port |
| Silence at the start | Look for `OpenAI error` (key without Realtime access, wrong model) or `OpenAI did not confirm the session in time` |
| The assistant interrupts itself | Raise `vad_threshold` (0.6–0.7) or try `semantic_vad`; echo is the usual cause |
| "Could not reach Home Assistant" | Check the add-on log; it uses `http://supervisor/core/api` and falls back to `http://172.30.32.2/core/api` |
| A home request is not understood | Try a `fallback_agent`, describe your device names in `extra_instructions`, or check that the entity is exposed to Assist |
| The repository does not show the add-on | `repository.yaml` must be at the repository root and the URL must be the repository's; refresh the store |

## Uninstall

*Settings → Apps → Asterisk Realtime Bridge → Uninstall*; remove the repository from the store; remove the test contexts from your dialplan and revoke the OpenAI key.

## Development

```
python -m unittest discover -s tests -v
```

The tests use simulated Asterisk, OpenAI and Home Assistant endpoints and need no network. The GitHub Actions workflow runs the tests, the official add-on linter and builds the image to catch dependency problems.

## Status and limitations

* Early release. The logic is covered by unit tests with simulated endpoints; field reports are welcome.
* Without OpenAI, the local conversation (`local_conversation`) is slower and more literal, and the PIN must be typed.
* GPT-Live is not supported.

## License

MIT, see [LICENSE](LICENSE).
