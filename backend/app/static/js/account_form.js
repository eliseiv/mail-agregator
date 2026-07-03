/* =============================================================================
   account_form.js
   Mail-account add/edit UX:
     1. Provider auto-suggest based on email domain.
     2. "Test connection" button that calls POST /api/mail-accounts/test
        and renders inline result; Save is gated on a successful test.
     3. Form submit (create/update) via fetch with proper JSON envelope.
     4. Sync-now / delete inline buttons on the accounts list.

   Per 08-frontend.md sec. 3: provider table is hardcoded in JS (mirrors
   accounts/providers.py). Backend remains the source of truth and re-validates
   everything on POST/test. The 11 supported domains come from 05-modules.md
   sec. 9.

   Hooks:
     - <form data-account-form>           : the create/edit form.
     - <input data-account-email>         : triggers provider auto-suggest.
     - <input data-account-imap-host>     : auto-filled when known provider detected.
     - <input data-account-imap-port>     : ditto.
     - <input data-account-imap-ssl>      : ditto (checkbox).
     - <input data-account-smtp-host>     : ditto.
     - <input data-account-smtp-port>     : ditto.
     - <input data-account-smtp-ssl>      : ditto (checkbox).
     - <input data-account-smtp-starttls> : ditto (checkbox).
     - <button data-account-test>         : POST /api/mail-accounts/test.
     - <span data-account-test-result>    : inline result text.
     - <button data-account-submit>       : main submit (re-enabled after edits).
     - <p data-account-error>             : top-of-form error.
     - <p data-account-success>           : top-of-form success.
     - <form data-account-sync-form>      : list-page sync-now form.
     - <form data-account-delete-form>    : list-page delete form (with confirm).
   ========================================================================== */
