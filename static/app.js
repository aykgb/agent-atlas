'use strict';

const $ = id => document.getElementById(id);
const SERIES_COLORS = ['#4f75e3', '#2fa98f', '#9b72cf', '#e59a4c', '#e06d85', '#4da6d9', '#8ab34e'];
const OTHER_COLOR = 'var(--other-color)';
const COMP_PARTS = [
  ['new', '新增输入', '#4f75e3'],
  ['cc', '缓存写入', '#e59a4c'],
  ['cr', '缓存读取', '#2fa98f'],
  ['out', '模型输出', '#9b72cf'],
];
const METRICS = {
  total: {label: '总用量', unit: 'tokens', value: r => r.total},
  active: {label: '非缓存', unit: 'tokens', value: r => r.new + r.out},
  out: {label: '模型输出', unit: 'tokens', value: r => r.out},
  new: {label: '新增输入', unit: 'tokens', value: r => r.new},
  msgs: {label: '调用次数', unit: '次', value: r => r.msgs},
};
const OUTPUT_LABELS = {
  '思考': ['thinking', 'kind-thinking'],
  '工具调用': ['tool call', 'kind-tool'],
  '工具结果': ['tool result', 'kind-result'],
  '正文': ['text', 'kind-text'],
  '上下文': ['context', 'kind-context'],
  '过程': ['process', 'kind-context'],
  '输入': ['input', 'kind-input'],
  '输入(后文)': ['input', 'kind-input'],
};
const THEME_KEY = 'agent-atlas-theme';

const state = {
  tab: 'usage',
  page: 1,
  term: '',
  usage: null,
  excluded: [],
  request: 0,
  writable: true,
  remotes: [],
  sessionPage: 1,
  session: null,
  modelNames: new Map(),
  metric: 'total',
  trendMode: 'stack',
  group: 'agent',
  searchGroup: 'turn',
  rangePreset: 'recent',
  hiddenKeys: new Set(),
  showOther: false,
  compactTable: false,
  sort: {col: 'total', dir: 'desc'},
  hoveredKey: null,
  wordKind: 'all',
  wordView: 'bars',
  showAllExcluded: false,
  lastExcluded: null,
  sessionReverse: false,
  sessionCards: [],
  expandToken: 0,
};

const SORT_LABELS = {created: '按创建时间', active: '按最近活跃', tokens: '按 Token 消耗', turns: '按轮次数'};
const WORD_SORT_LABELS = {turns: '按覆盖轮次', count: '按出现次数', avg: '按单轮密度'};

const controllers = {load: null, session: null};
const tipMap = new WeakMap();
let knownVersion = null;
let pendingMeta = null;
let checking = false;
let suppressHashSync = false;

const modelOf = name => state.modelNames.get(String(name || '').toLowerCase()) || name;
const number = n => Number(n || 0).toLocaleString('en-US');
const syncTime = iso => new Date(iso).toLocaleString('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'});
const compact = n => n >= 1e9 ? (n / 1e9).toFixed(2) + 'B' : n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'K' : number(n);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const empty = text => '<div class="empty">' + esc(text) + '</div>';
const dateValue = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
const metricValue = row => METRICS[state.metric].value(row);
const formatMetric = val => state.metric === 'msgs' ? number(val) : compact(val);

function notice(text = '', error = false) {
  $('notice').textContent = text;
  $('notice').classList.toggle('error', error);
}

function setStatus(text = '') {
  $('status').textContent = text;
}

/* ---------- Theme ---------- */
function effectiveTheme() {
  const explicit = document.documentElement.dataset.theme;
  if (explicit === 'dark' || explicit === 'light') return explicit;
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function applyTheme(theme) {
  if (theme === 'dark' || theme === 'light') {
    document.documentElement.dataset.theme = theme;
  } else {
    delete document.documentElement.dataset.theme;
  }
  const current = effectiveTheme();
  $('themeToggle').textContent = current === 'dark' ? '☀' : '☾';
  $('themeToggle').title = current === 'dark' ? '切换为浅色主题' : '切换为深色主题';
}

function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch (_) {}
  applyTheme(saved);
  $('themeToggle').addEventListener('click', () => {
    const next = effectiveTheme() === 'dark' ? 'light' : 'dark';
    try { localStorage.setItem(THEME_KEY, next); } catch (_) {}
    applyTheme(next);
  });
}

/* ---------- Safe Tooltip ---------- */
const tip = $('tip');

function setTipContent(el, lines) {
  tipMap.set(el, lines);
  el.setAttribute('data-has-tip', '1');
}

function showTip(lines, x, y) {
  if (!lines || !lines.length) return hideTip();
  const nodes = lines.map((line, index) => {
    const div = document.createElement('div');
    if (typeof line === 'object' && line !== null) {
      div.className = line.cls || (index === 0 ? 'tip-head' : 'tip-line');
      div.textContent = String(line.text ?? '');
    } else {
      div.className = index === 0 ? 'tip-head' : 'tip-line';
      div.textContent = String(line ?? '');
    }
    return div;
  });
  tip.replaceChildren(...nodes);
  tip.hidden = false;
  const pad = 14;
  const left = Math.min(x + pad, window.innerWidth - tip.offsetWidth - 8);
  const top = y + pad + tip.offsetHeight > window.innerHeight ? y - tip.offsetHeight - pad : y + pad;
  tip.style.left = Math.max(8, left) + 'px';
  tip.style.top = Math.max(8, top) + 'px';
}

function hideTip() {
  tip.hidden = true;
}

function bindTips(container) {
  container.querySelectorAll('[data-has-tip]').forEach(el => {
    el.addEventListener('mouseenter', event => showTip(tipMap.get(el), event.clientX, event.clientY));
    el.addEventListener('mousemove', event => showTip(tipMap.get(el), event.clientX, event.clientY));
    el.addEventListener('mouseleave', hideTip);
    el.addEventListener('focus', () => {
      const r = el.getBoundingClientRect();
      showTip(tipMap.get(el), r.left + r.width / 2, r.top);
    });
    el.addEventListener('blur', hideTip);
  });
}

/* ---------- API & Highlight ---------- */
async function api(path, params = {}, options = {}) {
  const qs = Object.keys(params).length ? '?' + new URLSearchParams(params) : '';
  const response = await fetch(path + qs, options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '读取失败');
  return result;
}

function filters() {
  return Object.fromEntries(['agent', 'model', 'start', 'end'].map(k => [k, $(k).value]).filter(([, v]) => v));
}

function parseSearchTerms(raw) {
  const terms = [], str = String(raw || '');
  let i = 0;
  while (i < str.length) {
    if (/\s/.test(str[i])) { i++; continue; }
    if (str[i] === '"') {
      const end = str.indexOf('"', i + 1);
      const token = (end === -1 ? str.slice(i + 1) : str.slice(i + 1, end)).trim();
      i = end === -1 ? str.length : end + 1;
      if (token) terms.push(token);
      continue;
    }
    let j = i;
    while (j < str.length && !/\s/.test(str[j])) j++;
    terms.push(str.slice(i, j));
    i = j;
  }
  const seen = new Set();
  return terms.filter(t => {
    const k = t.toLowerCase();
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  });
}

function activeHighlightTerms() {
  if (state.tab !== 'search') return [];
  const words = parseSearchTerms($('q').value.trim());
  if (words.length) return words;
  return state.term ? [state.term] : [];
}

function hasTermMatch(text, terms) {
  if (!terms || !terms.length || !text) return false;
  const lower = String(text).toLowerCase();
  return terms.some(t => lower.includes(t.toLowerCase()));
}

function partMatchesScope(kind, scope) {
  if (scope === 'all') return true;
  if (scope === 'input') return false;
  if (scope === 'text') return kind === '正文';
  if (scope === 'thinking') return kind === '思考';
  if (scope === 'tool') return kind === '工具调用' || kind === '工具结果';
  return true;
}

function highlightTerms(text, wordsList) {
  const words = [...new Set((wordsList || []).filter(Boolean))].sort((a, b) => b.length - a.length);
  if (!words.length) return esc(text);
  const expression = new RegExp('(' + words.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')', 'gi');
  return String(text ?? '').split(expression).map((part, i) => i % 2 ? '<mark>' + esc(part) + '</mark>' : esc(part)).join('');
}

function highlight(text) {
  return highlightTerms(text, activeHighlightTerms());
}

function syncSegGroup(groupId, attr, current) {
  $(groupId).querySelectorAll('button').forEach(btn => {
    const active = btn.dataset[attr] === current;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', String(active));
  });
}

function syncSearchGroupButtons() {
  syncSegGroup('searchGroup', 'group', state.searchGroup);
}

function syncWordViewButtons() {
  syncSegGroup('wordView', 'wview', state.wordView);
}

function syncWordKindButtons() {
  syncSegGroup('wordKind', 'kind', state.wordKind);
}

function syncLenPreset() {
  const min = $('minLen').value.trim(), max = $('maxLen').value.trim();
  let preset = 'custom';
  if (!min && !max) preset = '';
  else if (!min && max === '30') preset = 'short';
  else if (min === '31' && max === '300') preset = 'medium';
  else if (min === '301' && !max) preset = 'long';
  $('lenPreset').value = preset;
}

function syncQClear() {
  $('qClear').hidden = !$('q').value;
}

function syncSessionQueryClear() {
  $('sessionQueryClear').hidden = !$('sessionQuery').value;
}

/* ---------- URL Hash Routing ---------- */
function buildHashParams() {
  const p = new URLSearchParams();
  if (state.tab !== 'usage') p.set('tab', state.tab);
  if ($('agent').value) p.set('agent', $('agent').value);
  if ($('model').value) p.set('model', $('model').value);
  if (state.tab === 'usage') {
    if (state.rangePreset !== 'recent') p.set('preset', state.rangePreset);
    if (state.rangePreset === 'recent' && Number($('n').value) !== 14) p.set('n', $('n').value);
    if ($('unit').value !== 'day') p.set('unit', $('unit').value);
    if (state.rangePreset === 'custom') {
      if ($('start').value) p.set('start', $('start').value);
      if ($('end').value) p.set('end', $('end').value);
    }
    if (state.metric !== 'total') p.set('metric', state.metric);
    if (state.trendMode !== 'stack') p.set('mode', state.trendMode);
    if (state.group !== 'agent') p.set('group', state.group);
  } else if (state.tab === 'search') {
    if ($('q').value.trim()) p.set('q', $('q').value.trim());
    if ($('excludeQuery').value.trim()) p.set('exclude', $('excludeQuery').value.trim());
    if ($('searchScope').value !== 'all') p.set('scope', $('searchScope').value);
    if ($('searchSort').value !== 'newest') p.set('sort', $('searchSort').value);
    if (state.searchGroup !== 'turn') p.set('sview', state.searchGroup);
    const srcVal = $('searchSource').value || $('searchSource').dataset.pending || '';
    if (srcVal) p.set('source', srcVal);
    if ($('sessionFilter').value.trim()) p.set('session', $('sessionFilter').value.trim());
    if (state.term) p.set('term', state.term);
    if ($('start').value) p.set('start', $('start').value);
    if ($('end').value) p.set('end', $('end').value);
    if ($('minLen').value) p.set('minlen', $('minLen').value);
    if ($('maxLen').value) p.set('maxlen', $('maxLen').value);
    if (state.page > 1) p.set('page', String(state.page));
  } else if (state.tab === 'sessions') {
    if (state.session) {
      p.set('skey', state.session.key);
      p.set('sagent', state.session.agent);
      p.set('sid', state.session.session);
    } else {
      if ($('sessionQuery').value.trim()) p.set('sq', $('sessionQuery').value.trim());
      const sSrc = $('sessionSource').value || $('sessionSource').dataset.pending || '';
      if (sSrc) p.set('ssource', sSrc);
      if ($('sessionSort').value !== 'created') p.set('ssort', $('sessionSort').value);
      if ($('start').value) p.set('start', $('start').value);
      if ($('end').value) p.set('end', $('end').value);
      if (state.sessionPage > 1) p.set('spage', String(state.sessionPage));
    }
  } else if (state.tab === 'words') {
    if ($('start').value) p.set('start', $('start').value);
    if ($('end').value) p.set('end', $('end').value);
    const wSrc = $('wordSource').value || $('wordSource').dataset.pending || '';
    if (wSrc) p.set('wsource', wSrc);
    if ($('wordSort').value !== 'count') p.set('wsort', $('wordSort').value);
    if ($('wordOrder').value !== 'desc') p.set('worder', $('wordOrder').value);
    if ($('wordLimit').value !== '40') p.set('wlimit', $('wordLimit').value);
    if (state.wordKind !== 'all') p.set('wkind', state.wordKind);
    if ($('wordMinTurns').value !== '1') p.set('wmint', $('wordMinTurns').value);
    if ($('wordQuery').value.trim()) p.set('wq', $('wordQuery').value.trim());
    if (state.wordView !== 'bars') p.set('wview', state.wordView);
  }
  return p.toString();
}

function syncHash(push = false) {
  if (suppressHashSync) return;
  const serialized = buildHashParams();
  const nextHash = serialized ? '#' + serialized : '';
  if (location.hash === nextHash) return;
  try {
    history[push ? 'pushState' : 'replaceState'](null, '', nextHash || location.pathname + location.search);
  } catch (_) {}
}

