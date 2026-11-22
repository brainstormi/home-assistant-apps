# Changelog

## 0.4.0

Phase 5: trunk monitoring.

- Watches the trunk registration and the Asterisk (AMI) connection. If either stays down for the configured minutes, everyone with the HA app is told, and again when it comes back. Short blips are ignored.
- Real test call on a schedule (daily, every 6 hours or weekly, at a chosen hour) or from the panel: the Asterisk Realtime Bridge calls the line's own number through the trunk and reports whether it went out and came back (`trunk_test` command, `asterisk_realtime_bridge_trunk` event). A failing test alerts once and announces the recovery. Tests wait while the line is busy with an alarm, a drill or a test call.
- A Bridge that does not answer the test yet is shown in the panel but never triggers an alert.
- Home page: "Línea telefónica" tile with the last test; settings for the line's own number, schedule, hour and the watched entities; "Probar la troncal ahora" button.
- Line events in the incident history.
- Docker health check on the panel port: with the App's watchdog switch on, the Supervisor restarts it if the panel stops answering.

## 0.3.0

Needs Asterisk Realtime Bridge 0.9.2 or later.

- When the alarm is disarmed or someone else takes it, the note to the person on the phone closes the notice (`resolve`): the assistant says it at once and stops asking for confirmation.
- If the person who presses "Lo atiendo" is the one on the phone, the assistant thanks them instead of telling them about themselves.
- Voicemail detection (on by default): the assistant recognises voicemail, leaves a short message after the beep (new setting, with the same variables as the notice) and the chain moves on. A voicemail never counts as a confirmation.
- Clearer timeline: voicemail (and whether the message was left), "contesta pero no responde" when the Bridge hangs up after unanswered prompts, "llamada retirada", "llamada cerrada".

## 0.2.3

- The Bridge App id is worked out from this App's own id (both come from the same repository, so they share its prefix). The setting is now optional: leave it empty to use the derived id.
- Default trunk status entity: `binary_sensor.siptrunk_registered`.
- Tests moved into the App folder (`alarm_contact_center/tests`).

## 0.2.2

- Fix: once an incident is settled (disarmed, acknowledged or cancelled), a call still in progress no longer changes it. Before, confirming on the phone after a disarm turned "disarmed" into "acknowledged" and sent an "Alarma atendida" notification.
- Fix: pressing "Simular desarme" twice sent the note and the notification twice. The second press now says the drill is already over.
- The live banner says when it is only waiting for the call in progress to end.

## 0.2.1

- **Drill** (Simulacro): runs the whole chain as if the alarm had triggered, without touching Alarmo (no siren). Only the chosen contacts are notified and called, every message says it is a drill, and a "Simular desarme" button tests the disarm path. It never takes the phone line from other calls, and a real alarm cancels it.
- Clearer call results: a rejected or busy call reads "rechaza la llamada o comunica".
- The timeline shows when the Bridge runs a call with a different profile than requested (e.g. Management downgraded to query only because the person has no valid PIN).

## 0.2.0

Phase 2: the alert chain.

- Watches the alarm entity (Alarmo) in real time through Home Assistant's WebSocket API, and catches up after a restart.
- On trigger: critical notification in the HA app (with a "Lo atiendo" button that stops the chain and never disarms), then a configurable wait before calling. Disarming within that wait calls nobody.
- Alarm calls take priority: other queued runs of the call script are stopped and any call on the line is hung up.
- Circular call chain through `script.llamada_saliente`: in order, contact retries handled by the App, for N rounds or with no limit (capped by a maximum time). External contacts always call as QueryOnly; persons allowed to manage call as Management with their `person` entity.
- When someone confirms (phone or app button): the chain stops, whoever is on the phone is told (Bridge `note`), a call still ringing is withdrawn (Bridge `hangup`), and the household notification is updated.
- Trunk down (`CHANUNAVAIL`/`CONGESTION`): stops calling and says so in the notifications.
- Test call to one contact from the panel; never during a real alarm, and a real alarm cancels a test.
- Incident log with a timeline (last 50), live banner with a "Parar avisos" button (does not disarm).
- Settings: unlimited rounds with a maximum chain time, alarm entity, call script and Bridge App id; export and import of the configuration.
- New dependency: aiohttp.

## 0.1.3

- Reads the Bridge PIN entity out of the box (`sensor.asterisk_realtime_bridge_pin`), including its `people_with_pin` attribute.
- Shows the Bridge's own PIN diagnostics: incomplete setup and the list of issues it reports.

## 0.1.2

- Dark theme that follows Home Assistant's dark mode (falls back to the system preference).
- New settings: wait before starting to call (default 30 s) and wait before alerting external contacts (default 30 s). If the alarm is disarmed within that time, nobody is called.
- Icon with a phone handset inside the shield.

## 0.1.1

- Renamed to **Alarm Contact Center** (slug `alarm_contact_center`). It installs as a new App: remove the old "Avisos de alarma" App.
- Configuration file renamed to `/data/contact_center.json`.
- Code comments and documentation in English. The panel stays in Spanish.
- App icon and logo.

## 0.1.0

Phase 1: configuration panel.

- Panel inside Home Assistant (Ingress), administrators only.
- Contacts: Home Assistant persons or external contacts, with phone, email, Telegram chat and app notification.
- Calling order, with buttons or drag and drop.
- "Can manage" only for HA persons and, when the Bridge uses individual PINs, only if they have a PIN.
- General settings: call message with variables and preview, call chain, trunk monitoring, Frigate and incident log.
- System status: trunk, Asterisk, OpenAI, Bridge PIN mode and alarm.
- No alerts are sent yet: the call chain comes in phase 2.
