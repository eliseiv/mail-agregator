/* =============================================================================
   integrations.js
   /my/integrations — outbound webhook config UX (ADR-0023, 04-api-contracts.md §4b).

   CSP-safe: no inline handlers; every behaviour is wired by data-* attributes.
   All state-changing requests go through window.MAS.csrfFetch which injects
   the X-CSRF-Token header from the mas_csrf cookie (csrf.js, ADR-0010).

   Hooks (declared in templates/my/integrations.html):
     - <form  data-webhook-create-form>        Create webhook → POST /api/webhooks/me.
     - <form  data-webhook-update-form>        Edit URL or resume from dead → PATCH.
     - <form  data-webhook-toggle-form>        Container for the active checkbox.
     - <input data-webhook-toggle-active>      The checkbox itself; PATCH on change.
     - <form  data-webhook-rotate-form>        Rotate secret → POST /rotate-secret.
     - <form  data-webhook-test-form>          Send synchronous test → POST /test.
     - <form  data-webhook-delete-form>        Delete webhook → POST /delete (override).
     - <dialog data-secret-reveal-dialog>      One-shot secret reveal modal.
     - <button data-secret-copy>               Copy secret value to clipboard.

   Progressive enhancement: every form is a working <form method="POST" action=...>.
   This script intercepts submit to upgrade the UX (no full page reload for
   test/rotate, native dialog open for secret reveal, etc). If JS fails to load
   the no-JS path still works via the form-encoded endpoints (ADR-0015).
   ========================================================================== */