function readHash() {
  const raw = location.hash.startsWith('#') ? location.hash.slice(1) : location.hash;
  const p = new URLSearchParams(raw);
  const validTab = ['usage', 'search', 'sessions', 'words'].includes(p.get('tab')) ? p.get('tab') : 'usage';
  state.tab = validTab;
  $('agent').value = p.get('agent') || '';
  $('model').value = p.get('model') || '';
  state.rangePreset = ['recent', 'today', '7d', '30d', 'month', 'custom'].includes(p.get('preset')) ? p.get('preset') : 'recent';
  $('rangePreset').value = state.rangePreset;
  $('n').value = p.get('n') || '14';
  $('unit').value = ['day', 'week', 'month'].includes(p.get('unit')) ? p.get('unit') : 'day';
  $('start').value = p.get('start') || '';
  $('end').value = p.get('end') || '';
  state.metric = METRICS[p.get('metric')] ? p.get('metric') : 'total';
  state.trendMode = ['stack', 'percent', 'composition'].includes(p.get('mode')) ? p.get('mode') : 'stack';
  $('trendMode').value = state.trendMode;
  state.group = ['agent', 'model', 'pair'].includes(p.get('group')) ? p.get('group') : 'agent';
  $('group').value = state.group;
  $('q').value = p.get('q') || '';
  $('excludeQuery').value = p.get('exclude') || '';
  $('searchScope').value = ['all', 'input', 'text', 'thinking', 'tool'].includes(p.get('scope')) ? p.get('scope') : 'all';
  $('searchSort').value = ['newest', 'oldest', 'relevance'].includes(p.get('sort')) ? p.get('sort') : 'newest';
  state.searchGroup = p.get('sview') === 'session' ? 'session' : 'turn';
  const srcHash = p.get('source') || '';
  $('searchSource').dataset.pending = srcHash;
  $('searchSource').value = srcHash;
  $('sessionFilter').value = p.get('session') || '';
  state.term = p.get('term') || '';
  $('minLen').value = p.get('minlen') || '';
  $('maxLen').value = p.get('maxlen') || '';
  state.page = Math.max(1, parseInt(p.get('page') || '1', 10) || 1);
  $('sessionQuery').value = p.get('sq') || '';
  const sSrcHash = p.get('ssource') || '';
  $('sessionSource').dataset.pending = sSrcHash;
  $('sessionSource').value = sSrcHash;
  $('sessionSort').value = ['created', 'active', 'tokens', 'turns'].includes(p.get('ssort')) ? p.get('ssort') : 'created';
  state.sessionPage = Math.max(1, parseInt(p.get('spage') || '1', 10) || 1);
  const wSrcHash = p.get('wsource') || '';
  $('wordSource').dataset.pending = wSrcHash;
  $('wordSource').value = wSrcHash;
  $('wordSort').value = ['turns', 'count', 'avg'].includes(p.get('wsort')) ? p.get('wsort') : 'count';
  $('wordOrder').value = p.get('worder') === 'asc' ? 'asc' : 'desc';
  $('wordLimit').value = ['20', '40', '100', '200'].includes(p.get('wlimit')) ? p.get('wlimit') : '40';
  const wk = p.get('wkind');
  state.wordKind = ['all', 'zh', 'code'].includes(wk) ? wk : (p.get('zh') === '1' ? 'zh' : 'all');
  $('wordMinTurns').value = ['1', '2', '3', '5', '10'].includes(p.get('wmint')) ? p.get('wmint') : '1';
  $('wordQuery').value = p.get('wq') || '';
  state.wordView = p.get('wview') === 'cloud' ? 'cloud' : 'bars';
  syncWordKindButtons();
  syncWordViewButtons();
  syncMetricButtons();
  syncSearchGroupButtons();
  syncLenPreset();
  syncQClear();
  syncSessionQueryClear();
  updateWordLimitLabels();
  const pendingSession = p.get('sid') ? {key: p.get('skey') || 'local', agent: p.get('sagent') || '', session: p.get('sid')} : null;
  return {tab: validTab, pendingSession};
}

/* ---------- Remotes ---------- */
function renderRemotes() {
  const list = state.remotes || [];
  const off = list.filter(r => !r.enabled).length;
  const failed = list.filter(r => r.enabled && r.status && r.status.error).length;
  $('remotes').hidden = !state.writable;
  const parts = [];
  if (list.length) parts.push(list.length + ' 台');
  if (off) parts.push('停用 ' + off);
  const summaryEl = $('remoteSummary');
  summaryEl.textContent = parts.length ? '（' + parts.join(' · ') + '）' : '（未配置）';
  if (failed) {
    const errBadge = document.createElement('span');
    errBadge.className = 'remote-bad';
    errBadge.textContent = ' · ' + failed + ' 台同步失败';
    summaryEl.appendChild(errBadge);
  }
  $('remoteList').innerHTML = list.map((r, index) => {
    const tags = [r.usage && '用量', r.search && '会话', r.words && '词频'].filter(Boolean).map(esc).join(' · ');
    const status = !r.enabled
      ? '<span class="subtle">已停用</span>'
      : r.status && r.status.error
        ? '<span class="remote-bad" title="' + esc(r.status.error) + '">同步失败：' + esc(String(r.status.error).slice(0, 48)) + '</span>'
        : r.status && r.status.turns
          ? '<span class="remote-good">' + number(r.status.turns) + ' 轮已同步' + (r.status.synced_at ? ' · ' + syncTime(r.status.synced_at) : '') + '</span>'
          : '<span class="subtle">未同步</span>';
    const address = esc(r.host) + ':' + r.port;
    const label = '<input class="remote-label" data-label="' + index + '" value="' + esc(r.label || '') + '" placeholder="备注名" aria-label="备注名">';
    const sync = r.enabled ? '<button class="text-button" data-sync="' + index + '" type="button">同步</button>' : '';
    return '<div class="remote-row"><label class="remote-switch"><input type="checkbox" data-toggle="' + index + '"' + (r.enabled ? ' checked' : '') + '> 启用</label>' + label + '<a class="remote-host" href="http://' + address + '/" target="_blank" rel="noopener">' + address + '</a><span class="subtle">' + tags + '</span>' + status + '<span class="remote-actions">' + sync + '<button class="text-button" data-index="' + index + '" type="button">移除</button></span></div>';
  }).join('');
  $('remoteList').querySelectorAll('input[data-toggle]').forEach(box => box.addEventListener('change', () =>
    saveRemotes(state.remotes.map((r, index) => index === +box.dataset.toggle ? {...r, enabled: box.checked} : r))));
  $('remoteList').querySelectorAll('input[data-label]').forEach(input => {
    input.addEventListener('keydown', event => { if (event.key === 'Enter') input.blur(); });
    input.addEventListener('change', () =>
      saveRemotes(state.remotes.map((r, index) => index === +input.dataset.label ? {...r, label: input.value.trim()} : r)));
  });
  $('remoteList').querySelectorAll('button[data-sync]').forEach(button => button.addEventListener('click', () => {
    const remote = state.remotes[+button.dataset.sync];
    syncRemotes({host: remote.host, port: remote.port});
  }));
  $('remoteList').querySelectorAll('button[data-index]').forEach(button => button.addEventListener('click', () =>
    saveRemotes(state.remotes.filter((_, index) => index !== +button.dataset.index))));
}

async function loadRemotes() {
  try {
    state.remotes = (await api('/api/remotes')).remotes || [];
  } catch (_) {
    state.remotes = [];
  }
  renderRemotes();
}

async function syncRemotes(target) {
  try {
    setStatus('同步远端…');
    await api('/api/sync', {}, {method: 'POST', headers: {'X-Stats-Request': '1', 'Content-Type': 'application/json'}, body: JSON.stringify(target || {})});
    await metadata();
    state.page = 1;
    await load();
  } catch (error) {
    notice(error.message, true);
  } finally {
    setStatus();
  }
}

async function saveRemotes(list, target) {
  try {
    state.remotes = (await api('/api/remotes', {}, {method: 'POST', headers: {'X-Stats-Request': '1', 'Content-Type': 'application/json'}, body: JSON.stringify({remotes: list})})).remotes;
    renderRemotes();
    if (target) return syncRemotes(target);
    await metadata();
    state.page = 1;
    await load();
  } catch (error) {
    notice(error.message, true);
  }
}

/* ---------- Freshness & Metadata ---------- */
const dataVersion = d => [d.updated, d.turns, d.sessions].join('|');

function isUserReading() {
  if (!tip.hidden) return true;
  if (state.tab === 'sessions' && state.session) return true;
  if (state.tab === 'search' && (state.page > 1 || $('results').querySelector('details.turn[open]'))) return true;
  return false;
}

async function applyPendingUpdate() {
  const data = pendingMeta;
  pendingMeta = null;
  $('freshNotice').hidden = true;
  await metadata(data);
  await load();
}

async function checkFresh() {
  if (checking || document.hidden) return;
  checking = true;
  try {
    const data = await api('/api/meta');
    if (dataVersion(data) === knownVersion) return;
    if (isUserReading()) {
      pendingMeta = data;
      $('freshNotice').hidden = false;
      return;
    }
    pendingMeta = null;
    $('freshNotice').hidden = true;
    await metadata(data);
    await load();
  } catch (_) {
  } finally {
    checking = false;
  }
}

function syncSourceSelect(selectId, wrapId, items) {
  const sel = $(selectId);
  const pending = sel.dataset.pending ?? sel.value ?? '';
  delete sel.dataset.pending;
  sel.innerHTML = '<option value="">全部来源</option>' +
    items.map(s => '<option value="' + esc(s.key) + '">' + esc(s.label) + '</option>').join('');
  sel.value = pending;
  $(wrapId).hidden = items.length <= 1 && !sel.value;
}

async function metadata(preloaded) {
  const data = preloaded || await api('/api/meta');
  knownVersion = dataVersion(data);
  state.modelNames = new Map(data.models.map(model => [model.toLowerCase(), model]));
  for (const [id, values, label] of [['agent', data.agents, '全部 agent'], ['model', data.models, '全部模型']]) {
    const selected = $(id).value;
    $(id).innerHTML = '<option value="">' + label + '</option>' + values.map(v => '<option value="' + esc(v) + '">' + esc(v) + '</option>').join('');
    $(id).value = selected;
  }
  const searchSources = data.search_sources || [];
  const wordsSources = data.words_sources || searchSources;
  syncSourceSelect('searchSource', 'searchSourceWrap', searchSources);
  syncSourceSelect('sessionSource', 'sessionSourceWrap', searchSources);
  syncSourceSelect('wordSource', 'wordSourceWrap', wordsSources);
  state.writable = data.writable !== false;
  $('refresh').hidden = !state.writable;
  $('excludeInput').hidden = !state.writable;
  $('excludeAdd').hidden = !state.writable;
  document.querySelector('.local').textContent = state.writable ? 'LOCAL' : 'REMOTE';
  if (state.writable) await loadRemotes(); else renderRemotes();
  $('updated').textContent = '更新于 ' + syncTime(data.updated);
  $('coverage').textContent = number(data.sessions) + ' 个会话 · ' + number(data.turns) + ' 轮输入 · 数据仅保存在本机' + (state.writable ? '' : ' · 远端只读');
  $('warningList').textContent = data.warnings.length ? data.warnings.join('\n') : '各数据源读取完成，无解析异常。';
  $('warnings').querySelector('summary').textContent = data.warnings.length ? '数据口径与读取提示（' + data.warnings.length + '）' : '数据口径与读取提示';
}

/* ---------- Date Buckets & Drilldown ---------- */
function bucketRange(bucket, unit) {
  if (unit === 'week') {
    const [y, m, d] = bucket.split('-').map(Number);
    return [bucket, dateValue(new Date(y, m - 1, d + 6))];
  }
  if (unit === 'month') {
    const [y, m] = bucket.split('-').map(Number);
    return [bucket + '-01', dateValue(new Date(y, m, 0))];
  }
  return [bucket, bucket];
}

function drilldown(bucket, key, group) {
  const [start, end] = bucketRange(bucket, $('unit').value);
  $('start').value = start;
  $('end').value = end;
  if (group !== 'composition') {
    $('agent').value = '';
    $('model').value = '';
    if (key && key !== '其他' && group === 'agent') $('agent').value = key;
    if (key && key !== '其他' && group === 'model') $('model').value = key;
    if (key && key !== '其他' && group === 'pair') {
      const [agent, ...rest] = key.split(' / ');
      $('agent').value = agent;
      $('model').value = rest.join(' / ');
    }
  }
  $('q').value = '';
  syncQClear();
  state.term = '';
  state.page = 1;
  setTab('search', {push: true});
}

function usageDateParams() {
  const unit = $('unit').value;
  const preset = state.rangePreset;
  const now = new Date();
  const today = dateValue(now);
  if (preset === 'today') return {start: today, end: today, unit};
  if (preset === '7d') return {start: dateValue(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 6)), end: today, unit};
  if (preset === '30d') return {start: dateValue(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 29)), end: today, unit};
  if (preset === 'month') return {start: today.slice(0, 7) + '-01', end: today, unit};
  if (preset === 'custom') {
    const start = $('start').value || today.slice(0, 7) + '-01';
    const end = $('end').value || today;
    return {start, end, unit};
  }
  const n = Math.max(1, Math.min(366, Math.round(Number($('n').value) || 14)));
  $('n').value = String(n);
  return {n: String(n), unit};
}

function syncRangeControls() {
  const isUsage = state.tab === 'usage';
  const custom = state.rangePreset === 'custom';
  $('periodControls').hidden = !isUsage;
  $('recentCountWrap').hidden = !isUsage || state.rangePreset !== 'recent';
  $('dateControls').hidden = !(state.tab === 'search' || state.tab === 'sessions' || state.tab === 'words' || (isUsage && custom));
  $('allDates').hidden = isUsage;
}

