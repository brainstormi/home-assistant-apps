# Asterisk Realtime Bridge for Home Assistant

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

## PIN

Two modes, chosen with `pin_mode`; only one is active at a time:

* **`global`** (default): one PIN, `pin` (digits only), for `Management` calls and incoming calls. Personal PINs are ignored.
* **`individual`**: one PIN per Home Assistant person, in `pins`; the global PIN is ignored. Each entry has:
  * `person`: the person's entity id (`person.sonia`). The form `person.xxx` is checked when you save, and the bridge checks that the person exists in Home Assistant (a typo is reported in the log and in the status entity, and leaves the mode incomplete). The options form cannot offer a list of persons: copy the ids from `people_without_pin` in the status entity.
  * `pin`: at least **6 digits**, not trivial (not `111111`, `123456`, `987654`, `121212`, `123123`...) and **different for every person**.
  * `numbers` (optional, comma separated): that person's phones. An incoming call from one of them asks for **that person's** PIN (see [Caller number](#caller-number)). A number with no person linked is **query only**.

In an outbound `Management` call only the PIN of `person` is accepted (`person` can be the entity id or the person's name). If that person has no PIN, the call runs as `QueryOnly` and a warning is logged.

The caller can **say** the PIN or **type it on the keypad** (keypad entry depends on your trunk forwarding DTMF over AudioSocket).

**Incomplete mode.** A wrong entry (missing `person`, a trivial or repeated PIN...) does not stop the add-on: it is logged (never with the PIN) and the mode is *incomplete* until you fix it. Meanwhile `Management` calls are query only and incoming calls can only ask. The same happens right after switching modes, until the new mode is filled in; a change of mode is logged as a warning.

**Status entity.** `sensor.asterisk_realtime_bridge_pin` shows the mode (`global`, `individual` or `off`) and, as attributes, whether it is `complete`, whether `management` is enabled, the `issues` found, and in individual mode `people_with_pin`, `people_without_pin` and `people_with_number`. It never contains a PIN. It is republished every 10 minutes (and after a restart of the add-on).

`pin_enabled: false` disables PINs altogether: any call that reaches the bridge can control your home, and your Asterisk dialplan is the only barrier. A warning is logged at startup. PINs and the OpenAI key are stored in the add-on configuration, visible to Home Assistant administrators.

## Setting up from scratch

What the app creates by itself, as soon as it starts (no setup needed):

* `binary_sensor.asterisk_realtime_bridge_openai`: whether OpenAI works.
* `sensor.asterisk_realtime_bridge_pin`: the PIN mode and who has a PIN.

These two are published by the app and are not in the entity registry, so they cannot have labels, areas or categories. They come back after a restart of Home Assistant within minutes (the PIN entity within 10 minutes, the OpenAI entity at the next check).

What you set up yourself, once:

1. **Asterisk dialplan.** A complete template with every context the bridge needs (incoming calls with the caller number, outbound calls, notice and dial status report) is in [`examples/extensions.conf`](https://github.com/brainstormi/home-assistant-apps/blob/main/asterisk_realtime_bridge/examples/extensions.conf). Copy it into your `extensions.conf`, replace the placeholders (`<trunk-endpoint>`, `<caller-id>`, `<allowed-number-N>`, `<inbound-context>`) and run `dialplan reload`. Your trunk itself (`pjsip.conf`: registration, credentials) depends on your provider and is not covered here.
2. **Home Assistant script for outbound calls.** A ready-made script that announces the call, asks Asterisk to dial, waits for the result and retries is in [`examples/script_outbound_call.yaml`](https://github.com/brainstormi/home-assistant-apps/blob/main/asterisk_realtime_bridge/examples/script_outbound_call.yaml). Paste it in the script editor (Edit in YAML) and replace the two app slugs.
3. **Labels, areas, categories** of your own entities and scripts: as you like; nothing in the app depends on them.

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

Once you are happy with the test, send real calls to the bridge from the context that handles your calls. Authorize callers in the dialplan first (for example, compare `${CALLERID(num)}` with an allowed number and hang up otherwise) and keep the PIN enabled as a second barrier.

#### Caller number

AudioSocket passes no channel variables, so the dialplan packs the caller's number into the UUID. Use these lines instead of `Set(UUID=${UUID()})` + `AudioSocket(${UUID},...)`:

```
 same => n,Answer()
 same => n,Set(N=${FILTER(0123456789,${CALLERID(num)})})
 same => n,Set(N=${N:-20})
 same => n,Set(L=00${LEN(${N})})
 same => n,Set(P=00000000000000000000${N})
 same => n,Set(P=${P:-20})
 same => n,Set(R=0000${RAND(0,9999)})
 same => n,AudioSocket(ca11e0${L:-2}-${R:-4}-${P:0:4}-${P:4:4}-${P:8:12},127.0.0.1:9092)
 same => n,Hangup()
```

The bridge reads the number from it (`ca11e0` + number of digits + a random part + the digits), matches it with the `numbers` of each person (with or without the country prefix) and reports it in the call events. A plain `${UUID()}` still works: the call is then from an unknown number (query only in individual mode).

## Outbound calls (notices)

Home Assistant can have the bridge call a phone, read a notice and stay on the line for questions. It is meant to be reused by automations and by an alarm system: each call goes to **one** number, and the caller (your script or automation) decides retries, contact chains and fallbacks from the result it gets back.

### How it works

1. Home Assistant sends a command to the add-on's **standard input** with `hassio.addon_stdin`: a call id (a UUID it generates), the notice and a profile. No port is opened for this.
2. Home Assistant asks Asterisk to dial the number (`channel originate`, for example through `hassio.addon_stdin` to the Asterisk add-on). When the phone is answered, the dialplan connects it to the bridge with `AudioSocket(<call id>,127.0.0.1:9092)`.
3. The bridge recognises the call id, reads the notice and asks the person to confirm it.
4. The bridge fires the event `asterisk_realtime_bridge_call` on the Home Assistant bus when the call is announced (`started`), answered and when it ends.

Command (the `input` of `hassio.addon_stdin`, addon `<slug>_asterisk_realtime_bridge`):

```yaml
action: outbound
call_id: "0b6c3a52-7f0e-4d3c-9a51-2f5d8e9a1c07"  # a new UUID per attempt; also used in the dialplan
message: "The alarm went off: motion in the garage at 03:12."
profile: Management     # QueryOnly or Management
person: person.sonia    # optional; selects the personal PIN (entity id or name) and is reported back
number: "600111222"     # optional, informative: reported in the events
timeout: 60             # seconds to answer before the result is no_answer
confirm: voice_or_dtmf  # optional: implicit (default), voice, dtmf or voice_or_dtmf
confirm_key: "1"        # optional, default 1 (0-9, * or #)
confirm_text: "If you confirm, nobody else will be called."   # optional, said when asking
assistant_name: "the alarm service of the house"              # optional, default: the add-on's
engine: realtime        # optional: realtime (OpenAI, default) or local (no OpenAI; needs fallback_tts and local_stt)
```

`engine: local` runs the whole call with the local conversation (see below): no OpenAI cost and no internet needed, but slower and more literal. Useful for routine notices; `realtime` remains better when the person should be able to ask naturally.

Other commands (same `hassio.addon_stdin`):

```yaml
action: note            # tell the person on the phone something, without hanging up
call_id: "0b6c3a52-..."
text: "The alarm has been disarmed by Sonia."
```

```yaml
action: hangup          # hang up this call; without call_id, every call
call_id: "0b6c3a52-..."
```

A note is said at once by the assistant in its own words (or read with the fallback voice in the local conversation); the event `stage: note` with `delivered: true/false` tells whether there was a conversation to deliver it to. `hangup` ends calls in progress (`ended_by: hangup`) and withdraws calls that are still ringing (`result: cancelled`); to stop the phone ringing, also hang the channel up in Asterisk (for example `channel request hangup all`). A withdrawn or expired call that is answered later is hung up at once: it is never taken for an incoming call.

The bridge keeps no contacts and no call policy: who to call, with which profile, what counts as a confirmation and what to do with the result are decided by the caller (your script, automation or alarm) in every command.

### Profiles

| | Incoming call (`InboundCall`) | `QueryOnly` | `Management` |
|---|---|---|---|
| Starts with | greeting + PIN (query only: greeting) | the notice | the notice |
| Questions about the home | after the PIN; read-only without one (see below) | yes, read-only (`home_status`) | yes, read-only (`home_status`) |
| Changes | after the PIN | **never** (no PIN, not even a correct one) | after the PIN |
| Accepted PINs | global mode: the global PIN. Individual: the PIN of the person whose `numbers` include the caller | none | global mode: the global PIN. Individual: the PIN of `person` |

An incoming call is **query only** (read-only `home_status`, no PIN possible) when, in individual mode, the number is not linked to a person with a PIN or is unknown, and in any mode while the PIN mode is incomplete. A caller ID can be spoofed, so a known number never skips the PIN: it only decides whose PIN is asked for. In an outbound call you chose the number, so questions are allowed; but someone else may answer, so changes still need the PIN (`Management`) or are impossible (`QueryOnly`). Someone else who answers that phone cannot use their own PIN. Without a PIN that applies, a `Management` call runs as `QueryOnly` (events carry `effective_profile`).

### Confirmation

| `confirm` | The assistant | What counts |
|---|---|---|
| `implicit` (default) | does not ask; it reads the conversation | the person reacts to the notice: replies, asks something, says ok or goodbye |
| `voice` | asks explicitly (and says `confirm_text`) | a clear yes |
| `dtmf` | asks to press `confirm_key` (and says `confirm_text`) | only the key |
| `voice_or_dtmf` | asks for either | a clear yes or the key |

A voicemail greeting, silence or hanging up without reacting is never a confirmation. In a `Management` call where a PIN can be typed, a digit confirmation key only counts if no other key follows within 2 seconds (otherwise the keys are taken as a PIN); `#` or `*` avoid that wait.

### Questions without a PIN (read-only scope)

Home Assistant has no read-only Assist: an agent that can see the exposed entities can also control them. So, for questions asked without a PIN, the bridge builds a read-only list itself and gives it to the assistant with the `home_status` tool (name, state, type and since when, plus the current time). The tool that can act (`home`) is not offered in `QueryOnly` and is rejected in `Management` until the PIN is verified. Requests without a PIN never reach `conversation_agent` or `fallback_agent`.

What can be read is set **only in the add-on configuration** (never per call, so a call cannot widen it):

* `query_include`: labels, comma separated, by name or id. **Empty = the same entities exposed to Assist** in Home Assistant (the scope you already control by voice).
* `query_exclude`: labels, entity ids or device names to remove from that scope. A name that is both a label and a device excludes both.

Labels on a device apply to its entities. Names are matched ignoring case and accents. Incoming calls and `Management` calls after the PIN are unchanged: the request goes to your conversation agent, which only sees what is exposed to Assist.

### Results

Event `asterisk_realtime_bridge_call`:

| Field | Values |
|---|---|
| `call_id` | the UUID of the command |
| `direction` | `outbound` or `inbound` |
| `stage` | `started` (command accepted / incoming call connected), `answered` (outbound), `note`, `ended` |
| `result` (when `ended`) | `no_answer` (not answered within `timeout`, or Asterisk reported `NOANSWER`), `answered`, `confirmed` (the person confirmed the notice), `cancelled` (withdrawn by `hangup` before it was answered), `failed` (invalid command, too many calls, a dial failure such as `BUSY`, or the assistant could not start) |
| `dialstatus` | when Asterisk reports why the call was not answered: `NOANSWER`, `BUSY`, `CONGESTION`, `CHANUNAVAIL`... (needs the dialplan below) |
| `profile`, `person`, `number`, `engine` | as sent in the command (`InboundCall` for incoming calls, with the caller's `number` and matched `person`) |
| `effective_profile` | outbound: the profile actually applied (`QueryOnly` when `Management` had no PIN to use) |
| `access` | inbound: `pin`, `query_only` or `open` |
| `confirmed_by` | when confirmed: `implicit`, `voice` or `dtmf` |
| `ended_by` | `hangup` when Home Assistant hung the call up |
| `duration`, `pin_verified`, `pin_person` | when `ended`; `pin_person` is the person of the personal PIN (empty for the global one) |

A voicemail can answer the call: then the result is `answered`, not `confirmed`. Use `confirmed` when you need to know that a person got the notice.

### Dialplan

```
; HA: channel originate Local/<number>_<call_id>@bridge-outbound extension <call_id>@bridge-notice
; (Local/<number>@bridge-outbound, without the call id, also works but reports no dial status)
[bridge-outbound]
exten => _X.,1,Set(NUM=${CUT(EXTEN,_,1)})
 same => n,Set(CALL=${CUT(EXTEN,_,2)})
 same => n,NoOp(Notice call to ${NUM})
 ; Some trunks require a specific caller ID: Set(CALLERID(all)=...)
 same => n,Dial(PJSIP/${NUM}@<your-trunk-endpoint>,30)
 same => n,GotoIf($["${CALL}" = "" | "${DIALSTATUS}" = "ANSWER"]?done)
 ; Tell the bridge why it was not answered (a short report connection, not a call)
 same => n,Set(LIST=NOANSWER-BUSY-CONGESTION-CHANUNAVAIL-CANCEL-DONTCALL-TORTURE-INVALIDARGS-ANSWER)
 same => n,Set(CODE=00${FIELDNUM(LIST,-,${DIALSTATUS})})
 same => n,Originate(Local/fa1100${CODE:-2}${CALL:8}@bridge-report,app,Wait,3)
 same => n(done),Hangup()

[bridge-report]
exten => _fa11.,1,Answer()
 same => n,AudioSocket(${EXTEN},127.0.0.1:9092)
 same => n,Hangup()

[bridge-notice]
exten => _[0-9a-f].,1,NoOp(Notice ${EXTEN} answered)
 same => n,Answer()
 same => n,AudioSocket(${EXTEN},127.0.0.1:9092)
 same => n,Hangup()
```

The ring time (30 s here) should be shorter than the command's `timeout`.

## When OpenAI is unavailable (fallback)

With `fallback_tts` set to a Home Assistant TTS entity (for example `tts.piper`), the bridge keeps working in a reduced mode when OpenAI cannot be used (outage, invalid key, no credit, or the connection drops mid-call):

* **Outbound calls:** the notice is read twice with that voice and the person confirms by pressing `confirm_key` (speech cannot be understood without OpenAI, so the key counts whatever `confirm` says). The notice audio is generated while the phone rings. The `ended` event has `fallback: true` and `result` `confirmed` (with `confirmed_by: dtmf`) or `answered`.
* **Incoming calls:** a short "the assistant is not available" message, then the call ends (`result: failed`, `fallback: true`).

Home Assistant converts the audio to 8 kHz mono WAV itself, so nothing else is needed. Without `fallback_tts`, a call that cannot reach OpenAI ends with `result: failed`.

### Local conversation (`local_conversation`)

With `local_conversation: true` (and `fallback_tts`), a call that cannot use OpenAI becomes a local conversation instead of only a notice or a message: the caller's speech is cut into sentences, transcribed with `local_stt` (for example Whisper), handled by Home Assistant's own Assist, and the answer is spoken with the fallback voice. The same rules apply:

* **PIN on the keypad only** (spoken numbers are not reliable with local speech recognition).
* **Incoming calls:** nothing until the PIN is verified; then requests go to your conversation agent.
* **Outbound calls:** the notice is read; it can be confirmed with the key and, when `confirm` allows it, by voice ("received", "ok"...). Questions without a PIN are answered only if Home Assistant's built-in agent recognises them as a read-only intent (checked before executing) about entities in the read-only scope. Changes need the PIN (`Management`) and are impossible in `QueryOnly`.
* The assistant does not listen while it speaks, and it is slower (about 2–3 s per answer, with a soft typing sound meanwhile) and more literal than OpenAI. Say "goodbye" to end the call.

Events carry `fallback: "local"`.

### Transcription test (local STT)

To check how well a local speech-to-text engine understands phone audio before relying on it, send this with `hassio.addon_stdin`:

```yaml
action: stt_test
engine: stt.faster_whisper   # any Home Assistant STT entity
language: es                 # optional, default: the add-on's language
```

The next incoming call within 5 minutes is a test call: it does not go to OpenAI and never acts on the home. Each sentence you say is transcribed and read back with the fallback voice (`fallback_tts`), and the log shows the audio length and how long the transcription took.

### OpenAI check

Every `openai_check_minutes` (default 15; 0 = only at start-up) the bridge opens a Realtime session and closes it as soon as OpenAI confirms it: no audio and no tokens, so no cost. It detects an invalid key, exhausted credit (`insufficient_quota`), no access to the model and outages. Real calls also update the result. It is published as `binary_sensor.asterisk_realtime_bridge_openai` (`on` = available, attribute `reason` when not), which you can use to be notified. The entity is created through the REST API: after a Home Assistant restart it reappears at the next check.

OpenAI has no API for the remaining prepaid credit; set spending alerts and auto recharge in your OpenAI project settings to be warned before the credit runs out.

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
| `pin_enabled` / `pin_mode` | `true` / `global` | Require a PIN; `global` or `individual` (see [PIN](#pin)) |
| `pin` | – | The global PIN (global mode) |
| `pins` | – | Personal PINs (individual mode): `person`, `pin`, optional `numbers` |
| `max_pin_attempts` | `3` | Failed PIN attempts before hanging up |
| `conversation_agent` | `conversation.home_assistant` | Agent that executes requests |
| `fallback_agent` | – | Agent used when the main one cannot handle a request |
| `query_include` / `query_exclude` | – / – | Read-only scope for outbound calls without a PIN (empty include = Assist scope) |
| `max_call_seconds` | `300` | Hard limit on call length (cost control) |
| `max_concurrent_calls` | `1` | Simultaneous calls |
| `vad`, `vad_threshold`, `vad_silence_ms` | `server_vad`, `0.5`, `500` | Turn detection |
| `noise_reduction` | `near_field` | `near_field`, `far_field` or `off` |
| `port` / `bind_address` | `9092` / `127.0.0.1` | Where AudioSocket listens |
| `fallback_tts` / `fallback_tts_voice` | – / – | TTS entity (and voice) used when OpenAI is unavailable |
| `openai_check_minutes` | `15` | OpenAI check interval (0 = only at start-up) |
| `local_conversation` / `local_stt` | `false` / `stt.faster_whisper` | Converse locally when OpenAI is unavailable |
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

## Status and limitations

* Early release. The logic is covered by unit tests with simulated endpoints; field reports are welcome.
* Without OpenAI, the local conversation (`local_conversation`) is slower and more literal, and the PIN must be typed.
* GPT-Live is not supported.
