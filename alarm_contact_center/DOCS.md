# Alarm Contact Center

Alerts a list of contacts when the Alarmo alarm goes off: phone calls through Asterisk (via the Asterisk Realtime Bridge), Home Assistant app notifications, email and Telegram.

> **Status: phase 2.** The call chain and the Home Assistant app notifications work. Email, Telegram, Frigate snapshots and the trunk test come in later phases.

## How an alarm is handled

1. The alarm entity (Alarmo by default) goes to `triggered`: an incident starts and every contact with the HA app gets a critical notification with a **Lo atiendo** button.
2. The App waits **Espera antes de empezar a llamar** (30 s by default). If the alarm is disarmed in that time, nobody is called.
3. Alarm calls take priority: other runs of the call script are stopped and any call on the line is hung up.
4. Active contacts with a phone are called in order through the call script (`script.llamada_saliente`), with each contact's retries. The chain is circular: after the last contact it goes back to the first, for the configured rounds, or with no limit until someone confirms (capped by a maximum time).
5. When someone confirms, on the phone or with the app button, the chain stops: whoever is on the phone is told, a call still ringing is withdrawn, and everyone's notification is updated. **Nothing here ever disarms the alarm.**

A voicemail (recognised by the assistant, see **Ajustes → Buzón de voz**) or a person who never answers the prompts does not count as a confirmation: the chain moves on to the next contact.
6. If the trunk is down (`CHANUNAVAIL` or `CONGESTION`), the App stops calling and says so in the notifications.
7. **Taking charge is not the end.** After someone confirms (or the chain ends with nobody answering), the incident stays **open**: cameras and sensors keep going into the timeline, because an intruder may still be around. When the alarm is **disarmed**, it keeps recording for **Seguir registrando después del desarme** (10 minutes by default) and then **closes**. If nobody disarms it, it closes after **Cerrar si nadie desarma tras** (120 minutes by default). Entries after the disarm are marked "después del desarme". While a real alarm is open, test calls and drills cannot start; a new alarm closes the open one.

External contacts always call in query-only mode. Contacts allowed to manage call in Management mode with their Home Assistant person, so the Bridge asks for their PIN.

**Simulacro** on the home page runs the whole chain as if the alarm had triggered, without touching the alarm (no siren): you choose who takes part and which sensor "triggers". Notifications and calls say it is a drill, **Simular desarme** (also after someone has taken charge) tests the disarm and closes the drill like a real disarm, it never takes the phone line from other calls, and a real alarm cancels it.

**Probar una llamada** on the home page calls one contact once, with a message that starts with "Esto es una prueba". It sends no notifications, cannot run during a real alarm, and a real alarm cancels it.

The panel itself is in Spanish, because it is used by the household.

## Panel

After installing and starting the App, open it from its page (**Open web UI**). To have it in the sidebar, turn on **Show in sidebar** on the App's Info tab. Only administrators can open it.

### Contacts

- **Home Assistant person**: picked from the list of persons; the name comes from Home Assistant. Can receive app notifications and, when enabled, **manage the home during the call** (Management profile, PIN required).
- **External contact**: free-text name. Calls are always **query only**: they can ask questions but never change anything.

Each contact needs at least one way to be reached (phone, email, Telegram or app notification). The order of the list is the calling order.

### Permission to manage

It can only be enabled for Home Assistant persons that have a phone number. When the Bridge uses **individual PINs**, the person must also have a PIN in the Bridge. The App checks this on the server when saving, not only in the UI.

To let the App know the PIN mode, set the entity that reports it in **Ajustes → Asterisk Realtime Bridge**. That entity exposes the mode (`global` or `individual`) and the persons with a PIN, never the PINs themselves.

## Trunk monitoring

Two checks, set up in **Ajustes → Vigilancia de la troncal**:

- **Registration:** the trunk registration and the Asterisk connection entities (by default `binary_sensor.siptrunk_registered` and `binary_sensor.ami_connected`). If either stays off for the configured minutes, everyone with the HA app is told, and again when it comes back.
- **Stability:** every drop is counted, even a short one. If there are too many in a time window (by default 3 in 60 minutes), the household is told the line is unstable, once per window. The panel shows the drops of the last 24 hours and 7 days.
- **Real test call:** at the chosen time, the App sends the Bridge `{"action": "trunk_test", "test_id": "...", "number": "<the line's own number>", "timeout": 20}`. The Bridge calls that number through the trunk; the call must come back in on the same line and be dropped before it is answered. It answers with the event `asterisk_realtime_bridge_trunk`: `{"test_id": "...", "result": "ok" | "out_only" | "failed", "dialstatus": "..."}`. `ok`: it went out and came back. `out_only`: the call went out but did not come back; many operators refuse calls to the line's own number (`BUSY`), so on those lines this is the normal result and counts as a working trunk. `failed`: it never left (`CHANUNAVAIL`, `CONGESTION`...). A failed test alerts once; the recovery is announced too. While the Bridge does not answer these tests, the panel says so and nothing is alerted.

