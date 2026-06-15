/* Network Command Translator — vanilla JS, no build step. */
'use strict';

/* Used by "request a command". Auto-derived on GitHub Pages; the fallback
   below is only used elsewhere (e.g. localhost). TODO: update if it differs. */
const GITHUB_REPO = (() => {
  const host = location.hostname;
  if (host.endsWith('.github.io')) {
    const owner = host.split('.')[0];
    const repo = location.pathname.split('/').filter(Boolean)[0];
    if (owner && repo) return 'https://github.com/' + owner + '/' + repo;
  }
  return 'https://github.com/Choaterboater/securessid';
})();

const VENDOR_COLORS = {
  'aos-cx': '#FF8300',
  'aos-s': '#FFB066',
  'junos': '#84B135',
  'cisco-ios': '#1BA0D7',
  'ruckus': '#E0A526',
  'mist': '#41B6E6',
};

const VENDOR_SHORT = {
  'aos-cx': 'AOS-CX',
  'aos-s': 'AOS-S',
  'junos': 'JUNOS',
  'cisco-ios': 'CISCO IOS',
  'ruckus': 'FASTIRON',
  'mist': 'MIST',
};

/* When a live var is empty, commands show (and copy) these example values —
   per-vendor for interfaces, per the brief, so users know what to swap. */
const IFACE_EXAMPLE = {
  'aos-cx': '1/1/1',
  'aos-s': '1',
  'junos': 'ge-0/0/0',
  'cisco-ios': 'gi0/1',
  'ruckus': '1/1/1',
};
const VAR_EXAMPLE = { vlan: '120', ip: '10.0.0.1', hostname: 'sw-core-01' };

const VAR_KEYS = ['interface', 'vlan', 'ip', 'hostname'];
const PLACEHOLDER_SPLIT = /(\{\{(?:interface|vlan|ip|hostname)\}\})/g;
const PLACEHOLDER_ONE = /^\{\{(interface|vlan|ip|hostname)\}\}$/;
const PLACEHOLDER_ALL = /\{\{(interface|vlan|ip|hostname)\}\}/g;

const LS = {
  vendors: 'nct.vendors',
  theme: 'nct.theme',
  font: 'nct.font',
  vars: 'nct.vars',
  varsOpen: 'nct.varsOpen',
};

const store = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem(key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ }
  },
};

const state = {
  loaded: false,
  vendors: [],
  entries: [],
  categories: [],
  q: '',
  cat: 'all',
  enabled: new Set(),
  vars: { interface: '', vlan: '', ip: '', hostname: '' },
};

const $ = (id) => document.getElementById(id);
const resultsEl = $('results');

const ICON_COPY = '<span class="ic-copy" aria-hidden="true"><svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="5" y="5" width="8.5" height="9" rx="1.5"/><path d="M11 5V3.5A1.5 1.5 0 0 0 9.5 2h-6A1.5 1.5 0 0 0 2 3.5v8A1.5 1.5 0 0 0 3.5 13H5"/></svg></span>';
const ICON_CHECK = '<span class="ic-check" aria-hidden="true"><svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M2.5 8.5l3.5 3.5 7-8"/></svg></span>';

/* ── helpers ─────────────────────────────────────────────────── */

function issueUrl(taskGuess) {
  const title = encodeURIComponent(('Command request: ' + (taskGuess || '')).trim());
  const body = encodeURIComponent(
    '**Task:** ' + (taskGuess || '') +
    '\n**Category:** ' +
    '\n**Vendors needed:** aos-cx / aos-s / junos / cisco-ios / ruckus / mist' +
    '\n**Known syntax (any vendor):**\n```\n\n```\n');
  return GITHUB_REPO + '/issues/new?title=' + title + '&body=' + body;
}

function exampleFor(key, vendorId) {
  return key === 'interface' ? (IFACE_EXAMPLE[vendorId] || '1/1/1') : VAR_EXAMPLE[key];
}