/* ---------- Usage Rendering: KPI, Composition, Charts, Rankings, Table ---------- */
function syncMetricButtons() {
  $('metricSwitch').querySelectorAll('button').forEach(btn => {
    const active = btn.dataset.metric === state.metric;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', String(active));
  });
}

function renderCompositionBar(sums, total) {
  const bar = $('compBar');
  const legend = $('compLegend');
  if (!total) {
    bar.innerHTML = '';
    legend.innerHTML = '';
    $('compSummary').textContent = '暂无用量';
    return;
  }
  const cacheHit = sums.new + sums.cr > 0 ? (sums.cr / (sums.new + sums.cr) * 100).toFixed(1) : '0.0';
  const activeShare = ((sums.new + sums.out) / total * 100).toFixed(1);
  $('compSummary').textContent = '缓存读取率 ' + cacheHit + '% · 非缓存（输入+输出）占总用量 ' + activeShare + '%';
  bar.replaceChildren();
  legend.innerHTML = '';
  for (const [key, label, color] of COMP_PARTS) {
    const val = sums[key];
    const share = val / total * 100;
    if (val > 0) {
      const seg = document.createElement('div');
      seg.className = 'comp-seg';
      seg.style.width = Math.max(share, 0.35) + '%';
      seg.style.background = color;
      setTipContent(seg, [
        label + ' · ' + share.toFixed(1) + '%',
        number(val) + ' tokens（' + compact(val) + '）',
      ]);
      bar.appendChild(seg);
    }
    const item = document.createElement('span');
    item.innerHTML = '<i class="swatch" style="background:' + color + '"></i>' + esc(label) + ' ' + compact(val) + ' (' + share.toFixed(1) + '%)';
    legend.appendChild(item);
  }
  bindTips(bar);
}

function renderRanking(id, entries, total, filterField) {
  const container = $(id);
  if (!entries.length) {
    container.innerHTML = empty('这个时间范围还没有用量');
    return;
  }
  const maximum = Math.max(...entries.map(([, value]) => value), 1);
  const currentFilter = $(filterField).value;
  const unitLabel = METRICS[state.metric].unit;
  container.replaceChildren();
  entries.forEach(([name, value], index) => {
    const share = total > 0 ? (value / total * 100).toFixed(1) : '0.0';
    const active = currentFilter && currentFilter.toLowerCase() === name.toLowerCase();
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'rank-row' + (active ? ' is-active' : '');
    row.title = name + ' · ' + number(value) + ' ' + unitLabel + ' · 占比 ' + share + '%（点击筛选）';
    row.innerHTML = '<span class="rank-index">' + (index + 1) + '</span>' +
      '<span class="rank-name">' + esc(name) + '</span>' +
      '<div class="rank-track"><div class="rank-fill" style="width:' + (value / maximum * 100).toFixed(1) + '%"></div></div>' +
      '<span class="rank-value">' + esc(formatMetric(value)) + ' · ' + share + '%</span>';
    row.addEventListener('click', () => {
      $(filterField).value = active ? '' : name;
      state.page = 1;
      syncHash(true);
      load();
    });
    container.appendChild(row);
  });
}

function renderUsageTable(rowsMap) {
  const rows = [...rowsMap.values()];
  const totalRow = rows.reduce((acc, r) => {
    for (const k of ['msgs', 'new', 'cc', 'cr', 'out', 'total']) acc[k] += r[k];
    return acc;
  }, {msgs: 0, new: 0, cc: 0, cr: 0, out: 0, total: 0});
  const valOf = (r, col) => col === 'name' ? (r.model + '\0' + r.agent).toLowerCase()
    : col === 'share' ? r.total
    : r[col];
  const dir = state.sort.dir === 'asc' ? 1 : -1;
  rows.sort((a, b) => {
    const av = valOf(a, state.sort.col), bv = valOf(b, state.sort.col);
    if (typeof av === 'string') return av.localeCompare(bv) * dir;
    return (av - bv) * dir || (b.total - a.total);
  });

  const cols = [
    ['name', 'Agent / 模型'],
    ['msgs', '调用次数'],
    ['new', '新增输入'],
    ['cc', '缓存写入'],
    ['cr', '缓存读取'],
    ['out', '输出'],
    ['total', '总用量'],
    ['share', '总占比'],
  ];
  $('usageHead').innerHTML = '<tr>' + cols.map(([key, label]) => {
    const sorted = state.sort.col === key;
    const arrow = sorted ? (state.sort.dir === 'asc' ? ' ▲' : ' ▼') : '';
    return '<th data-sort="' + key + '" class="' + (sorted ? 'is-sorted' : '') + '">' + esc(label + arrow) + '</th>';
  }).join('') + '</tr>';

  $('usageHead').querySelectorAll('th[data-sort]').forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.sort;
      if (state.sort.col === col) state.sort.dir = state.sort.dir === 'desc' ? 'asc' : 'desc';
      else state.sort = {col, dir: col === 'name' ? 'asc' : 'desc'};
      renderUsageTable(rowsMap);
    });
  });

  const fmt = v => state.compactTable ? compact(v) : number(v);
  const curAgent = $('agent').value;
  const curModel = $('model').value.toLowerCase();
  if (!rows.length) {
    $('usageRows').innerHTML = '<tr><td colspan="8">当前范围没有用量记录，请调整时间或筛选。</td></tr>';
    $('usageFoot').innerHTML = '';
    return;
  }
  $('usageRows').innerHTML = rows.map(r => {
    const active = curAgent === r.agent && curModel === r.model.toLowerCase();
    const share = totalRow.total > 0 ? (r.total / totalRow.total * 100).toFixed(1) + '%' : '0.0%';
    return '<tr class="usage-row' + (active ? ' is-active' : '') + '" data-agent="' + esc(r.agent) + '" data-model="' + esc(r.model) + '" title="点击筛选该 Agent 与模型">' +
      '<td><b>' + esc(r.model) + '</b><small>' + esc(r.agent) + '</small></td>' +
      ['msgs', 'new', 'cc', 'cr', 'out', 'total'].map(k => '<td title="' + number(r[k]) + '">' + fmt(r[k]) + '</td>').join('') +
      '<td>' + share + '</td></tr>';
  }).join('');

  $('usageFoot').innerHTML = '<tr><td><b>合计</b><small>' + rows.length + ' 项</small></td>' +
    ['msgs', 'new', 'cc', 'cr', 'out', 'total'].map(k => '<td title="' + number(totalRow[k]) + '">' + fmt(totalRow[k]) + '</td>').join('') +
    '<td>100.0%</td></tr>';

  $('usageRows').querySelectorAll('tr.usage-row').forEach(tr => {
    tr.addEventListener('click', () => {
      const same = $('agent').value === tr.dataset.agent && $('model').value.toLowerCase() === tr.dataset.model.toLowerCase();
      if (same) {
        $('agent').value = '';
        $('model').value = '';
      } else {
        $('agent').value = tr.dataset.agent;
        $('model').value = tr.dataset.model;
      }
      state.page = 1;
      syncHash(true);
      load();
    });
  });
}

function renderUsage(data) {
  state.usage = data;
  const sums = {new: 0, cc: 0, cr: 0, out: 0, msgs: 0, total: 0};
  for (const r of data.rows) {
    for (const k of ['new', 'cc', 'cr', 'out', 'msgs', 'total']) sums[k] += r[k];
  }
  const bucketCount = Math.max(data.buckets.length, 1);
  const avgPerBucket = sums.total / bucketCount;
  $('total').textContent = compact(sums.total);
  $('total').title = number(sums.total) + ' tokens';
  $('range').textContent = data.start + ' — ' + data.end + ' · 均 ' + compact(avgPerBucket) + '/周期';

  $('input').textContent = compact(sums.new);
  $('input').title = number(sums.new) + ' tokens';
  const avgNewCall = sums.msgs ? Math.round(sums.new / sums.msgs) : 0;
  $('inputSub').textContent = (sums.total ? (sums.new / sums.total * 100).toFixed(1) : '0.0') + '% 占比 · 均 ' + number(avgNewCall) + '/次';

  const cacheTotal = sums.cc + sums.cr;
  $('cache').textContent = compact(cacheTotal);
  $('cache').title = number(cacheTotal) + ' tokens';
  const hitRate = sums.new + sums.cr > 0 ? (sums.cr / (sums.new + sums.cr) * 100).toFixed(1) : '0.0';
  $('cacheDetail').textContent = '命中率 ' + hitRate + '% · 读 ' + compact(sums.cr) + ' / 写 ' + compact(sums.cc);

  $('output').textContent = compact(sums.out);
  $('output').title = number(sums.out) + ' tokens';
  const activeTokens = sums.new + sums.out;
  $('outputSub').textContent = '含思考 · 占非缓存 ' + (activeTokens ? (sums.out / activeTokens * 100).toFixed(1) : '0.0') + '%';

  renderCompositionBar(sums, sums.total);

  const rowsMap = new Map();
  for (const row of data.rows) {
    const mName = modelOf(row.model);
    const key = row.agent + '\0' + mName;
    if (!rowsMap.has(key)) rowsMap.set(key, {agent: row.agent, model: mName, new: 0, cc: 0, cr: 0, out: 0, msgs: 0, total: 0});
    const entry = rowsMap.get(key);
    for (const field of ['new', 'cc', 'cr', 'out', 'msgs', 'total']) entry[field] += row[field];
  }
  state.usageRowsMap = rowsMap;

  const metricTotal = [...rowsMap.values()].reduce((s, r) => s + metricValue(r), 0);
  const rank = field => {
    const totals = new Map();
    for (const r of rowsMap.values()) totals.set(r[field], (totals.get(r[field]) || 0) + metricValue(r));
    return [...totals.entries()].filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
  };
  renderRanking('agentRank', rank('agent'), metricTotal, 'agent');
  renderRanking('modelRank', rank('model'), metricTotal, 'model');
  renderUsageTable(rowsMap);
  renderCharts();
}

function highlightSeries(activeKey) {
  state.hoveredKey = activeKey || null;
  document.querySelectorAll('#bars .bar-seg, #pie circle, #pieLegend .pie-row').forEach(el => {
    const k = el.dataset.series;
    el.classList.toggle('is-highlighted', Boolean(activeKey && k === activeKey));
    el.classList.toggle('is-dimmed', Boolean(activeKey && k !== activeKey && !el.classList.contains('pie-row')));
  });
  const center = document.querySelector('#pie .donut-center');
  if (center && state.pieSummary) {
    if (activeKey && state.pieSummary.byKey.has(activeKey)) {
      const info = state.pieSummary.byKey.get(activeKey);
      center.querySelector('b').textContent = info.share.toFixed(1) + '%';
      center.querySelector('small').textContent = activeKey + ' · ' + formatMetric(info.value);
    } else {
      center.querySelector('b').textContent = formatMetric(state.pieSummary.total);
      center.querySelector('small').textContent = METRICS[state.metric].label + ' · ' + state.pieSummary.activeCount + ' 项';
    }
  }
}

function bucketDisplayRange(bucket, unit) {
  if (unit === 'week') {
    const [s, e] = bucketRange(bucket, 'week');
    return s + ' ~ ' + e.slice(5);
  }
  return bucket;
}

