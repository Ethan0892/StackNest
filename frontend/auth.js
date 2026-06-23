/**
 * StackNest shared auth module.
 * Include this script on any page to get a Login button in .nav-cta / .header-right,
 * the full auth modal, and a user pill with logout + profile links.
 *
 * Safe to include on pages that already have their own auth (e.g. app.html, profile.html)
 * — it checks for existing elements before injecting anything.
 */
(function () {
  'use strict';

  const TOKEN_KEY = 'stacknest_auth_token';
  const GOOGLE_CLIENT_ID = '166054350584-pg0f0s18lb8pgfm0rc5itlfu93ngdtjb.apps.googleusercontent.com';
  let authToken = localStorage.getItem(TOKEN_KEY) || '';
  let currentUser = null;
  let _gsiReady = false;

  // ─────────────────────────────────────────────────────────────────────────
  // CSS injection
  // ─────────────────────────────────────────────────────────────────────────
  function injectCSS() {
    if (document.getElementById('sn-auth-css')) return;
    const s = document.createElement('style');
    s.id = 'sn-auth-css';
    s.textContent = `
      /* ── Auth modal overlay ── */
      .sn-auth-modal {
        position: fixed; inset: 0;
        background: rgba(7,10,24,0.78);
        display: none; align-items: center; justify-content: center;
        padding: 18px; z-index: 2000;
      }
      .sn-auth-modal.open { display: flex; }

      .sn-auth-card {
        width: min(460px, 100%);
        background: var(--surface, #17171a);
        border: 1px solid var(--border, #2a2a30);
        border-radius: 14px;
        box-shadow: 0 22px 60px rgba(0, 0, 0, .5);
        padding: 28px 26px 24px;
        color: var(--text, #f0f0f5);
        font-family: var(--sans, system-ui, sans-serif);
      }

      .sn-auth-head {
        display: flex; align-items: center;
        justify-content: space-between; margin-bottom: 18px;
      }
      .sn-auth-title { font-size: 1.05rem; font-weight: 700; }
      .sn-auth-close {
        border: 1px solid var(--border, #2a2a30);
        background: var(--surface2, #111113);
        color: var(--muted, #8b8b99);
        border-radius: 8px; padding: 6px 11px;
        cursor: pointer; font-family: inherit; font-size: 0.8rem;
        transition: border-color 0.15s, color 0.15s;
      }
      .sn-auth-close:hover { color: var(--text, #f0f0f5); border-color: var(--accent, #5c6fff); }

      .sn-mode-tabs {
        display: inline-flex;
        background: var(--surface2, #111113);
        border: 1px solid var(--border, #2a2a30);
        border-radius: 10px; overflow: hidden; margin-bottom: 18px;
      }
      .sn-mode-btn {
        border: none; background: transparent;
        color: var(--muted, #8b8b99); padding: 9px 18px;
        font-size: 0.82rem; font-weight: 600;
        cursor: pointer; font-family: inherit;
        transition: color 0.15s;
      }
      .sn-mode-btn.active { background: var(--accent, #5c6fff); color: #fff; }

      .sn-field { margin-bottom: 13px; }
      .sn-field label {
        display: block; font-size: 0.76rem; font-weight: 600;
        color: var(--muted, #8b8b99); margin-bottom: 5px; letter-spacing: 0.05em;
      }
      .sn-field input {
        width: 100%; box-sizing: border-box;
        background: var(--bg2, #0d0d0f);
        color: var(--text, #f0f0f5);
        border: 1px solid var(--border, #2a2a30);
        border-radius: 8px; padding: 10px 13px;
        font-size: 0.9rem; font-family: inherit;
        outline: none; transition: border-color 0.2s;
      }
      .sn-field input:focus { border-color: var(--accent, #5c6fff); }
      .sn-field input::placeholder { color: var(--muted, #6b6b7b); }
      .sn-field input:-webkit-autofill {
        -webkit-box-shadow: 0 0 0 100px var(--bg2, #0d0d0f) inset !important;
        -webkit-text-fill-color: var(--text, #f0f0f5) !important;
      }

      .sn-submit-btn {
        background: var(--accent, #5c6fff); color: #fff;
        border: none; border-radius: 8px; padding: 10px 20px;
        font-size: 0.85rem; font-weight: 600;
        cursor: pointer; font-family: inherit; transition: opacity 0.15s;
      }
      .sn-submit-btn:hover { opacity: 0.85; }

      .sn-auth-msg {
        margin-top: 11px; font-size: 0.8rem;
        color: var(--muted, #8b8b99); line-height: 1.5;
      }

      /* ── Nav Login button ── */
      .sn-login-btn {
        border: 1px solid var(--border, #2a2a30);
        background: var(--surface2, #111113);
        color: var(--text, #f0f0f5);
        border-radius: 999px; padding: 8px 16px;
        font-size: 0.78rem; font-weight: 600;
        cursor: pointer; font-family: inherit;
        transition: border-color 0.15s;
      }
      .sn-login-btn:hover { border-color: var(--accent, #5c6fff); }

      /* ── User pill ── */
      .sn-user-pill {
        display: none; align-items: center; gap: 8px;
        border: 1px solid var(--border, #2a2a30);
        background: var(--surface2, #111113);
        border-radius: 999px; padding: 5px 12px 5px 5px;
        cursor: pointer; font-size: 0.78rem; font-weight: 600;
        color: var(--text, #f0f0f5); position: relative;
        user-select: none; font-family: inherit;
      }
      .sn-user-pill.visible { display: flex; }
      .sn-pill-avatar {
        width: 26px; height: 26px; border-radius: 50%;
        font-size: 0.72rem; font-weight: 700; color: #fff;
        display: flex; align-items: center; justify-content: center;
        background: var(--accent, #5c6fff); flex-shrink: 0;
      }
      .sn-pill-name { max-width: 120px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

      .sn-user-dropdown {
        display: none; position: absolute;
        top: calc(100% + 8px); right: 0;
        min-width: 175px;
        background: var(--surface, #17171a);
        border: 1px solid var(--border, #2a2a30);
        border-radius: 10px; padding: 6px;
        box-shadow: 0 8px 30px rgba(0,0,0,.4); z-index: 1500;
      }
      .sn-user-pill.open .sn-user-dropdown { display: block; }
      .sn-dd-item {
        display: block; width: 100%; text-align: left;
        background: none; border: none;
        color: var(--text, #f0f0f5); padding: 8px 10px;
        font-size: 0.8rem; font-weight: 500;
        border-radius: 6px; cursor: pointer; font-family: inherit;
        transition: background 0.12s;
      }
      .sn-dd-item:hover { background: var(--surface2, #111113); }
      .sn-dd-item.danger { color: #ff6b6b; }
      .sn-dd-sep { border: none; border-top: 1px solid var(--border, #2a2a30); margin: 4px 0; }

      /* ── Toast container (shared with main app) ── */
      #sn-toast-wrap {
        position: fixed; top: 18px; right: 18px; z-index: 9998;
        display: flex; flex-direction: column; gap: 8px; pointer-events: none;
      }

      /* ── Google button ── */
      .sn-or-divider {
        display: flex; align-items: center; gap: 10px;
        margin: 16px 0;
        font-size: 0.75rem; color: var(--muted, #6b6b7b); font-weight: 500;
      }
      .sn-or-divider::before,
      .sn-or-divider::after {
        content: ''; flex: 1;
        height: 1px; background: var(--border, #2a2a30);
      }
      .sn-google-btn {
        width: 100%; display: flex; align-items: center; justify-content: center;
        gap: 10px; padding: 10px 16px;
        background: #fff; color: #3c4043;
        border: 1px solid #dadce0; border-radius: 8px;
        font-size: 0.88rem; font-weight: 500; font-family: 'Google Sans', Roboto, inherit;
        cursor: pointer; transition: box-shadow 0.15s, background 0.12s;
        box-shadow: 0 1px 3px rgba(0,0,0,.12);
      }
      .sn-google-btn:hover { background: #f8f9fa; box-shadow: 0 2px 6px rgba(0,0,0,.18); }
      .sn-google-btn svg { flex-shrink: 0; }
    `;
    document.head.appendChild(s);
  }

  // ─────────────────────────────────────────────────────────────────────────
  // HTML injection
  // ─────────────────────────────────────────────────────────────────────────
  function injectModal() {
    if (document.getElementById('sn-auth-modal')) return; // already present
    if (document.getElementById('auth-modal')) return;    // page has own modal (app.html)

    const el = document.createElement('div');
    el.id = 'sn-auth-modal';
    el.className = 'sn-auth-modal';
    el.setAttribute('role', 'dialog');
    el.setAttribute('aria-modal', 'true');
    el.innerHTML = `
      <div class="sn-auth-card" id="sn-auth-card">
        <div class="sn-auth-head">
          <div class="sn-auth-title" id="sn-auth-title">Welcome to StackNest</div>
          <button class="sn-auth-close" id="sn-auth-close-btn" type="button">Close</button>
        </div>
        <div class="sn-mode-tabs" role="tablist">
          <button class="sn-mode-btn active" id="sn-tab-login" type="button" role="tab">Login</button>
          <button class="sn-mode-btn" id="sn-tab-signup" type="button" role="tab">Sign up</button>
        </div>
        <div class="sn-field">
          <label for="sn-email">Email</label>
          <input type="email" id="sn-email" placeholder="you@example.com" autocomplete="email" />
        </div>
        <div class="sn-field">
          <label for="sn-password">Password</label>
          <input type="password" id="sn-password" placeholder="At least 8 characters" autocomplete="current-password" />
        </div>
        <div class="sn-field" id="sn-name-field" style="display:none">
          <label for="sn-name">Display name</label>
          <input type="text" id="sn-name" placeholder="StackBuilder" autocomplete="name" />
        </div>
        <div>
          <button class="sn-submit-btn" id="sn-action-btn" type="button">Login</button>
        </div>
        <div class="sn-auth-msg" id="sn-auth-msg">Sign in to save projects across devices.</div>
        <div class="sn-or-divider">or</div>
        <button class="sn-google-btn" id="sn-google-btn" type="button">
          <svg width="18" height="18" viewBox="0 0 18 18" fill="none" xmlns="http://www.w3.org/2000/svg">
            <path d="M17.64 9.205c0-.639-.057-1.252-.164-1.841H9v3.481h4.844a4.14 4.14 0 01-1.796 2.716v2.259h2.908c1.702-1.567 2.684-3.875 2.684-6.615z" fill="#4285F4"/>
            <path d="M9 18c2.43 0 4.467-.806 5.956-2.18l-2.908-2.259c-.806.54-1.837.86-3.048.86-2.344 0-4.328-1.584-5.036-3.711H.957v2.332A8.997 8.997 0 009 18z" fill="#34A853"/>
            <path d="M3.964 10.71A5.41 5.41 0 013.682 9c0-.593.102-1.17.282-1.71V4.958H.957A8.996 8.996 0 000 9c0 1.452.348 2.827.957 4.042l3.007-2.332z" fill="#FBBC05"/>
            <path d="M9 3.58c1.321 0 2.508.454 3.44 1.345l2.582-2.58C13.463.891 11.426 0 9 0A8.997 8.997 0 00.957 4.958L3.964 7.29C4.672 5.163 6.656 3.58 9 3.58z" fill="#EA4335"/>
          </svg>
          Sign in with Google
        </button>
      </div>
    `;
    document.body.appendChild(el);

    // Toast container (only if nothing else added one)
    if (!document.getElementById('toast-container') && !document.getElementById('sn-toast-wrap')) {
      const tc = document.createElement('div');
      tc.id = 'sn-toast-wrap';
      document.body.appendChild(tc);
    }

    // Wire events
    el.addEventListener('click', (e) => { if (e.target === el) snClose(); });
    document.getElementById('sn-auth-close-btn').addEventListener('click', snClose);
    document.getElementById('sn-tab-login').addEventListener('click', () => snMode('login'));
    document.getElementById('sn-tab-signup').addEventListener('click', () => snMode('signup'));
    document.getElementById('sn-action-btn').addEventListener('click', () => {
      const signup = document.getElementById('sn-tab-signup').classList.contains('active');
      signup ? snRegister() : snLogin();
    });
    document.getElementById('sn-google-btn').addEventListener('click', snGoogleSignIn);
    // Enter key submits
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        const signup = document.getElementById('sn-tab-signup').classList.contains('active');
        signup ? snRegister() : snLogin();
      }
    });
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') snClose(); });
  }

  // ─────────────────────────────────────────────────────────────────────────
  // Inject nav Login button + user pill
  // ─────────────────────────────────────────────────────────────────────────
  function injectNavAuth() {
    // Skip if page already has auth UI (app.html)
    if (document.getElementById('auth-nav-btn')) return;

    const existingLoginBtn = document.getElementById('sn-login-btn');
    if (existingLoginBtn) {
      existingLoginBtn.addEventListener('click', openAuthModal);
      // Also wire up the mobile variant if present
      const mobileBtn = document.getElementById('sn-login-btn-m');
      if (mobileBtn) mobileBtn.addEventListener('click', openAuthModal);
      // Inject user pill next to the existing login button so logged-in state is visible
      if (!document.getElementById('sn-user-pill')) {
        _injectPill(existingLoginBtn.parentElement);
      }
      return;
    }

    // Try multiple nav container selectors in order of preference.
    // .nav-cta on index.html is a <div>, but on pricing.html it's an <a> link —
    // so we specifically avoid injecting INTO anchor/button elements.
    let host = null;
    const candidates = [
      '.nav-cta',        // index.html (div)
      '.header-right',   // app.html-like pages
      '.nav-links',      // gallery.html
      '.nav-inner',      // pricing.html
      'header',          // logs.html, editor.html
    ];
    for (const sel of candidates) {
      const el = document.querySelector(sel);
      if (el && el.tagName !== 'A' && el.tagName !== 'BUTTON') {
        host = el;
        break;
      }
    }
    if (!host) return;

    // Login button
    const loginBtn = document.createElement('button');
    loginBtn.id = 'sn-login-btn';
    loginBtn.className = 'sn-login-btn';
    loginBtn.type = 'button';
    loginBtn.textContent = 'Login';
    loginBtn.addEventListener('click', openAuthModal);

    host.appendChild(loginBtn);
    _injectPill(host);
  }

  function _injectPill(host) {
    if (!host || document.getElementById('sn-user-pill')) return;

    // User pill  
    const pill = document.createElement('div');
    pill.id = 'sn-user-pill';
    pill.className = 'sn-user-pill';
    pill.setAttribute('aria-haspopup', 'true');
    pill.innerHTML = `
      <div class="sn-pill-avatar" id="sn-pill-avatar"></div>
      <span class="sn-pill-name" id="sn-pill-name">Account</span>
      <div class="sn-user-dropdown" id="sn-user-dropdown">
        <button class="sn-dd-item" type="button" onclick="location.href='/profile'">&#9881;&#xFE0E; Profile</button>
        <button class="sn-dd-item" type="button" onclick="location.href='/app'">&#128640; Launch App</button>
        <hr class="sn-dd-sep" />
        <button class="sn-dd-item danger" type="button" id="sn-logout-btn">&#8594; Log out</button>
      </div>
    `;
    pill.addEventListener('click', (e) => {
      e.stopPropagation();
      pill.classList.toggle('open');
    });
    document.addEventListener('click', () => pill.classList.remove('open'));

    host.appendChild(pill);

    document.getElementById('sn-logout-btn').addEventListener('click', async (e) => {
      e.stopPropagation();
      try {
        if (authToken) {
          await fetch('/api/auth/logout', {
            method: 'POST',
            headers: { Authorization: `Bearer ${authToken}` },
          });
        }
      } catch {}
      authToken = '';
      currentUser = null;
      localStorage.removeItem(TOKEN_KEY);
      pill.classList.remove('open');
      updateAuthNav();
      if (typeof window.snOnAuthReady === 'function') window.snOnAuthReady(null);
      snToast('Logged out.', 'info');
    });
  }

  // ─────────────────────────────────────────────────────────────────────────
  // Modal helpers
  // ─────────────────────────────────────────────────────────────────────────
  function snMode(mode) {
    const isSignup = mode === 'signup';
    document.getElementById('sn-tab-login').classList.toggle('active', !isSignup);
    document.getElementById('sn-tab-signup').classList.toggle('active', isSignup);
    document.getElementById('sn-name-field').style.display = isSignup ? 'block' : 'none';
    document.getElementById('sn-action-btn').textContent = isSignup ? 'Create account' : 'Login';
    if (isSignup) {
      document.getElementById('sn-password').setAttribute('autocomplete', 'new-password');
    } else {
      document.getElementById('sn-password').setAttribute('autocomplete', 'current-password');
    }
  }

  function snMsg(text, isErr = false) {
    const el = document.getElementById('sn-auth-msg');
    if (!el) return;
    el.textContent = text;
    el.style.color = isErr ? '#ff6b6b' : 'var(--muted, #8b8b99)';
  }

  function openAuthModal() {
    // If the page has its own modal (app.html), use that instead
    const appModal = document.getElementById('auth-modal');
    if (appModal) {
      if (window.openAuthModal && window.openAuthModal !== openAuthModal) {
        window.openAuthModal();
        return;
      }
    }
    const modal = document.getElementById('sn-auth-modal');
    if (!modal) return;
    snMode('login');
    snMsg('Sign in to save projects across devices.');
    modal.classList.add('open');
    setTimeout(() => document.getElementById('sn-email')?.focus(), 50);
  }
  window.snOpenAuthModal = openAuthModal;

  function snClose() {
    const modal = document.getElementById('sn-auth-modal');
    if (modal) modal.classList.remove('open');
  }

  // ─────────────────────────────────────────────────────────────────────────
  // Login / Register
  // ─────────────────────────────────────────────────────────────────────────
  async function snLogin() {
    const email = (document.getElementById('sn-email')?.value || '').trim();
    const password = document.getElementById('sn-password')?.value || '';
    if (!email || !password) { snMsg('Email and password are required.', true); return; }
    snMsg('Signing in…');
    try {
      const resp = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || 'Login failed');
      authToken = data.token;
      currentUser = data.user;
      localStorage.setItem(TOKEN_KEY, authToken);
      updateAuthNav();
      snClose();
      snToast('Welcome back, ' + (currentUser.display_name || 'there') + '!', 'success');
      if (typeof window.snOnAuthReady === 'function') window.snOnAuthReady(currentUser);
    } catch (e) {
      snMsg(e.message, true);
    }
  }

  async function snRegister() {
    const email = (document.getElementById('sn-email')?.value || '').trim();
    const password = document.getElementById('sn-password')?.value || '';
    const display_name = (document.getElementById('sn-name')?.value || '').trim();
    if (!email || !password) { snMsg('Email and password are required.', true); return; }
    snMsg('Creating account…');
    try {
      const resp = await fetch('/api/auth/register', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password, display_name }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || 'Sign-up failed');
      authToken = data.token;
      currentUser = data.user;
      localStorage.setItem(TOKEN_KEY, authToken);
      updateAuthNav();
      if (typeof window.snOnAuthReady === 'function') window.snOnAuthReady(currentUser);
      // Show email-check screen
      const card = document.getElementById('sn-auth-card');
      if (card) {
        card.innerHTML = `
          <div style="text-align:center;padding:24px 8px">
            <div style="font-size:2.4rem;margin-bottom:14px">&#128231;</div>
            <div style="font-weight:700;font-size:1.1rem;margin-bottom:10px;color:var(--text,#f0f0f5)">Check your inbox</div>
            <p style="color:var(--muted,#8b8b99);font-size:0.84rem;line-height:1.65;margin:0 0 20px">
              We've sent a verification link to <strong style="color:var(--text,#f0f0f5)">${escHtml(email)}</strong>.<br>
              Click it to unlock plugin generation.
            </p>
            <button class="sn-submit-btn" onclick="document.getElementById('sn-auth-modal').classList.remove('open')">Got it</button>
          </div>`;
      }
    } catch (e) {
      snMsg(e.message, true);
    }
  }

  function escHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  // ─────────────────────────────────────────────────────────────────────────
  // Google Sign-In
  // ─────────────────────────────────────────────────────────────────────────
  function _loadGSI(cb) {
    if (window.google?.accounts?.id) { cb(); return; }
    if (document.getElementById('sn-gsi-script')) {
      // Script is already loading — wait for it
      window.addEventListener('sn-gsi-ready', cb, { once: true });
      return;
    }
    const s = document.createElement('script');
    s.id = 'sn-gsi-script';
    s.src = 'https://accounts.google.com/gsi/client';
    s.async = true;
    s.defer = true;
    s.onload = () => {
      _gsiReady = true;
      window.dispatchEvent(new Event('sn-gsi-ready'));
      cb();
    };
    document.head.appendChild(s);
  }

  function _initGSI() {
    if (!window.google?.accounts?.id || !GOOGLE_CLIENT_ID) return;
    window.google.accounts.id.initialize({
      client_id: GOOGLE_CLIENT_ID,
      callback: _handleGoogleCredential,
      auto_select: false,
      cancel_on_tap_outside: false,
    });
  }

  async function _handleGoogleCredential(response) {
    snMsg('Signing in with Google\u2026');
    try {
      const resp = await fetch('/api/auth/google', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ credential: response.credential }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || 'Google sign-in failed');
      authToken = data.token;
      currentUser = data.user;
      localStorage.setItem(TOKEN_KEY, authToken);
      updateAuthNav();
      snClose();
      snToast('Welcome, ' + (currentUser.display_name || 'there') + '! \uD83D\uDE80', 'success');
    } catch (e) {
      snMsg(e.message, true);
    }
  }

  function snGoogleSignIn() {
    if (!GOOGLE_CLIENT_ID) { snMsg('Google sign-in is not configured.', true); return; }
    // Use the GSI (Google Identity Services) flow — credential delivered via JS
    // callback, no redirect_uri required, no popup-blocker risk.
    _loadGSI(() => {
      _initGSI();
      window.google.accounts.id.prompt((notification) => {
        if (notification.isDismissedMoment()) {
          snMsg('Sign in with Google cancelled.', true);
        }
      });
    });
  }

  // Also handle Google Sign-In inside app.html's existing modal
  // (the button is injected there too — see app.html changes)
  window._snHandleGoogleCredential = _handleGoogleCredential;


  // ─────────────────────────────────────────────────────────────────────────
  function updateAuthNav() {
    const loginBtn = document.getElementById('sn-login-btn');
    const loginBtnM = document.getElementById('sn-login-btn-m');
    const pill = document.getElementById('sn-user-pill');
    const avatar = document.getElementById('sn-pill-avatar');
    const name = document.getElementById('sn-pill-name');

    if (!loginBtn && !loginBtnM && !pill) return;

    if (currentUser) {
      if (loginBtn) loginBtn.style.display = 'none';
      if (loginBtnM) loginBtnM.style.display = 'none';
      if (pill) {
        pill.classList.add('visible');
        const color = currentUser.avatar_color || '#5c6fff';
        if (avatar) {
          avatar.style.background = color;
          avatar.textContent = (currentUser.display_name || '?')[0].toUpperCase();
        }
        if (name) name.textContent = currentUser.display_name || 'Account';
      }
    } else {
      if (loginBtn) loginBtn.style.display = '';
      if (loginBtnM) loginBtnM.style.display = '';
      if (pill) pill.classList.remove('visible');
    }
  }

  // ─────────────────────────────────────────────────────────────────────────
  // Toast
  // ─────────────────────────────────────────────────────────────────────────
  function snToast(msg, type = 'info') {
    const container =
      document.getElementById('toast-container') ||
      document.getElementById('sn-toast-wrap');
    if (!container) return;
    const colors = {
      success: { bg: '#3ddc8422', border: '#3ddc8466', text: '#3ddc84' },
      error:   { bg: '#ff5c5c22', border: '#ff5c5c66', text: '#ff6b6b' },
      warn:    { bg: '#ffc94d22', border: '#ffc94d66', text: '#ffc94d' },
      info:    { bg: '#5c6fff22', border: '#5c6fff66', text: '#5c6fff' },
    };
    const c = colors[type] || colors.info;
    const t = document.createElement('div');
    t.style.cssText = [
      `background:${c.bg}`, `border:1px solid ${c.border}`, `color:${c.text}`,
      'padding:10px 14px', 'border-radius:8px', 'font-size:0.82rem', 'font-weight:600',
      'pointer-events:none', 'box-shadow:0 4px 16px rgba(0,0,0,.3)', 'max-width:300px',
      'opacity:1', 'transition:opacity 0.4s', 'font-family:var(--sans,system-ui,sans-serif)',
    ].join(';');
    t.textContent = msg;
    container.appendChild(t);
    setTimeout(() => { t.style.opacity = '0'; setTimeout(() => t.remove(), 420); }, 3500);
  }

  // ─────────────────────────────────────────────────────────────────────────
  // Init
  // ─────────────────────────────────────────────────────────────────────────
  async function init() {
    injectCSS();
    injectModal();
    injectNavAuth();

    // Pre-load Google Sign-In script in the background (non-blocking)
    if (GOOGLE_CLIENT_ID) _loadGSI(_initGSI);

    if (!authToken) {
      // Definitely logged out — show login button and notify pages immediately
      updateAuthNav();
      if (typeof window.snOnAuthReady === 'function') window.snOnAuthReady(null);
      return;
    }

    // Token exists: pre-hide login buttons NOW to prevent a flash-of-unsigned-state
    // while /api/auth/me is in flight. updateAuthNav() will restore correct state.
    const _lb  = document.getElementById('sn-login-btn');
    const _lbm = document.getElementById('sn-login-btn-m');
    if (_lb)  _lb.style.display  = 'none';
    if (_lbm) _lbm.style.display = 'none';

    try {
      const resp = await fetch('/api/auth/me', {
        headers: { Authorization: `Bearer ${authToken}` },
      });
      if (resp.status === 401) {
        // Token is definitively invalid — clear it and show the login button
        authToken = '';
        currentUser = null;
        localStorage.removeItem(TOKEN_KEY);
        updateAuthNav();
        if (typeof window.snOnAuthReady === 'function') window.snOnAuthReady(null);
        return;
      }
      if (!resp.ok) {
        // Transient error (rate limit, server busy, network blip) — keep token
        // but restore the login button so the user isn't invisibly locked out
        updateAuthNav();
        return;
      }
      const data = await resp.json();
      currentUser = data.user;
      // Sliding renewal: server issues a fresh token when the current one is
      // older than the refresh threshold — save it silently so the user stays
      // logged in indefinitely as long as they visit within 90 days.
      if (data.new_token) {
        authToken = data.new_token;
        localStorage.setItem(TOKEN_KEY, authToken);
      }
      updateAuthNav();
      if (typeof window.snOnAuthReady === 'function') window.snOnAuthReady(currentUser);
    } catch {
      // Network error — keep the token but restore login button as fallback
      updateAuthNav();
    }
  }

  // Expose public API
  window.snOpenAuthModal = openAuthModal;
  /** Returns the current auth token string, or null if not logged in. */
  window.snGetToken = function() { return authToken || null; };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