function substitute(raw, vendorId) {
  return raw.replace(PLACEHOLDER_ALL, (m, key) => state.vars[key] || exampleFor(key, vendorId));
}

function verifySet(entry) {
  if (entry.verify === true) {
    const all = Object.keys(entry.commands || {});
    if (entry.mist_note) all.push('mist');
    return new Set(all);
  }
  if (Array.isArray(entry.verify)) return new Set(entry.verify);
  return new Set();
}

function matches(entry, ql) {
  if (!ql) return true;
  if (entry.task.toLowerCase().includes(ql)) return true;
  return (entry.tags || []).some((t) => t.toLowerCase().includes(ql));
}

async function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    try { await navigator.clipboard.writeText(text); return true; } catch { /* fall through */ }
  }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.cssText = 'position:fixed;left:-9999px;top:0';
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  return ok;
}

/* Append `text` to `el`, wrapping case-insensitive matches of `q`
   in <u class="hit"> (amber underline). DOM-built, no innerHTML. */
function appendHighlighted(el, text, q) {
  if (!q) { el.appendChild(document.createTextNode(text)); return; }
  const lower = text.toLowerCase();
  let pos = 0;
  let idx = lower.indexOf(q, pos);
  while (idx !== -1) {
    if (idx > pos) el.appendChild(document.createTextNode(text.slice(pos, idx)));
    const u = document.createElement('u');
    u.className = 'hit';
    u.textContent = text.slice(idx, idx + q.length);
    el.appendChild(u);
    pos = idx + q.length;
    idx = lower.indexOf(q, pos);
  }
  if (pos < text.length) el.appendChild(document.createTextNode(text.slice(pos)));
}

/* Render a raw command string into `el`, turning {{placeholders}} into
   value chips: green when the user set a value, amber example when empty. */
function appendCommand(el, raw, vendorId) {
  raw.split(PLACEHOLDER_SPLIT).forEach((part) => {
    const m = part.match(PLACEHOLDER_ONE);
    if (!m) {
      if (part) el.appendChild(document.createTextNode(part));
      return;
    }
    const key = m[1];
    const val = state.vars[key];
    const chip = document.createElement('span');
    chip.className = val ? 'chip' : 'chip empty';
    chip.textContent = val || exampleFor(key, vendorId);
    if (!val) chip.title = 'example value — set ' + key.toUpperCase() + ' in live vars';
    el.appendChild(chip);
  });
}

/* ── row / panel builders ────────────────────────────────────── */

function buildCopyButton(entry, vendorId, raw) {
  const btn = document.createElement('button');
  btn.className = 'copybtn';
  btn.setAttribute('aria-label', 'Copy ' + (VENDOR_SHORT[vendorId] || vendorId) + ' command: ' + entry.task);
  btn.innerHTML = ICON_COPY + ICON_CHECK;
  btn.addEventListener('click', async () => {
    const ok = await copyText(substitute(raw, vendorId));
    btn.focus({ preventScroll: true }); // execCommand fallback steals focus
    btn.classList.add(ok ? 'copied' : 'copyfail');
    clearTimeout(btn._t);
    btn._t = setTimeout(() => btn.classList.remove('copied', 'copyfail'), 1200);
  });
  return btn;
}

