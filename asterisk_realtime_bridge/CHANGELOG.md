# Changelog

## 0.9.1
- **The assistant recognises voicemail:** with `detect_voicemail: true` in the outbound command, it gets a `report_voicemail` tool for recorded greetings, beeps and call screening such as iPhone's Live Voicemail. It leaves `voicemail_message` (if any) and hangs up; the call ends with `result: voicemail` and `detected_by: assistant` (`amd` when Asterisk's `AMD()` detected it).
- `bridge-notice-amd` dialplan: a jitter buffer around `AMD()` fills the gaps when the mobile network sends no audio during silence (before, `AMD()` heard nothing and could not decide).
- The example script passes `detect_voicemail` together with the `bridge-notice-amd` context.

## 0.9.0
- **Withdrawn calls stop ringing.** `hangup` on an outbound call that is still ringing now also hangs it up in Asterisk (through the Asterisk app's stdin and a new `bridge-kill` context), so the phone stops in a second or two. New option `asterisk_app`. A failure is logged and the call stays `cancelled`.
- **Notes that close the notice:** `note` with `resolve: true` interrupts the assistant, says the note, stops asking for confirmation, offers to answer a question and then says goodbye. The call ends with `result: resolved`. Without `resolve`, notes work as before.
- **Silence:** after `silence_seconds` (8) without speech or a key, the assistant asks again; after `silence_reprompts` (2), it says goodbye and hangs up, with `ended_by: silence`. Also in the local conversation (it replaces its fixed 45 s idle time).
- **Answering machines (optional):** the new `bridge-notice-amd` context runs `AMD()`; on `MACHINE` the call ends with `result: voicemail`, `amdstatus` and `amdcause`, optionally leaving `voicemail_message` with the fallback voice.
- New dialplan contexts `bridge-kill` and `bridge-notice-amd` (see DOCS and `examples/extensions.conf`); the outbound call script example has `detect_voicemail` and `voicemail_message`.

## 0.8.3
- Documentation: "Setting up from scratch" (what the app creates by itself and what you set up), with a complete dialplan template (`examples/extensions.conf`) and a ready-made outbound call script (`examples/script_outbound_call.yaml`).
- The example phone numbers in the option descriptions are now fictitious.

## 0.8.2
- Descriptions for each field of a personal PIN (person, PIN, phone numbers, label) in the configuration form (Home Assistant 2026.5 or newer shows them).

## 0.8.1
- Personal PINs: `person` must have the form `person.xxx` (Home Assistant refuses to save the configuration otherwise).
- The bridge checks that each configured `person` exists in Home Assistant. One that does not (a typo) is logged and shown in the `issues` of `sensor.asterisk_realtime_bridge_pin`, and the PIN mode stays incomplete until it is fixed.

## 0.8.0
- **PIN modes** (`pin_mode`), mutually exclusive: `global` (one PIN for Management and incoming calls; personal PINs ignored) or `individual` (one PIN per Home Assistant person, `pins[].person` = `person.xxx`; the global PIN ignored). Individual PINs must have at least 6 digits, not be trivial and be different for each person. An invalid entry no longer stops the add-on: the mode is *incomplete*, and until it is fixed Management calls are query only and incoming calls can only ask. A change of mode is logged.
- **Breaking:** in global mode, personal PINs are no longer accepted; to keep personal PINs use `pin_mode: individual` (with `person` entity ids and PINs that follow the new rules).
- Management call to a person without a PIN (individual mode): runs as query only, with a warning in the log and `effective_profile: QueryOnly` in the events.
- New entity `sensor.asterisk_realtime_bridge_pin`: PIN mode, whether it is complete and who has a PIN. Never the PINs.
- **Incoming calls by caller number:** the dialplan can pack the caller's number into the AudioSocket UUID (see DOCS). In individual mode a number in a person's `numbers` asks for that person's PIN; any other number (or an unknown one) is query only, with the read-only `home_status` tool.
- Event `stage: started` when an outbound command is accepted and when an incoming call connects, with `call_id`, `person` and `number`. Events now carry `number`; incoming ones also `access`.
- New command `note`: tell the person on the phone something without hanging up (said by the assistant at once, or read with the fallback voice in the local conversation). Event `stage: note` with `delivered`.
- New command `hangup`: hang up a call by `call_id`, or every call; calls still ringing are withdrawn (`result: cancelled`). An expired or withdrawn call that is answered later is hung up instead of being taken for an incoming call.
- **Dial status:** with the new `bridge-outbound` dialplan, Asterisk tells the bridge why a call was not answered; the `ended` event carries `dialstatus` (`BUSY`, `NOANSWER`...) right away instead of waiting for `timeout`.

## 0.7.0
- New `engine` field in outbound commands: `realtime` (default: OpenAI, with the local fallback if it fails) or `local` (straight to the local conversation: no OpenAI, no cost, works without internet). Same profile, PIN and confirmation rules. Events report `engine`.

## 0.6.2
- Local conversation: the phrases say "pin" in lower case, because Piper spelled an upper-case "PIN" letter by letter.

## 0.6.1
- Local conversation: a soft keyboard-typing sound plays while the assistant transcribes and works, so the caller knows it is busy instead of hearing silence.

## 0.6.0
- **Local conversation when OpenAI is unavailable** (`local_conversation`, off by default): speech is transcribed with a Home Assistant STT engine (`local_stt`, e.g. Whisper), handled by Home Assistant's own Assist and answered with the fallback voice. PIN on the keypad only; in outbound calls, questions without a PIN are allowed only for read-only intents within the read-only scope, verified with Assist's debug API before anything runs. Events carry `fallback: "local"`.

## 0.5.2
- Transcription test mode, to evaluate a local conversation fallback (Whisper through Home Assistant): send `{"action": "stt_test", "engine": "stt.faster_whisper"}` with `hassio.addon_stdin`; the next incoming call within 5 minutes does not go to OpenAI, each utterance is transcribed and read back with the fallback voice, and the timings are logged. It never acts on the home.

## 0.5.1
- **Security fix:** an HTTP error from Home Assistant was logged with aiohttp's full error text, which includes the request headers and so the add-on's Supervisor token. Errors are now reported as status and path only.
- Fix: the fallback TTS failed when the engine does not accept the language code ("es" with Piper, which wants "es_ES"; Home Assistant answers 500). The request is retried with the engine's default language.

## 0.5.0
- **Fallback when OpenAI is unavailable** (`fallback_tts`, for example `tts.piper`): outbound notices are read twice with Home Assistant's TTS and confirmed with a key; incoming calls hear that the assistant is not available. The notice audio is generated while the phone rings. Also used if OpenAI drops mid-call before the notice was confirmed. Events carry `fallback: true`.
- **OpenAI check** every `openai_check_minutes` (default 15): opens and closes a Realtime session (no audio, no tokens) and publishes `binary_sensor.asterisk_realtime_bridge_openai` with the reason when it fails (for example `insufficient_quota`).

## 0.4.0
- **Read-only questions without a PIN, built into the bridge.** In outbound calls, the assistant answers questions with the new `home_status` tool: a read-only list of states (name, state, type, since when, current time) that the bridge builds from Home Assistant. It replaces the `query_agent` option, which is removed.
- New `query_include` and `query_exclude` options set that scope, only in the add-on configuration. Empty include = the entities exposed to Assist.
- In `QueryOnly` calls the tool that can act is no longer offered at all; in `Management` it is rejected until the PIN is verified.
- The assistant is told to check the state before every answer and how to read it (local times, "year 2000 = never", door/motion states).

## 0.3.2
- Fix: in an outbound call, if the person spoke over the greeting (for example "¿sí?" when answering), the assistant lost the notice and asked the person for it. The notice is now also part of the call's instructions, not only of the greeting.

## 0.3.1
- Jitter buffer for the assistant's voice: playback of each response starts once 200 ms of audio are queued (or the response's audio is complete) and buffers again if the audio runs dry mid-response. Short delays from OpenAI no longer cut the voice, at the cost of about 0.2 s before the assistant starts speaking.
- The log reports how many times the audio ran dry in a call (each one at debug level).

