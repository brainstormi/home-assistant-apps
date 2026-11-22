"use strict";
// Follow Home Assistant's dark mode.
// Ingress serves this panel from the same origin as Home Assistant, so the parent
// page's `hass` object can be read. When it can't (opened on its own, or HA changes
// its internals), fall back to the operating system's preference.
(function () {
  const media = window.matchMedia("(prefers-color-scheme: dark)");

  function haDarkMode() {
    try {
      const root = window.parent && window.parent !== window && window.parent.document.querySelector("home-assistant");
      const themes = root && root.hass && root.hass.themes;
      if (themes && typeof themes.darkMode === "boolean") return themes.darkMode;
    } catch (_) {
      /* cross-origin or not available: use the fallback */
    }
    return media.matches;
  }

  function apply() {
    const theme = haDarkMode() ? "dark" : "light";
    if (document.documentElement.dataset.theme !== theme) document.documentElement.dataset.theme = theme;
  }

  apply();
  // HA has no event for this that an iframe can listen to, so check now and then.
  setInterval(apply, 2000);
  media.addEventListener("change", apply);
  document.addEventListener("visibilitychange", apply);
})();
