/* =============================================================================
   admin_users.js
   Admin users-page UX:
     - "Create user" dialog (POST /api/admin/users) with display_name,
       role (group_leader|group_member) and group_id select that toggles
       visibility based on the chosen role (ADR-0019).
     - "Reset password" with confirm.
     - "Delete user" with type-the-username confirmation.

   Hooks (CSP-safe, addEventListener only):
     - <button data-admin-create-user>           : opens create dialog.
     - <dialog data-admin-create-dialog>         : the dialog element.
     - <form data-admin-create-user-form>        : the form inside dialog.
     - <input data-admin-role-input>             : radio buttons for role.
     - <div   data-admin-group-field>            : group <select> wrapper.
     - <select data-admin-group-select>          : group_id select.
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

  // Toggle the group select visibility according to the selected role.
  // - role=group_leader  → group select hidden + cleared (auto-create on backend).
  // - role=group_member  → group select visible + required.
  const roleInputs   = document.querySelectorAll('[data-admin-role-input]');
  const groupField   = document.querySelector('[data-admin-group-field]');
  const groupSelect  = document.querySelector('[data-admin-group-select]');

  function applyRoleVisibility() {
    let role = 'group_member';
    roleInputs.forEach(function (r) { if (r.checked) role = r.value; });
    if (!groupField || !groupSelect) return;
    if (role === 'group_leader') {
      groupField.hidden = true;
      groupSelect.required = false;
      groupSelect.value = '';
    } else {
      groupField.hidden = false;
      groupSelect.required = true;
    }
  }

  roleInputs.forEach(function (r) {
    r.addEventListener('change', applyRoleVisibility);
  });
  // Initial state on load.
  applyRoleVisibility();

  if (createBtn && createDialog) {
    createBtn.addEventListener('click', function () {
      // Reset role to default + apply visibility before opening.
      roleInputs.forEach(function (r) {
        r.checked = (r.getAttribute('data-role-value') === 'group_member');
      });
      applyRoleVisibility();
      if (typeof createDialog.showModal === 'function') {
        createDialog.showModal();
      } else {
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
      const displayName = (fd.get('display_name') || '').toString().trim();
      if (displayName) payload.display_name = displayName;
      const role = (fd.get('role') || '').toString().trim();
      if (role) payload.role = role;
      const groupIdRaw = (fd.get('group_id') || '').toString().trim();
      if (role === 'group_member') {
        if (!groupIdRaw) {
          window.MAS.flash('Выберите группу для участника.', 'error');
          return;
        }
        const gid = parseInt(groupIdRaw, 10);
        if (Number.isFinite(gid) && gid > 0) {
          payload.group_id = gid;
        }
      }
      // For group_leader we deliberately omit group_id — backend auto-creates.

      const submitBtn = createForm.querySelector('button[type="submit"]');
      if (submitBtn) submitBtn.disabled = true;
      try {
        const resp = await window.MAS.csrfFetch('/api/admin/users', {
          method: 'POST',
          body: payload,
        });
        if (resp.ok) {
          window.MAS.flash(
            'Пользователь создан. Сообщите ему логин — при первом входе нужно будет задать пароль.',
            'success'
          );
          if (createDialog && typeof createDialog.close === 'function') createDialog.close();
          window.location.reload();
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        window.MAS.flash(err.message || 'Не удалось создать пользователя.', 'error');
      } catch (_e) {
        window.MAS.flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
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
                  'Сбросить пароль? Пользователь должен будет задать новый при следующем входе.';
      if (!window.confirm(msg)) return;
      const action = f.getAttribute('action');
      if (!action) return;
      const btn = f.querySelector('button[type="submit"]');
      if (btn) btn.disabled = true;
      try {
        const resp = await window.MAS.csrfFetch(action, { method: 'POST' });
        if (resp.ok) {
          window.MAS.flash('Пароль сброшен. Все сессии завершены.', 'success');
          window.location.reload();
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        window.MAS.flash(err.message || 'Не удалось сбросить пароль.', 'error');
      } catch (_e) {
        window.MAS.flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
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
          window.MAS.flash('Пользователь удалён.', 'success');
          if (deleteDialog && typeof deleteDialog.close === 'function') deleteDialog.close();
          window.location.reload();
          return;
        }
        const err = await window.MAS.readJsonError(resp);
        window.MAS.flash(err.message || 'Не удалось удалить пользователя.', 'error');
      } catch (_e) {
        window.MAS.flash('Сетевая ошибка. Попробуйте ещё раз.', 'error');
      } finally {
        if (deleteGoBtn) deleteGoBtn.disabled = false;
      }
    });
  }
})();