function renderCharts() {
  hideTip();
  const data = state.usage;
  if (!data) return;
  const grouping = state.group;
  const mode = state.trendMode;
  const unit = $('unit').value;
  const isComposition = mode === 'composition';
  $('group').disabled = isComposition;
  $('group').title = isComposition ? 'Token 构成模式按 4 类 token 展示，不按 Agent/模型分组' : '';

  let allKeys, seriesColorMap, bucketData;
  let otherEntries = [];

  if (isComposition) {
    allKeys = COMP_PARTS.map(([, label]) => label);
    seriesColorMap = new Map(COMP_PARTS.map(([, label, color]) => [label, color]));
    const totals = new Map(allKeys.map(k => [k, 0]));
    const groups = data.buckets.map(b => ({bucket: b, values: Object.fromEntries(allKeys.map(k => [k, 0]))}));
    const lookup = new Map(groups.map(g => [g.bucket, g]));
    for (const row of data.rows) {
      const target = lookup.get(row.bucket);
      if (!target) continue;
      for (const [field, label] of COMP_PARTS) {
        target.values[label] += row[field];
        totals.set(label, totals.get(label) + row[field]);
      }
    }
    bucketData = {keys: allKeys.filter(k => totals.get(k) > 0), totals, groups, activeCount: 4};
  } else {
    const keyOf = row => grouping === 'pair'
      ? row.agent + ' / ' + modelOf(row.model)
      : grouping === 'model'
        ? modelOf(row.model)
        : row.agent;
    const totals = new Map();
    for (const row of data.rows) {
      const k = keyOf(row);
      const val = metricValue(row);
      if (val > 0) totals.set(k, (totals.get(k) || 0) + val);
    }
    const ranked = [...totals.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
    const top = ranked.slice(0, 7).map(([k]) => k);
    otherEntries = ranked.slice(7);
    const keys = otherEntries.length ? [...top, '其他'] : top;
    seriesColorMap = new Map(keys.map((k, i) => [k, k === '其他' ? OTHER_COLOR : SERIES_COLORS[i]]));
    const mergedTotals = new Map();
    for (const k of top) mergedTotals.set(k, totals.get(k));
    if (otherEntries.length) mergedTotals.set('其他', otherEntries.reduce((s, [, v]) => s + v, 0));

    const groups = data.buckets.map(b => ({bucket: b, values: Object.fromEntries(keys.map(k => [k, 0]))}));
    const lookup = new Map(groups.map(g => [g.bucket, g]));
    for (const row of data.rows) {
      const b = lookup.get(row.bucket);
      const val = metricValue(row);
      if (!b || !val) continue;
      const rawKey = keyOf(row);
      const k = top.includes(rawKey) ? rawKey : '其他';
      b.values[k] += val;
    }
    bucketData = {keys, totals: mergedTotals, groups, activeCount: ranked.length};
  }

  const allTotal = bucketData.keys.reduce((s, k) => s + (bucketData.totals.get(k) || 0), 0);
  if (!allTotal) {
    $('barsY').innerHTML = '';
    $('bars').innerHTML = empty('这个时间范围还没有用量');
    $('pie').innerHTML = empty('暂无占比数据');
    $('barLegend').innerHTML = '';
    $('pieLegend').innerHTML = '';
    $('otherDetail').hidden = true;
    state.pieSummary = null;
    return;
  }

  const visibleKeys = bucketData.keys.filter(k => !state.hiddenKeys.has(k));
  const activeKeys = visibleKeys.length ? visibleKeys : bucketData.keys;
  if (!visibleKeys.length) state.hiddenKeys.clear();

  const groups = bucketData.groups;
  const isPercent = mode === 'percent';
  const bucketTotals = groups.map(g => activeKeys.reduce((s, k) => s + (g.values[k] || 0), 0));
  const rawMax = Math.max(...bucketTotals, 1);
  const max = isPercent ? 100 : rawMax * 1.1;
  const avg = bucketTotals.reduce((a, b) => a + b, 0) / Math.max(bucketTotals.length, 1);

  const height = 260, topPad = 18, bottom = 228, plot = bottom - topPad;
  const avail = Math.max(220, $('bars').clientWidth || 640);
  const minCol = groups.length > 45 ? 22 : (avail < 360 && groups.length <= 16 ? 18 : 34);
  const width = Math.max(avail, groups.length * minCol + 16);
  const stride = (width - 16) / groups.length;
  const barW = Math.max(6, Math.min(44, stride * 0.62));
  const labelStep = Math.max(1, Math.ceil(52 / stride));

  // Sticky Y axis
  let ySvg = '<svg width="58" height="' + height + '" viewBox="0 0 58 ' + height + '">';
  for (let i = 0; i <= 4; i++) {
    const y = bottom - i * plot / 4;
    const tick = isPercent ? (i * 25) + '%' : (isComposition ? compact(max * i / 4) : formatMetric(max * i / 4));
    ySvg += '<text x="50" y="' + (y + 3.5) + '" text-anchor="end">' + esc(tick) + '</text>';
  }
  ySvg += '</svg>';
  $('barsY').innerHTML = ySvg;

  // Scrollable Plot
  let svg = '<svg width="' + width + '" height="' + height + '" viewBox="0 0 ' + width + ' ' + height + '" style="width:' + width + 'px;height:' + height + 'px" role="img" aria-label="按周期和分组显示用量的柱状图">';
  for (let i = 0; i <= 4; i++) {
    const y = bottom - i * plot / 4;
    svg += '<line x1="0" x2="' + width + '" y1="' + y + '" y2="' + y + '" stroke="var(--grid-line)"/>';
  }

  let prevYear = '';
  const colTips = [];
  const segTips = [];

  groups.forEach((g, i) => {
    const xCol = 8 + stride * i;
    const xBar = xCol + (stride - barW) / 2;
    const bTotal = bucketTotals[i];
    const displayRange = bucketDisplayRange(g.bucket, unit);

    svg += '<g class="bar-col" data-col="' + i + '">';
    svg += '<rect class="bar-col-bg" x="' + xCol + '" y="' + topPad + '" width="' + stride + '" height="' + plot + '" rx="4"/>';
    svg += '<rect class="bar-col-hit" data-col-hit="' + i + '" x="' + xCol + '" y="' + topPad + '" width="' + stride + '" height="' + plot + '" fill="transparent"/>';

    const contributors = activeKeys
      .map(k => [k, g.values[k] || 0])
      .filter(([, v]) => v > 0)
      .sort((a, b) => b[1] - a[1]);

    const fmtVal = v => isComposition ? compact(v) + ' tokens' : (formatMetric(v) + ' ' + METRICS[state.metric].unit);
    const colLines = [
      {text: displayRange + ' · 合计 ' + (bTotal ? fmtVal(bTotal) : '0'), cls: 'tip-head'},
      ...contributors.slice(0, 4).map(([k, v]) => ({
        text: k + ': ' + fmtVal(v) + ' (' + (v / Math.max(bTotal, 1) * 100).toFixed(1) + '%)',
        cls: 'tip-line',
      })),
    ];
    if (contributors.length > 4) colLines.push({text: '…另有 ' + (contributors.length - 4) + ' 项', cls: 'tip-muted'});
    colTips.push(colLines);

    let y = bottom;
    activeKeys.forEach(key => {
      const value = g.values[key] || 0;
      if (!value || !bTotal) return;
      const ratio = isPercent ? (value / bTotal) : (value / max);
      const h = ratio * plot;
      y -= h;
      const share = (value / bTotal * 100).toFixed(1);
      const segIdx = segTips.length;
      segTips.push([
        {text: displayRange + ' · ' + key, cls: 'tip-head'},
        {text: (isComposition ? number(value) + ' tokens' : number(value) + ' ' + METRICS[state.metric].unit) + ' · 占该周期 ' + share + '%', cls: 'tip-line'},
        {text: '周期合计 ' + fmtVal(bTotal) + ' · 双击或 Enter 查会话', cls: 'tip-muted'},
      ]);
      const aria = displayRange + ' ' + key + ' ' + number(value);
      svg += '<rect class="bar-seg" tabindex="0" role="button" aria-label="' + esc(aria) + '" data-seg="' + segIdx + '" data-series="' + esc(key) + '" data-bucket="' + esc(g.bucket) + '" data-key="' + esc(isComposition ? '' : key) + '" data-group="' + esc(isComposition ? 'composition' : grouping) + '" x="' + xBar + '" y="' + y + '" width="' + barW + '" height="' + Math.max(h, 1) + '" fill="' + seriesColorMap.get(key) + '"></rect>';
    });

    if (i % labelStep === 0 || i === groups.length - 1) {
      const yr = g.bucket.slice(0, 4);
      let labelText;
      if (g.bucket.length === 7) {
        labelText = g.bucket.slice(2);
      } else if (!prevYear || yr !== prevYear) {
        labelText = g.bucket.slice(2);
      } else {
        labelText = g.bucket.slice(5);
      }
      prevYear = yr;
      svg += '<text x="' + (xCol + stride / 2) + '" y="248" text-anchor="middle">' + esc(labelText) + '</text>';
    }
    svg += '</g>';
  });

  if (!isPercent && avg > 0) {
    const avgY = bottom - (avg / max) * plot;
    const avgText = '均值 ' + (isComposition ? compact(avg) : formatMetric(avg));
    const pillW = Math.round(avgText.length * 6.6 + 18);
    const pillY = Math.max(4, avgY - 18);
    svg += '<line class="bar-avg-line" x1="0" x2="' + width + '" y1="' + avgY + '" y2="' + avgY + '"/>';
    svg += '<rect class="bar-avg-bg" x="' + (width - pillW - 4) + '" y="' + pillY + '" width="' + pillW + '" height="16" rx="4"/>';
    svg += '<text class="bar-avg-label" x="' + (width - 10) + '" y="' + (pillY + 11.5) + '" text-anchor="end">' + esc(avgText) + '</text>';
  }

  svg += '</svg>';
  $('bars').innerHTML = svg;

  $('bars').querySelectorAll('[data-col-hit]').forEach(el => {
    setTipContent(el, colTips[+el.dataset.colHit]);
    el.addEventListener('mouseenter', () => el.closest('.bar-col').classList.add('is-hovered'));
    el.addEventListener('mouseleave', () => el.closest('.bar-col').classList.remove('is-hovered'));
  });
  $('bars').querySelectorAll('.bar-seg').forEach(el => {
    setTipContent(el, segTips[+el.dataset.seg]);
    el.addEventListener('mouseenter', () => {
      el.closest('.bar-col').classList.add('is-hovered');
      highlightSeries(el.dataset.series);
    });
    el.addEventListener('mouseleave', () => {
      el.closest('.bar-col').classList.remove('is-hovered');
      highlightSeries(null);
    });
    el.addEventListener('focus', () => highlightSeries(el.dataset.series));
    el.addEventListener('blur', () => highlightSeries(null));
  });

  // Interactive Legend
  $('barLegend').replaceChildren();
  bucketData.keys.forEach(k => {
    const hidden = state.hiddenKeys.has(k);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'legend-item' + (hidden ? ' is-hidden' : '');
    btn.title = hidden ? '点击恢复显示「' + k + '」' : '点击隐藏「' + k + '」';
    btn.innerHTML = '<i class="swatch" style="background:' + seriesColorMap.get(k) + '"></i>' + esc(k);
    btn.addEventListener('mouseenter', () => highlightSeries(k));
    btn.addEventListener('mouseleave', () => highlightSeries(null));
    btn.addEventListener('click', () => {
      if (state.hiddenKeys.has(k)) state.hiddenKeys.delete(k);
      else if (bucketData.keys.filter(x => !state.hiddenKeys.has(x)).length > 1) state.hiddenKeys.add(k);
      renderCharts();
    });
    $('barLegend').appendChild(btn);
  });

  if (!isComposition && otherEntries.length) {
    const detailBtn = document.createElement('button');
    detailBtn.type = 'button';
    detailBtn.className = 'legend-item' + (state.showOther ? ' is-active' : '');
    detailBtn.textContent = (state.showOther ? '收起「其他」(' : '展开「其他」(') + otherEntries.length + ')';
    detailBtn.addEventListener('click', () => {
      state.showOther = !state.showOther;
      renderCharts();
    });
    $('barLegend').appendChild(detailBtn);
  }

  if (!isComposition && otherEntries.length && state.showOther) {
    const shown = otherEntries.slice(0, 24);
    $('otherDetail').hidden = false;
    $('otherDetail').innerHTML = shown.map(([name, val]) =>
      '<span>' + esc(name) + ' <strong>' + esc(formatMetric(val)) + '</strong> (' + (val / allTotal * 100).toFixed(1) + '%)</span>'
    ).join('') + (otherEntries.length > shown.length ? '<span>…共 ' + otherEntries.length + ' 项</span>' : '');
  } else {
    $('otherDetail').hidden = true;
  }

  // Donut Chart (mirrors activeKeys & visible totals)
  const visibleTotal = activeKeys.reduce((s, k) => s + (bucketData.totals.get(k) || 0), 0);
  const byKey = new Map();
  let start = 0;
  const sliceTips = [];
  const slices = activeKeys.map(key => {
    const value = bucketData.totals.get(key) || 0;
    const share = visibleTotal > 0 ? value / visibleTotal * 100 : 0;
    byKey.set(key, {value, share});
    if (!value) return '';
    const idx = sliceTips.length;
    const unitText = isComposition ? 'tokens' : METRICS[state.metric].unit;
    sliceTips.push([
      {text: key, cls: 'tip-head'},
      {text: number(value) + ' ' + unitText + ' · 占当前可见 ' + share.toFixed(1) + '%', cls: 'tip-line'},
    ]);
    const label = key + ' · ' + number(value) + ' ' + unitText + ' · ' + share.toFixed(1) + '%';
    const slice = '<circle tabindex="0" role="img" aria-label="' + esc(label) + '" data-slice="' + idx + '" data-series="' + esc(key) + '" cx="21" cy="21" r="17.375" fill="none" stroke="' + seriesColorMap.get(key) + '" stroke-width="7.25" pathLength="100" stroke-dasharray="0 ' + start + ' ' + share + ' 100" transform="rotate(-90 21 21)"></circle>';
    start += share;
    return slice;
  }).join('');

  state.pieSummary = {
    total: visibleTotal,
    activeCount: isComposition ? activeKeys.length : bucketData.activeCount,
    byKey,
  };
  const centerLabel = isComposition ? 'Token 合计' : METRICS[state.metric].label;
  const centerVal = isComposition ? compact(visibleTotal) : formatMetric(visibleTotal);
  $('pie').innerHTML = '<div class="pie-wrap"><svg class="donut" viewBox="0 0 42 42" role="img" aria-label="用量占比环形图">' + slices + '</svg><div class="donut-center"><b>' + esc(centerVal) + '</b><small>' + esc(centerLabel + ' · ' + state.pieSummary.activeCount + ' 项') + '</small></div></div>';

  $('pie').querySelectorAll('circle[data-slice]').forEach(el => {
    setTipContent(el, sliceTips[+el.dataset.slice]);
    el.addEventListener('mouseenter', () => highlightSeries(el.dataset.series));
    el.addEventListener('mouseleave', () => highlightSeries(null));
    el.addEventListener('focus', () => highlightSeries(el.dataset.series));
    el.addEventListener('blur', () => highlightSeries(null));
  });

  $('pieLegend').replaceChildren();
  bucketData.keys.forEach(k => {
    const hidden = state.hiddenKeys.has(k);
    const v = bucketData.totals.get(k) || 0;
    const share = !hidden && visibleTotal > 0 ? (v / visibleTotal * 100).toFixed(1) + '%' : '—';
    const row = document.createElement('div');
    row.className = 'pie-row' + (hidden ? ' is-hidden' : '');
    row.dataset.series = k;
    row.title = number(v) + '（点击切换显隐）';
    row.innerHTML = '<i class="swatch" style="background:' + seriesColorMap.get(k) + '"></i><span class="pie-name">' + esc(k) + '</span><span>' + esc(isComposition ? compact(v) : formatMetric(v)) + '</span><strong>' + share + '</strong>';
    row.addEventListener('mouseenter', () => { if (!hidden) highlightSeries(k); });
    row.addEventListener('mouseleave', () => highlightSeries(null));
    row.addEventListener('click', () => {
      if (state.hiddenKeys.has(k)) state.hiddenKeys.delete(k);
      else if (bucketData.keys.filter(x => !state.hiddenKeys.has(x)).length > 1) state.hiddenKeys.add(k);
      renderCharts();
    });
    $('pieLegend').appendChild(row);
  });

  bindTips($('bars'));
  bindTips($('pie'));
}

/* ---------- Turn Detail, Copy, Sessions, Search ---------- */
function copyText(text, button) {
  const done = () => {
    const prev = button.textContent;
    button.textContent = '已复制';
    setTimeout(() => { button.textContent = prev; }, 1200);
  };
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => {});
  } else {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); done(); } catch (_) {}
    ta.remove();
  }
}