Tests never run while an alarm, a drill or a test call is using the line: they wait until it is free.

## Cameras (Frigate)

The App does not talk to Frigate or MQTT directly. It follows the `input_text` helpers that a Home Assistant automation fills on every Frigate person alert, one helper per camera, with the value:

```
<review id>,<detection id>,<detection id>...
```

for example `1795354401.831865-vqtljq,1795354411.489668-aradwo`. An automation on the MQTT topic `frigate/reviews` that keeps only `severity: alert` reviews with a `person` object does this in two actions; the App never decides which cameras or zones count, the automation does.

- **Which helpers:** **Ajustes → Cámaras (Frigate)**, one entity per line. Left empty, every `input_text` whose value looks like the above is used. The camera name comes from the helpers' friendly names, without the words they all share ("Person event garden" and "Person event garage" become "Garden" and "Garage").
- **In the incident:** alerts from the chosen minutes before the trigger (default 5) and every alert while the chain runs are added to the timeline, with a photo. The App keeps its own copy of the snapshot (in `/data/media`, deleted after the chosen days, default 90) and asks Frigate to retain the event (`frigate/event/retain` through the Frigate integration), so the clip is not deleted by Frigate's retention. Photos and clips come straight from Frigate's API (`/api/events/<id>/snapshot.jpg` and `clip.mp4`): its address is taken from the Frigate integration (the entry's `host:port`), or set in **Dirección de Frigate**. The integration's notification proxy is not used: it fails for requests that come through the Supervisor, the only way an App can reach Home Assistant.
- **Incident sensors:** entities listed under **Registro del incidente** (doors, windows, motion) are added to the timeline when they change during an incident.
- **Home page:** the last person seen and the cameras being followed.

Drills include cameras and sensors too; test calls do not.

## Email and Telegram

Contacts with an email or a Telegram chat are told alongside the calls. Nothing is sent if the alarm is disarmed within **Espera antes de avisar por Telegram y correo** (30 s by default). Drills are sent too, marked as such, only to the contacts taking part; test calls are not.

- **Telegram:** one message per contact with the timeline (a coloured dot per entry, as in the panel), edited as the incident goes on (at most every few seconds; edits are silent), a short new message when someone takes charge or nobody does, when the alarm is disarmed and when the incident closes, and the photos of people seen by the cameras as they arrive. A message that is edited cannot hold pictures, so photos and videos are numbered: the entry says 📷1 and the photo comes with "📷1 · Persona en Entrada · 17:27:22". The video (🎬1) is sent when Frigate closes the alert, which is when its clip exists (**Enviar vídeos por Telegram**, up to 50 MB; not by email, where several clips would exceed what many servers accept).
- **Email:** one when the chain of calls ends, with the timeline so far (who took charge, or that nobody did; not sent when the alarm was disarmed during the calls), and the final report when the incident closes, with the whole timeline and up to 12 photos attached.
- Photos go only when **Enviar fotos por Telegram y correo** is on. A message that cannot be sent is written in the timeline.

Setting up, in **Ajustes**:

- **Correo:** the SMTP account the App sends from (server, port, STARTTLS/SSL, user, password, sender address and name; the address needs no mailbox, only to be allowed by the sending service). Gmail and Outlook need an app password. **Probar** sends a test email.
- **Telegram:** create a bot with @BotFather (`/newbot`) and paste its token. Use a bot only for this App: the chat finder reads the bot's pending updates, which another program polling the same bot would take first.
- **Contact:** the email, and the Telegram chat: the person sends any message to the bot, then **Buscar** lists the chats that wrote to it. **Probar** sends a test message.

The SMTP password and the bot token are stored inside the App only: the panel shows whether they are set but never their value, and exports leave them out (an import keeps the ones already stored).

## Sensor for Assist

When the MQTT integration is set up in Home Assistant, the App publishes (retained, through `mqtt.publish`) the sensor `sensor.alarm_contact_center_incident`, "Último incidente de alarma". Its state is one Spanish sentence about the last real alarm (when, which sensor, who handled it, the last person seen), and its attributes hold the details and the last 20 timeline entries. Expose it to Assist to ask about the last alarm by voice. Drills and test calls are not published.

## Data

The configuration is stored in `/data/contact_center.json` the last 50 incidents in `/data/incidents.json` and the incidents' photos in `/data/media`, inside the App; both are included in Home Assistant backups. **Ajustes → Copia de la configuración** exports and imports contacts and settings (the file contains phone numbers and emails, but not the SMTP password nor the bot token).

## Requirements

- Optional: the Frigate integration (or Frigate's address), for photos and clips, and the MQTT integration, for the Assist sensor.
- The Asterisk Realtime Bridge App (0.9.2 or later) and its call script (`script.llamada_saliente`). When the Bridge is installed from the same repository as this App, its id is found automatically; otherwise set it in **Ajustes → Integración con Home Assistant**, where the alarm entity and the call script can be changed too.

## Options

| Option | Description |
|--------|-------------|
| `log_level` | Log level: `debug`, `info`, `warning` or `error`. |