function buildRow(entry, vendorId, unverified) {
  const row = document.createElement('div');
  row.className = 'vrow';
  row.style.setProperty('--vc', VENDOR_COLORS[vendorId] || 'var(--line)');

  const name = document.createElement('span');
  name.className = 'vname';
  name.appendChild(document.createTextNode(VENDOR_SHORT[vendorId] || vendorId.toUpperCase()));
  if (unverified) {
    const u = document.createElement('span');
    u.className = 'unv';
    u.textContent = 'unverified';
    name.appendChild(u);
  }
  row.appendChild(name);

  if (vendorId === 'mist' && entry.mist_note) {
    const note = document.createElement('span');
    note.className = 'mist-note';
    note.textContent = entry.mist_note;
    row.appendChild(note);
    return row;
  }

  const raw = vendorId === 'mist' ? null : (entry.commands || {})[vendorId];
  if (!raw) {
    const no = document.createElement('span');
    no.className = 'noeq';
    no.textContent = 'no direct equivalent';
    row.appendChild(no);
    return row;
  }

  const cmd = document.createElement('code');
  cmd.className = 'cmd';
  appendCommand(cmd, raw, vendorId);
  row.appendChild(cmd);
  row.appendChild(buildCopyButton(entry, vendorId, raw));
  return row;
}

function buildPanel(entry, ql) {
  const panel = document.createElement('article');
  panel.className = 'panel';

  const h = document.createElement('h3');
  h.className = 'task';
  appendHighlighted(h, entry.task, ql);
  panel.appendChild(h);

  const flagged = verifySet(entry);
  for (const v of state.vendors) {
    if (!state.enabled.has(v.id)) continue;
    panel.appendChild(buildRow(entry, v.id, flagged.has(v.id)));
  }

  if (entry.notes) {
    const note = document.createElement('p');
    note.className = 'entry-note';
    note.textContent = entry.notes;
    panel.appendChild(note);
  }
  return panel;
}

/* ── render ──────────────────────────────────────────────────── */

function render() {
  if (!state.loaded) return; // never clobber the skeleton or a load error

  if (state.enabled.size === 0) {
    resultsEl.textContent = '';
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.textContent = 'No vendors selected — tap a vendor pill above to turn one back on.';
    resultsEl.appendChild(empty);
    updateStat(0);
    return;
  }

  const ql = state.q.trim().toLowerCase();
  const visible = state.entries.filter(
    (e) => (state.cat === 'all' || e.category === state.cat) && matches(e, ql)
  );

  resultsEl.textContent = '';

  if (visible.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.appendChild(document.createTextNode('No commands match — '));
    const a = document.createElement('a');
    a.href = issueUrl(state.q.trim());
    a.target = '_blank';
    a.rel = 'noopener';
    a.textContent = 'request one';
    empty.appendChild(a);
    resultsEl.appendChild(empty);
    updateStat(0);
    return;
  }

  const frag = document.createDocumentFragment();
  for (const cat of state.categories) {
    const group = visible.filter((e) => e.category === cat);
    if (group.length === 0) continue;

    const rail = document.createElement('div');
    rail.className = 'rail';
    const label = document.createElement('span');
    label.className = 'silk rail-label';
    label.textContent = cat;
    const line = document.createElement('span');
    line.className = 'rail-line';
    const count = document.createElement('span');
    count.className = 'rail-count';
    count.textContent = group.length + (group.length === 1 ? ' command' : ' commands');
    rail.append(label, line, count);
    frag.appendChild(rail);

    for (const entry of group) frag.appendChild(buildPanel(entry, ql));
  }
  resultsEl.appendChild(frag);
  updateStat(visible.length);
}

let renderTimer = null;
function scheduleRender() {
  clearTimeout(renderTimer);
  renderTimer = setTimeout(render, 50);
}

function updateStat(visibleCount) {
  $('statLine').textContent =
    visibleCount + '/' + state.entries.length + ' tasks · ' +
    state.enabled.size + '/' + state.vendors.length + ' vendors';
}

function renderSkeleton() {
  resultsEl.textContent = '';
  for (let p = 0; p < 4; p++) {
    const panel = document.createElement('div');
    panel.className = 'panel';
    for (let r = 0; r < 4; r++) {
      const bar = document.createElement('div');
      bar.className = 'skel-row';
      panel.appendChild(bar);
    }
    resultsEl.appendChild(panel);
  }
}

