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

  // ADR-0022 §1.3 — Persistent SSO attempt.
  //
  // When the page is rendered for an anonymous visitor (server-side marker
  // `<body data-anonymous="1">` — see base.html; `mas_session` is HttpOnly and
  // cannot be inspected from JS), and we have a non-empty `initData` from the
  // Telegram WebApp, POST it to `/api/telegram/auth` to see if this Telegram
  // user is already linked to an internal account.
  //
  // Endpoint is CSRF-exempt (ADR-0022 §1.2) — no `X-CSRF-Token` header needed;
  // protection relies on HMAC of `init_data` + 5-minute `auth_date` TTL.
  // We never log `initData` itself, only the response status.
  //
  // `__masTgSsoTried` guards against duplicate calls (HMR re-execution, repeated
  // DOMContentLoaded handlers, etc.) — set BEFORE the fetch so an in-flight call
  // is never re-fired.
  var initData = typeof tg.initData === "string" ? tg.initData : "";
  var isAnonymous =
    document.body && document.body.dataset && document.body.dataset.anonymous === "1";

  if (initData && isAnonymous && !window.__masTgSsoTried) {
    window.__masTgSsoTried = true;

    fetch("/api/telegram/auth", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ init_data: initData }),
      credentials: "same-origin",
    })
      .then(function (response) {
        if (response.status === 200) {
          return response
            .json()
            .then(function (body) {
              if (body && body.linked === true) {
                // Backend set `mas_session` + `mas_csrf` cookies; reload into
                // the authenticated app. `redirect` is provided by the backend
                // (defaults to "/").
                var target = body.redirect || "/";
                window.location.replace(target);
              }
              // linked === false: backend has set the short-lived `mas_tg_pending`
              // cookie. We stay on the anonymous page so the user can complete the
              // normal login flow, which will pick up the cookie and create the
              // telegram_links row on success.
            })
            .catch(function () {
              // Malformed JSON from a 200 — non-fatal, fall back to manual login.
            });
        }
        // 401 (invalid_init_data / init_data_expired), 429 (rate_limited),
        // 5xx — degrade silently to the server-rendered login page.
        if (response.status >= 400) {
          // eslint-disable-next-line no-console
          console.warn("[tg.js] /api/telegram/auth status", response.status);
        }
        return null;
      })
      .catch(function () {
        // Network error (offline, CSP block, DNS, etc.) — page still works
        // without SSO; user can log in manually.
        // eslint-disable-next-line no-console
        console.warn("[tg.js] /api/telegram/auth network error");
      });
  }
})();
