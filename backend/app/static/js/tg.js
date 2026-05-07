/* Telegram WebApp adaptation — ADR-0018, 08-frontend.md §10.
 *
 * Loaded on every page via base.html with `defer` (after the official Telegram
 * SDK is fetched from https://telegram.org/js/telegram-web-app.js, also defer).
 * `defer` guarantees execution order: SDK first, then this glue.
 *
 * In a regular browser `window.Telegram` is undefined and this script
 * early-returns — no DOM/CSS mutations, no observable side effects.
 *
 * Inside Telegram WebView:
 *   - tg.ready()  -> Telegram hides its loading splash;
 *   - tg.expand() -> WebApp takes the full screen height (mobile);
 *   - body class `tg-app` is added (CSS rules in main.css §"Telegram WebApp
 *     adaptation" override the palette to use Telegram's themeParams);
 *   - themeParams are mirrored to CSS custom properties (--tg-bg, --tg-text,
 *     --tg-hint, --tg-link, --tg-button, --tg-button-text, --tg-secondary-bg)
 *     on <html>, with re-application on the `themeChanged` event.
 *
 * No `eval`, no `innerHTML`, no `document.write` — only addEventListener,
 * classList.add, and CSSStyleDeclaration.setProperty.
 */
(function () {
  "use strict";

  var tg = window.Telegram && window.Telegram.WebApp;
  if (!tg) {
    // Regular browser: no SDK was loaded (no /js/telegram-web-app.js, or
    // network blocked it, or we're outside Telegram WebView). No-op.
    return;
  }

  // Tell Telegram we've finished mounting; this hides the WebApp splash.
  if (typeof tg.ready === "function") {
    tg.ready();
  }
  // Take the full available height on mobile WebView.
  if (typeof tg.expand === "function") {
    tg.expand();
  }

  document.body.classList.add("tg-app");

  // Map Telegram themeParams -> CSS variables on <html>. The CSS in main.css
  // reads these via `var(--tg-bg, <fallback>)` so missing values fall back
  // to the default light theme.
  var THEME_MAP = {
    bg_color: "--tg-bg",
    secondary_bg_color: "--tg-secondary-bg",
    text_color: "--tg-text",
    hint_color: "--tg-hint",
    link_color: "--tg-link",
    button_color: "--tg-button",
    button_text_color: "--tg-button-text",
  };

  function applyTheme() {
    var params = tg.themeParams || {};
    var root = document.documentElement;
    for (var key in THEME_MAP) {
      if (Object.prototype.hasOwnProperty.call(THEME_MAP, key)) {
        var value = params[key];
        if (value) {
          root.style.setProperty(THEME_MAP[key], value);
        }
      }
    }
  }

  applyTheme();

  // Re-apply when the user toggles light/dark in Telegram itself.
  if (typeof tg.onEvent === "function") {
    tg.onEvent("themeChanged", applyTheme);
  }
})();