function showLoadError(err) {
  clearTimeout(renderTimer);
  resultsEl.textContent = '';
  const box = document.createElement('div');
  box.className = 'err-panel';
  box.appendChild(document.createTextNode('Could not load command data (' + err.message + '). '));
  box.appendChild(document.createElement('br'));
  box.appendChild(document.createTextNode('If you opened index.html directly, serve it over HTTP: '));
  const code = document.createElement('code');
  code.textContent = 'python3 -m http.server';
  box.appendChild(code);
  resultsEl.appendChild(box);
}

/* ── filter pills ────────────────────────────────────────────── */

function buildVendorPills() {
  const wrap = $('vendorPills');
  wrap.textContent = '';
  for (const v of state.vendors) {
    const pill = document.createElement('button');
    pill.className = 'pill vpill';
    pill.style.setProperty('--vc', VENDOR_COLORS[v.id] || 'var(--text-dim)');
    pill.textContent = VENDOR_SHORT[v.id] || v.name;
    pill.title = v.name;
    pill.setAttribute('aria-pressed', String(state.enabled.has(v.id)));
    pill.addEventListener('click', () => {
      if (state.enabled.has(v.id)) state.enabled.delete(v.id);
      else state.enabled.add(v.id);
      pill.setAttribute('aria-pressed', String(state.enabled.has(v.id)));
      store.set(LS.vendors, [...state.enabled]);
      render();
    });
    wrap.appendChild(pill);
  }
}

function buildCatPills() {
  const wrap = $('catPills');
  wrap.textContent = '';
  const cats = ['all', ...state.categories];
  for (const cat of cats) {
    const pill = document.createElement('button');
    pill.className = 'pill cpill' + (state.cat === cat ? ' on' : '');
    pill.textContent = cat === 'all' ? 'All' : cat;
    pill.addEventListener('click', () => {
      state.cat = cat;
      for (const p of wrap.children) p.classList.toggle('on', p === pill);
      render();
    });
    wrap.appendChild(pill);
  }
}

/* ── live variables bar ──────────────────────────────────────── */

function clampVlan(raw) {
  if (raw === '') return '';
  const n = parseInt(raw, 10);
  if (Number.isNaN(n)) return '';
  return String(Math.min(4094, Math.max(1, n)));
}

function wireVars() {
  const slider = $('vlanSlider');
  const savedRaw = store.get(LS.vars, {});
  const saved = savedRaw && typeof savedRaw === 'object' ? savedRaw : {};
  for (const key of VAR_KEYS) {
    if (typeof saved[key] === 'string') state.vars[key] = saved[key];
    $('var-' + key).value = state.vars[key];
  }
  if (state.vars.vlan) slider.value = state.vars.vlan;

  const persist = () => store.set(LS.vars, state.vars);

  for (const key of VAR_KEYS) {
    $('var-' + key).addEventListener('input', (e) => {
      let val = e.target.value.trim();
      if (key === 'vlan') {
        val = clampVlan(val);
        if (val && e.target.value !== val) e.target.value = val; // show the clamp
        if (val) slider.value = val;
      }
      state.vars[key] = val;
      persist();
      scheduleRender();
    });
  }

  slider.addEventListener('input', () => {
    state.vars.vlan = slider.value;
    $('var-vlan').value = slider.value;
    persist();
    scheduleRender();
  });

  $('varsClear').addEventListener('click', () => {
    for (const key of VAR_KEYS) {
      state.vars[key] = '';
      $('var-' + key).value = '';
    }
    slider.value = '1';
    persist();
    render();
  });

  /* mobile: collapse/expand the bottom dock */
  const bar = $('varsBar');
  const toggle = $('varsToggle');
  const applyOpen = (open) => {
    bar.classList.toggle('collapsed', !open);
    document.body.classList.toggle('vars-min', !open);
    toggle.setAttribute('aria-expanded', String(open));
  };
  applyOpen(store.get(LS.varsOpen, true));
  toggle.addEventListener('click', () => {
    if (window.matchMedia('(min-width: 720px)').matches) return; // desktop: label only
    const open = bar.classList.contains('collapsed');
    applyOpen(open);
    store.set(LS.varsOpen, open);
  });
}