function kindTagHtml(kind) {
  const [label, cls] = OUTPUT_LABELS[kind] || [kind, 'kind-text'];
  return '<span class="kind-tag ' + cls + '">' + esc(label) + '</span>';
}

function shortSessionId(sid) {
  const s = String(sid || '');
  return s.length > 22 ? s.slice(0, 14) + '…' + s.slice(-6) : s;
}

async function loadTurn(detail, id) {
  if (!detail.open || detail.dataset.loaded) return;
  const body = detail.querySelector('.turn-body');
  body.textContent = '正在加载完整会话…';
  detail.dataset.loaded = 'loading';
  try {
    const turn = await api('/api/turn', {id});
    const isSearch = state.tab === 'search';
    const scope = isSearch ? $('searchScope').value : 'all';
    const terms = activeHighlightTerms();

    let anyPartHit = false;
    let openedParts = 0;
    const parts = turn.output.map((part, index) => {
      const scoped = isSearch && partMatchesScope(part.kind, scope);
      const hasHit = scoped && hasTermMatch(part.text, terms);
      if (hasHit) anyPartHit = true;
      const autoOpen = hasHit && openedParts < 8;
      if (autoOpen) openedParts++;
      const [label, cls] = OUTPUT_LABELS[part.kind] || [part.kind, 'kind-text'];
      const renderedText = scoped ? highlight(part.text) : esc(part.text);
      return '<details class="part"' + (autoOpen ? ' open' : '') + '><summary><span>第 ' + (index + 1) + ' 段 · ' + number(part.text.length) + ' 字符</span>' +
        '<span class="turn-summary-actions">' + (hasHit ? '<span class="hit-pill">命中</span>' : '') +
        '<span class="kind-tag ' + cls + '">' + esc(label) + '</span>' +
        '<button type="button" class="copy-btn" data-copy-part="' + index + '">复制</button></span></summary>' +
        '<pre>' + renderedText + '</pre></details>';
    }).join('');

    const highlightInput = isSearch && (scope === 'all' || scope === 'input');
    const inputHit = highlightInput && hasTermMatch(turn.input, terms);
    const inputOpen = !isSearch || !anyPartHit || inputHit || scope === 'all' || scope === 'input';

    body.innerHTML =
      '<details' + (inputOpen ? ' open' : '') + '><summary><span>用户输入</span>' +
      '<span class="turn-summary-actions">' + (inputHit ? '<span class="hit-pill">命中</span>' : '') +
      '<button type="button" class="copy-btn" data-copy-input="1">复制</button></span></summary><pre>' +
      (highlightInput ? highlight(turn.input) : esc(turn.input)) + '</pre></details>' +
      (turn.output.length
        ? '<details class="response"' + (anyPartHit ? ' open' : '') + '><summary><span>Agent 响应 · ' + turn.output.length + ' 段</span><button type="button" class="copy-btn" data-expand-parts="1">展开全部段</button></summary><div class="response-body">' + parts + '</div></details>'
        : '<p>该轮尚无可见输出。</p>') +
      '<div class="source">会话 ' + esc(turn.session) + '<br>来源 ' + esc(turn.source) + '</div>';

    const marks = [...body.querySelectorAll('mark')];
    if (marks.length) {
      const nav = document.createElement('div');
      nav.className = 'turn-hit-nav';
      nav.innerHTML = '<span class="hit-pos">命中 ' + marks.length + ' 处 · 第 1 / ' + marks.length + ' 处</span>' +
        '<span class="turn-hit-actions"><button type="button" class="copy-btn" data-hit-prev="1">↑ 上一处</button>' +
        '<button type="button" class="copy-btn" data-hit-next="1">↓ 下一处</button></span>';
      body.prepend(nav);
      let markIdx = 0;
      marks[0].classList.add('is-current');
      const focusMark = nextIdx => {
        marks[markIdx].classList.remove('is-current');
        markIdx = (nextIdx + marks.length) % marks.length;
        const target = marks[markIdx];
        target.classList.add('is-current');
        let el = target.parentElement;
        while (el && el !== body) {
          if (el.tagName === 'DETAILS') el.open = true;
          el = el.parentElement;
        }
        nav.querySelector('.hit-pos').textContent = '命中 ' + marks.length + ' 处 · 第 ' + (markIdx + 1) + ' / ' + marks.length + ' 处';
        target.scrollIntoView({block: 'center', behavior: 'smooth'});
      };
      nav.querySelector('[data-hit-prev]').addEventListener('click', () => focusMark(markIdx - 1));
      nav.querySelector('[data-hit-next]').addEventListener('click', () => focusMark(markIdx + 1));
    }

    body.querySelector('[data-copy-input]')?.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      copyText(turn.input, event.currentTarget);
    });
    body.querySelectorAll('[data-copy-part]').forEach(btn => {
      btn.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        const part = turn.output[+btn.dataset.copyPart];
        if (part) copyText(part.text, btn);
      });
    });
    const expandPartsBtn = body.querySelector('[data-expand-parts]');
    if (expandPartsBtn) {
      const resp = expandPartsBtn.closest('details.response');
      const allParts = [...resp.querySelectorAll('details.part')];
      if (allParts.length && allParts.every(p => p.open)) expandPartsBtn.textContent = '收起全部段';
      expandPartsBtn.addEventListener('click', event => {
        event.preventDefault();
        event.stopPropagation();
        const openAll = allParts.some(p => !p.open);
        if (openAll) resp.open = true;
        allParts.forEach(p => { p.open = openAll; });
        expandPartsBtn.textContent = openAll ? '收起全部段' : '展开全部段';
      });
    }
    detail._turnData = turn;
    detail.dataset.loaded = 'yes';
  } catch (error) {
    body.textContent = error.message + '；收起后重新展开可重试。';
    delete detail.dataset.loaded;
  }
}

function showSessionList() {
  state.expandToken++;
  state.session = null;
  state.sessionCards = [];
  state.sessionReverse = false;
  $('detailOrder').setAttribute('aria-pressed', 'false');
  $('detailOrder').textContent = '↓ 正序';
  $('detailJumpEdge').textContent = '↓ 末轮';
  $('detailFilter').value = '';
  $('detailMatchCount').textContent = '';
  $('sessionListView').hidden = false;
  $('sessionDetail').hidden = true;
  $('sessionDetailStats').hidden = true;
  $('sessionExpandAll').textContent = '全部展开';
}

function formatDuration(firstIso, lastIso) {
  if (!firstIso || !lastIso) return '';
  const mins = Math.max(0, Math.round((new Date(lastIso) - new Date(firstIso)) / 60000));
  if (mins < 1) return '';
  if (mins < 60) return mins + ' 分钟';
  const hrs = mins / 60;
  if (hrs < 24) return (hrs >= 10 ? Math.round(hrs) : hrs.toFixed(1)) + ' 小时';
  return (hrs / 24).toFixed(1) + ' 天';
}

function formatSessionSpan(firstIso, lastIso) {
  if (!firstIso) return '';
  const startStr = new Date(firstIso).toLocaleString('zh-CN');
  const dur = formatDuration(firstIso, lastIso);
  return dur ? startStr + ' · ' + dur : startStr;
}

function renderSessionTokens(s) {
  const span = document.createElement('span');
  if (s.total === null || s.total === undefined) {
    span.className = 'session-tokens subtle';
    span.textContent = '—';
    return span;
  }
  span.className = 'session-tokens';
  span.textContent = compact(s.total);
  setTipContent(span, [
    '会话用量 · ' + number(s.total) + ' tokens',
    '新增 ' + number(s.new) + ' · 输出 ' + number(s.out),
    '缓存读 ' + number(s.cr) + ' · 写 ' + number(s.cc),
    '调用 ' + number(s.msgs) + ' 次',
  ]);
  return span;
}

function renderSessions(data) {
  const rows = data.rows || [];
  const sortLabel = SORT_LABELS[$('sessionSort').value] || '按创建时间';
  const sq = $('sessionQuery').value.trim();
  const s = data.summary || {sessions: data.total, turns: 0, total_tokens: null};
  $('sessionCount').textContent = number(data.total) + ' 个会话 · ' + sortLabel + (data.total ? ' · 第 ' + data.page + ' / ' + data.pages + ' 页' : '');

  const bar = $('sessionSummaryBar');
  if (data.total) {
    bar.hidden = false;
    bar.innerHTML = '<span>匹配会话 <b>' + number(s.sessions) + '</b> 个</span>' +
      '<span>累计轮次 <b>' + number(s.turns) + '</b> 轮</span>' +
      '<span>Token 总计 <b' + (s.total_tokens !== null ? ' title="' + number(s.total_tokens) + ' tokens"' : '') + '>' +
      (s.total_tokens !== null ? compact(s.total_tokens) : '—') + '</b></span>' +
      (sq ? '<span>过滤词：<b>' + esc(sq) + '</b></span>' : '');
  } else {
    bar.hidden = true;
  }

  const listEl = $('sessionList');
  listEl.replaceChildren();
  if (!rows.length) {
    listEl.innerHTML = empty('当前筛选下没有会话。');
  } else {
    const terms = sq ? [sq] : [];
    rows.forEach(item => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'session-row';

      const top = document.createElement('div');
      top.className = 'session-row-top';
      const models = item.models || [];
      const shownModels = models.slice(0, 2);
      const extraModels = models.length - shownModels.length;
      const modelsHtml = shownModels.map(m => '<span class="session-model" title="' + esc(m) + '">' + esc(m) + '</span>').join('') +
        (extraModels > 0 ? '<span class="session-model" title="' + esc(models.join(' · ')) + '">+' + extraModels + '</span>' : '');

      top.innerHTML = '<span class="badge">' + esc(item.agent) + '</span>' +
        (item.label ? '<span class="badge-muted">' + esc(item.label) + '</span>' : '') +
        '<span class="session-name" title="' + esc(item.session) + '">' + highlightTerms(item.session, terms) + '</span>' +
        modelsHtml;

      const metrics = document.createElement('span');
      metrics.className = 'session-metrics';
      const turnsSpan = document.createElement('span');
      turnsSpan.className = 'subtle';
      turnsSpan.textContent = number(item.turns) + ' 轮';
      metrics.appendChild(turnsSpan);
      metrics.appendChild(renderSessionTokens(item));
      const dateSpan = document.createElement('span');
      dateSpan.className = 'session-date';
      if (item.first && item.last && item.first !== item.last) {
        dateSpan.title = new Date(item.first).toLocaleString('zh-CN') + ' — ' + new Date(item.last).toLocaleString('zh-CN');
      }
      dateSpan.textContent = formatSessionSpan(item.first, item.last);
      metrics.appendChild(dateSpan);
      top.appendChild(metrics);

      const topic = document.createElement('div');
      topic.className = 'session-topic' + (item.topic ? '' : ' is-empty');
      if (item.topic) {
        topic.title = item.topic;
        topic.innerHTML = highlightTerms(item.topic, terms);
      } else {
        topic.textContent = '（首轮无文本输入）';
      }

      btn.appendChild(top);
      btn.appendChild(topic);
      btn.addEventListener('click', () => openSession(item, true));
      listEl.appendChild(btn);
    });
    bindTips(listEl);
  }
  $('sessionPage').textContent = data.total ? data.page + ' / ' + data.pages : '0 / 0';
  $('sessionPrev').disabled = data.page <= 1;
  $('sessionNext').disabled = data.page >= data.pages;
}

