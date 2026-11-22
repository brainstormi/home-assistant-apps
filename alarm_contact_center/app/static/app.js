"use strict";
// Alarm Contact Center panel. No dependencies, no build step.
// All URLs are relative so it works behind Ingress.
// UI text is in Spanish on purpose: it is what the household reads.

const view = document.getElementById("view");
let STATE = null;

// ------------------------------------------------------------------- helpers
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, "");
    else node.setAttribute(k, String(v));
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function icon(path, size = 18) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("width", size); svg.setAttribute("height", size);
  svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor"); svg.setAttribute("stroke-width", "2");
  svg.setAttribute("stroke-linecap", "round"); svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  const p = document.createElementNS(ns, "path");
  p.setAttribute("d", path);
  svg.append(p);
  return svg;
}
const ICON = {
  up: "M18 15l-6-6-6 6",
  down: "M6 9l6 6 6-6",
  grip: "M9 6h.01M15 6h.01M9 12h.01M15 12h.01M9 18h.01M15 18h.01",
  plus: "M12 5v14M5 12h14",
  back: "M15 18l-6-6 6-6",
  warn: "M12 9v4M12 17h.01M10.3 3.9L2 18a2 2 0 001.7 3h16.6a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z",
  check: "M5 12l5 5L20 7",
};

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
    credentials: "same-origin",
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* no body */ }
  if (!res.ok) {
    const err = new Error((data && data.error) || `Error ${res.status}`);
    err.fields = (data && data.fields) || {};
    throw err;
  }
  return data;
}

let toastTimer = null;
function toast(text, isError = false) {
  const t = document.getElementById("toast");
  t.textContent = text;
  t.className = "show" + (isError ? " error" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = ""; }, 3500);
}

async function reload() {
  STATE = await api("GET", "api/state");
  return STATE;
}

