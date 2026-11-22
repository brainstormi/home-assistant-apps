/*
 * Alarm Contact Center card: the latest incident (or drill) in a Home Assistant
 * dashboard. Installed and kept up to date by the Alarm Contact Center App.
 *
 * It reads the sensor the App publishes (sensor.alarm_contact_center_incident, or
 * sensor.alarm_contact_center_drill): its state is a one-line summary and its
 * attributes hold the timeline. Photos and clips of people seen by the cameras come
 * through the Frigate integration's notification proxy, so any household user can
 * open them, in the browser and in the mobile app.
 *
 *   type: custom:alarm-contact-center-card
 *   entity: sensor.alarm_contact_center_incident   # optional
 *   title: Alarma                                   # optional
 *   max_entries: 40                                 # optional
 *
 * Texts are in Spanish, like the App's panel.
 */

const CARD_VERSION = "__VERSION__";
const DEFAULT_ENTITY = "sensor.alarm_contact_center_incident";

const DOT = {
  triggered: "bad", person: "bad", error: "bad", trunk_down: "bad", notify_error: "bad",
  confirmed: "ok", acknowledged: "ok", disarmed: "ok", resolved: "ok", trunk_ok: "ok",
  sensor: "warn", profile: "warn", voicemail: "warn", round: "warn", answered: "warn",
  failed: "warn", timeout: "warn", limit: "warn",
  call: "info", closed: "muted",
};

const PHASE = {
  calling: ["En curso", "bad"],
  open: ["Pendiente de desarmar", "warn"],
  closing: ["Desarmada · sigue registrando", "info"],
  closed: ["Cerrado", "ok"],
};