function renderSessionDetailStats(data) {
  const box = $('sessionDetailStats');
  const u = data.usage;
  const models = data.models || [];
  if (!u && !models.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const modelsHtml = models.length
    ? '<div class="session-stats-models">' + models.map(m => '<span class="badge-muted">' + esc(m) + '</span>').join('') + '</div>'
    : '';
  let usageHtml = '<span class="subtle">该来源未采集 Token 用量</span>';
  let barHtml = '';
  if (u) {
    usageHtml = '<div class="session-stats-nums">' +
      '<span>总计 <b title="' + number(u.total) + ' tokens">' + compact(u.total) + '</b></span>' +
      '<span>新增 ' + compact(u.new) + '</span>' +
      '<span>输出 ' + compact(u.out) + '</span>' +
      '<span>缓存读 ' + compact(u.cr) + '</span>' +
      '<span>缓存写 ' + compact(u.cc) + '</span>' +
      '<span>调用 ' + number(u.msgs) + ' 次</span>' +
      '</div>';
    if (u.total > 0) {
      barHtml = '<div class="comp-bar">' + COMP_PARTS.filter(([k]) => u[k] > 0).map(([k, label, color]) => {
        const share = (u[k] / u.total * 100);
        return '<div class="comp-seg" style="width:' + Math.max(share, 0.4) + '%;background:' + color + '" title="' + esc(label) + ' ' + share.toFixed(1) + '% (' + number(u[k]) + ')"></div>';
      }).join('') + '</div>';
    }
  }
  box.innerHTML = '<div class="session-stats-row">' + usageHtml + modelsHtml + '</div>' + barHtml;
}

function applyDetailTurnsView() {
  const cards = state.sessionCards || [];
  const q = $('detailFilter').value.trim();
  const needle = q.toLowerCase();
  const ordered = state.sessionReverse ? [...cards].reverse() : cards;
  const matched = [];
  ordered.forEach(item => {
    const hit = !needle || item.haystack.includes(needle);
    if (!hit) return;
    item.previewEl.innerHTML = q ? highlightTerms(item.preview, [q]) : esc(item.preview);
    matched.push(item.el);
  });
  $('sessionTurns').replaceChildren(...matched);
  if (!matched.length) {
    $('sessionTurns').innerHTML = empty(q ? '没有匹配筛选条件的轮次。' : '该会话没有可显示的轮次。');
  }
  $('detailMatchCount').textContent = cards.length ? (q ? '命中 ' + matched.length + ' / ' + cards.length + ' 轮' : cards.length + ' 轮') : '';
  const openCount = matched.filter(el => el.open).length;
  $('sessionExpandAll').textContent = matched.length && openCount === matched.length ? '全部收起' : '全部展开';
}

async function openSession(target, push = true) {
  controllers.session?.abort();
  controllers.session = new AbortController();
  state.expandToken++;
  setStatus('读取会话…');
  try {
    const data = await api('/api/session', {key: target.key, agent: target.agent, session: target.session}, {signal: controllers.session.signal});
    state.session = data;
    state.sessionReverse = false;
    $('sessionListView').hidden = true;
    $('sessionDetail').hidden = false;
    $('detailFilter').value = '';
    $('detailOrder').setAttribute('aria-pressed', 'false');
    $('detailOrder').textContent = '↓ 正序';
    $('detailJumpEdge').textContent = '↓ 末轮';
    $('sessionExpandAll').textContent = '全部展开';
    const dur = formatDuration(data.first, data.last);
    $('sessionMeta').innerHTML = '<span class="badge">' + esc(data.agent) + '</span>' +
      '<b>' + esc(data.session) + '</b>' +
      '<button type="button" class="copy-btn" id="copySessionId">复制 ID</button>' +
      '<span class="subtle">' + number(data.turns.length) + ' 轮 · ' + esc(new Date(data.first).toLocaleString('zh-CN')) + ' — ' + esc(new Date(data.last).toLocaleString('zh-CN')) + (dur ? '（' + dur + '）' : '') + '</span>' +
      '<span class="subtle">来源 ' + esc(data.source) + '</span>' +
      '<button type="button" class="chip-btn" id="searchInSession">在此会话内搜索</button>';
    $('copySessionId').addEventListener('click', event => copyText(data.session, event.currentTarget));
    $('searchInSession').addEventListener('click', () => {
      $('agent').value = data.agent;
      $('sessionFilter').value = data.session;
      $('searchSource').value = data.key || '';
      if ($('searchSource').options.length > 1) $('searchSourceWrap').hidden = false;
      state.page = 1;
      setTab('search', {push: true});
    });
    renderSessionDetailStats(data);
    state.sessionCards = data.turns.map((t, index) => {
      const el = document.createElement('details');
      el.className = 'turn';
      el.dataset.id = t.id;
      const num = index + 1;
      const metaExtra = (t.input_len ? '<span>' + number(t.input_len) + ' 字</span>' : '') +
        (t.part_count ? '<span class="subtle">' + t.part_count + ' 段响应</span>' : '');
      el.innerHTML = '<summary><div class="turn-meta">' +
        '<span class="turn-index">#' + num + '</span>' +
        '<span class="badge">' + esc(data.agent) + '</span>' +
        '<span>' + esc(t.model) + '</span>' +
        '<span>' + esc(new Date(t.timestamp).toLocaleString('zh-CN')) + '</span>' +
        '<span class="turn-meta-right">' + metaExtra + '</span>' +
        '</div><div class="preview">' + esc(t.preview) + '</div></summary><div class="turn-body"></div>';
      el.addEventListener('toggle', () => loadTurn(el, el.dataset.id));
      const previewEl = el.querySelector('.preview');
      const timeStr = new Date(t.timestamp).toLocaleString('zh-CN');
      return {
        el, previewEl, preview: t.preview || '',
        haystack: ('#' + num + ' ' + t.model + ' ' + timeStr + ' ' + (t.preview || '')).toLowerCase(),
      };
    });
    applyDetailTurnsView();
    syncHash(push);
    notice();
    setStatus();
  } catch (error) {
    if (error.name === 'AbortError') return;
    notice(error.message, true);
    setStatus();
  }
}

function renderTurnCard(r, showSessionChip = true) {
  const hitKinds = (r.hit_kinds || []).map(k => kindTagHtml(k)).join('');
  const matchList = (r.matches && r.matches.length)
    ? '<div class="match-list">' + r.matches.map(m =>
        '<div class="match-item">' + kindTagHtml(m.kind) + '<span class="match-text">' + highlight(m.text) + '</span></div>'
      ).join('') + '</div>'
    : (r.match ? '<div class="match-list"><div class="match-item"><span class="match-text">' + highlight(r.match) + '</span></div></div>' : '');
  return '<details class="turn" data-id="' + esc(r.id) + '"><summary><div class="turn-meta">' +
    '<span class="badge">' + esc(r.agent) + '</span>' +
    (r.label ? '<span class="badge-muted">' + esc(r.label) + '</span>' : '') +
    '<span>' + esc(r.model) + '</span>' +
    (showSessionChip && r.session ? '<button type="button" class="session-chip" data-session-filter="' + esc(r.session) + '" title="点击仅在此会话内搜索：' + esc(r.session) + '">会话 ' + esc(shortSessionId(r.session)) + '</button>' : '') +
    '<span>' + esc(new Date(r.timestamp).toLocaleString('zh-CN')) + '</span>' +
    hitKinds +
    '<span class="turn-meta-right">' +
    (r.part_count ? '<span class="subtle">' + r.part_count + ' 段</span>' : '') +
    '<button type="button" class="text-button session-link" data-id="' + esc(r.id) + '" data-agent="' + esc(r.agent) + '" data-session="' + esc(r.session) + '">完整会话</button>' +
    '</span></div><div class="preview">' + highlight(r.preview) + '</div>' + matchList +
    '</summary><div class="turn-body"></div></details>';
}

function renderSearch(data) {
  const min = $('minLen').value, max = $('maxLen').value;
  const length = min && max ? ' · 长度 ' + min + '–' + max + ' 字' : min ? ' · 长度 ≥' + min + ' 字' : max ? ' · 长度 ≤' + max + ' 字' : '';
  const scopeSelect = $('searchScope');
  const scopeText = scopeSelect.value !== 'all' ? ' · ' + scopeSelect.options[scopeSelect.selectedIndex].textContent : '';
  const excludeVal = $('excludeQuery').value.trim();
  const excludeText = excludeVal ? ' · 排除：' + excludeVal : '';
  const sessionVal = $('sessionFilter').value.trim();
  const sessionText = sessionVal ? ' · 会话：' + shortSessionId(sessionVal) : '';
  const isGrouped = data.group === 'session';
  const countPrefix = isGrouped
    ? number(data.total) + ' 个会话共 ' + number(data.total_turns ?? data.total) + ' 轮匹配'
    : number(data.total) + ' 轮匹配';
  const hint = state.term ? ' · 输入词：' + state.term : $('q').value.trim() ? ' · 空格全命中 / 引号精确短语' : ' · 按筛选条件匹配';
  $('searchCount').textContent = countPrefix + hint + scopeText + excludeText + sessionText + length;
  $('clearTerm').hidden = !state.term;
  $('clearSessionFilter').hidden = !sessionVal;

  if (isGrouped) {
    $('results').innerHTML = (data.rows || []).map(g =>
      '<div class="session-group"><div class="session-group-head"><div class="session-group-title">' +
      '<span class="badge">' + esc(g.agent) + '</span>' +
      (g.label ? '<span class="badge-muted">' + esc(g.label) + '</span>' : '') +
      '<b class="session-group-id">' + esc(g.session) + '</b>' +
      '<span class="subtle">' + number(g.hit_turns) + ' 轮命中 · ' + esc(new Date(g.first).toLocaleString('zh-CN')) + ' — ' + esc(new Date(g.last).toLocaleString('zh-CN')) + '</span>' +
      '</div><div class="session-group-actions">' +
      '<button type="button" class="chip-btn filter-session-btn" data-session-filter="' + esc(g.session) + '">只看此会话命中</button>' +
      '<button type="button" class="chip-btn open-session-btn" data-key="' + esc(g.key) + '" data-agent="' + esc(g.agent) + '" data-session="' + esc(g.session) + '">完整会话</button>' +
      '</div></div>' +
      g.turns.map(r => renderTurnCard(r, false)).join('') +
      (g.hit_turns > g.turns.length ? '<div class="session-group-more"><span>仅预览前 ' + g.turns.length + ' 轮命中（共 ' + number(g.hit_turns) + ' 轮）</span><button type="button" class="text-button filter-session-btn" data-session-filter="' + esc(g.session) + '">展开全部 ' + number(g.hit_turns) + ' 轮命中 →</button></div>' : '') +
      '</div>'
    ).join('') || empty('没有找到匹配的会话。试试更短的关键词、调整范围或扩大时间筛选。');
  } else {
    $('results').innerHTML = (data.rows || []).map(r => renderTurnCard(r, true)).join('') ||
      empty('没有找到匹配轮次。试试更短的关键词、放宽范围或扩大时间筛选。');
  }

  $('results').querySelectorAll('.turn').forEach(el => el.addEventListener('toggle', () => loadTurn(el, el.dataset.id)));
  $('results').querySelectorAll('.session-chip, .filter-session-btn').forEach(button => button.addEventListener('click', event => {
    event.preventDefault();
    event.stopPropagation();
    $('sessionFilter').value = button.dataset.sessionFilter;
    state.searchGroup = 'turn';
    syncSearchGroupButtons();
    state.page = 1;
    load();
  }));
  $('results').querySelectorAll('.session-link, .open-session-btn').forEach(button => button.addEventListener('click', event => {
    event.preventDefault();
    event.stopPropagation();
    const key = button.dataset.key || button.dataset.id.split(':')[0];
    setTab('sessions', {load: false, push: true});
    openSession({key, agent: button.dataset.agent, session: button.dataset.session}, false);
  }));
  $('page').textContent = data.total ? data.page + ' / ' + data.pages : '0 / 0';
  $('prev').disabled = data.page <= 1;
  $('next').disabled = data.page >= data.pages;
}

/* ---------- Words ---------- */
function wordMetricValue(w, sortBy) {
  if (sortBy === 'turns') return w.turns;
  if (sortBy === 'avg') return w.avg ?? (w.turns ? w.count / w.turns : 0);
  return w.count;
}

function jumpToWordSearch(term) {
  state.term = term;
  $('q').value = '';
  syncQClear();
  const wSrc = $('wordSource').value;
  if (wSrc) {
    $('searchSource').value = wSrc;
    if ($('searchSource').options.length > 1) $('searchSourceWrap').hidden = false;
  }
  state.page = 1;
  setTab('search', {push: true});
}

function renderExcludedPanel() {
  const list = state.excluded || [];
  const collapseLimit = 10;
  const hiddenCount = Math.max(0, list.length - collapseLimit);
  const visible = state.showAllExcluded ? list : list.slice(0, collapseLimit);
  $('excludedWords').innerHTML = visible.map(term => state.writable
    ? '<button type="button" class="chip" data-term="' + esc(term) + '" title="点击恢复「' + esc(term) + '」">' + esc(term) + ' ×</button>'
    : '<span class="chip" title="远端只读，无法修改">' + esc(term) + '</span>').join('');
  $('excludedWords').querySelectorAll('button.chip').forEach(chip => chip.addEventListener('click', () => {
    state.lastExcluded = null;
    saveExcluded(state.excluded.filter(term => term !== chip.dataset.term));
  }));
  const toggleBtn = $('excludedToggle');
  toggleBtn.hidden = hiddenCount === 0;
  if (hiddenCount > 0) {
    toggleBtn.textContent = state.showAllExcluded ? '收起部分排除词' : '展开全部 ' + list.length + ' 词';
  }
  $('excludedClearAll').hidden = !state.writable || !list.length;
  const undoEl = $('excludeUndo');
  const showUndo = state.writable && state.lastExcluded && state.excluded.includes(state.lastExcluded);
  undoEl.hidden = !showUndo;
  if (showUndo) $('excludeUndoText').textContent = '已排除「' + state.lastExcluded + '」';
}

function renderWords(data) {
  state.excluded = data.excluded || [];
  const words = data.words || [];
  const count = words.length;
  const sortBy = $('wordSort').value;
  const minTurns = Number($('wordMinTurns').value) || 1;
  const s = data.summary || {unique_terms: count, total_count: 0, scoped_turns: 0, max_turns: 0};

  const statsEl = $('wordStats');
  if (count) {
    statsEl.hidden = false;
    const top = words[0];
    statsEl.innerHTML =
      '<div class="word-stat-card"><span>不同词条</span><strong>' + number(s.unique_terms) + '</strong><small>当前展示 ' + count + ' 词</small></div>' +
      '<div class="word-stat-card"><span>累计词次</span><strong>' + compact(s.total_count) + '</strong><small title="' + number(s.total_count) + '">' + number(s.total_count) + ' 次</small></div>' +
      '<div class="word-stat-card"><span>覆盖人工轮次</span><strong>' + number(s.scoped_turns) + '</strong><small>最高单条覆盖 ' + number(s.max_turns) + ' 轮</small></div>' +
      '<div class="word-stat-card"><span>当前榜首</span><strong title="' + esc(top.term) + '">' + esc(top.term) + '</strong><small>' + number(top.turns) + ' 轮 · ' + number(top.count) + ' 次</small></div>';
  } else {
    statsEl.hidden = true;
  }

  $('wordSummary').textContent = '共展示 ' + count + ' 词 · ' + (WORD_SORT_LABELS[sortBy] || '按出现次数') +
    (minTurns > 1 ? ' · 至少覆盖 ' + minTurns + ' 轮' : '') +
    (state.excluded.length ? ' · 已排除 ' + state.excluded.length + ' 词' : '') +
    ' · 点击词语跳转到会话搜索';

  const chartEl = $('wordChart');
  const maximum = Math.max(...words.map(w => wordMetricValue(w, sortBy)), 1);
  if (!count) {
    chartEl.classList.remove('is-cloud');
    chartEl.style.gridTemplateRows = '';
    chartEl.innerHTML = empty('当前筛选下没有可统计的输入词语。');
    renderExcludedPanel();
    return;
  }

  const tipLinesFor = (w, rank) => [
    w.term + ' · 第 ' + rank + ' 位',
    '出现次数：' + number(w.count) + ' 次（占词频 ' + (w.share ?? 0) + '%）',
    '覆盖轮次：' + number(w.turns) + ' 轮（人工轮次占比 ' + (w.turn_rate ?? 0) + '%）',
    '单轮平均：' + (w.avg ?? 0) + ' 次/轮',
    '点击在会话搜索中查看轮次',
  ];

  const excludeBtn = term => state.writable
    ? '<button type="button" class="word-exclude" data-term="' + esc(term) + '" title="排除「' + esc(term) + '」" aria-label="排除 ' + esc(term) + '">×</button>'
    : '';

  if (state.wordView === 'cloud') {
    chartEl.classList.add('is-cloud');
    chartEl.style.gridTemplateRows = '';
    chartEl.innerHTML = '<div class="word-cloud">' + words.map(w => {
      const val = wordMetricValue(w, sortBy);
      const ratio = Math.sqrt(Math.max(0, val) / maximum);
      const size = (12.5 + ratio * 15).toFixed(1);
      const tier = ratio >= 0.72 ? 'tier-4' : ratio >= 0.45 ? 'tier-3' : ratio >= 0.25 ? 'tier-2' : 'tier-1';
      return '<span class="cloud-item">' +
        '<button type="button" class="cloud-word ' + tier + '" data-term="' + esc(w.term) + '" style="font-size:' + size + 'px">' + esc(w.term) + '</button>' +
        (state.writable ? '<button type="button" class="cloud-exclude" data-term="' + esc(w.term) + '" title="排除「' + esc(w.term) + '」" aria-label="排除 ' + esc(w.term) + '">×</button>' : '') +
        '</span>';
    }).join('') + '</div>';
    chartEl.querySelectorAll('.cloud-word').forEach((btn, idx) => {
      setTipContent(btn, tipLinesFor(words[idx], idx + 1));
      btn.addEventListener('click', () => jumpToWordSearch(btn.dataset.term));
    });
    chartEl.querySelectorAll('.cloud-exclude').forEach(btn => btn.addEventListener('click', () => {
      state.lastExcluded = btn.dataset.term;
      saveExcluded([...state.excluded, btn.dataset.term]);
    }));
  } else {
    chartEl.classList.remove('is-cloud');
    const rowsPerCol = Math.max(1, Math.ceil(count / 2));
    chartEl.style.gridTemplateRows = 'repeat(' + rowsPerCol + ', auto)';
    chartEl.innerHTML = words.map((w, idx) => {
      const metric = wordMetricValue(w, sortBy);
      const countLabel = sortBy === 'turns'
        ? '<b>' + number(w.turns) + '</b> 轮 / ' + number(w.count) + ' 次'
        : sortBy === 'avg'
          ? '<b>' + w.avg + '</b> /轮 · ' + number(w.turns) + ' 轮'
          : '<b>' + number(w.count) + '</b> / ' + number(w.turns) + ' 轮';
      return '<div class="word-item"><button type="button" class="word-row" data-term="' + esc(w.term) + '">' +
        '<span class="word-rank">' + (idx + 1) + '</span>' +
        '<span class="word-label">' + esc(w.term) + '</span>' +
        '<div class="word-track"><div class="word-fill" style="width:' + (metric / maximum * 100) + '%"></div></div>' +
        '<span class="word-count">' + countLabel + '</span></button>' + excludeBtn(w.term) +
        '</div>';
    }).join('');
    chartEl.querySelectorAll('.word-row').forEach((btn, idx) => {
      setTipContent(btn, tipLinesFor(words[idx], idx + 1));
      btn.addEventListener('click', () => jumpToWordSearch(btn.dataset.term));
    });
    chartEl.querySelectorAll('.word-exclude').forEach(btn => btn.addEventListener('click', () => {
      state.lastExcluded = btn.dataset.term;
      saveExcluded([...state.excluded, btn.dataset.term]);
    }));
  }
  bindTips(chartEl);
  renderExcludedPanel();
}

async function saveExcluded(words) {
  try {
    const result = await api('/api/words-exclude', {}, {method: 'POST', headers: {'X-Stats-Request': '1', 'Content-Type': 'application/json'}, body: JSON.stringify({words})});
    state.excluded = result.excluded;
    await load();
  } catch (error) {
    notice(error.message, true);
  }
}

function addExcluded() {
  const words = $('excludeInput').value.split(/[\s,，、]+/).filter(Boolean);
  if (!words.length) return;
  $('excludeInput').value = '';
  state.lastExcluded = words.length === 1 ? words[0] : null;
  saveExcluded([...state.excluded, ...words]);
}

/* ---------- Main Loader & Navigation ---------- */
async function load() {
  controllers.load?.abort();
  controllers.load = new AbortController();
  const request = ++state.request, tab = state.tab;
  hideTip();
  setStatus('读取中…');
  try {
    if (!$('dateControls').hidden && $('start').value && $('end').value && $('start').value > $('end').value) {
      throw new Error('开始日期不能晚于结束日期');
    }
    const signal = controllers.load.signal;
    let result;
    if (tab === 'usage') {
      result = await api('/api/usage', {agent: $('agent').value, model: $('model').value, ...usageDateParams()}, {signal});
    } else if (tab === 'search') {
      result = await api('/api/search', {
        ...filters(),
        q: $('q').value,
        exclude: $('excludeQuery').value,
        scope: $('searchScope').value,
        sort: $('searchSort').value,
        group: state.searchGroup,
        source: $('searchSource').value,
        session: $('sessionFilter').value.trim(),
        page: state.page,
        term: state.term,
        minlen: $('minLen').value,
        maxlen: $('maxLen').value,
      }, {signal});
    } else if (tab === 'sessions') {
      result = await api('/api/sessions', {
        ...filters(),
        source: $('sessionSource').value,
        sort: $('sessionSort').value,
        q: $('sessionQuery').value.trim(),
        page: state.sessionPage,
      }, {signal});
    } else {
      result = await api('/api/words', {
        ...filters(),
        source: $('wordSource').value,
        sort: $('wordSort').value,
        order: $('wordOrder').value,
        limit: $('wordLimit').value,
        kind: state.wordKind,
        min_turns: $('wordMinTurns').value,
        q: $('wordQuery').value.trim(),
      }, {signal});
    }
    if (request !== state.request) return;
    ({usage: renderUsage, search: renderSearch, sessions: renderSessions, words: renderWords}[tab])(result);
    syncHash(false);
    notice();
    setStatus();
  } catch (error) {
    if (error.name === 'AbortError') return;
    if (request === state.request) {
      notice(error.message, true);
      setStatus();
    }
  }
}

function updateWordLimitLabels() {
  const prefix = $('wordOrder').value === 'asc' ? '末 ' : '前 ';
  for (const opt of $('wordLimit').options) opt.textContent = prefix + opt.value + ' 词';
}

function setTab(tab, options = {}) {
  const {load: shouldLoad = true, push = false} = options;
  state.tab = tab;
  document.querySelectorAll('nav button').forEach(b => {
    const active = b.dataset.tab === tab;
    b.classList.toggle('active', active);
    b.setAttribute('aria-selected', String(active));
    if (active) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  for (const name of ['usage', 'search', 'sessions', 'words']) $(name).hidden = name !== tab;
  syncRangeControls();
  if (tab === 'sessions' && shouldLoad) showSessionList();
  if (push) syncHash(true);
  if (shouldLoad) load();
}

/* ---------- Event Listeners ---------- */
document.querySelectorAll('nav button').forEach(b => b.addEventListener('click', () => setTab(b.dataset.tab, {push: true})));

$('agent').addEventListener('change', () => {
  state.page = 1;
  state.sessionPage = 1;
  if (state.tab === 'sessions' && state.session) showSessionList();
  load();
});

for (const id of ['model', 'unit', 'start', 'end', 'wordLimit']) {
  $(id).addEventListener('change', () => {
    state.page = 1;
    state.sessionPage = 1;
    load();
  });
}

$('n').addEventListener('change', () => {
  const clamped = Math.max(1, Math.min(366, Math.round(Number($('n').value) || 14)));
  $('n').value = String(clamped);
  load();
});

$('rangePreset').addEventListener('change', () => {
  state.rangePreset = $('rangePreset').value;
  if (state.rangePreset === 'custom' && !$('start').value && !$('end').value) {
    if (state.usage) {
      $('start').value = state.usage.start;
      $('end').value = state.usage.end;
    } else {
      const today = new Date();
      $('start').value = dateValue(new Date(today.getFullYear(), today.getMonth(), today.getDate() - 13));
      $('end').value = dateValue(today);
    }
  }
  syncRangeControls();
  load();
});

$('metricSwitch').querySelectorAll('button').forEach(btn => {
  btn.addEventListener('click', () => {
    state.metric = btn.dataset.metric;
    if (state.trendMode === 'composition') {
      state.trendMode = 'stack';
      $('trendMode').value = 'stack';
      state.hiddenKeys.clear();
    }
    syncMetricButtons();
    syncHash(false);
    if (state.usage) renderUsage(state.usage);
  });
});

$('trendMode').addEventListener('change', () => {
  state.trendMode = $('trendMode').value;
  state.hiddenKeys.clear();
  syncHash(false);
  renderCharts();
});

$('group').addEventListener('change', () => {
  state.group = $('group').value;
  state.hiddenKeys.clear();
  state.showOther = false;
  syncHash(false);
  renderCharts();
});

$('compactToggle').addEventListener('click', () => {
  state.compactTable = !state.compactTable;
  $('compactToggle').setAttribute('aria-pressed', String(state.compactTable));
  if (state.usageRowsMap) renderUsageTable(state.usageRowsMap);
});

$('wordOrder').addEventListener('change', () => { updateWordLimitLabels(); load(); });
for (const id of ['wordSource', 'wordSort', 'wordMinTurns']) {
  $(id).addEventListener('change', load);
}
$('wordView').querySelectorAll('button').forEach(btn => {
  btn.addEventListener('click', () => {
    if (state.wordView === btn.dataset.wview) return;
    state.wordView = btn.dataset.wview;
    syncWordViewButtons();
    syncHash(false);
    load();
  });
});
$('wordKind').querySelectorAll('button').forEach(btn => {
  btn.addEventListener('click', () => {
    if (state.wordKind === btn.dataset.kind) return;
    state.wordKind = btn.dataset.kind;
    syncWordKindButtons();
    load();
  });
});
let wordQueryTimer;
$('wordQuery').addEventListener('input', () => {
  clearTimeout(wordQueryTimer);
  wordQueryTimer = setTimeout(load, 250);
});
$('wordQuery').addEventListener('keydown', event => {
  if (event.key === 'Enter') {
    event.preventDefault();
    clearTimeout(wordQueryTimer);
    load();
  }
});
document.querySelectorAll('[data-wrange]').forEach(btn => {
  btn.addEventListener('click', () => {
    const kind = btn.dataset.wrange;
    const now = new Date();
    const today = dateValue(now);
    if (kind === 'all') {
      $('start').value = '';
      $('end').value = '';
    } else if (kind === 'month') {
      $('start').value = today.slice(0, 7) + '-01';
      $('end').value = today;
    } else {
      const days = kind === '7d' ? 6 : 29;
      $('start').value = dateValue(new Date(now.getFullYear(), now.getMonth(), now.getDate() - days));
      $('end').value = today;
    }
    load();
  });
});
$('excludeUndoBtn').addEventListener('click', () => {
  const term = state.lastExcluded;
  state.lastExcluded = null;
  if (term) saveExcluded(state.excluded.filter(w => w !== term));
});
$('excludedToggle').addEventListener('click', () => {
  state.showAllExcluded = !state.showAllExcluded;
  renderExcludedPanel();
});
$('excludedClearAll').addEventListener('click', () => {
  state.lastExcluded = null;
  saveExcluded([]);
});

$('bars').addEventListener('dblclick', event => {
  const rect = event.target.closest('.bar-seg');
  if (rect) drilldown(rect.dataset.bucket, rect.dataset.key, rect.dataset.group);
});

$('bars').addEventListener('keydown', event => {
  if (event.key !== 'Enter' && event.key !== ' ') return;
  const rect = event.target.closest('.bar-seg');
  if (rect) {
    event.preventDefault();
    drilldown(rect.dataset.bucket, rect.dataset.key, rect.dataset.group);
  }
});

$('searchForm').addEventListener('submit', e => {
  e.preventDefault();
  state.page = 1;
  state.term = '';
  syncQClear();
  load();
});

$('q').addEventListener('input', syncQClear);
$('qClear').addEventListener('click', () => {
  $('q').value = '';
  syncQClear();
  state.page = 1;
  state.term = '';
  load();
  $('q').focus();
});

for (const id of ['searchScope', 'searchSort', 'searchSource']) {
  $(id).addEventListener('change', () => {
    state.page = 1;
    load();
  });
}

$('searchGroup').querySelectorAll('button').forEach(btn => {
  btn.addEventListener('click', () => {
    if (state.searchGroup === btn.dataset.group) return;
    state.searchGroup = btn.dataset.group;
    syncSearchGroupButtons();
    state.page = 1;
    load();
  });
});

let searchFilterTimer;
for (const id of ['excludeQuery', 'sessionFilter']) {
  $(id).addEventListener('input', () => {
    clearTimeout(searchFilterTimer);
    searchFilterTimer = setTimeout(() => {
      state.page = 1;
      load();
    }, 300);
  });
  $(id).addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      clearTimeout(searchFilterTimer);
      state.page = 1;
      load();
    }
  });
}