function setNav(name) {
  document.querySelectorAll("[data-nav]").forEach((a) => {
    if (a.dataset.nav === name) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
}

function render(...nodes) {
  view.replaceChildren(...nodes);
  view.focus({ preventScroll: true });
}

const AVATAR_COLORS = ["#0b6aa8", "#7b3fa0", "#2f7d4f", "#a0522d", "#3b5998", "#8a3b5c", "#2d6f73"];
function avatarColor(id) {
  let h = 0;
  for (const ch of id) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return AVATAR_COLORS[h % AVATAR_COLORS.length];
}

function channels(c) {
  const out = [];
  if (c.phone) out.push("Llamada");
  if (c.notify_service) out.push("App HA");
  if (c.telegram_chat_id) out.push("Telegram");
  if (c.email) out.push("Correo");
  return out;
}

// --------------------------------------------------------------------- home
const TRUNK_RESULT = { ok: ["Funciona", "ok"], out_only: ["Sale, no vuelve", "warn"], failed: ["No sale", "bad"], timeout: ["Sin respuesta", "warn"] };

function trunkTile(s, regValue, regClass) {
  const t = s.trunk;
  let value = regValue, cls = regClass, detail = "Registro de la troncal";
  if (t && t.last_test) {
    const [v, c] = TRUNK_RESULT[t.last_test.result] || [t.last_test.result, "warn"];
    if (regClass !== "bad") { value = v; cls = c; }
    detail = `Prueba: ${fmtTime(t.last_test.t)} · ${t.last_test.text}`;
  } else if (t && !t.number_set) {
    detail = "Sin prueba real: falta el número propio de la línea en Ajustes";
    if (regClass === "ok") cls = "warn";
  } else if (t && t.next_test) {
    detail = `Primera prueba: ${fmtTime(t.next_test)}`;
  }
  if (t && Object.keys(t.down_since || {}).length) { value = "Caída"; cls = "bad"; }
  return el("div", { class: "tile" },
    el("div", { class: "label" }, el("span", { class: "dot " + cls, "aria-hidden": "true" }), "Línea telefónica"),
    el("div", { class: "value" }, value),
    el("div", { class: "small muted" }, detail));
}
function statusTiles(s) {
  const st = s.status;
  const tile = (title, value, detail, cls) =>
    el("div", { class: "tile" },
      el("div", { class: "label" }, el("span", { class: "dot " + cls, "aria-hidden": "true" }), title),
      el("div", { class: "value" }, value),
      el("div", { class: "small muted" }, detail));

  const onoff = (x, onText, offText) => x.state === "on" ? [onText, "ok"] : x.state === "off" ? [offText, "bad"] : ["Sin datos", ""];
  const [trunkV, trunkC] = onoff(st.trunk, "Registrada", "No registrada");
  const [amiV, amiC] = onoff(st.ami, "Conectado", "Desconectado");
  const openai = st.openai.state === "on" ? ["Disponible", "ok"] : st.openai.state === "off" ? ["Respaldo local", "warn"] : ["Sin datos", ""];
  let pinMode = { global: ["PIN global", "ok"], individual: ["PIN individual", "ok"], unknown: ["Sin datos", "warn"] }[s.pins.mode] || ["Sin datos", "warn"];
  if (s.pins.mode !== "unknown" && s.pins.complete === false) pinMode = [pinMode[0] + " · incompleto", "warn"];
  const ALARM = {
    disarmed: ["Desarmada", ""], armed_away: ["Armada · ausente", "ok"], armed_home: ["Armada · en casa", "ok"],
    armed_night: ["Armada · noche", "ok"], armed_vacation: ["Armada · vacaciones", "ok"], arming: ["Armando…", "warn"],
    pending: ["Retardo de entrada", "warn"], triggered: ["¡Disparada!", "bad"], missing: ["Sin datos", ""],
  };
  const alarm = ALARM[st.alarm.state] || [st.alarm.state, ""];
  const pinDetail = s.pins.mode !== "unknown" && s.pins.complete === false
    ? "Gestión por teléfono desactivada hasta completar los PIN"
    : s.pins.mode === "individual"
    ? `${s.pins.persons.length} persona(s) con PIN`
    : s.pins.mode === "global" ? "Un PIN común para gestionar" : "El Bridge aún no publica esta información";

  return el("div", { class: "tiles" },
    trunkTile(s, trunkV, trunkC),
    tile("Asterisk (AMI)", amiV, st.ami.entity_id, amiC),
    tile("Asistente IA", openai[0], "Si falla OpenAI: Piper y Whisper locales", openai[1]),
    tile("Bridge · PIN", pinMode[0], pinDetail, pinMode[1]),
    tile("Alarma", alarm[0], st.alarm.entity_id, alarm[1]));
}

async function moveContact(ids, from, to) {
  if (to < 0 || to >= ids.length || from === to) return;
  const next = ids.slice();
  const [moved] = next.splice(from, 1);
  next.splice(to, 0, moved);
  try {
    await api("PUT", "api/order", { ids: next });
    await reload();
    showHome();
    toast("Orden guardado.");
  } catch (e) { toast(e.message, true); }
}

function contactsTable(s) {
  const ids = s.contacts.map((c) => c.id);
  if (!ids.length) {
    return el("div", { class: "empty" }, "Todavía no hay contactos. Añade el primero.");
  }
  let dragFrom = null;
  const rows = s.contacts.map((c, i) => {
    const sub = c.kind === "person" ? `Persona de HA · ${c.person}` : "Contacto externo";
    const row = el("tr", { class: c.active ? "" : "inactive", draggable: "true" },
      el("td", {},
        el("div", { class: "order" },
          el("span", { class: "handle", title: "Arrastra para mover", "aria-hidden": "true" }, icon(ICON.grip, 16)),
          el("span", { class: "num" }, i + 1),
          el("button", { class: "icon-btn", type: "button", "aria-label": `Subir a ${c.name}`, disabled: i === 0, onclick: () => moveContact(ids, i, i - 1) }, icon(ICON.up)),
          el("button", { class: "icon-btn", type: "button", "aria-label": `Bajar a ${c.name}`, disabled: i === ids.length - 1, onclick: () => moveContact(ids, i, i + 1) }, icon(ICON.down)))),
      el("td", {},
        el("div", { class: "who" },
          el("span", { class: "avatar", style: `background:${avatarColor(c.id)}`, "aria-hidden": "true" }, (c.name || "?").charAt(0).toUpperCase()),
          el("div", {}, el("div", { style: "font-weight:500" }, c.name || "(sin nombre)"), el("div", { class: "sub" }, sub)))),
      el("td", {}, el("div", { class: "chips" },
        channels(c).map((k) => el("span", { class: "chip" }, k)),
        c.warnings.length ? el("span", { class: "chip warn", title: c.warnings.join(" ") }, "Revisar") : null)),
      el("td", {}, c.can_manage
        ? el("span", { class: "chip manage" }, "Puede gestionar (PIN)")
        : el("span", { class: "chip query" }, "Solo consulta")),
      el("td", {}, c.phone ? `${c.attempts} × ${c.retry_wait} s` : "—"),
      el("td", {}, c.active ? "Activo" : "Inactivo"),
      el("td", {}, el("a", { href: `#/contacto/${encodeURIComponent(c.id)}`, class: "btn", style: "border:none" }, "Editar")));

    row.addEventListener("dragstart", (ev) => { dragFrom = i; row.classList.add("dragging"); ev.dataTransfer.effectAllowed = "move"; });
    row.addEventListener("dragend", () => row.classList.remove("dragging"));
    row.addEventListener("dragover", (ev) => { ev.preventDefault(); row.classList.add("drop-target"); });
    row.addEventListener("dragleave", () => row.classList.remove("drop-target"));
    row.addEventListener("drop", (ev) => { ev.preventDefault(); row.classList.remove("drop-target"); if (dragFrom !== null) moveContact(ids, dragFrom, i); dragFrom = null; });
    return row;
  });

  return el("div", { class: "table-wrap" },
    el("table", {},
      el("thead", {}, el("tr", {},
        el("th", { scope: "col" }, "Orden"), el("th", { scope: "col" }, "Contacto"), el("th", { scope: "col" }, "Canales"),
        el("th", { scope: "col" }, "En la llamada"), el("th", { scope: "col" }, "Intentos"), el("th", { scope: "col" }, "Estado"),
        el("th", { scope: "col" }, el("span", { class: "muted" }, "Acciones")))),
      el("tbody", {}, rows)));
}

function showHome() {
  setNav("inicio");
  const s = STATE;
  const warned = s.contacts.filter((c) => c.warnings.length);
  const activeWithCall = s.contacts.filter((c) => c.active && c.phone).length;
  render(
    el("section", { "aria-labelledby": "estado-h", style: "display:flex;flex-direction:column;gap:12px" },
      el("div", { id: "current-incident" }),
      el("h2", { id: "estado-h" }, "Estado del sistema"),
      statusTiles(s),
      activeWithCall === 0 ? el("div", { class: "banner warn" }, icon(ICON.warn, 20), "No hay ningún contacto activo con teléfono: nadie recibirá llamadas.") : null,
      s.pins.issues && s.pins.issues.length ? el("div", { class: "banner warn" }, icon(ICON.warn, 20),
        el("div", { style: "flex:1 1 320px;display:flex;flex-direction:column;gap:4px" },
          el("span", {}, "El Bridge informa de problemas con los PIN (se revisan en la configuración del Bridge):"),
          el("ul", { style: "margin:0;padding-left:20px" }, s.pins.issues.map((i) => el("li", {}, i))))) : null,
      warned.map((c) => el("div", { class: "banner warn" }, icon(ICON.warn, 20),
        el("span", { style: "flex:1 1 320px" }, `${c.name}: ${c.warnings.join(" ")}`),
        el("a", { href: `#/contacto/${encodeURIComponent(c.id)}` }, "Revisar contacto")))),
    el("section", { class: "card flush", "aria-labelledby": "contactos-h" },
      el("div", { class: "card-head" },
        el("div", { class: "grow" },
          el("h2", { id: "contactos-h" }, "Contactos, en orden de aviso"),
          el("span", { class: "small muted" }, "Se llama de arriba abajo. Las notificaciones se envían a todos a la vez.")),
        el("button", { class: "btn", type: "button", onclick: () => toggleTestBox(), "aria-expanded": "false", id: "test-toggle" }, "Probar una llamada"),
        el("button", { class: "btn", type: "button", onclick: () => toggleDrillBox(), "aria-expanded": "false", id: "drill-toggle" }, "Simulacro"),
        el("a", { class: "btn primary", href: "#/contacto/nuevo" }, icon(ICON.plus), "Añadir contacto")),
      el("div", { id: "test-box", hidden: true }),
      el("div", { id: "drill-box", hidden: true }),
      contactsTable(s)));
  refreshCurrent();
}

const STATUS_TEXT = {
  waiting: "Esperando antes de llamar", calling: "Llamando", acknowledged: "Atendida",
  disarmed: "Desarmada", no_answer: "Nadie ha confirmado", trunk_down: "Línea telefónica caída",
  cancelled: "Cancelada", failed: "Error", ok: "Recuperada",
};

function trunkSummary(t) {
  if (!t) return "";
  const parts = [];
  if (t.last_test) parts.push(`Última prueba: ${fmtTime(t.last_test.t)} · ${t.last_test.text}`);
  else parts.push("Todavía no se ha hecho ninguna prueba.");
  if (t.next_test) parts.push(`Próxima: ${fmtTime(t.next_test)}`);
  if (t.last_test && t.last_test.result === "timeout" && !t.bridge_answers) parts.push("El Bridge no responde a la prueba: necesita una versión con el comando trunk_test.");
  return parts.join(" · ");
}

function toggleTestBox() {
  const box = document.getElementById("test-box");
  const btn = document.getElementById("test-toggle");
  if (!box) return;
  const open = box.hidden;
  box.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
  if (!open) return;
  const callable = STATE.contacts.filter((c) => c.phone);
  const select = el("select", { id: "test-contact" }, callable.map((c) => el("option", { value: c.id }, `${c.name} · ${c.phone}`)));
  const go = el("button", { class: "btn primary", type: "button", disabled: !callable.length, onclick: async () => {
    go.disabled = true;
    try {
      await api("POST", "api/test", { contact_id: select.value });
      toast("Llamada de prueba en marcha.");
      box.hidden = true; btn.setAttribute("aria-expanded", "false");
      refreshCurrent();
    } catch (e) { toast(e.message, true); } finally { go.disabled = false; }
  } }, "Llamar ahora");
  box.replaceChildren(el("div", { class: "test-box" },
    el("div", { class: "field", style: "flex:1 1 260px" }, el("label", { for: "test-contact" }, "Contacto"), select),
    go,
    el("span", { class: "small muted", style: "flex:1 1 100%" },
      callable.length ? "Llama de verdad a ese contacto, una sola vez, con un mensaje que empieza por «Esto es una prueba». No envía notificaciones ni se puede hacer con una alarma en curso."
                      : "Ningún contacto tiene teléfono.")));
}

function toggleDrillBox() {
  const box = document.getElementById("drill-box");
  const btn = document.getElementById("drill-toggle");
  if (!box) return;
  const open = box.hidden;
  box.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
  if (!open) return;
  const active = STATE.contacts.filter((c) => c.active);
  const checks = active.map((c) => el("input", { type: "checkbox", value: c.id, id: `drill-${c.id}` }));
  const sensor = el("select", { id: "drill-sensor" },
    el("option", { value: "" }, "Sensor de prueba (genérico)"),
    (STATE.sensors || []).map((x) => el("option", { value: x.entity_id }, x.name)));
  const go = el("button", { class: "btn primary", type: "button", onclick: async () => {
    const ids = checks.filter((c) => c.checked).map((c) => c.value);
    if (!ids.length) { toast("Elige al menos un contacto.", true); return; }
    go.disabled = true;
    try {
      await api("POST", "api/drill", { contact_ids: ids, sensor: sensor.value });
      toast("Simulacro en marcha.");
      box.hidden = true; btn.setAttribute("aria-expanded", "false");
      refreshCurrent();
    } catch (e) { toast(e.message, true); } finally { go.disabled = false; }
  } }, "Empezar simulacro");
  box.replaceChildren(el("div", { class: "test-box" },
    el("fieldset", { class: "drill-people" }, el("legend", {}, "Quién participa"),
      active.map((c, i) => el("label", { class: "check" }, checks[i], `${c.name}${c.phone ? "" : " (sin teléfono: solo notificación)"}`))),
    el("div", { class: "field", style: "flex:1 1 260px" }, el("label", { for: "drill-sensor" }, "Sensor que «salta»"), sensor),
    go,
    el("span", { class: "small muted", style: "flex:1 1 100%" },
      "Hace todo como en una alarma real (notificación con «Lo atiendo», espera, llamadas en cadena), pero sin tocar Alarmo: sin sirena. " +
      "Solo avisa a quien marques, y todo dice que es un simulacro. Durante la espera puedes pulsar «Simular desarme».")));
}

let currentTimer = null;
async function refreshCurrent() {
  clearTimeout(currentTimer);
  const slot = document.getElementById("current-incident");
  if (!slot) return;
  try {
    const data = await api("GET", "api/incidents");
    const cur = data.current;
    if (!cur) { slot.replaceChildren(); }
    else {
      const last = cur.events[cur.events.length - 1] || {};
      slot.replaceChildren(el("div", { class: "banner " + (cur.kind === "alarm" ? "alarm" : "info") },
        icon(ICON.warn, 20),
        el("div", { style: "flex:1 1 320px;display:flex;flex-direction:column;gap:2px" },
          el("strong", {}, cur.kind === "alarm" ? `Alarma en curso · ${cur.sensor}` : cur.kind === "drill" ? `Simulacro en curso · ${cur.sensor}` : "Prueba en curso"),
          el("span", {}, `${STATUS_TEXT[cur.status] || cur.status} · ${last.text || ""}` +
            (["calling", "waiting"].includes(cur.status) ? "" : " · terminando la llamada en curso"))),
        el("a", { href: "#/incidentes" }, "Ver detalle"),
        cur.kind === "drill" && ["calling", "waiting"].includes(cur.status) ? el("button", { class: "btn", type: "button", onclick: async () => {
          try { await api("POST", "api/drill/disarm"); toast("Desarme simulado."); refreshCurrent(); } catch (e) { toast(e.message, true); }
        } }, "Simular desarme") : null,
        el("button", { class: "btn danger", type: "button", onclick: async () => {
          if (!window.confirm("¿Parar las llamadas y avisos? La alarma NO se desarma.")) return;
          try { await api("POST", "api/cancel"); toast("Avisos parados."); refreshCurrent(); } catch (e) { toast(e.message, true); }
        } }, "Parar avisos")));
    }
  } catch (_) { /* keep the last view */ }
  currentTimer = setTimeout(refreshCurrent, 3000);
}

// ------------------------------------------------------------------- contact
function field(id, labelText, input, help, error) {
  if (error) { input.setAttribute("aria-invalid", "true"); input.setAttribute("aria-describedby", `${id}-err`); }
  return el("div", { class: "field" },
    el("label", { for: id }, labelText), input,
    error ? el("span", { class: "error", id: `${id}-err` }, error) : null,
    help ? el("span", { class: "help" }, help) : null);
}

function showContact(id, draft = null, errors = {}) {
  setNav("inicio");
  const s = STATE;
  const isNew = id === "nuevo";
  const saved = isNew ? null : s.contacts.find((c) => c.id === id);
  if (!isNew && !saved) { render(el("p", {}, "Ese contacto no existe. ", el("a", { href: "#/" }, "Volver"))); return; }
  const c = draft || Object.assign({
    kind: "external", person: "", name: "", phone: "", email: "", telegram_chat_id: "", notify_service: "",
    attempts: 1, retry_wait: 60, confirm: "voice_or_dtmf", can_manage: false, active: true,
  }, saved || {});

  const takenPersons = new Set(s.contacts.filter((x) => x.kind === "person" && x.id !== id).map((x) => x.person));
  const rerender = () => showContact(id, collect(), {});

  // Fields
  const kindPerson = el("input", { type: "radio", name: "kind", value: "person", checked: c.kind === "person", onchange: rerender });
  const kindExternal = el("input", { type: "radio", name: "kind", value: "external", checked: c.kind === "external", onchange: rerender });
  const person = el("select", { id: "person", onchange: rerender },
    el("option", { value: "" }, "— Elige una persona —"),
    s.persons.map((p) => el("option", { value: p.entity_id, selected: p.entity_id === c.person, disabled: takenPersons.has(p.entity_id) },
      `${p.name} · ${p.entity_id}${takenPersons.has(p.entity_id) ? " (ya está en la lista)" : ""}`)));
  const name = el("input", { id: "name", type: "text", value: c.name, maxlength: "60", autocomplete: "off" });
  const phone = el("input", { id: "phone", type: "tel", value: c.phone, inputmode: "tel", autocomplete: "off" });
  const email = el("input", { id: "email", type: "email", value: c.email, autocomplete: "off" });
  const telegram = el("input", { id: "telegram", type: "text", value: c.telegram_chat_id, inputmode: "numeric", autocomplete: "off" });
  const notify = el("select", { id: "notify" },
    el("option", { value: "" }, "Sin notificación"),
    s.notify_targets.map((n) => el("option", { value: n, selected: n === c.notify_service }, n.replace(/^mobile_app_/, ""))));
  const attempts = el("input", { id: "attempts", type: "number", min: "1", max: "5", value: c.attempts });
  const retry = el("input", { id: "retry", type: "number", min: "10", max: "600", value: c.retry_wait });
  const confirm = el("select", { id: "confirm" },
    [["voice_or_dtmf", "Voz o tecla 1"], ["dtmf", "Solo tecla 1"], ["voice", "Solo voz"]].map(([v, t]) => el("option", { value: v, selected: c.confirm === v }, t)));
  const manage = el("input", { id: "manage", type: "checkbox", checked: c.can_manage });
  const active = el("input", { id: "active", type: "checkbox", checked: c.active });

  function collect() {
    const kind = kindPerson.checked ? "person" : "external";
    return {
      kind, person: kind === "person" ? person.value : "", name: name.value,
      phone: phone.value, email: email.value, telegram_chat_id: telegram.value,
      notify_service: kind === "person" ? notify.value : "",
      attempts: Number(attempts.value), retry_wait: Number(retry.value), confirm: confirm.value,
      can_manage: kind === "person" && manage.checked, active: active.checked,
    };
  }

  const isPerson = c.kind === "person";
  const personName = (s.persons.find((p) => p.entity_id === c.person) || {}).name || "";

  // PIN status for the "Can manage" box
  let pinLine = null;
  if (isPerson && c.person) {
    if (s.pins.mode === "global") pinLine = el("span", { class: "ok" }, "El Bridge usa un PIN global: se pedirá ese PIN.");
    else if (s.pins.mode === "individual" && s.pins.persons.includes(c.person)) pinLine = el("span", { class: "ok" }, `${personName || c.person} tiene PIN en el Bridge.`);
    else if (s.pins.mode === "individual") pinLine = el("span", { class: "bad" }, `${personName || c.person} no tiene PIN en el Bridge: no puede gestionar.`);
    else pinLine = el("span", { class: "small muted" }, "No se puede comprobar el PIN: el Bridge aún no publica esa información.");
  }
  const manageBlocked = isPerson && s.pins.mode === "individual" && c.person && !s.pins.persons.includes(c.person);
  if (manageBlocked) manage.disabled = true;

  async function save() {
    try {
      const body = collect();
      if (isNew) await api("POST", "api/contacts", body);
      else await api("PUT", `api/contacts/${encodeURIComponent(id)}`, body);
      await reload();
      toast("Contacto guardado.");
      location.hash = "#/";
    } catch (e) {
      toast(e.message, true);
      showContact(id, collect(), e.fields || {});
    }
  }

  async function remove() {
    if (!window.confirm(`¿Eliminar a ${c.name || "este contacto"} de la lista?`)) return;
    try {
      await api("DELETE", `api/contacts/${encodeURIComponent(id)}`);
      await reload();
      toast("Contacto eliminado.");
      location.hash = "#/";
    } catch (e) { toast(e.message, true); }
  }

  view.className = "narrow";
  render(
    el("div", { style: "display:flex;align-items:center;gap:8px" },
      el("a", { href: "#/", class: "icon-btn", "aria-label": "Volver a contactos" }, icon(ICON.back, 22)),
      el("h2", {}, isNew ? "Nuevo contacto" : `Editar contacto · ${saved.name}`)),

    el("section", { class: "card", "aria-labelledby": "quien-h" },
      el("h2", { id: "quien-h", style: "font-size:17px" }, "Quién es"),
      el("div", { class: "field" },
        el("span", { class: "label", id: "kind-l" }, "Tipo de contacto"),
        el("div", { class: "segmented", role: "radiogroup", "aria-labelledby": "kind-l" },
          el("label", {}, kindPerson, "Persona de Home Assistant"),
          el("label", {}, kindExternal, "Contacto externo (sin cuenta de HA)"))),
      isPerson
        ? el("div", { class: "grid" },
            field("person", "Persona de HA", person, "El nombre se toma de la persona: no se escribe a mano.", errors.person),
            el("div", { class: "field" }, el("span", { class: "label" }, "Nombre que usará el asistente"),
              el("span", { class: "readonly" }, personName || "—")))
        : field("name", "Nombre", name, "Cómo le llamará el asistente al teléfono.", errors.name)),

    el("section", { class: "card", "aria-labelledby": "canales-h" },
      el("div", {}, el("h2", { id: "canales-h", style: "font-size:17px" }, "Cómo avisarle"),
        el("span", { class: "small muted" }, "Al menos un canal. Las notificaciones se envían al momento; la llamada, cuando le toque.")),
      el("div", { class: "grid" },
        field("phone", "Teléfono", phone, "9 cifras, con o sin +34.", errors.phone),
        field("email", "Correo electrónico", email, "Aviso inicial e informe final.", errors.email),
        field("telegram", "Chat de Telegram", telegram, "Número del chat. El bot llegará en una fase posterior.", errors.telegram_chat_id),
        isPerson ? field("notify", "Notificación de la app HA", notify, "Con botón «Lo atiendo» (no desarma la alarma).", errors.notify_service) : null)),

    el("section", { class: "card", "aria-labelledby": "llamada-h" },
      el("h2", { id: "llamada-h", style: "font-size:17px" }, "La llamada"),
      el("div", { class: "grid" },
        field("attempts", "Intentos seguidos", attempts, null, errors.attempts),
        field("retry", "Espera entre intentos (s)", retry, null, errors.retry_wait),
        field("confirm", "Cómo confirma", confirm, null, errors.confirm)),
      isPerson
        ? el("div", { class: "manage-box" },
            el("label", { class: "check title" }, manage, "Puede gestionar la casa durante la llamada"),
            el("span", { class: "small" }, "La llamada usa el perfil Management: para actuar, el asistente pide el PIN. Sin PIN, solo puede consultar."),
            pinLine,
            errors.can_manage ? el("span", { class: "bad" }, errors.can_manage) : null)
        : el("div", { class: "banner info" }, "Un contacto externo siempre llama en solo consulta: puede preguntar, nunca cambiar nada.")),

    el("section", { class: "card", "aria-labelledby": "activo-h" },
      el("h2", { id: "activo-h", style: "font-size:17px" }, "Estado"),
      el("label", { class: "check" }, active, "Activo"),
      el("span", { class: "small muted" }, "Desactívalo para quitarlo temporalmente sin perder sus datos.")),

    el("div", { class: "actions" },
      isNew ? null : el("button", { class: "btn danger left", type: "button", onclick: remove }, "Eliminar contacto"),
      el("a", { class: "btn", href: "#/" }, "Cancelar"),
      el("button", { class: "btn primary", type: "button", onclick: save }, "Guardar")));
}

// ------------------------------------------------------------------ settings
function showSettings(draft = null, errors = {}) {
  setNav("ajustes");
  view.className = "narrow";
  const st = draft || STATE.settings;
  const num = (id, value, min, max) => el("input", { id, type: "number", min, max, value });

  const message = el("textarea", { id: "message", rows: "3" }, st.message);
  const confirmText = el("input", { id: "confirm_text", type: "text", value: st.confirm_text });
  const detectVm = el("input", { id: "detect_voicemail", type: "checkbox", checked: st.detect_voicemail });
  const vmMessage = el("textarea", { id: "voicemail_message", rows: "2" }, st.voicemail_message);
  const callDelay = num("call_delay", st.call_delay, 0, 600);
  const externalDelay = num("external_notify_delay", st.external_notify_delay, 0, 600);
  const rounds = el("select", { id: "rounds" },
    [[0, "Sin límite (hasta que alguien confirme)"], ...[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((n) => [n, n === 1 ? "1 ronda" : `${n} rondas`])]
      .map(([v, t]) => el("option", { value: v, selected: Number(st.rounds) === v }, t)));
  const maxChain = num("max_chain_minutes", st.max_chain_minutes, 5, 240);
  const alarmEntity = el("input", { id: "alarm_entity", type: "text", value: st.alarm_entity });
  const callScript = el("input", { id: "call_script", type: "text", value: st.call_script });
  const autoBridge = (STATE.defaults && STATE.defaults.bridge_addon) || "";
  const bridgeAddon = el("input", { id: "bridge_addon", type: "text", value: st.bridge_addon,
    placeholder: autoBridge ? `${autoBridge} (automático)` : "abcd1234_asterisk_realtime_bridge" });
  const answer = num("answer_timeout", st.answer_timeout, 20, 300);
  const pause = num("round_pause_min", st.round_pause_min, 0, 30);
  const trunkTest = el("select", { id: "trunk_test" },
    [["daily", "Cada día"], ["every_6h", "Cada 6 horas"], ["weekly", "Cada semana (lunes)"], ["off", "Desactivada"]]
      .map(([v, t]) => el("option", { value: v, selected: st.trunk_test === v }, t)));
  const trunkAlert = num("trunk_alert_after_min", st.trunk_alert_after_min, 1, 60);
  const trunkHour = el("select", { id: "trunk_test_hour" },
    Array.from({ length: 24 }, (_, h) => el("option", { value: h, selected: Number(st.trunk_test_hour) === h }, `${String(h).padStart(2, "0")}:07`)));
  const trunkNumber = el("input", { id: "trunk_test_number", type: "tel", value: st.trunk_test_number, inputmode: "tel", autocomplete: "off" });
  const trunkEntity = el("input", { id: "trunk_entity", type: "text", value: st.trunk_entity });
  const amiEntity = el("input", { id: "ami_entity", type: "text", value: st.ami_entity });
  const trunkInfo = el("div", { class: "small muted" }, trunkSummary(STATE.trunk));
  const trunkBtn = el("button", { class: "btn", type: "button", onclick: async () => {
    trunkBtn.disabled = true; trunkInfo.textContent = "Probando… (hasta un minuto y medio)";
    try {
      const t = await api("POST", "api/trunk/test");
      toast(t.text, t.result !== "ok" && t.result !== "out_only");
      await reload(); trunkInfo.textContent = trunkSummary(STATE.trunk);
    } catch (e) { toast(e.message, true); trunkInfo.textContent = trunkSummary(STATE.trunk); }
    finally { trunkBtn.disabled = false; }
  } }, "Probar la troncal ahora");
  const pre = num("frigate_pre_minutes", st.frigate_pre_minutes, 0, 30);
  const retention = num("media_retention_days", st.media_retention_days, 7, 365);
  const photos = el("input", { id: "send_photos_external", type: "checkbox", checked: st.send_photos_external });
  const entities = el("textarea", { id: "incident_entities", rows: "4", placeholder: "binary_sensor.movimiento_salon_occupancy" }, (st.incident_entities || []).join("\n"));
  const pinEntity = el("input", { id: "bridge_pin_entity", type: "text", value: st.bridge_pin_entity, placeholder: "sensor.asterisk_realtime_bridge_pin (por defecto)" });

  const preview = el("div", { class: "preview" });
  const updatePreview = () => {
    preview.textContent = "«" + message.value
      .replaceAll("{nombre}", "Pablo").replaceAll("{sensor}", "puerta del garaje")
      .replaceAll("{zona}", "garaje").replaceAll("{hora}", "23:41")
      .replaceAll("{atendido}", "Luis y Marta no han contestado.") + "»";
  };
  message.addEventListener("input", updatePreview);
  updatePreview();

  function collect() {
    return {
      message: message.value, confirm_text: confirmText.value,
      detect_voicemail: detectVm.checked, voicemail_message: vmMessage.value,
      call_delay: Number(callDelay.value), external_notify_delay: Number(externalDelay.value),
      rounds: Number(rounds.value), max_chain_minutes: Number(maxChain.value),
      answer_timeout: Number(answer.value), round_pause_min: Number(pause.value),
      alarm_entity: alarmEntity.value, call_script: callScript.value, bridge_addon: bridgeAddon.value,
      trunk_test: trunkTest.value, trunk_alert_after_min: Number(trunkAlert.value),
      trunk_test_hour: Number(trunkHour.value), trunk_test_number: trunkNumber.value,
      trunk_entity: trunkEntity.value, ami_entity: amiEntity.value,
      frigate_pre_minutes: Number(pre.value), media_retention_days: Number(retention.value),
      send_photos_external: photos.checked,
      incident_entities: entities.value.split(/[\s,]+/).filter(Boolean),
      bridge_pin_entity: pinEntity.value,
    };
  }

  async function save() {
    try {
      await api("PUT", "api/settings", collect());
      await reload();
      toast("Ajustes guardados.");
      showSettings();
    } catch (e) {
      toast(e.message, true);
      showSettings(collect(), e.fields || {});
    }
  }

  render(
    el("section", { class: "card", "aria-labelledby": "msg-h" },
      el("h2", { id: "msg-h", style: "font-size:17px" }, "Mensaje de la llamada"),
      field("message", "Aviso que lee el asistente al descolgar", message, null, errors.message),
      el("div", { class: "chips", style: "align-items:center" }, el("span", { class: "small muted" }, "Variables:"),
        ["{nombre}", "{sensor}", "{zona}", "{hora}", "{atendido}"].map((v) => el("code", { class: "var" }, v))),
      el("div", {}, el("div", { class: "small muted", style: "margin-bottom:4px" }, "Vista previa"), preview),
      field("confirm_text", "Texto al pedir confirmación", confirmText, null, errors.confirm_text)),

    el("section", { class: "card", "aria-labelledby": "vm-h" },
      el("h2", { id: "vm-h", style: "font-size:17px" }, "Buzón de voz"),
      el("label", { class: "check" }, detectVm, "Detectar el buzón de voz"),
      el("span", { class: "small muted" }, "El asistente reconoce el saludo grabado o el pitido, no lee el aviso, deja el mensaje de abajo tras el pitido y pasa al siguiente contacto. Un buzón nunca cuenta como confirmación. Solo con OpenAI disponible."),
      field("voicemail_message", "Mensaje para el buzón", vmMessage, "Corto: mismas variables que el aviso, salvo {atendido}.", errors.voicemail_message)),

    el("section", { class: "card", "aria-labelledby": "espera-h" },
      el("h2", { id: "espera-h", style: "font-size:17px" }, "Antes de avisar"),
      el("div", { class: "grid" },
        field("call_delay", "Espera antes de empezar a llamar (s)", callDelay, "Si alguien desarma la alarma en este tiempo, no se llama a nadie. 0 = llamar al momento.", errors.call_delay),
        field("external_notify_delay", "Espera antes de avisar a contactos externos (s)", externalDelay, "Telegram y correo. La notificación de la app de HA a la familia es inmediata.", errors.external_notify_delay)),
      el("div", { class: "banner info" }, "Si lo que pasa es que la alarma salta al entrar antes de poder desarmarla, lo mejor es ajustar también el retardo de entrada de Alarmo: si se desarma durante ese retardo, la alarma ni siquiera llega a dispararse.")),

    el("section", { class: "card", "aria-labelledby": "cadena-h" },
      el("h2", { id: "cadena-h", style: "font-size:17px" }, "Cadena de llamadas"),
      el("div", { class: "grid" },
        field("rounds", "Rondas por la lista", rounds, "Al llegar al último contacto vuelve al primero.", errors.rounds),
        field("max_chain_minutes", "Tiempo máximo sin límite de rondas (min)", maxChain, "Solo con «Sin límite»: evita llamar toda la noche si nadie responde.", errors.max_chain_minutes),
        field("answer_timeout", "Tiempo para descolgar (s)", answer, null, errors.answer_timeout),
        field("round_pause_min", "Pausa entre rondas (min)", pause, null, errors.round_pause_min)),
      el("div", { class: "banner info" }, "Las llamadas de alarma tienen prioridad: al dispararse se cancelan las pruebas y cualquier otra llamada.")),

    el("section", { class: "card", "aria-labelledby": "troncal-h" },
      el("h2", { id: "troncal-h", style: "font-size:17px" }, "Vigilancia de la troncal"),
      el("span", { class: "small muted" }, "Dos comprobaciones: que la línea siga registrada (al momento) y una llamada real al número propio de la línea, que sale por la troncal y vuelve a entrar sin que suene nada. Si algo falla, avisa a quien tenga la app de HA."),
      el("div", { class: "grid" },
        field("trunk_test_number", "Número propio de la línea", trunkNumber, "El fijo de la troncal. Vacío = sin prueba real.", errors.trunk_test_number),
        field("trunk_test", "Prueba real de llamada", trunkTest, null, errors.trunk_test),
        field("trunk_test_hour", "Hora de la prueba", trunkHour, "Cada 6 horas: a esta hora y cada 6 h desde ella.", errors.trunk_test_hour),
        field("trunk_alert_after_min", "Avisar si el registro cae más de (min)", trunkAlert, null, errors.trunk_alert_after_min),
        field("trunk_entity", "Entidad del registro de la troncal", trunkEntity, null, errors.trunk_entity),
        field("ami_entity", "Entidad de la conexión con Asterisk", amiEntity, null, errors.ami_entity)),
      el("div", { class: "actions", style: "justify-content:flex-start;align-items:center" }, trunkBtn, trunkInfo)),

    el("section", { class: "card", "aria-labelledby": "cam-h" },
      el("h2", { id: "cam-h", style: "font-size:17px" }, "Cámaras (Frigate)"),
      el("div", { class: "grid" },
        field("frigate_pre_minutes", "Incluir desde antes del disparo (min)", pre, null, errors.frigate_pre_minutes),
        field("media_retention_days", "Conservar fotos y vídeos (días)", retention, null, errors.media_retention_days),
        el("div", { class: "field" }, el("span", { class: "label" }, "Contactos externos"),
          el("label", { class: "check" }, photos, "Enviarles fotos por Telegram y correo")))),

    el("section", { class: "card", "aria-labelledby": "inc-h" },
      el("h2", { id: "inc-h", style: "font-size:17px" }, "Registro del incidente"),
      field("incident_entities", "Sensores que se añaden a la cronología", entities, "Un entity_id por línea: movimiento, puertas, ventanas…", errors.incident_entities)),

    el("section", { class: "card", "aria-labelledby": "bridge-h" },
      el("h2", { id: "bridge-h", style: "font-size:17px" }, "Asterisk Realtime Bridge"),
      field("bridge_pin_entity", "Entidad con el estado del PIN", pinEntity, "Vacío = sensor.asterisk_realtime_bridge_pin. La publica el Bridge: modo de PIN y personas con PIN (nunca los PIN).", errors.bridge_pin_entity)),

    el("section", { class: "card", "aria-labelledby": "int-h" },
      el("h2", { id: "int-h", style: "font-size:17px" }, "Integración con Home Assistant"),
      el("div", { class: "grid" },
        field("alarm_entity", "Entidad de la alarma", alarmEntity, "La que se vigila: al pasar a «triggered» empieza el aviso.", errors.alarm_entity),
        field("call_script", "Script de llamada", callScript, "Hace una llamada a través del Bridge y devuelve el resultado.", errors.call_script),
        field("bridge_addon", "Identificador de la App del Bridge", bridgeAddon,
          "Para avisar o colgar la llamada en curso. Vacío = se calcula solo (misma tienda de Apps que esta).", errors.bridge_addon))),

    el("section", { class: "card", "aria-labelledby": "copia-h" },
      el("h2", { id: "copia-h", style: "font-size:17px" }, "Copia de la configuración"),
      el("span", { class: "small muted" }, "Contactos y ajustes en un fichero, para pasarlos a otra instalación de la App. Contiene teléfonos y correos: guárdalo en un sitio seguro."),
      el("div", { class: "actions", style: "justify-content:flex-start" },
        el("button", { class: "btn", type: "button", onclick: exportConfig }, "Exportar"),
        el("label", { class: "btn", for: "import-file", style: "cursor:pointer" }, "Importar…"),
        el("input", { id: "import-file", type: "file", accept: "application/json,.json", hidden: true, onchange: importConfig }))),

    el("div", { class: "actions" }, el("button", { class: "btn primary", type: "button", onclick: save }, "Guardar ajustes")));
}

async function exportConfig() {
  try {
    const data = await api("GET", "api/export");
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = el("a", { href: URL.createObjectURL(blob), download: `alarm-contact-center-${new Date().toISOString().slice(0, 10)}.json` });
    document.body.append(a); a.click(); a.remove();
  } catch (e) { toast(e.message, true); }
}

async function importConfig(ev) {
  const file = ev.target.files && ev.target.files[0];
  ev.target.value = "";
  if (!file) return;
  if (!window.confirm("Se sustituirán TODOS los contactos y ajustes por los del fichero. ¿Continuar?")) return;
  try {
    const data = JSON.parse(await file.text());
    const res = await api("POST", "api/import", data);
    await reload();
    toast(`Importados ${res.contacts} contactos.`);
    showSettings();
  } catch (e) {
    const detail = e.fields ? " " + Object.entries(e.fields).map(([k, v]) => `${k}: ${v}`).join(" · ") : "";
    toast((e.message || "No se pudo importar.") + detail, true);
  }
}

// ----------------------------------------------------------------- incidents
const KIND_LABEL = { alarm: "Alarma", drill: "Simulacro", test: "Prueba", trunk: "Línea" };
const KIND_COLOR = {
  triggered: "bad", trunk_down: "bad", error: "bad", confirmed: "ok", acknowledged: "ok", disarmed: "ok",
  trunk_ok: "ok", call: "accent", wait: "", profile: "warn", voicemail: "warn", resolved: "ok", cancelled: "", round: "warn", no_answer: "", answered: "warn", failed: "warn", timeout: "warn", limit: "warn",
};

function fmtTime(iso) {
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString("es-ES", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

let incidentTimer = null;
async function showIncidents(selectedId = null) {
  setNav("incidentes");
  view.className = "";
  clearTimeout(incidentTimer);
  let data;
  try { data = await api("GET", "api/incidents"); } catch (e) { render(el("div", { class: "banner warn" }, e.message)); return; }
  if (!location.hash.startsWith("#/incidentes")) return;
  const items = data.items;
  const selected = items.find((i) => i.id === selectedId) || items[0];
  const list = el("section", { class: "card flush", style: "flex:1 1 300px", "aria-labelledby": "inc-list-h" },
    el("h2", { id: "inc-list-h", style: "font-size:16px;padding:16px 20px;border-bottom:1px solid var(--line)" }, "Incidentes"),
    items.length ? items.map((i) => el("button", { type: "button", class: "inc-item" + (selected && i.id === selected.id ? " selected" : ""),
      onclick: () => showIncidents(i.id) },
      el("span", { class: "inc-row" }, el("span", {}, fmtTime(i.started)),
        el("span", { class: "chip " + (i.kind === "alarm" ? "manage" : i.kind === "drill" ? "warn" : "query") }, KIND_LABEL[i.kind] || i.kind)),
      el("span", { class: "small muted" }, `${i.sensor} · ${STATUS_TEXT[i.status] || i.status}${i.acknowledged_by ? " · " + i.acknowledged_by : ""}`)))
      : el("div", { class: "empty" }, "Todavía no hay incidentes."));
  const detail = selected ? el("section", { class: "card", style: "flex:999 1 560px;min-width:0", "aria-labelledby": "inc-det-h" },
    el("h2", { id: "inc-det-h" }, `${fmtTime(selected.started)} · ${selected.sensor}`),
    el("span", { class: "muted" }, `${KIND_LABEL[selected.kind] || selected.kind} · ${STATUS_TEXT[selected.status] || selected.status}` +
      (selected.acknowledged_by ? ` · atendida por ${selected.acknowledged_by} (${selected.ack_via})` : "") + (selected.active ? " · en curso" : "")),
    el("ol", { class: "timeline" }, selected.events.map((e) => el("li", {},
      el("span", { class: "dot " + (KIND_COLOR[e.kind] || ""), "aria-hidden": "true" }),
      el("div", {}, el("div", {}, el("strong", {}, fmtTime(e.t).split(", ").pop()), " · ", e.text),
        e.detail ? el("div", { class: "small muted" }, e.detail) : null)))),
    el("div", { class: "banner info" }, "Las alertas de Frigate con fotos y vídeos se añadirán a esta cronología en la fase 3.")) : null;
  render(el("div", { style: "display:flex;flex-wrap:wrap;gap:20px;align-items:flex-start" }, list, detail));
  if (selected && selected.active) incidentTimer = setTimeout(() => showIncidents(selected.id), 3000);
}

// ------------------------------------------------------------------- router
async function route() {
  view.className = "";
  try {
    if (!STATE) await reload();
  } catch (e) {
    render(el("div", { class: "banner warn" }, icon(ICON.warn, 20), `No se pudo cargar el panel: ${e.message}`));
    return;
  }
  const hash = location.hash.replace(/^#/, "") || "/";
  const parts = hash.split("/").filter(Boolean);
  if (parts[0] === "contacto" && parts[1]) showContact(decodeURIComponent(parts[1]));
  else if (parts[0] === "ajustes") showSettings();
  else if (parts[0] === "incidentes") showIncidents();
  else { await reload().catch(() => {}); showHome(); }
}

window.addEventListener("hashchange", route);
route();