const media = (id, name) => `/api/frigate/notifications/${encodeURIComponent(id)}/${name}`;

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function clock(iso) {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleTimeString("es-ES", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function day(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return "";
  const today = new Date();
  const yesterday = new Date(Date.now() - 86400000);
  if (d.toDateString() === today.toDateString()) return "hoy";
  if (d.toDateString() === yesterday.toDateString()) return "ayer";
  return d.toLocaleDateString("es-ES", { day: "numeric", month: "short" });
}

class AlarmContactCenterCard extends HTMLElement {
  static getStubConfig() {
    return { entity: DEFAULT_ENTITY };
  }

  static getConfigForm() {
    return {
      schema: [
        { name: "entity", required: true, selector: { entity: { domain: "sensor" } } },
        { name: "title", selector: { text: {} } },
        { name: "max_entries", selector: { number: { min: 5, max: 60, mode: "box" } } },
      ],
      computeLabel: (s) => ({ entity: "Sensor del incidente", title: "Título", max_entries: "Entradas a mostrar" }[s.name]),
      computeHelper: (s) => (s.name === "entity"
        ? "sensor.alarm_contact_center_incident (alarmas) o sensor.alarm_contact_center_drill (simulacros)" : undefined),
    };
  }

  setConfig(config) {
    this._config = { entity: DEFAULT_ENTITY, max_entries: 40, ...config };
    this._key = "";
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    if (this._hass) this._render();
  }

  set hass(hass) {
    this._hass = hass;
    const st = hass.states[this._config?.entity];
    const key = st ? `${st.last_updated}` : "missing";
    if (key !== this._key) {
      this._key = key;
      this._render();
    }
  }

  getCardSize() {
    const n = (this._hass?.states[this._config?.entity]?.attributes?.events || []).length;
    return 3 + Math.min(n, this._config?.max_entries || 40) / 2;
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }

  _render() {
    if (!this.shadowRoot || !this._config) return;
    const st = this._hass?.states[this._config.entity];
    if (!st) {
      this.shadowRoot.innerHTML = `${STYLE}<ha-card><div class="empty">No existe ${esc(this._config.entity)}. ¿Está instalada y en marcha la App Alarm Contact Center, con MQTT configurado?</div></ha-card>`;
      return;
    }
    const a = st.attributes || {};
    const events = a.events || [];
    const drill = a.kind === "drill" || this._config.entity.endsWith("_drill");
    const title = this._config.title || (drill ? "Último simulacro" : "Último incidente de alarma");
    if (!events.length) {
      this.shadowRoot.innerHTML = `${STYLE}<ha-card><div class="head"><div class="title">${esc(title)}</div></div><div class="empty">${esc(st.state)}</div></ha-card>`;
      return;
    }
    const [phaseText, phaseClass] = PHASE[a.phase] || ["", "muted"];
    const numbers = new Map();
    events.forEach((e) => { if (e.event_id && !numbers.has(e.event_id)) numbers.set(e.event_id, numbers.size + 1); });
    const shown = events.slice(-this._config.max_entries);
    const cut = events.length - shown.length;
    const people = events.filter((e) => e.event_id && e.kind === "person");
    const until = a.phase === "closing" && a.recording_until ? ` hasta las ${clock(a.recording_until).slice(0, 5)}` : "";

    this.shadowRoot.innerHTML = `${STYLE}
      <ha-card>
        <div class="head">
          <div class="title">${drill ? "🧪 " : ""}${esc(title)}</div>
          <span class="chip ${phaseClass}">${esc(phaseText + until)}</span>
        </div>
        <div class="summary">${esc(st.state)}</div>
        ${people.length ? `<div class="gallery">${people.map((e) => `
          <button class="thumb" data-id="${esc(e.event_id)}" data-n="${numbers.get(e.event_id)}" data-text="${esc(e.text)}" data-t="${esc(e.t)}" title="Ver foto y vídeo">
            <img loading="lazy" src="${media(e.event_id, "thumbnail.jpg")}" alt="${esc(e.text)}">
            <span class="badge">📷${numbers.get(e.event_id)} · ${esc(clock(e.t).slice(0, 5))}</span>
          </button>`).join("")}</div>` : ""}
        <ol class="timeline">
          ${cut > 0 ? `<li class="more">… ${cut} entradas anteriores</li>` : ""}
          ${shown.map((e) => `
            <li>
              <span class="dot ${DOT[e.kind] || "muted"}"></span>
              <span class="time">${esc(clock(e.t))}</span>
              <span class="text">${esc(e.text)}${e.event_id ? ` <button class="link" data-id="${esc(e.event_id)}" data-n="${numbers.get(e.event_id)}" data-text="${esc(e.text)}" data-t="${esc(e.t)}">📷${numbers.get(e.event_id)}</button>` : ""}
                ${e.detail ? `<span class="detail">${esc(e.detail)}</span>` : ""}</span>
            </li>`).join("")}
        </ol>
        <div class="foot">${esc(day(events[0].t))} · Alarm Contact Center</div>
      </ha-card>
      <div class="viewer" hidden></div>`;
    this.shadowRoot.querySelectorAll(".thumb img").forEach((img) => img.addEventListener("error", () => {
      img.replaceWith(Object.assign(document.createElement("span"), { className: "noimg", textContent: "Sin foto" }));
    }));
    this.shadowRoot.querySelectorAll("[data-id]").forEach((b) => b.addEventListener("click", () => this._open(b.dataset)));
  }

  _open({ id, n, text, t }) {
    const v = this.shadowRoot.querySelector(".viewer");
    v.innerHTML = `
      <div class="panel" role="dialog" aria-label="${esc(text)}">
        <div class="vhead"><div><b>📷${esc(n)} ${esc(text)}</b><div class="detail">${esc(day(t))}, ${esc(clock(t))}</div></div>
          <button class="close" aria-label="Cerrar">✕</button></div>
        <img src="${media(id, "snapshot.jpg")}" alt="${esc(text)}">
        <div class="vfoot"><button class="play">▶ Ver vídeo</button></div>
      </div>`;
    v.hidden = false;
    const close = () => { v.hidden = true; v.innerHTML = ""; document.removeEventListener("keydown", onKey); };
    const onKey = (ev) => { if (ev.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    v.addEventListener("click", (ev) => { if (ev.target === v) close(); });
    v.querySelector(".close").addEventListener("click", close);
    v.querySelector(".play").addEventListener("click", (ev) => {
      const video = Object.assign(document.createElement("video"), { controls: true, autoplay: true, playsInline: true, src: media(id, "clip.mp4") });
      video.addEventListener("error", () => { ev.target.parentElement.textContent = "El vídeo todavía no está disponible o ya no existe."; });
      ev.target.parentElement.replaceChildren(video);
    });
  }
}

const STYLE = `<style>
  ha-card { padding: 16px; display: flex; flex-direction: column; gap: 10px; }
  .head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .title { font-size: 1.15em; font-weight: 500; flex: 1 1 auto; color: var(--primary-text-color); }
  .summary { color: var(--primary-text-color); }
  .empty { color: var(--secondary-text-color); padding: 8px 0; }
  .chip { border-radius: 12px; padding: 2px 10px; font-size: 0.85em; font-weight: 500; color: #fff; }
  .chip.bad { background: var(--error-color, #db4437); }
  .chip.warn { background: var(--warning-color, #ffa600); }
  .chip.info { background: var(--info-color, #039be5); }
  .chip.ok { background: var(--success-color, #43a047); }
  .chip.muted { background: var(--secondary-text-color); }
  .gallery { display: flex; gap: 8px; overflow-x: auto; padding-bottom: 4px; }
  .thumb { position: relative; flex: 0 0 auto; width: 128px; aspect-ratio: 16 / 9; border: 0; padding: 0; border-radius: 8px;
    overflow: hidden; cursor: pointer; background: var(--divider-color); }
  .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .thumb .badge { position: absolute; left: 4px; bottom: 4px; background: rgba(0,0,0,.6); color: #fff; font-size: 11px; padding: 1px 6px; border-radius: 6px; }
  .noimg { display: flex; align-items: center; justify-content: center; height: 100%; font-size: 12px; color: var(--secondary-text-color); }
  .timeline { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
  .timeline li { display: flex; gap: 8px; align-items: baseline; font-size: 0.95em; }
  .timeline li.more { color: var(--secondary-text-color); font-size: 0.85em; }
  .dot { width: 9px; height: 9px; border-radius: 50%; flex: 0 0 9px; transform: translateY(-1px); }
  .dot.bad { background: var(--error-color, #db4437); }
  .dot.ok { background: var(--success-color, #43a047); }
  .dot.warn { background: var(--warning-color, #ffa600); }
  .dot.info { background: var(--info-color, #039be5); }
  .dot.muted { background: var(--secondary-text-color); }
  .time { font-family: var(--code-font-family, monospace); color: var(--secondary-text-color); font-size: 0.9em; white-space: nowrap; }
  .text { color: var(--primary-text-color); display: flex; flex-direction: column; }
  .detail { color: var(--secondary-text-color); font-size: 0.85em; }
  .link { border: 0; background: none; padding: 0; cursor: pointer; font: inherit; }
  .foot { color: var(--secondary-text-color); font-size: 0.8em; }
  .viewer { position: fixed; inset: 0; z-index: 10; background: rgba(0,0,0,.65); display: flex; align-items: center; justify-content: center; padding: 12px; }
  .viewer[hidden] { display: none; }
  .panel { background: var(--card-background-color, #fff); color: var(--primary-text-color); border-radius: 12px; width: min(900px, 100%);
    max-height: 95vh; overflow: auto; }
  .vhead { display: flex; justify-content: space-between; align-items: center; gap: 8px; padding: 12px 16px; }
  .panel img, .panel video { display: block; width: 100%; height: auto; background: #000; }
  .vfoot { padding: 12px 16px; display: flex; justify-content: center; }
  .play, .close { border: 1px solid var(--divider-color); background: var(--secondary-background-color, transparent); color: var(--primary-text-color);
    border-radius: 8px; padding: 6px 14px; cursor: pointer; font: inherit; }
</style>`;

if (!customElements.get("alarm-contact-center-card")) {
  customElements.define("alarm-contact-center-card", AlarmContactCenterCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "alarm-contact-center-card",
    name: "Alarm Contact Center",
    description: "Cronología del último incidente de alarma, con fotos y vídeos de las cámaras.",
    preview: false,
    documentationURL: "https://github.com/brainstormi/home-assistant-apps/tree/main/alarm_contact_center",
  });
  console.info(`%c ALARM-CONTACT-CENTER-CARD %c ${CARD_VERSION} `, "background:#c62828;color:#fff", "");
}