## 0.3.0
- **Outbound calls with a notice.** Home Assistant registers a call through the add-on's stdin (`hassio.addon_stdin`, no port needed) and has Asterisk dial the number; when it is answered, the assistant reads the notice and stays on the line for questions.
- Profiles for outbound calls: `QueryOnly` (questions only, changes impossible, no PIN) and `Management` (questions right away, changes after the global PIN or the personal PIN of the person called). Incoming calls (`InboundCall`) still require the PIN for everything.
- Per-call confirmation chosen by the caller: `implicit`, `voice`, `dtmf` or `voice_or_dtmf`, with an optional key and text (for example the consequence of confirming). The result reports how it was confirmed (`confirmed_by`).
- Per-call `assistant_name`, to introduce the assistant differently (for example to people outside the home).
- New `query_agent` option: a read-only conversation agent for questions in outbound calls before a PIN is verified.
- New `pins` option: personal PINs (name and digits), alongside or instead of the global PIN.
- The bridge fires `asterisk_realtime_bridge_call` events (`answered`, `ended` with `no_answer`, `answered`, `confirmed` or `failed`) so scripts can retry, escalate or fall back.
- The greeting now introduces the assistant by its name.

## 0.2.6
- Fix: spoken PINs were rejected when the model passed them as words or grouped numbers. The `verify_pin` tool now asks for individual numerals, and spelled-out digit words (English/Spanish) are accepted.
- With `log_level: debug`, PIN checks log the digit count and number of matching positions (never the PIN itself).