$('lenPreset').addEventListener('change', () => {
  const preset = $('lenPreset').value;
  if (preset === '') { $('minLen').value = ''; $('maxLen').value = ''; }
  else if (preset === 'short') { $('minLen').value = ''; $('maxLen').value = '30'; }
  else if (preset === 'medium') { $('minLen').value = '31'; $('maxLen').value = '300'; }
  else if (preset === 'long') { $('minLen').value = '301'; $('maxLen').value = ''; }
  else { $('minLen').focus(); return; }
  state.page = 1;
  state.term = '';
  load();
});

$('prev').addEventListener('click', () => { state.page--; load(); });
$('next').addEventListener('click', () => { state.page++; load(); });
$('sessionPrev').addEventListener('click', () => { state.sessionPage--; load(); });
$('sessionNext').addEventListener('click', () => { state.sessionPage++; load(); });
$('sessionBack').addEventListener('click', () => { showSessionList(); syncHash(true); load(); });

for (const id of ['sessionSource', 'sessionSort']) {
  $(id).addEventListener('change', () => {
    state.sessionPage = 1;
    load();
  });
}

$('detailFilter').addEventListener('input', applyDetailTurnsView);
$('detailOrder').addEventListener('click', () => {
  state.sessionReverse = !state.sessionReverse;
  $('detailOrder').setAttribute('aria-pressed', String(state.sessionReverse));
  $('detailOrder').textContent = state.sessionReverse ? '↑ 倒序' : '↓ 正序';
  applyDetailTurnsView();
});
$('detailJumpEdge').addEventListener('click', () => {
  const turns = [...$('sessionTurns').querySelectorAll('details.turn')];
  if (!turns.length) return;
  const toBottom = $('detailJumpEdge').textContent.includes('末轮');
  const target = turns[toBottom ? turns.length - 1 : 0];
  target.scrollIntoView({behavior: 'smooth', block: 'center'});
  $('detailJumpEdge').textContent = toBottom ? '↑ 首轮' : '↓ 末轮';
});