(function () {
  'use strict';

  if (!window.MAS) return;

  // ---- Provider table -------------------------------------------------------
  // Mirrors accounts/providers.py (05-modules.md sec 9).
  // Keep in sync if backend updates the table.
  const PROVIDERS = {
    // ADR-0032 follow-up: prod host blocks outbound :465 → SMTP on :587/STARTTLS.
    'gmail.com':       { label: 'Gmail',   imap_host: 'imap.gmail.com',     imap_port: 993, imap_ssl: true,  smtp_host: 'smtp.gmail.com',     smtp_port: 587, smtp_ssl: false, smtp_starttls: true },
    'googlemail.com':  { label: 'Gmail',   imap_host: 'imap.gmail.com',     imap_port: 993, imap_ssl: true,  smtp_host: 'smtp.gmail.com',     smtp_port: 587, smtp_ssl: false, smtp_starttls: true },
    'yandex.ru':       { label: 'Yandex',  imap_host: 'imap.yandex.ru',     imap_port: 993, imap_ssl: true,  smtp_host: 'smtp.yandex.ru',     smtp_port: 587, smtp_ssl: false, smtp_starttls: true },
    'yandex.com':      { label: 'Yandex',  imap_host: 'imap.yandex.com',    imap_port: 993, imap_ssl: true,  smtp_host: 'smtp.yandex.com',    smtp_port: 587, smtp_ssl: false, smtp_starttls: true },
    'mail.ru':         { label: 'Mail.ru', imap_host: 'imap.mail.ru',       imap_port: 993, imap_ssl: true,  smtp_host: 'smtp.mail.ru',       smtp_port: 587, smtp_ssl: false, smtp_starttls: true },
    'inbox.ru':        { label: 'Mail.ru', imap_host: 'imap.mail.ru',       imap_port: 993, imap_ssl: true,  smtp_host: 'smtp.mail.ru',       smtp_port: 587, smtp_ssl: false, smtp_starttls: true },
    'bk.ru':           { label: 'Mail.ru', imap_host: 'imap.mail.ru',       imap_port: 993, imap_ssl: true,  smtp_host: 'smtp.mail.ru',       smtp_port: 587, smtp_ssl: false, smtp_starttls: true },
    'list.ru':         { label: 'Mail.ru', imap_host: 'imap.mail.ru',       imap_port: 993, imap_ssl: true,  smtp_host: 'smtp.mail.ru',       smtp_port: 587, smtp_ssl: false, smtp_starttls: true },
    'aol.com':         { label: 'AOL',     imap_host: 'imap.aol.com',       imap_port: 993, imap_ssl: true,  smtp_host: 'smtp.aol.com',       smtp_port: 587, smtp_ssl: false, smtp_starttls: true },
    'yahoo.com':       { label: 'Yahoo',   imap_host: 'imap.mail.yahoo.com',imap_port: 993, imap_ssl: true,  smtp_host: 'smtp.mail.yahoo.com',smtp_port: 587, smtp_ssl: false, smtp_starttls: true },
    'outlook.com':     { label: 'Outlook', imap_host: 'outlook.office365.com', imap_port: 993, imap_ssl: true, smtp_host: 'smtp.office365.com', smtp_port: 587, smtp_ssl: false, smtp_starttls: true },
    'hotmail.com':     { label: 'Outlook', imap_host: 'outlook.office365.com', imap_port: 993, imap_ssl: true, smtp_host: 'smtp.office365.com', smtp_port: 587, smtp_ssl: false, smtp_starttls: true },
    'live.com':        { label: 'Outlook', imap_host: 'outlook.office365.com', imap_port: 993, imap_ssl: true, smtp_host: 'smtp.office365.com', smtp_port: 587, smtp_ssl: false, smtp_starttls: true },
  };

  function getDomain(email) {
    if (!email) return '';
    const at = email.lastIndexOf('@');
    if (at < 0) return '';
    return email.substring(at + 1).toLowerCase().trim();
  }

  // ---- Form (add/edit) ------------------------------------------------------

  const form = document.querySelector('[data-account-form]');
  if (form) initAccountForm(form);

  function initAccountForm(form) {
    const emailInput     = form.querySelector('[data-account-email]');
    const imapHost       = form.querySelector('[data-account-imap-host]');
    const imapPort       = form.querySelector('[data-account-imap-port]');
    const imapSsl        = form.querySelector('[data-account-imap-ssl]');
    const smtpHost       = form.querySelector('[data-account-smtp-host]');
    const smtpPort       = form.querySelector('[data-account-smtp-port]');
    const smtpSsl        = form.querySelector('[data-account-smtp-ssl]');
    const smtpStartTls   = form.querySelector('[data-account-smtp-starttls]');
    const testBtn        = form.querySelector('[data-account-test]');
    const testResult     = form.querySelector('[data-account-test-result]');
    const submitBtn      = form.querySelector('[data-account-submit]');
    const errorPane      = document.querySelector('[data-account-error]');
    const successPane    = document.querySelector('[data-account-success]');
    const accountId      = form.getAttribute('data-account-id') || '';
    const isEdit         = !!accountId;
    // OAuth-аккаунты (ADR-0025): IMAP/SMTP/токены управляются Microsoft и не
    // редактируются. В режиме редактирования такого ящика правится только
    // никнейм (display_name). Признак приходит из шаблона (data-auth-type на
    // account.auth_type). На create этого режима нет — OAuth-ящики заводятся
    // отдельным flow («Подключить Outlook»), а не этой формой.
    const authType       = (form.getAttribute('data-auth-type') || '').trim();
    const isOauthEdit    = isEdit && authType === 'oauth_outlook';

    // Для OAuth-edit прячем и отключаем все секции с учётными данными
    // (пароль, IMAP, SMTP, «Проверить соединение»), чтобы они не отправлялись
    // и не сбивали с толку. disabled-поля не попадают в FormData.
    if (isOauthEdit) {
      form.querySelectorAll('[data-account-credentials]').forEach(function (el) {
        el.hidden = true;
        el.querySelectorAll('input, button, select, textarea').forEach(function (ctrl) {
          ctrl.disabled = true;
        });
      });
    }

    // Auto-suggest. Edit mode: only fill empty fields (don't clobber existing values).
    // Для OAuth-edit auto-suggest не нужен (хосты не редактируются).
    if (emailInput && !isOauthEdit) {
      emailInput.addEventListener('input', applyProviderDefaults);
      emailInput.addEventListener('blur', applyProviderDefaults);
    }

    function applyProviderDefaults() {
      const domain = getDomain(emailInput ? emailInput.value : '');
      if (!domain || !PROVIDERS[domain]) return;
      const p = PROVIDERS[domain];

      if (imapHost && (!imapHost.value || !isEdit)) imapHost.value = p.imap_host;
      if (imapPort && (!imapPort.value || !isEdit)) imapPort.value = String(p.imap_port);
      if (imapSsl) imapSsl.checked = !!p.imap_ssl;

      if (smtpHost && (!smtpHost.value || !isEdit)) smtpHost.value = p.smtp_host;
      if (smtpPort && (!smtpPort.value || !isEdit)) smtpPort.value = String(p.smtp_port);
      if (smtpSsl)      smtpSsl.checked      = !!p.smtp_ssl;
      if (smtpStartTls) smtpStartTls.checked = !!p.smtp_starttls;

      // Re-arm Save: any change should clear the cached pass/fail.
      clearTestResult();
    }

    function clearTestResult() {
      if (!testResult) return;
      testResult.textContent = '';
      testResult.className = 'account-test-result';
    }

    function setTestResult(text, kind) {
      if (!testResult) return;
      testResult.textContent = text;
      testResult.className = 'account-test-result account-test-result--' + (kind || 'pending');
    }

    function setError(text) {
      if (!errorPane) return;
      if (!text) { errorPane.hidden = true; errorPane.textContent = ''; return; }
      errorPane.hidden = false;
      errorPane.textContent = text;
    }
    function setSuccess(text) {
      if (!successPane) return;
      if (!text) { successPane.hidden = true; successPane.textContent = ''; return; }
      successPane.hidden = false;
      successPane.textContent = text;
    }

    function buildPayload() {
      const fd = new FormData(form);
      // OAuth-edit: учётные данные фиксированы Microsoft — шлём только никнейм.
      // clear-семантика как у password-edit: пустой никнейм → clear_display_name:true
      // (sentinel JSON-пути backend MailAccountUpdateRequest; display_name:null
      //  игнорируется сервисом). Непустой → display_name: "<имя>".
      if (isOauthEdit) {
        const rawName = (fd.get('display_name') || '').toString().trim();
        return rawName ? { display_name: rawName } : { clear_display_name: true };
      }
      const payload = {
        email:           (fd.get('email') || '').toString().trim(),
        password:        (fd.get('password') || '').toString(),
        imap_host:       (fd.get('imap_host') || '').toString().trim(),
        imap_port:       Number(fd.get('imap_port') || 0),
        imap_ssl:        !!fd.get('imap_ssl'),
        smtp_host:       (fd.get('smtp_host') || '').toString().trim(),
        smtp_port:       Number(fd.get('smtp_port') || 0),
        smtp_ssl:        !!fd.get('smtp_ssl'),
        smtp_starttls:   !!fd.get('smtp_starttls'),
      };
      // Mail account nickname (ADR-0020). Trim. On edit mode: непустой →
      // display_name; пустой → clear_display_name:true (sentinel JSON-пути
      // backend; display_name:null сервисом игнорируется). On create mode:
      // omit when empty so the backend default kicks in.
      const displayNameRaw = (fd.get('display_name') || '').toString();
      const displayName = displayNameRaw.trim();
      if (isEdit) {
        if (displayName) {
          payload.display_name = displayName;
        } else {
          payload.clear_display_name = true;
        }
      } else if (displayName) {
        payload.display_name = displayName;
      }
      const smtpUser = (fd.get('smtp_username') || '').toString().trim();
      const smtpPass = (fd.get('smtp_password') || '').toString();
      if (smtpUser) payload.smtp_username = smtpUser;
      if (smtpPass) payload.smtp_password = smtpPass;
      // Team selector (ADR-0031 §2). Only rendered on create when the user has
      // >1 selectable team (or is super_admin). When present, we always send
      // group_id: a non-empty value is the chosen team id; an empty value is
      // the super_admin "Без команды" option ⇒ group_id: null (personal box).
      // When the selector is absent (single-team user) we omit group_id so the
      // backend falls back to the home team (full backward compatibility).
      if (!isEdit) {
        const groupSel = form.querySelector('[data-account-group]');
        if (groupSel) {
          const raw = (groupSel.value || '').toString().trim();
          payload.group_id = raw ? Number(raw) : null;
        }
      }
      // For edit mode: empty password means "keep existing"; do not send it.
      if (isEdit && !payload.password) delete payload.password;
      return payload;
    }

    // Test-connection button. Для OAuth-edit его нет (секция скрыта/disabled).
    if (testBtn && !isOauthEdit) {
      testBtn.addEventListener('click', async function () {
        setError('');
        setSuccess('');
        const payload = buildPayload();
        if (!payload.email || (!isEdit && !payload.password) || !payload.imap_host || !payload.smtp_host) {
          setTestResult('Заполните email, пароль, хост IMAP и хост SMTP.', 'error');
          return;
        }
        if (isEdit && !payload.password) {
          setTestResult('Введите текущий пароль, чтобы проверить соединение.', 'error');
          return;
        }
        setTestResult('Проверка…', 'pending');
        testBtn.disabled = true;
        try {
          const resp = await window.MAS.csrfFetch('/api/mail-accounts/test', {
            method: 'POST',
            body: payload,
          });
          if (resp.ok) {
            setTestResult('IMAP OK, SMTP OK', 'ok');
          } else {
            const err = await window.MAS.readJsonError(resp);
            const detail = err.details && err.details.detail ? ' — ' + err.details.detail : '';
            setTestResult('Ошибка: ' + err.message + detail, 'error');
          }
        } catch (_e) {
          setTestResult('Сетевая ошибка во время проверки.', 'error');
        } finally {
          testBtn.disabled = false;
        }
      });
    }

    // Submit (create/update) via fetch.
    form.addEventListener('submit', async function (event) {
      event.preventDefault();
      setError('');
      setSuccess('');

      const payload = buildPayload();

      // OAuth-edit: единственное поле — никнейм; серверных credential-проверок
      // не требуется (пропускаем гварды email/пароль/хосты/порты).
      if (!isOauthEdit) {
        // Basic client-side guards (server-side is authoritative).
        if (!payload.email) { setError('Email обязателен.'); return; }
        if (!isEdit && !payload.password) { setError('Пароль обязателен.'); return; }
        if (!payload.imap_host || !payload.smtp_host) {
          setError('Хосты IMAP и SMTP обязательны.'); return;
        }
        if (payload.imap_port < 1 || payload.imap_port > 65535) {
          setError('Неверный порт IMAP.'); return;
        }
        if (payload.smtp_port < 1 || payload.smtp_port > 65535) {
          setError('Неверный порт SMTP.'); return;
        }
        if (payload.smtp_ssl && payload.smtp_starttls) {
          setError('SMTP не может одновременно использовать SSL и STARTTLS.'); return;
        }
      }

      submitBtn && (submitBtn.disabled = true);
      try {
        const url = isEdit
          ? '/api/mail-accounts/' + encodeURIComponent(accountId)
          : '/api/mail-accounts';
        const method = isEdit ? 'PATCH' : 'POST';
        const resp = await window.MAS.csrfFetch(url, { method: method, body: payload });
        if (resp.ok) {
          window.MAS.flash(isEdit ? 'Аккаунт обновлён.' : 'Аккаунт добавлен.', 'success');
          window.location.href = '/accounts';
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        const detail = err.details && err.details.detail ? ' — ' + err.details.detail : '';
        setError(err.message + detail);
      } catch (_e) {
        setError('Сетевая ошибка. Попробуйте ещё раз.');
      } finally {
        submitBtn && (submitBtn.disabled = false);
      }
    });
  }

  // ---- List page: sync-now & delete ----------------------------------------

  document.querySelectorAll('[data-account-sync-form]').forEach(function (f) {
    f.addEventListener('submit', async function (event) {
      event.preventDefault();
      const action = f.getAttribute('action');
      if (!action) return;
      const btn = f.querySelector('button[type="submit"]');
      if (btn) btn.disabled = true;
      try {
        const resp = await window.MAS.csrfFetch(action, { method: 'POST' });
        if (resp.ok || resp.status === 202) {
          window.MAS.flash('Синхронизация запущена. Новые письма появятся в течение минуты.', 'info');
        } else {
          const err = await window.MAS.readJsonError(resp);
          window.MAS.flash(err.message || 'Не удалось запустить синхронизацию.', 'error');
        }
      } catch (_e) {
        window.MAS.flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
      } finally {
        if (btn) btn.disabled = false;
      }
    });
  });

  // ---- List page: transfer mailbox to another team (ADR-0031 §3) -----------
  // Sends PATCH /api/mail-accounts/{id} with ONLY group_id. Empty value
  // (super_admin "Без команды") maps to group_id: null. On success the list is
  // reloaded so the row reflects its new team. On 403/404 (member / missing
  // team / out-of-scope) the inline error is shown — never a stack trace.
  document.querySelectorAll('[data-account-transfer-form]').forEach(function (f) {
    const errorPane = f.querySelector('[data-account-transfer-error]');
    function setTransferError(text) {
      if (!errorPane) return;
      if (!text) { errorPane.hidden = true; errorPane.textContent = ''; return; }
      errorPane.hidden = false;
      errorPane.textContent = text;
    }
    f.addEventListener('submit', async function (event) {
      event.preventDefault();
      setTransferError('');
      const action = f.getAttribute('action');
      if (!action) return;
      const sel = f.querySelector('[data-account-transfer-group]');
      const raw = sel ? (sel.value || '').toString().trim() : '';
      const payload = { group_id: raw ? Number(raw) : null };
      const btn = f.querySelector('button[type="submit"]');
      if (btn) btn.disabled = true;
      try {
        const resp = await window.MAS.csrfFetch(action, { method: 'PATCH', body: payload });
        if (resp.ok) {
          window.MAS.flash('Ящик перенесён в другую команду.', 'success');
          window.location.reload();
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        setTransferError(err.message || 'Не удалось перенести ящик.');
      } catch (_e) {
        setTransferError('Сетевая ошибка. Попробуйте ещё раз.');
      } finally {
        if (btn) btn.disabled = false;
      }
    });
  });

  document.querySelectorAll('[data-account-delete-form]').forEach(function (f) {
    f.addEventListener('submit', async function (event) {
      event.preventDefault();
      const msg = f.getAttribute('data-confirm') || 'Удалить аккаунт? Все кешированные письма будут удалены.';
      if (!window.confirm(msg)) return;
      const action = f.getAttribute('action');
      if (!action) return;
      // Prefer DELETE on the canonical resource URL.
      let deleteUrl = action;
      // Strip trailing /delete if backend exposes it as a sibling endpoint.
      deleteUrl = deleteUrl.replace(/\/delete$/, '');
      const btn = f.querySelector('button[type="submit"]');
      if (btn) btn.disabled = true;
      try {
        const resp = await window.MAS.csrfFetch(deleteUrl, { method: 'DELETE' });
        if (resp.ok || resp.status === 204) {
          window.MAS.flash('Аккаунт удалён.', 'success');
          window.location.reload();
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        window.MAS.flash(err.message || 'Не удалось удалить аккаунт.', 'error');
      } catch (_e) {
        window.MAS.flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
      } finally {
        if (btn) btn.disabled = false;
      }
    });
  });
})();