## 0.2.5
- Added `icon.png` and `logo.png` for the add-on.

## 0.2.4
- The assistant prompt is now configurable (`prompt` option), pre-filled with the default template. Placeholders pull the assistant name, language and accent from their own options (`{assistant_name}`, `{language}`, `{language_name}`, `{accent_line}`) and the PIN rules from `pin_enabled` (`{pin_rules}`). An empty value uses the built-in template. `extra_instructions` is still appended after it.

## 0.2.3
- Fix: the assistant greeted in English until the caller spoke. The greeting instruction was in English and did not name a language. It now states the configured language explicitly, and the instructions use the language name ("Spanish") instead of only the ISO code.
- New optional `accent` setting to steer the accent/dialect (e.g. Castilian Spanish).

## 0.2.2
- Fix: calls were dropped about 2 seconds after being answered. Asterisk's `AudioSocket()` application hangs up when it sees no activity for 2000 ms, and some carriers only start sending media after receiving some. The bridge now streams silence to Asterisk continuously from the first moment (also while connecting to OpenAI and whenever the assistant is not speaking).

## 0.2.1
- Project made English-only and generic: code, comments, logs, default prompts, tool names (`home`, `verify_pin`, `hang_up`) and documentation. The spoken language is controlled by the `language` option. A Spanish UI translation is kept.
- New `bind_address` option (default `127.0.0.1`) for setups where Asterisk runs on another machine; it logs a warning when it is not loopback.
- Neutral defaults: `assistant_name: Assistant`, `language: en`.

## 0.2.0
- Configurable model: `gpt-realtime-2.1` (default), `gpt-realtime-2.1-mini`, `gpt-realtime-2`... any `gpt-realtime-*` Realtime API model. `gpt-live-*` is rejected with a clear message (different API, not supported yet). Warning for models retiring on 2027-01-20.
- New `pin_enabled` option to require or disable the PIN. With the PIN disabled the assistant never mentions a PIN and a warning is logged at startup.
- New `fallback_agent` option: if the main Home Assistant conversation agent cannot handle a request, another one (e.g. `conversation.openai_conversation`) is tried, like "prefer local intents" in an Assist pipeline.
- Repository ready to be added to the Home Assistant add-on store.

## 0.1.0
- First version: AudioSocket <-> OpenAI Realtime bridge (`audio/pcmu`, no resampling).
- Barge-in: local audio queue, instant flush and `conversation.item.truncate`.
- Tools to control the home, verify a PIN and hang up.
- PIN by voice or DTMF keypad, lockout after failed attempts.
- Listens on 127.0.0.1 only; only outbound connections to OpenAI.
