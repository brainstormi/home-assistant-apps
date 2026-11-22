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

External contacts always call in query-only mode. Contacts allowed to manage call in Management mode with their Home Assistant person, so the Bridge asks for their PIN.

**Simulacro** on the home page runs the whole chain as if the alarm had triggered, without touching the alarm (no siren): you choose who takes part and which sensor "triggers". Notifications and calls say it is a drill, **Simular desarme** tests the disarm path, it never takes the phone line from other calls, and a real alarm cancels it.

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

## Data

The configuration is stored in `/data/contact_center.json` and the last 50 incidents in `/data/incidents.json`, inside the App; both are included in Home Assistant backups. **Ajustes → Copia de la configuración** exports and imports contacts and settings (the file contains phone numbers and emails).

## Requirements

- The Asterisk Realtime Bridge App (0.9.2 or later) and its call script (`script.llamada_saliente`). When the Bridge is installed from the same repository as this App, its id is found automatically; otherwise set it in **Ajustes → Integración con Home Assistant**, where the alarm entity and the call script can be changed too.

## Options

| Option | Description |
|--------|-------------|
| `log_level` | Log level: `debug`, `info`, `warning` or `error`. |
