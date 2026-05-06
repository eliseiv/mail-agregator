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
    'gmail.com':       { label: 'Gmail',   imap_host: 'imap.gmail.com',     imap_port: 993, imap_ssl: true,  smtp_host: 'smtp.gmail.com',     smtp_port: 465, smtp_ssl: true,  smtp_starttls: false },
    'googlemail.com':  { label: 'Gmail',   imap_host: 'imap.gmail.com',     imap_port: 993, imap_ssl: true,  smtp_host: 'smtp.gmail.com',     smtp_port: 465, smtp_ssl: true,  smtp_starttls: false },
    'yandex.ru':       { label: 'Yandex',  imap_host: 'imap.yandex.ru',     imap_port: 993, imap_ssl: true,  smtp_host: 'smtp.yandex.ru',     smtp_port: 465, smtp_ssl: true,  smtp_starttls: false },
    'yandex.com':      { label: 'Yandex',  imap_host: 'imap.yandex.com',    imap_port: 993, imap_ssl: true,  smtp_host: 'smtp.yandex.com',    smtp_port: 465, smtp_ssl: true,  smtp_starttls: false },
    'mail.ru':         { label: 'Mail.ru', imap_host: 'imap.mail.ru',       imap_port: 993, imap_ssl: true,  smtp_host: 'smtp.mail.ru',       smtp_port: 465, smtp_ssl: true,  smtp_starttls: false },
    'inbox.ru':        { label: 'Mail.ru', imap_host: 'imap.mail.ru',       imap_port: 993, imap_ssl: true,  smtp_host: 'smtp.mail.ru',       smtp_port: 465, smtp_ssl: true,  smtp_starttls: false },
    'bk.ru':           { label: 'Mail.ru', imap_host: 'imap.mail.ru',       imap_port: 993, imap_ssl: true,  smtp_host: 'smtp.mail.ru',       smtp_port: 465, smtp_ssl: true,  smtp_starttls: false },
    'list.ru':         { label: 'Mail.ru', imap_host: 'imap.mail.ru',       imap_port: 993, imap_ssl: true,  smtp_host: 'smtp.mail.ru',       smtp_port: 465, smtp_ssl: true,  smtp_starttls: false },
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

    // Auto-suggest. Edit mode: only fill empty fields (don't clobber existing values).
    if (emailInput) {
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
      const smtpUser = (fd.get('smtp_username') || '').toString().trim();
      const smtpPass = (fd.get('smtp_password') || '').toString();
      if (smtpUser) payload.smtp_username = smtpUser;
      if (smtpPass) payload.smtp_password = smtpPass;
      // For edit mode: empty password means "keep existing"; do not send it.
      if (isEdit && !payload.password) delete payload.password;
      return payload;
    }

    // Test-connection button
    if (testBtn) {
      testBtn.addEventListener('click', async function () {
        setError('');
        setSuccess('');
        const payload = buildPayload();
        if (!payload.email || (!isEdit && !payload.password) || !payload.imap_host || !payload.smtp_host) {
          setTestResult('Please fill in email, password, IMAP host, and SMTP host first.', 'error');
          return;
        }
        if (isEdit && !payload.password) {
          setTestResult('Please enter the current password to test the connection.', 'error');
          return;
        }
        setTestResult('Testing…', 'pending');
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
            setTestResult('Failed: ' + err.message + detail, 'error');
          }
        } catch (_e) {
          setTestResult('Network error during test.', 'error');
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

      // Basic client-side guards (server-side is authoritative).
      if (!payload.email) { setError('Email is required.'); return; }
      if (!isEdit && !payload.password) { setError('Password is required.'); return; }
      if (!payload.imap_host || !payload.smtp_host) {
        setError('IMAP and SMTP host are required.'); return;
      }
      if (payload.imap_port < 1 || payload.imap_port > 65535) {
        setError('Invalid IMAP port.'); return;
      }
      if (payload.smtp_port < 1 || payload.smtp_port > 65535) {
        setError('Invalid SMTP port.'); return;
      }
      if (payload.smtp_ssl && payload.smtp_starttls) {
        setError('SMTP cannot use SSL and STARTTLS at the same time.'); return;
      }

      submitBtn && (submitBtn.disabled = true);
      try {
        const url = isEdit
          ? '/api/mail-accounts/' + encodeURIComponent(accountId)
          : '/api/mail-accounts';
        const method = isEdit ? 'PATCH' : 'POST';
        const resp = await window.MAS.csrfFetch(url, { method: method, body: payload });
        if (resp.ok) {
          window.MAS.flash(isEdit ? 'Account updated.' : 'Account added.', 'success');
          window.location.href = '/accounts';
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        const detail = err.details && err.details.detail ? ' — ' + err.details.detail : '';
        setError(err.message + detail);
      } catch (_e) {
        setError('Network error. Please try again.');
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
          window.MAS.flash('Sync queued. New messages will appear within a minute.', 'info');
        } else {
          const err = await window.MAS.readJsonError(resp);
          window.MAS.flash(err.message || 'Could not queue sync.', 'error');
        }
      } catch (_e) {
        window.MAS.flash('Network error. Please try again.', 'error');
      } finally {
        if (btn) btn.disabled = false;
      }
    });
  });

  document.querySelectorAll('[data-account-delete-form]').forEach(function (f) {
    f.addEventListener('submit', async function (event) {
      event.preventDefault();
      const msg = f.getAttribute('data-confirm') || 'Delete account? All cached messages will be removed.';
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
          window.MAS.flash('Account deleted.', 'success');
          window.location.reload();
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        window.MAS.flash(err.message || 'Could not delete account.', 'error');
      } catch (_e) {
        window.MAS.flash('Network error. Please try again.', 'error');
      } finally {
        if (btn) btn.disabled = false;
      }
    });
  });
})();
