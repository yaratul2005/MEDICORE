// MediCore Application JavaScript

document.addEventListener('DOMContentLoaded', () => {
  initThemeAndDensity();
  initGlobalShortcuts();
});

function initThemeAndDensity() {
  const savedTheme = localStorage.getItem('medicore_theme') || document.documentElement.getAttribute('data-theme') || 'light';
  const savedDensity = localStorage.getItem('medicore_density') || document.documentElement.getAttribute('data-density') || 'compact';
  
  setTheme(savedTheme, false);
  setDensity(savedDensity, false);
}

function setTheme(theme, save = true) {
  document.documentElement.setAttribute('data-theme', theme);
  if (save) {
    localStorage.setItem('medicore_theme', theme);
    persistUserPreference('theme', theme);
  }
}

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme') || 'light';
  const next = current === 'dark' ? 'light' : 'dark';
  setTheme(next);
}

function setDensity(density, save = true) {
  document.documentElement.setAttribute('data-density', density);
  if (save) {
    localStorage.setItem('medicore_density', density);
    persistUserPreference('density', density);
  }
}

function toggleDensity() {
  const current = document.documentElement.getAttribute('data-density') || 'compact';
  const next = current === 'comfortable' ? 'compact' : 'comfortable';
  setDensity(next);
}

function persistUserPreference(key, value) {
  fetch('/api/preferences', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ [key]: value })
  }).catch(() => {});
}

function initGlobalShortcuts() {
  document.addEventListener('keydown', (e) => {
    // Ctrl+K or Cmd+K for command palette
    if ((e.ctrlKey || e.metaKey) && e.key === 'k') {
      e.preventDefault();
      toggleCommandPalette();
    }
    // Escape to close modals and palette
    if (e.key === 'Escape') {
      closeModals();
    }
  });
}

function toggleCommandPalette() {
  const palette = document.getElementById('command-palette-modal');
  if (palette) {
    if (palette.classList.contains('hidden')) {
      palette.classList.remove('hidden');
      const input = document.getElementById('palette-search-input');
      if (input) {
        input.value = '';
        input.focus();
        // Load initial actions
        htmx.ajax('GET', '/api/palette?q=', { target: '#palette-results', swap: 'innerHTML' });
      }
    } else {
      palette.classList.add('hidden');
    }
  }
}

function closeModals() {
  const palette = document.getElementById('command-palette-modal');
  if (palette) palette.classList.add('hidden');

  const modalContainer = document.getElementById('modal-container');
  if (modalContainer) modalContainer.innerHTML = '';
}

function showToast(message, type = 'info') {
  const container = document.getElementById('toast-container');
  if (!container) return;

  const toast = document.createElement('div');
  const bg = type === 'error' ? 'bg-rose-600 text-white' : (type === 'success' ? 'bg-emerald-600 text-white' : 'bg-slate-800 text-white');
  toast.className = `flex items-center gap-2 px-4 py-2.5 rounded-lg shadow-lg text-xs font-medium transform transition-all duration-300 opacity-0 translate-y-2 ${bg}`;
  toast.textContent = message;

  container.appendChild(toast);
  requestAnimationFrame(() => {
    toast.classList.remove('opacity-0', 'translate-y-2');
  });

  setTimeout(() => {
    toast.classList.add('opacity-0', '-translate-y-2');
    setTimeout(() => toast.remove(), 300);
  }, 3500);
}

// HTMX Global Listeners
document.addEventListener('htmx:responseError', (event) => {
  showToast(event.detail.error || 'Request failed. Please check permissions.', 'error');
});
