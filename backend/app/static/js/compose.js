/* =============================================================================
   compose.js
   Compose-form UX: subject counter, basic email-format hint, AJAX submit
   that calls POST /api/messages/send and on success redirects to inbox.

   Hooks (CSP-safe):
     - <form data-compose-form>            : intercept submit, send via fetch.
     - <input data-compose-emails>         : highlight invalid addresses on blur.
     - <input data-compose-subject>        : update counter.
     - <span data-compose-subject-count>   : counter target.
     - <textarea data-compose-body>        : enforce 1 MiB client-side.
     - <button data-compose-submit>        : disabled while in-flight.
     - <p data-compose-error>              : inline error pane.
   ========================================================================== */
(function () {
  'use strict';

  if (!window.MAS) return;

  const form = document.querySelector('[data-compose-form]');
  if (!form) return;

  const subjectInput = form.querySelector('[data-compose-subject]');
  const subjectCount = document.querySelector('[data-compose-subject-count]');
  const bodyInput = form.querySelector('[data-compose-body]');
  const submitBtn = form.querySelector('[data-compose-submit]');
  const errorPane = document.querySelector('[data-compose-error]');
  const emailInputs = form.querySelectorAll('[data-compose-emails]');

  const MAX_BODY_BYTES = 1024 * 1024;
  // RFC 5322 is more permissive; for UX hint only we use a pragmatic regex.
  const EMAIL_RE = /^[^\s@,]+@[^\s@,]+\.[^\s@,]+$/;

  function setError(text) {
    if (!errorPane) return;
    if (!text) {
      errorPane.hidden = true;
      errorPane.textContent = '';
      return;
    }
    errorPane.hidden = false;
    errorPane.textContent = text;
  }

  function updateSubjectCount() {
    if (!subjectInput || !subjectCount) return;
    subjectCount.textContent = String(subjectInput.value.length);
  }

  function splitAddresses(value) {
    return value.split(',').map(function (s) { return s.trim(); }).filter(Boolean);
  }

  function validateEmailField(input) {
    const addrs = splitAddresses(input.value);
    const allOk = addrs.every(function (a) { return EMAIL_RE.test(a); });
    if (input.value && !allOk) {
      input.setAttribute('aria-invalid', 'true');
      input.classList.add('field__input--invalid');
    } else {
      input.removeAttribute('aria-invalid');
      input.classList.remove('field__input--invalid');
    }
    return addrs;
  }

  // ---- Init -----------------------------------------------------------------

  if (subjectInput && subjectCount) {
    updateSubjectCount();
    subjectInput.addEventListener('input', updateSubjectCount);
  }

  for (let i = 0; i < emailInputs.length; i++) {
    emailInputs[i].addEventListener('blur', function () {
      validateEmailField(emailInputs[i]);
    });
  }

  if (bodyInput) {
    bodyInput.addEventListener('input', function () {
      const bytes = new Blob([bodyInput.value]).size;
      if (bytes > MAX_BODY_BYTES) {
        setError('Message body exceeds 1 MiB.');
        if (submitBtn) submitBtn.disabled = true;
      } else {
        if (errorPane && !errorPane.hidden && errorPane.textContent.indexOf('1 MiB') !== -1) {
          setError('');
        }
        if (submitBtn) submitBtn.disabled = false;
      }
    });
  }

  // ---- Submit (AJAX) --------------------------------------------------------

  form.addEventListener('submit', async function (event) {
    event.preventDefault();
    setError('');

    const fromAccountId = form.querySelector('[name="from_account_id"]');
    const toInput  = form.querySelector('[name="to"]');
    const ccInput  = form.querySelector('[name="cc"]');
    const bccInput = form.querySelector('[name="bcc"]');
    const subjInput = form.querySelector('[name="subject"]');
    const inReplyToInput = form.querySelector('[name="in_reply_to_message_id"]');

    if (!fromAccountId || !fromAccountId.value) {
      setError('Please select a From account.');
      return;
    }
    if (!toInput || !toInput.value.trim()) {
      setError('Please enter at least one recipient.');
      return;
    }

    const toAddrs = splitAddresses(toInput ? toInput.value : '');
    const ccAddrs = splitAddresses(ccInput ? ccInput.value : '');
    const bccAddrs = splitAddresses(bccInput ? bccInput.value : '');

    // Quick client-side address shape check (server-side is authoritative).
    const allAddrs = [].concat(toAddrs, ccAddrs, bccAddrs);
    const badAddr = allAddrs.find(function (a) { return !EMAIL_RE.test(a); });
    if (badAddr) {
      setError('Invalid address: ' + badAddr);
      return;
    }

    const payload = {
      from_account_id: Number(fromAccountId.value),
      to: toAddrs,
      body: bodyInput ? bodyInput.value : '',
    };
    if (ccAddrs.length)  payload.cc  = ccAddrs;
    if (bccAddrs.length) payload.bcc = bccAddrs;
    if (subjInput && subjInput.value) payload.subject = subjInput.value;
    if (inReplyToInput && inReplyToInput.value) {
      payload.in_reply_to_message_id = Number(inReplyToInput.value);
    }

    if (submitBtn) {
      submitBtn.disabled = true;
      submitBtn.dataset.originalLabel = submitBtn.textContent;
      submitBtn.textContent = 'Sending…';
    }

    try {
      const resp = await window.MAS.csrfFetch('/api/messages/send', {
        method: 'POST',
        body: payload,
      });
      if (resp.ok) {
        window.MAS.flash('Message sent.', 'success');
        window.location.href = '/';
        return;
      }
      const err = await window.MAS.readJsonError(resp);
      // Surface backend-provided detail when available.
      const detail = err.details && err.details.detail ? ' — ' + err.details.detail : '';
      setError(err.message + detail);
    } catch (_e) {
      setError('Network error. Please try again.');
    } finally {
      if (submitBtn) {
        submitBtn.disabled = false;
        if (submitBtn.dataset.originalLabel) {
          submitBtn.textContent = submitBtn.dataset.originalLabel;
        }
      }
    }
  });
})();