$('sessionExpandAll').addEventListener('click', async () => {
  const turns = [...$('sessionTurns').querySelectorAll('details.turn')];
  if (!turns.length) return;
  const btn = $('sessionExpandAll');
  if (btn.dataset.busy === '1') {
    state.expandToken++;
    delete btn.dataset.busy;
    btn.textContent = turns.every(t => t.open) ? '全部收起' : '全部展开';
    return;
  }
  const openAll = turns.some(t => !t.open);
  if (!openAll) {
    state.expandToken++;
    turns.forEach(t => { t.open = false; });
    btn.textContent = '全部展开';
    return;
  }
  const token = ++state.expandToken;
  const toOpen = turns.filter(t => !t.open);
  btn.dataset.busy = '1';
  let done = 0;
  try {
    for (let i = 0; i < toOpen.length; i += 6) {
      if (token !== state.expandToken) return;
      const chunk = toOpen.slice(i, i + 6);
      await Promise.all(chunk.map(el => {
        el.open = true;
        return loadTurn(el, el.dataset.id);
      }));
      done += chunk.length;
      if (token === state.expandToken) btn.textContent = '展开中 ' + done + '/' + toOpen.length + '（点击停止）';
    }
    if (token === state.expandToken) btn.textContent = '全部收起';
  } finally {
    delete btn.dataset.busy;
  }
});

$('exportSessionMd').addEventListener('click', async () => {
  if (!state.session || !state.sessionCards.length) return;
  const btn = $('exportSessionMd');
  const token = state.expandToken;
  btn.disabled = true;
  const orig = btn.textContent;
  try {
    const cards = state.sessionCards;
    const loaded = new Array(cards.length);
    for (let i = 0; i < cards.length; i += 6) {
      if (token !== state.expandToken) return;
      btn.textContent = '导出中 ' + Math.min(i + 6, cards.length) + '/' + cards.length + '…';
      await Promise.all(cards.slice(i, i + 6).map(async (item, off) => {
        const el = item.el;
        const t = el._turnData || await api('/api/turn', {id: el.dataset.id});
        el._turnData = t;
        loaded[i + off] = t;
      }));
    }
    const s = state.session;
    const lines = [
      '# ' + s.agent + ' · ' + s.session,
      '',
      '- 来源：' + s.source,
      '- 时间：' + new Date(s.first).toLocaleString('zh-CN') + ' — ' + new Date(s.last).toLocaleString('zh-CN'),
      ...(s.models?.length ? ['- 模型：' + s.models.join(' · ')] : []),
      ...(s.usage ? ['- Token：' + number(s.usage.total) + '（新增 ' + number(s.usage.new) + ' / 输出 ' + number(s.usage.out) + ' / 缓存读 ' + number(s.usage.cr) + ' / 写 ' + number(s.usage.cc) + '）'] : []),
      '',
    ];
    loaded.forEach((turn, idx) => {
      if (!turn) return;
      lines.push('## 第 ' + (idx + 1) + ' 轮 · ' + turn.model + ' · ' + new Date(turn.timestamp).toLocaleString('zh-CN'), '');
      lines.push('### 用户输入', '', '```text', turn.input || '', '```', '');
      if (turn.output?.length) {
        lines.push('### Agent 响应', '');
        turn.output.forEach((part, pidx) => {
          lines.push('#### 第 ' + (pidx + 1) + ' 段 · ' + (part.kind || '正文'), '', '```text', part.text || '', '```', '');
        });
      }
    });
    const blob = new Blob([lines.join('\n')], {type: 'text/markdown;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = String(s.session || 'session').replace(/[^\w.\-]+/g, '_') + '.md';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
    notice('已导出会话 Markdown（' + loaded.length + ' 轮）');
  } catch (error) {
    notice('导出失败：' + error.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = orig;
  }
});

let sessionTimer;
$('sessionQuery').addEventListener('input', () => {
  syncSessionQueryClear();
  clearTimeout(sessionTimer);
  sessionTimer = setTimeout(() => { state.sessionPage = 1; load(); }, 250);
});
$('sessionQuery').addEventListener('keydown', event => {
  if (event.key === 'Enter') {
    event.preventDefault();
    clearTimeout(sessionTimer);
    state.sessionPage = 1;
    load();
  }
});
$('sessionQueryClear').addEventListener('click', () => {
  $('sessionQuery').value = '';
  syncSessionQueryClear();
  state.sessionPage = 1;
  load();
  $('sessionQuery').focus();
});

$('clearTerm').addEventListener('click', () => { state.term = ''; state.page = 1; load(); });
$('clearSessionFilter').addEventListener('click', () => { $('sessionFilter').value = ''; state.page = 1; load(); });
$('excludeAdd').addEventListener('click', addExcluded);
$('excludeInput').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); addExcluded(); } });
$('allDates').addEventListener('click', () => { $('start').value = ''; $('end').value = ''; state.page = 1; state.sessionPage = 1; load(); });

$('reset').addEventListener('click', () => {
  for (const id of ['agent', 'model', 'start', 'end', 'q', 'excludeQuery', 'sessionFilter', 'minLen', 'maxLen', 'sessionQuery', 'wordQuery']) $(id).value = '';
  $('n').value = '14';
  $('unit').value = 'day';
  state.rangePreset = 'recent';
  $('rangePreset').value = 'recent';
  state.metric = 'total';
  syncMetricButtons();
  state.trendMode = 'stack';
  $('trendMode').value = 'stack';
  state.group = 'agent';
  $('group').value = 'agent';
  $('searchScope').value = 'all';
  $('searchSort').value = 'newest';
  $('searchSource').value = '';
  $('lenPreset').value = '';
  state.searchGroup = 'turn';
  syncSearchGroupButtons();
  syncQClear();
  syncSessionQueryClear();
  $('sessionSource').value = '';
  $('sessionSort').value = 'created';
  state.hiddenKeys.clear();
  state.showOther = false;
  $('wordSource').value = '';
  $('wordSort').value = 'count';
  $('wordOrder').value = 'desc';
  $('wordLimit').value = '40';
  $('wordMinTurns').value = '1';
  state.wordKind = 'all';
  state.wordView = 'bars';
  state.lastExcluded = null;
  syncWordKindButtons();
  syncWordViewButtons();
  updateWordLimitLabels();
  state.page = 1;
  state.sessionPage = 1;
  state.term = '';
  syncRangeControls();
  if (state.tab === 'sessions') showSessionList();
  load();
});

async function resync() {
  $('refresh').disabled = true;
  setStatus('重建索引…');
  try {
    await api('/api/refresh', {}, {method: 'POST', headers: {'X-Stats-Request': '1'}});
    pendingMeta = null;
    $('freshNotice').hidden = true;
    await metadata();
    state.page = 1;
    await load();
  } catch (error) {
    notice(error.message, true);
  } finally {
    $('refresh').disabled = false;
    setStatus();
  }
}

$('refresh').addEventListener('click', resync);
$('freshNotice').addEventListener('click', applyPendingUpdate);

$('remoteForm').addEventListener('submit', event => {
  event.preventDefault();
  const host = $('remoteHost').value.trim(), port = Number($('remotePort').value);
  if (!host || !Number.isInteger(port) || port < 1 || port > 65535) return notice('填写有效的主机与端口', true);
  $('remoteHost').value = '';
  saveRemotes([...(state.remotes || []).filter(r => r.host !== host || r.port !== port), {host, port, enabled: true, usage: $('remoteUsage').checked, search: $('remoteSearch').checked, words: $('remoteWords').checked}], {host, port});
});

let lengthTimer;
for (const id of ['minLen', 'maxLen']) {
  $(id).addEventListener('input', () => {
    syncLenPreset();
    clearTimeout(lengthTimer);
    lengthTimer = setTimeout(() => {
      const val = $(id).value.trim();
      if (val !== '' && (!/^\d+$/.test(val) || Number(val) > 100000)) return;
      state.page = 1;
      state.term = '';
      load();
    }, 350);
  });
  $(id).addEventListener('change', () => {
    const el = $(id);
    if (el.value !== '') el.value = String(Math.max(0, Math.min(100000, Math.round(Number(el.value) || 0))));
    syncLenPreset();
  });
  $(id).addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      state.page = 1;
      state.term = '';
      load();
    }
  });
}

document.addEventListener('keydown', event => {
  if (event.key !== '/' || event.ctrlKey || event.metaKey || event.altKey || state.tab !== 'search') return;
  const active = document.activeElement;
  if (active && (/^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName) || active.isContentEditable)) return;
  event.preventDefault();
  $('q').focus();
  $('q').select();
});

$('remoteWords').addEventListener('change', () => { if ($('remoteWords').checked) $('remoteSearch').checked = true; });
$('remoteSearch').addEventListener('change', () => { if (!$('remoteSearch').checked) $('remoteWords').checked = false; });

if (window.ResizeObserver) {
  let resizeTimer;
  new ResizeObserver(() => {
    if (state.tab !== 'usage' || !state.usage) return;
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => renderCharts(), 80);
  }).observe($('bars'));
}

window.addEventListener('hashchange', () => {
  suppressHashSync = true;
  const {tab, pendingSession} = readHash();
  setTab(tab, {load: false});
  suppressHashSync = false;
  if (tab === 'sessions' && pendingSession) openSession(pendingSession, false);
  else {
    if (tab === 'sessions') showSessionList();
    load();
  }
});

initTheme();
const initialRoute = readHash();
setTab(initialRoute.tab, {load: false});

metadata().then(() => {
  suppressHashSync = true;
  const {tab, pendingSession} = readHash();
  suppressHashSync = false;
  if (tab === 'sessions' && pendingSession) return openSession(pendingSession, false);
  return load();
}).catch(error => notice(error.message, true));

setInterval(checkFresh, 60000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) checkFresh(); });