/* ── theme / settings ────────────────────────────────────────── */

function applyTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  const btn = $('themeBtn');
  btn.textContent = theme === 'light' ? '☾' : '☀';
  btn.setAttribute('aria-label', theme === 'light' ? 'Switch to dark mode' : 'Switch to light mode');
}

function wireChrome() {
  let theme = store.get(LS.theme, 'dark') === 'light' ? 'light' : 'dark';
  applyTheme(theme);
  $('themeBtn').addEventListener('click', () => {
    theme = theme === 'dark' ? 'light' : 'dark';
    store.set(LS.theme, theme);
    applyTheme(theme);
  });

  const slider = $('fontSlider');
  const fontVal = $('fontVal');
  const applyFont = (px) => {
    document.documentElement.style.setProperty('--code-size', px + 'px');
    fontVal.textContent = px;
    slider.value = px;
  };
  const storedFont = parseInt(store.get(LS.font, 14), 10);
  applyFont(Number.isFinite(storedFont) ? Math.min(18, Math.max(12, storedFont)) : 14);
  slider.addEventListener('input', () => {
    applyFont(parseInt(slider.value, 10));
    store.set(LS.font, parseInt(slider.value, 10));
  });

  const pop = $('settingsPop');
  const btn = $('settingsBtn');
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    pop.hidden = !pop.hidden;
    btn.setAttribute('aria-expanded', String(!pop.hidden));
  });
  document.addEventListener('click', (e) => {
    if (!pop.hidden && !pop.contains(e.target) && e.target !== btn) {
      pop.hidden = true;
      btn.setAttribute('aria-expanded', 'false');
    }
  });

  const search = $('search');
  search.addEventListener('input', () => {
    state.q = search.value;
    scheduleRender();
  });
  document.addEventListener('keydown', (e) => {
    const tag = (e.target.tagName || '').toLowerCase();
    if (e.key === '/' && tag !== 'input' && tag !== 'textarea' && tag !== 'select') {
      e.preventDefault();
      search.focus();
      search.select();
    }
    if (e.key === 'Escape') {
      if (!pop.hidden) {
        e.preventDefault(); // keep native type=search clearing from also firing
        pop.hidden = true;
        btn.setAttribute('aria-expanded', 'false');
      } else if (document.activeElement === search && search.value) {
        e.preventDefault();
        search.value = '';
        state.q = '';
        render();
      }
    }
  });

  $('requestLink').href = issueUrl('');
}

/* ── boot ────────────────────────────────────────────────────── */

async function loadData() {
  renderSkeleton();
  try {
    const [vRes, cRes] = await Promise.all([
      fetch('data/vendors.json'),
      fetch('data/commands.json'),
    ]);
    if (!vRes.ok) throw new Error('vendors.json HTTP ' + vRes.status);
    if (!cRes.ok) throw new Error('commands.json HTTP ' + cRes.status);
    const [vJson, entries] = await Promise.all([vRes.json(), cRes.json()]);

    state.vendors = vJson.vendors;
    state.entries = entries;
    state.categories = [...new Set(entries.map((e) => e.category))];

    const stored = store.get(LS.vendors, null);
    const valid = Array.isArray(stored)
      ? stored.filter((id) => state.vendors.some((v) => v.id === id))
      : null;
    // A stored [] is a real choice (all off) — only fall back when nothing was saved.
    state.enabled = new Set(valid !== null ? valid : state.vendors.map((v) => v.id));

    state.loaded = true;
    buildVendorPills();
    buildCatPills();
    render();
  } catch (err) {
    showLoadError(err);
  }
}

wireChrome();
wireVars();
loadData();