(function () {
  'use strict';

  /* ---- 0. Utilities ----------------------------------------------------- */

  function flash(text, category) {
    if (window.MAS && typeof window.MAS.flash === 'function') {
      window.MAS.flash(text, category || 'info');
    }
  }

  async function readError(response) {
    if (window.MAS && typeof window.MAS.readJsonError === 'function') {
      return window.MAS.readJsonError(response);
    }
    return { code: 'http_' + response.status, message: 'Запрос не выполнен.' };
  }

  function buildUrlWithGroupId(base) {
    // Honour `?group_id=` on the current URL (super-admin flow). The backend
    // requires the query for super_admin and forbids it for group_leader; we
    // just propagate whatever the page was loaded with.
    var params = new URLSearchParams(window.location.search);
    var gid = params.get('group_id');
    if (!gid) return base;
    var sep = base.indexOf('?') >= 0 ? '&' : '?';
    return base + sep + 'group_id=' + encodeURIComponent(gid);
  }

  function disableButton(btn, busyLabel) {
    if (!btn) return;
    btn.disabled = true;
    btn.setAttribute('aria-busy', 'true');
    if (busyLabel) {
      if (!btn.hasAttribute('data-original-label')) {
        btn.setAttribute('data-original-label', btn.textContent || '');
      }
      btn.textContent = busyLabel;
    }
  }

  function restoreButton(btn) {
    if (!btn) return;
    btn.disabled = false;
    btn.removeAttribute('aria-busy');
    var original = btn.getAttribute('data-original-label');
    if (original !== null) {
      btn.textContent = original;
      btn.removeAttribute('data-original-label');
    }
  }

  function reloadPage() {
    window.location.reload();
  }

  /* ---- 1. Create webhook ------------------------------------------------ */

  var createForm = document.querySelector('[data-webhook-create-form]');
  if (createForm) {
    createForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var urlInput = createForm.querySelector('input[name="url"]');
      var submitBtn = createForm.querySelector('button[type="submit"]');
      var url = urlInput ? (urlInput.value || '').trim() : '';
      if (!url) {
        flash('Укажите URL приёмника.', 'error');
        return;
      }
      if (!/^https:\/\//i.test(url)) {
        flash('URL должен начинаться с https://', 'error');
        return;
      }
      disableButton(submitBtn, 'Создаём…');
      window.MAS.csrfFetch(buildUrlWithGroupId('/api/webhooks/me'), {
        method: 'POST',
        body: { url: url }
      }).then(function (response) {
        if (response.status === 201) {
          // Backend stashed secret_revealed in Redis (server-side flash); a
          // page reload pulls it back through the GET handler and the
          // template opens the secret-reveal modal.
          flash('Webhook создан. Секрет показан один раз — сохраните его.', 'success');
          // Small delay so the success flash is visible during the reload
          // navigation; the secret modal reveals on the freshly-loaded page.
          window.setTimeout(reloadPage, 250);
          return;
        }
        return readError(response).then(function (err) {
          flash(err.message || 'Не удалось создать webhook.', 'error');
          restoreButton(submitBtn);
        });
      }).catch(function () {
        flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
        restoreButton(submitBtn);
      });
    });
  }

  /* ---- 2. Update webhook (URL edit OR resume-from-dead) ----------------- */

  function patchAndReload(formElement, body, successMessage, busyLabel) {
    var submitBtn = formElement.querySelector('button[type="submit"]');
    disableButton(submitBtn, busyLabel);
    window.MAS.csrfFetch(buildUrlWithGroupId('/api/webhooks/me'), {
      method: 'PATCH',
      body: body
    }).then(function (response) {
      if (response.ok) {
        flash(successMessage, 'success');
        window.setTimeout(reloadPage, 250);
        return;
      }
      return readError(response).then(function (err) {
        flash(err.message || 'Не удалось обновить webhook.', 'error');
        restoreButton(submitBtn);
      });
    }).catch(function () {
      flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
      restoreButton(submitBtn);
    });
  }

  var updateForms = document.querySelectorAll('[data-webhook-update-form]');
  Array.prototype.forEach.call(updateForms, function (form) {
    form.addEventListener('submit', function (e) {
      e.preventDefault();
      var body = {};
      var urlInput = form.querySelector('input[name="url"]');
      var isActiveInput = form.querySelector('input[name="is_active"]');
      if (urlInput && urlInput.value) {
        var u = urlInput.value.trim();
        if (!/^https:\/\//i.test(u)) {
          flash('URL должен начинаться с https://', 'error');
          return;
        }
        body.url = u;
      }
      // hidden is_active hint (e.g. resume-from-dead form posts is_active=true).
      if (isActiveInput && isActiveInput.type === 'hidden') {
        var raw = (isActiveInput.value || '').toLowerCase();
        body.is_active = raw === 'true' || raw === '1' || raw === 'on' || raw === 'yes';
      }
      if (Object.keys(body).length === 0) {
        flash('Нечего сохранять.', 'warning');
        return;
      }
      patchAndReload(form, body, 'Webhook обновлён.', 'Сохраняем…');
    });
  });

  /* ---- 3. Toggle is_active checkbox ------------------------------------- */

  var toggleCheckbox = document.querySelector('[data-webhook-toggle-active]');
  var toggleForm = document.querySelector('[data-webhook-toggle-form]');
  if (toggleCheckbox && toggleForm) {
    toggleCheckbox.addEventListener('change', function () {
      var desired = toggleCheckbox.checked;
      toggleCheckbox.disabled = true;
      window.MAS.csrfFetch(buildUrlWithGroupId('/api/webhooks/me'), {
        method: 'PATCH',
        body: { is_active: desired }
      }).then(function (response) {
        if (response.ok) {
          flash(desired ? 'Webhook включён.' : 'Webhook отключён.', 'success');
          // Status badge depends on dead_at / consecutive_failures too — reload
          // so the UI fully reflects backend state.
          window.setTimeout(reloadPage, 250);
          return;
        }
        return readError(response).then(function (err) {
          flash(err.message || 'Не удалось переключить статус.', 'error');
          // Revert the checkbox visually on failure.
          toggleCheckbox.checked = !desired;
          toggleCheckbox.disabled = false;
        });
      }).catch(function () {
        flash('Сетевая ошибка.', 'error');
        toggleCheckbox.checked = !desired;
        toggleCheckbox.disabled = false;
      });
    });
  }

  /* ---- 4. Rotate secret ------------------------------------------------- */

  var rotateForm = document.querySelector('[data-webhook-rotate-form]');
  if (rotateForm) {
    rotateForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var msg = rotateForm.getAttribute('data-confirm');
      // eslint-disable-next-line no-alert
      if (msg && !window.confirm(msg)) return;
      var submitBtn = rotateForm.querySelector('button[type="submit"]');
      disableButton(submitBtn, 'Ротация…');
      window.MAS.csrfFetch(buildUrlWithGroupId('/api/webhooks/me/rotate-secret'), {
        method: 'POST'
      }).then(function (response) {
        if (response.ok) {
          // The new plaintext secret is stashed on the server (Redis); reload
          // and the secret-reveal modal opens.
          flash('Секрет ротирован. Сохраните новый.', 'success');
          window.setTimeout(reloadPage, 250);
          return;
        }
        return readError(response).then(function (err) {
          flash(err.message || 'Не удалось ротировать secret.', 'error');
          restoreButton(submitBtn);
        });
      }).catch(function () {
        flash('Сетевая ошибка.', 'error');
        restoreButton(submitBtn);
      });
    });
  }

  /* ---- 5. Send test ----------------------------------------------------- */

  var testForm = document.querySelector('[data-webhook-test-form]');
  if (testForm) {
    testForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var submitBtn = testForm.querySelector('button[type="submit"]');
      disableButton(submitBtn, 'Отправляем…');
      window.MAS.csrfFetch(buildUrlWithGroupId('/api/webhooks/me/test'), {
        method: 'POST'
      }).then(function (response) {
        if (response.ok) {
          return response.json().then(function (data) {
            var statusKey = data && data.status;
            var code = data && data.response_code;
            var duration = data && data.duration_ms;
            var category;
            var text;
            if (statusKey === 'ok') {
              category = 'success';
              text = 'Тест выполнен: HTTP ' + code + ', ' + duration + ' мс.';
            } else if (statusKey === 'http_error') {
              category = 'warning';
              text = 'Получатель ответил HTTP ' + code + ' (' + duration + ' мс).';
            } else if (statusKey === 'dns_failed') {
              category = 'error';
              text = 'DNS-резолв не удался.';
            } else if (statusKey === 'network') {
              category = 'error';
              text = 'Сетевая ошибка: ' + (data.detail || 'нет ответа') + '.';
            } else {
              category = 'info';
              text = 'Тест выполнен.';
            }
            flash(text, category);
            restoreButton(submitBtn);
          });
        }
        return readError(response).then(function (err) {
          flash(err.message || 'Тест не выполнен.', 'error');
          restoreButton(submitBtn);
        });
      }).catch(function () {
        flash('Сетевая ошибка.', 'error');
        restoreButton(submitBtn);
      });
    });
  }

  /* ---- 6. Delete webhook ------------------------------------------------ */

  var deleteForm = document.querySelector('[data-webhook-delete-form]');
  if (deleteForm) {
    deleteForm.addEventListener('submit', function (e) {
      e.preventDefault();
      var msg = deleteForm.getAttribute('data-confirm');
      // eslint-disable-next-line no-alert
      if (msg && !window.confirm(msg)) return;
      var submitBtn = deleteForm.querySelector('button[type="submit"]');
      disableButton(submitBtn, 'Удаляем…');
      window.MAS.csrfFetch(buildUrlWithGroupId('/api/webhooks/me'), {
        method: 'DELETE'
      }).then(function (response) {
        if (response.status === 204 || response.ok) {
          flash('Webhook удалён.', 'success');
          window.setTimeout(reloadPage, 250);
          return;
        }
        return readError(response).then(function (err) {
          flash(err.message || 'Не удалось удалить webhook.', 'error');
          restoreButton(submitBtn);
        });
      }).catch(function () {
        flash('Сетевая ошибка.', 'error');
        restoreButton(submitBtn);
      });
    });
  }

  /* ---- 7. Secret reveal modal + copy ------------------------------------ */

  var revealDialog = document.querySelector('[data-secret-reveal-dialog]');
  if (revealDialog) {
    // Auto-open the modal once on page load. The server only ever renders the
    // dialog markup when there's actually a secret to show (it's one-shot
    // server-side; see backend/app/webhooks/router.py:_consume_secret_reveal).
    if (revealDialog.getAttribute('data-autoopen') === '1') {
      if (typeof revealDialog.showModal === 'function') {
        try { revealDialog.showModal(); } catch (_e) { /* already open */ }
      }
    }
  }

  var copyBtn = document.querySelector('[data-secret-copy]');
  if (copyBtn) {
    copyBtn.addEventListener('click', function () {
      var input = document.querySelector('[data-secret-value]');
      if (!input) return;
      var value = input.value || '';
      // Prefer the async Clipboard API; fall back to selecting the input and
      // execCommand('copy') for browsers without clipboard access (older
      // WebViews / non-HTTPS test envs).
      var done = function (ok) {
        if (ok) {
          flash('Скопировано в буфер обмена.', 'success');
        } else {
          flash('Не удалось скопировать автоматически. Скопируйте вручную.', 'warning');
        }
      };
      if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
        navigator.clipboard.writeText(value).then(function () { done(true); }, function () {
          // Permission denied / not available — try the legacy path.
          try {
            input.removeAttribute('readonly');
            input.select();
            // eslint-disable-next-line deprecation/deprecation
            var ok = document.execCommand('copy');
            input.setAttribute('readonly', 'readonly');
            done(!!ok);
          } catch (_err) {
            done(false);
          }
        });
      } else {
        try {
          input.removeAttribute('readonly');
          input.select();
          // eslint-disable-next-line deprecation/deprecation
          var ok = document.execCommand('copy');
          input.setAttribute('readonly', 'readonly');
          done(!!ok);
        } catch (_err) {
          done(false);
        }
      }
    });
  }
})();
