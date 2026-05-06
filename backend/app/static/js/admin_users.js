/* =============================================================================
   admin_users.js
   Admin users-page UX:
     - "Create user" dialog (POST /api/admin/users).
     - "Reset password" with confirm.
     - "Delete user" with type-the-username confirmation.

   Hooks (CSP-safe, addEventListener only):
     - <button data-admin-create-user>           : opens create dialog.
     - <dialog data-admin-create-dialog>         : the dialog element.
     - <form data-admin-create-user-form>        : the form inside dialog.
     - <form data-admin-reset-form data-confirm> : reset-password form.
     - <form data-admin-delete-form data-username>: delete form.
     - <dialog data-admin-delete-dialog>         : confirm-deletion dialog.
     - <strong data-admin-delete-username>       : username text inside dialog.
     - <form data-admin-delete-confirm-form>     : confirmation form (text input).
     - <input id="delete-confirm-input">         : the type-username input.
     - <button data-admin-delete-go>             : final delete button.
     - <button data-admin-delete-cancel>         : cancel button.
   ========================================================================== */
(function () {
  'use strict';

  if (!window.MAS) return;

  // ---- Create user ---------------------------------------------------------

  const createBtn    = document.querySelector('[data-admin-create-user]');
  const createDialog = document.querySelector('[data-admin-create-dialog]');
  const createForm   = document.querySelector('[data-admin-create-user-form]');

  if (createBtn && createDialog) {
    createBtn.addEventListener('click', function () {
      if (typeof createDialog.showModal === 'function') {
        createDialog.showModal();
      } else {
        // Fallback for very old browsers — show as a section.
        createDialog.setAttribute('open', 'open');
      }
    });
  }

  if (createForm) {
    createForm.addEventListener('submit', async function (event) {
      event.preventDefault();
      const fd = new FormData(createForm);
      const payload = {
        username: (fd.get('username') || '').toString().trim(),
      };
      const email = (fd.get('email') || '').toString().trim();
      if (email) payload.email = email;
      const submitBtn = createForm.querySelector('button[type="submit"]');
      if (submitBtn) submitBtn.disabled = true;
      try {
        const resp = await window.MAS.csrfFetch('/api/admin/users', {
          method: 'POST',
          body: payload,
        });
        if (resp.ok) {
          window.MAS.flash(
            'User created. Tell them to log in with their username; password setup will be required.',
            'success'
          );
          if (createDialog && typeof createDialog.close === 'function') createDialog.close();
          window.location.reload();
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        window.MAS.flash(err.message || 'Could not create user.', 'error');
      } catch (_e) {
        window.MAS.flash('Network error. Please try again.', 'error');
      } finally {
        if (submitBtn) submitBtn.disabled = false;
      }
    });
  }

  // ---- Reset password ------------------------------------------------------

  document.querySelectorAll('[data-admin-reset-form]').forEach(function (f) {
    f.addEventListener('submit', async function (event) {
      event.preventDefault();
      const msg = f.getAttribute('data-confirm') ||
                  'Reset password? The user will be required to set a new one.';
      if (!window.confirm(msg)) return;
      const action = f.getAttribute('action');
      if (!action) return;
      const btn = f.querySelector('button[type="submit"]');
      if (btn) btn.disabled = true;
      try {
        const resp = await window.MAS.csrfFetch(action, { method: 'POST' });
        if (resp.ok) {
          window.MAS.flash('Password reset. All sessions revoked.', 'success');
          window.location.reload();
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        window.MAS.flash(err.message || 'Could not reset password.', 'error');
      } catch (_e) {
        window.MAS.flash('Network error. Please try again.', 'error');
      } finally {
        if (btn) btn.disabled = false;
      }
    });
  });

  // ---- Delete user (type-username confirmation) ----------------------------

  const deleteDialog        = document.querySelector('[data-admin-delete-dialog]');
  const deleteUsernameLabel = document.querySelector('[data-admin-delete-username]');
  const deleteConfirmForm   = document.querySelector('[data-admin-delete-confirm-form]');
  const deleteConfirmInput  = document.getElementById('delete-confirm-input');
  const deleteGoBtn         = document.querySelector('[data-admin-delete-go]');
  const deleteCancelBtn     = document.querySelector('[data-admin-delete-cancel]');

  let pendingDeleteAction = '';
  let pendingDeleteUsername = '';

  document.querySelectorAll('[data-admin-delete-form]').forEach(function (f) {
    f.addEventListener('submit', function (event) {
      event.preventDefault();
      pendingDeleteAction = f.getAttribute('action') || '';
      pendingDeleteUsername = f.getAttribute('data-username') || '';
      if (!pendingDeleteAction || !pendingDeleteUsername) return;
      if (deleteUsernameLabel) deleteUsernameLabel.textContent = pendingDeleteUsername;
      if (deleteConfirmInput) {
        deleteConfirmInput.value = '';
        deleteConfirmInput.focus();
      }
      if (deleteGoBtn) deleteGoBtn.disabled = true;
      if (deleteDialog && typeof deleteDialog.showModal === 'function') {
        deleteDialog.showModal();
      } else if (deleteDialog) {
        deleteDialog.setAttribute('open', 'open');
      }
    });
  });

  if (deleteConfirmInput && deleteGoBtn) {
    deleteConfirmInput.addEventListener('input', function () {
      deleteGoBtn.disabled = (deleteConfirmInput.value !== pendingDeleteUsername);
    });
  }

  if (deleteCancelBtn && deleteDialog) {
    deleteCancelBtn.addEventListener('click', function () {
      if (typeof deleteDialog.close === 'function') deleteDialog.close();
      else deleteDialog.removeAttribute('open');
    });
  }

  if (deleteConfirmForm) {
    deleteConfirmForm.addEventListener('submit', async function (event) {
      event.preventDefault();
      if (!pendingDeleteAction || !pendingDeleteUsername) return;
      if (!deleteConfirmInput || deleteConfirmInput.value !== pendingDeleteUsername) return;

      // Strip trailing /delete if present and use the canonical DELETE verb.
      const url = pendingDeleteAction.replace(/\/delete$/, '');
      if (deleteGoBtn) deleteGoBtn.disabled = true;
      try {
        const resp = await window.MAS.csrfFetch(url, { method: 'DELETE' });
        if (resp.ok || resp.status === 204) {
          window.MAS.flash('User deleted.', 'success');
          if (deleteDialog && typeof deleteDialog.close === 'function') deleteDialog.close();
          window.location.reload();
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        window.MAS.flash(err.message || 'Could not delete user.', 'error');
      } catch (_e) {
        window.MAS.flash('Network error. Please try again.', 'error');
      } finally {
        if (deleteGoBtn) deleteGoBtn.disabled = false;
      }
    });
  }
})();
