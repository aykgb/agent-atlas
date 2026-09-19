'use strict';
const $ = id => document.getElementById(id);
const colors = ['#6484d8', '#71b3a2', '#b49acb', '#e2b274', '#de8999', '#87acc8', '#a5b677', '#9299ad'];
const state = {tab: 'usage', page: 1, term: '', usage: null, excluded: [], request: 0, writable: true, remotes: [], sessionPage: 1, session: null};
const number = n => Number(n || 0).toLocaleString('en-US');
const compact = n => n >= 1e9 ? (n / 1e9).toFixed(2) + 'B' : n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'K' : number(n);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const empty = text => '<div class="empty">' + esc(text) + '</div>';
function notice(text = '', error = false) {
  $('notice').textContent = text;
  $('notice').classList.toggle('error', error);
}
function setStatus(text = '') {
  $('status').textContent = text;
}
const tip = $('tip');
function showTip(html, x, y) {
  if (tip.dataset.html !== html) { tip.innerHTML = html; tip.dataset.html = html; }
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
  container.querySelectorAll('[data-tip]').forEach(el => {
    el.addEventListener('mouseenter', event => showTip(el.dataset.tip, event.clientX, event.clientY));
    el.addEventListener('mousemove', event => showTip(el.dataset.tip, event.clientX, event.clientY));
    el.addEventListener('mouseleave', hideTip);
    el.addEventListener('focus', () => { const r = el.getBoundingClientRect(); showTip(el.dataset.tip, r.left + r.width / 2, r.top); });
    el.addEventListener('blur', hideTip);
  });
}
async function api(path, params = {}, options = {}) {
  const response = await fetch(path + (Object.keys(params).length ? '?' + new URLSearchParams(params) : ''), options);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '读取失败');
  return result;
}
function filters() {
  return Object.fromEntries(['agent', 'model', 'start', 'end'].map(k => [k, $(k).value]).filter(([,v]) => v));
}
function highlight(text) {
  const words = $('q').value.trim().split(/\s+/).filter(Boolean);
  if (!words.length) return esc(text);
  const expression = new RegExp('(' + words.map(w => w.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')', 'gi');
  return String(text).split(expression).map((part, i) => i % 2 ? '<mark>' + esc(part) + '</mark>' : esc(part)).join('');
}
function renderRemotes() {
  const list = state.remotes || [];
  const off = list.filter(r => !r.enabled).length;
  $('remotes').hidden = !state.writable;
  $('remoteSummary').textContent = list.length ? '（' + list.length + ' 台' + (off ? ' · 停用 ' + off : '') + '）' : '（未配置）';
  $('remoteList').innerHTML = list.map((r, index) => {
    const tags = [r.usage && '用量', r.search && '会话', r.words && '词频'].filter(Boolean).map(esc).join(' · ');
    const status = !r.enabled ? '<span class="subtle">已停用</span>' : r.status && r.status.error
      ? '<span class="remote-bad" title="' + esc(r.status.error) + '">同步失败</span>'
      : r.status && r.status.turns ? '<span class="remote-good">' + number(r.status.turns) + ' 轮已同步</span>'
      : '<span class="subtle">未同步</span>';
    const sync = r.enabled ? '<button class="text-button" data-sync="' + index + '">同步</button>' : '';
    return '<div class="remote-row"><label class="remote-switch"><input type="checkbox" data-toggle="' + index + '"' + (r.enabled ? ' checked' : '') + '> 启用</label><b>' + esc(r.host) + ':' + r.port + '</b><span class="subtle">' + tags + '</span>' + status + sync + '<button class="text-button" data-index="' + index + '">移除</button></div>';
  }).join('');
  $('remoteList').querySelectorAll('input[data-toggle]').forEach(box => box.addEventListener('change', () =>
    saveRemotes(state.remotes.map((r, index) => index === +box.dataset.toggle ? {...r, enabled: box.checked} : r))));
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
  } catch (error) {
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
  } catch (error) { notice(error.message, true); } finally { setStatus(); }
}
async function saveRemotes(list, target) {
  try {
    state.remotes = (await api('/api/remotes', {}, {method: 'POST', headers: {'X-Stats-Request': '1', 'Content-Type': 'application/json'}, body: JSON.stringify({remotes: list})})).remotes;
    renderRemotes();
    if (target) return syncRemotes(target);
    await metadata();
    state.page = 1;
    await load();
  } catch (error) { notice(error.message, true); }
}
const dataVersion = d => [d.updated, d.turns, d.sessions].join('|');
let knownVersion = null;
let checking = false;
async function checkFresh() {
  if (checking || document.hidden) return;
  checking = true;
  try {
    const data = await api('/api/meta');
    if (dataVersion(data) === knownVersion) return;
    await metadata(data);
    state.page = 1;
    await load();
  } catch (error) {
  } finally {
    checking = false;
  }
}
async function metadata(preloaded) {
  const data = preloaded || await api('/api/meta');
  knownVersion = dataVersion(data);
  for (const [id, values, label] of [['agent', data.agents, '全部 agent'], ['model', data.models, '全部模型']]) {
    const selected = $(id).value;
    $(id).innerHTML = '<option value="">' + label + '</option>' + values.map(v => '<option value="' + esc(v) + '">' + esc(v) + '</option>').join('');
    $(id).value = selected;
  }
  state.writable = data.writable !== false;
  $('refresh').hidden = !state.writable;
  $('excludeInput').hidden = !state.writable;
  $('excludeAdd').hidden = !state.writable;
  document.querySelector('.local').textContent = state.writable ? 'LOCAL' : 'REMOTE';
  if (state.writable) await loadRemotes(); else renderRemotes();
  $('updated').textContent = '更新于 ' + new Date(data.updated).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
  $('coverage').textContent = number(data.sessions) + ' 个会话 · ' + number(data.turns) + ' 轮输入 · 数据仅保存在本机' + (state.writable ? '' : ' · 远端只读');
  $('warningList').textContent = data.warnings.length ? data.warnings.join('\n') : '各数据源读取完成，无解析异常。';
  if (data.warnings.length) $('warnings').querySelector('summary').textContent = '数据口径与读取提示（' + data.warnings.length + '）';
}
const dateValue = d => d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
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
  $('start').value = start; $('end').value = end;
  $('agent').value = ''; $('model').value = '';
  if (key !== '其他' && group === 'agent') $('agent').value = key;
  if (key !== '其他' && group === 'model') $('model').value = key;
  if (key !== '其他' && group === 'pair') {
    const [agent, ...rest] = key.split(' / ');
    $('agent').value = agent; $('model').value = rest.join(' / ');
  }
  $('q').value = ''; state.term = ''; state.page = 1;
  setTab('search');
}
function renderRanking(id, entries, total) {
  if (!entries.length) { $(id).innerHTML = empty('这个时间范围还没有用量'); return; }
  const maximum = Math.max(...entries.map(([, value]) => value), 1);
  $(id).innerHTML = entries.map(([name, value], index) => {
    const share = (value / total * 100).toFixed(1);
    return '<div class="rank-row" title="' + esc(name) + ' · ' + number(value) + ' tokens · 占总量 ' + share + '%">' +
      '<span class="rank-index">' + (index + 1) + '</span><span class="rank-name">' + esc(name) + '</span>' +
      '<div class="rank-track"><div class="rank-fill" style="width:' + (value / maximum * 100).toFixed(1) + '%"></div></div>' +
      '<span class="rank-value">' + compact(value) + ' · ' + share + '%</span></div>';
  }).join('');
}
function renderUsage(data) {
  state.usage = data;
  const sum = key => data.rows.reduce((s, r) => s + r[key], 0);
  const total = sum('total');
  for (const [id, value] of [['total',total],['input',sum('new')],['cache',sum('cc')+sum('cr')],['output',sum('out')]]) {
    $(id).textContent = compact(value); $(id).title = number(value) + ' tokens';
  }
  $('range').textContent = data.start + ' — ' + data.end;
  $('cacheDetail').textContent = '读取 ' + compact(sum('cr')) + ' / 写入 ' + compact(sum('cc'));
  const rows = new Map();
  for (const row of data.rows) {
    const key = row.agent + '\0' + row.model;
    if (!rows.has(key)) rows.set(key, {...row, new:0,cc:0,cr:0,out:0,msgs:0,total:0});
    for (const field of ['new','cc','cr','out','msgs','total']) rows.get(key)[field] += row[field];
  }
  $('usageRows').innerHTML = [...rows.values()].sort((a,b) => b.total-a.total).map(r => '<tr><td><b>' + esc(r.model) + '</b><small>' + esc(r.agent) + '</small></td>' + ['msgs','new','cc','cr','out','total'].map(k => '<td>' + number(r[k]) + '</td>').join('') + '</tr>').join('') || '<tr><td colspan="7">当前范围没有用量记录，请调整时间或筛选。</td></tr>';
  const rank = field => {
    const totals = new Map();
    for (const r of rows.values()) totals.set(r[field], (totals.get(r[field]) || 0) + r.total);
    return [...totals.entries()].filter(([, value]) => value > 0).sort((a,b) => b[1]-a[1] || a[0].localeCompare(b[0]));
  };
  renderRanking('agentRank', rank('agent'), total);
  renderRanking('modelRank', rank('model'), total);
  renderCharts();
}
function renderCharts() {
  hideTip();
  const data = state.usage;
  if (!data) return;
  const grouping = $('group').value;
  const keyOf = row => grouping === 'pair' ? row.agent + ' / ' + row.model : row[grouping];
  const totals = new Map();
  for (const row of data.rows) totals.set(keyOf(row), (totals.get(keyOf(row)) || 0) + row.total);
  const ranked = [...totals.entries()].filter(([,v]) => v > 0).sort((a,b) => b[1]-a[1]);
  const top = ranked.slice(0, 7).map(([key]) => key);
  const keys = ranked.length > 7 ? [...top, '其他'] : top;
  const groups = data.buckets.map(b => ({bucket:b, values:Object.fromEntries(keys.map(k => [k,0]))}));
  const lookup = new Map(groups.map(g => [g.bucket,g]));
  for (const row of data.rows) {
    const k = top.includes(keyOf(row)) ? keyOf(row) : '其他';
    if (lookup.get(row.bucket) && row.total) lookup.get(row.bucket).values[k] += row.total;
  }
  const total = ranked.reduce((s,[,v]) => s+v,0);
  if (!total) {
    $('bars').innerHTML = empty('这个时间范围还没有用量');
    $('pie').innerHTML = empty('暂无占比数据');
    $('barLegend').innerHTML = ''; $('pieLegend').innerHTML = ''; return;
  }
  const width = Math.max(600,groups.length*34+70), height=260, left=56, bottom=228, plot=196;
  const max = Math.max(...groups.map(g => Object.values(g.values).reduce((a,b) => a+b,0)),1)*1.08;
  let svg = '<svg viewBox="0 0 '+width+' '+height+'" style="min-width:'+width+'px" role="img" aria-label="按周期和分组显示 token 用量的堆叠柱状图">';
  for (let i=0;i<=4;i++) {
    const y=bottom-i*plot/4;
    svg += '<line x1="'+left+'" x2="'+(width-10)+'" y1="'+y+'" y2="'+y+'" stroke="#edf0f4"/><text x="'+(left-10)+'" y="'+(y+4)+'" text-anchor="end">'+compact(max*i/4)+'</text>';
  }
  const stride=(width-left-10)/groups.length, bar=Math.min(30,stride*.62);
  groups.forEach((g,i) => {
    const bucketTotal=Object.values(g.values).reduce((a,b) => a+b,0);
    let y=bottom;
    keys.forEach((key,j) => {
      const value=g.values[key], h=value/max*plot;
      if (!value) return;
      y-=h;
      const label=g.bucket+' · '+key+' · '+number(value)+' tokens';
      const detail=esc(g.bucket)+' · '+esc(key)+'<br>'+number(value)+' tokens · 占该周期 '+(value/bucketTotal*100).toFixed(1)+'%';
      svg += '<rect tabindex="0" aria-label="'+esc(label)+'" data-tip="'+detail+'" data-bucket="'+esc(g.bucket)+'" data-key="'+esc(key)+'" data-group="'+esc(grouping)+'" x="'+(left+stride*i+(stride-bar)/2)+'" y="'+y+'" width="'+bar+'" height="'+h+'" fill="'+colors[j]+'"></rect>';
    });
    svg += '<text x="'+(left+stride*(i+.5))+'" y="251" text-anchor="middle">'+esc(g.bucket.length===7 ? g.bucket.slice(2) : g.bucket.slice(5))+'</text>';
  });
  $('bars').innerHTML = svg+'</svg>';
  $('barLegend').innerHTML = keys.map((k,i) => '<span><i class="swatch" style="background:'+colors[i]+'"></i>'+esc(k)+'</span>').join('');
  let start=0;
  const slices=keys.map((key,i) => {
    const value=key==='其他' ? ranked.slice(7).reduce((s,[,v])=>s+v,0) : totals.get(key);
    const share=value/total*100, label=key+' · '+number(value)+' tokens · 占总量 '+share.toFixed(1)+'%';
    const slice='<circle tabindex="0" aria-label="'+esc(label)+'" data-tip="'+esc(key)+'<br>'+number(value)+' tokens · 占总量 '+share.toFixed(1)+'%" cx="21" cy="21" r="17.375" fill="none" stroke="'+colors[i]+'" stroke-width="7.25" pathLength="100" stroke-dasharray="0 '+start+' '+share+' 100" transform="rotate(-90 21 21)"></circle>';
    start+=share;
    return slice;
  }).join('');
  $('pie').innerHTML = '<div class="pie-wrap"><svg class="donut" viewBox="0 0 42 42" role="img" aria-label="用量占比，共 '+ranked.length+' 个活跃分组">'+slices+'</svg><div class="donut-center"><b>'+ranked.length+'</b><small>活跃分组</small></div></div>';
  bindTips($('bars')); bindTips($('pie'));
  $('pieLegend').innerHTML = keys.map((k,i) => {
    const v=k==='其他' ? ranked.slice(7).reduce((s,[,v])=>s+v,0) : totals.get(k);
    return '<div class="pie-row" title="'+number(v)+' tokens"><i class="swatch" style="background:'+colors[i]+'"></i><span class="pie-name">'+esc(k)+'</span><span>'+compact(v)+'</span><strong>'+(v/total*100).toFixed(1)+'%</strong></div>';
  }).join('');
}
const OUTPUT_LABELS = {'思考':'thinking','工具调用':'tool call','工具结果':'tool result','正文':'text','上下文':'context','过程':'process'};
async function loadTurn(detail, id) {
  if (!detail.open || detail.dataset.loaded) return;
  const body = detail.querySelector('.turn-body');
  body.textContent = '正在加载完整会话…';
  detail.dataset.loaded = 'loading';
  try {
    const turn = await api('/api/turn',{id});
    const parts = turn.output.map((part,index) => '<details class="part"><summary><span>第 '+(index+1)+' 段 · '+number(part.text.length)+' 字符</span><span class="kind-tag">'+esc(OUTPUT_LABELS[part.kind]||part.kind)+'</span></summary><pre>'+highlight(part.text)+'</pre></details>').join('');
    body.innerHTML = '<details open><summary>用户输入</summary><pre>'+highlight(turn.input)+'</pre></details>' +
      (turn.output.length ? '<details class="response"><summary>Agent 响应 · '+turn.output.length+' 段</summary><div class="response-body">'+parts+'</div></details>' : '<p>该轮尚无可见输出。</p>') +
      '<div class="source">会话 '+esc(turn.session)+'<br>来源 '+esc(turn.source)+'</div>';
    detail.dataset.loaded = 'yes';
  } catch(error) {
    body.textContent = error.message + '；收起后重新展开可重试。'; delete detail.dataset.loaded;
  }
}
function showSessionList() {
  state.session = null;
  $('sessionListView').hidden = false;
  $('sessionDetail').hidden = true;
}
function renderSessions(data) {
  const rows = data.rows || [];
  $('sessionCount').textContent = number(data.total)+' 个会话 · 按创建时间倒序'+(data.total ? ' · 第 '+data.page+' / '+data.pages+' 页' : '');
  $('sessionList').innerHTML = rows.map((s,index) =>
    '<button class="session-row" data-index="'+index+'"><span class="badge">'+esc(s.agent)+'</span>'+
    '<span class="session-name">'+esc(s.session)+'</span><span class="subtle">'+number(s.turns)+' 轮</span>'+
    '<span class="subtle">'+esc(s.label)+'</span><span class="session-date">'+esc(s.first ? new Date(s.first).toLocaleString('zh-CN') : '')+'</span></button>').join('')
    || empty('当前筛选下没有会话。');
  $('sessionList').querySelectorAll('.session-row').forEach(button =>
    button.addEventListener('click',()=>openSession(rows[+button.dataset.index])));
  $('sessionPage').textContent = data.total ? data.page+' / '+data.pages : '0 / 0';
  $('sessionPrev').disabled = data.page <= 1; $('sessionNext').disabled = data.page >= data.pages;
}
async function openSession(target) {
  setStatus('读取会话…');
  try {
    const data = await api('/api/session',{key:target.key,agent:target.agent,session:target.session});
    state.session = data;
    $('sessionListView').hidden = true;
    $('sessionDetail').hidden = false;
    $('sessionMeta').innerHTML = '<span class="badge">'+esc(data.agent)+'</span><b>'+esc(data.session)+'</b>'+
      '<span class="subtle">'+number(data.turns.length)+' 轮 · '+esc(new Date(data.first).toLocaleString('zh-CN'))+' — '+esc(new Date(data.last).toLocaleString('zh-CN'))+'</span>'+
      '<span class="subtle">来源 '+esc(data.source)+'</span>';
    $('sessionTurns').innerHTML = data.turns.map(t => '<details class="turn" data-id="'+t.id+'"><summary><div class="turn-meta"><span class="badge">'+esc(data.agent)+'</span><span>'+esc(t.model)+'</span><span>'+esc(new Date(t.timestamp).toLocaleString('zh-CN'))+'</span></div><div class="preview">'+highlight(t.preview)+'</div></summary><div class="turn-body"></div></details>').join('') || empty('该会话没有可显示的轮次。');
    $('sessionTurns').querySelectorAll('.turn').forEach(el => el.addEventListener('toggle',()=>loadTurn(el,el.dataset.id)));
    notice(); setStatus();
  } catch(error) { notice(error.message, true); setStatus(); }
}
function renderSearch(data) {
  const min=$('minLen').value, max=$('maxLen').value;
  const length = min && max ? ' · 长度 '+min+'–'+max+' 字' : min ? ' · 长度 ≥'+min+' 字' : max ? ' · 长度 ≤'+max+' 字' : '';
  const hint = state.term ? ' · 输入词：'+state.term : $('q').value.trim() ? ' · 空格分隔的关键词需全部命中' : ' · 按筛选条件匹配';
  $('searchCount').textContent = number(data.total)+' 轮匹配'+hint+length;
  $('clearTerm').hidden = !state.term;
  $('results').innerHTML = data.rows.map(r => '<details class="turn" data-id="'+r.id+'"><summary><div class="turn-meta"><span class="badge">'+esc(r.agent)+'</span><span>'+esc(r.model)+'</span><span>'+esc(new Date(r.timestamp).toLocaleString('zh-CN'))+'</span><button class="text-button session-link" data-id="'+esc(r.id)+'" data-agent="'+esc(r.agent)+'" data-session="'+esc(r.session)+'">完整会话</button></div><div class="preview">'+highlight(r.preview)+'</div>'+(r.match ? '<div class="match">'+highlight(r.match)+'</div>' : '')+'</summary><div class="turn-body"></div></details>').join('') || empty('没有找到匹配轮次。试试更短的关键词或扩大时间范围。');
  $('results').querySelectorAll('.turn').forEach(el => el.addEventListener('toggle',()=>loadTurn(el,el.dataset.id)));
  $('results').querySelectorAll('.session-link').forEach(button => button.addEventListener('click',event => {
    event.preventDefault(); event.stopPropagation();
    const key = button.dataset.id.split(':')[0];
    setTab('sessions');
    openSession({key, agent: button.dataset.agent, session: button.dataset.session});
  }));
  $('page').textContent = data.total ? data.page+' / '+data.pages : '0 / 0';
  $('prev').disabled = data.page <= 1; $('next').disabled = data.page >= data.pages;
}
function renderWords(data) {
  state.excluded = data.excluded || [];
  const maximum = Math.max(...data.words.map(w=>w.count),1);
  $('wordChart').innerHTML = data.words.map(w=>'<div class="word-item"><button class="word-row" data-term="'+esc(w.term)+'" title="'+esc(w.term)+'：'+number(w.count)+' 次 / '+number(w.turns)+' 轮"><span class="word-label">'+esc(w.term)+'</span><div class="word-track"><div class="word-fill" style="width:'+(w.count/maximum*100)+'%"></div></div><span class="word-count">'+number(w.count)+' / '+number(w.turns)+'</span></button>'+(state.writable?'<button class="word-exclude" data-term="'+esc(w.term)+'" title="排除「'+esc(w.term)+'」" aria-label="排除 '+esc(w.term)+'">×</button>':'')+'</div>').join('') || empty('当前筛选下没有可统计的输入词语。');
  $('wordChart').querySelectorAll('.word-row').forEach(button=>button.addEventListener('click',()=>{
    state.term=button.dataset.term; $('q').value=''; state.page=1; setTab('search');
  }));
  $('wordChart').querySelectorAll('.word-exclude').forEach(button=>button.addEventListener('click',()=>saveExcluded([...state.excluded,button.dataset.term])));
  $('excludedWords').innerHTML = state.excluded.map(term=>state.writable
    ? '<button class="chip" data-term="'+esc(term)+'" title="点击恢复「'+esc(term)+'」">'+esc(term)+' ×</button>'
    : '<span class="chip" title="远端只读，无法修改">'+esc(term)+'</span>').join('');
  $('excludedWords').querySelectorAll('button.chip').forEach(chip=>chip.addEventListener('click',()=>saveExcluded(state.excluded.filter(term=>term!==chip.dataset.term))));
}
async function saveExcluded(words) {
  try {
    const result = await api('/api/words-exclude',{}, {method:'POST',headers:{'X-Stats-Request':'1','Content-Type':'application/json'},body:JSON.stringify({words})});
    state.excluded = result.excluded;
    await load();
  } catch(error) {notice(error.message,true);}
}
function addExcluded() {
  const words=$('excludeInput').value.split(/[\s,，、]+/).filter(Boolean);
  if (!words.length) return;
  $('excludeInput').value='';
  saveExcluded([...state.excluded,...words]);
}
async function load() {
  const request=++state.request, tab=state.tab;
  hideTip();
  setStatus('读取中…');
  try {
    if ($('start').value && $('end').value && $('start').value > $('end').value) throw new Error('开始日期不能晚于结束日期');
    let result;
    if (tab==='usage') result=await api('/api/usage',{agent:$('agent').value,model:$('model').value,n:$('n').value,unit:$('unit').value});
    else if (tab==='search') result=await api('/api/search',{...filters(),q:$('q').value,page:state.page,term:state.term,minlen:$('minLen').value,maxlen:$('maxLen').value});
    else if (tab==='sessions') result=await api('/api/sessions',{agent:$('agent').value,page:state.sessionPage});
    else result=await api('/api/words',{...filters(),limit:$('wordLimit').value,order:$('wordOrder').value});
    if (request!==state.request) return;
    ({usage:renderUsage,search:renderSearch,sessions:renderSessions,words:renderWords}[tab])(result);
    notice(); setStatus();
  } catch(error) {if(request===state.request){notice(error.message,true);setStatus();}}
}
function updateWordLimitLabels() {
  const prefix = $('wordOrder').value === 'asc' ? '末 ' : '前 ';
  for (const opt of $('wordLimit').options) opt.textContent = prefix + opt.value + ' 词';
}
function setTab(tab) {
  state.tab=tab;
  document.querySelectorAll('nav button').forEach(b=>{
    b.classList.toggle('active',b.dataset.tab===tab);
    if(b.dataset.tab===tab) b.setAttribute('aria-current','page'); else b.removeAttribute('aria-current');
  });
  for(const name of ['usage','search','sessions','words']) $(name).hidden=name!==tab;
  $('periodControls').hidden=tab!=='usage'; $('dateControls').hidden=tab!=='search'&&tab!=='words';
  $('model').closest('label').hidden=tab==='sessions';
  if(tab==='sessions') showSessionList();
  load();
}
document.querySelectorAll('nav button').forEach(b=>b.addEventListener('click',()=>setTab(b.dataset.tab)));
for(const id of ['agent','model','n','unit','start','end','wordLimit','minLen','maxLen']) $(id).addEventListener('change',()=>{state.page=1;state.sessionPage=1;load();});
$('wordOrder').addEventListener('change',()=>{updateWordLimitLabels();load();});
$('group').addEventListener('change',renderCharts);
$('bars').addEventListener('dblclick',event=>{
  const rect=event.target.closest('rect');
  if(rect) drilldown(rect.dataset.bucket,rect.dataset.key,rect.dataset.group);
});
$('searchForm').addEventListener('submit',e=>{e.preventDefault();state.page=1;state.term='';load();});
$('prev').addEventListener('click',()=>{state.page--;load();});
$('next').addEventListener('click',()=>{state.page++;load();});
$('sessionPrev').addEventListener('click',()=>{state.sessionPage--;load();});
$('sessionNext').addEventListener('click',()=>{state.sessionPage++;load();});
$('sessionBack').addEventListener('click',()=>{showSessionList();});
$('clearTerm').addEventListener('click',()=>{state.term='';state.page=1;load();});
$('excludeAdd').addEventListener('click',addExcluded);
$('excludeInput').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();addExcluded();}});
$('allDates').addEventListener('click',()=>{$('start').value='';$('end').value='';state.page=1;load();});
$('reset').addEventListener('click',()=>{
  for(const id of ['agent','model','start','end','q','minLen','maxLen']) $(id).value='';
  $('n').value=14;$('unit').value='day';state.page=1;state.term='';load();
});
async function resync() {
  $('refresh').disabled=true; setStatus('重建索引…');
  try {await api('/api/refresh',{}, {method:'POST',headers:{'X-Stats-Request':'1'}});await metadata();state.page=1;await load();}
  catch(error){notice(error.message,true);}
  finally{$('refresh').disabled=false;setStatus();}
}
$('refresh').addEventListener('click',resync);
$('remoteForm').addEventListener('submit',event=>{
  event.preventDefault();
  const host=$('remoteHost').value.trim(), port=Number($('remotePort').value);
  if (!host || !Number.isInteger(port) || port<1 || port>65535) return notice('填写有效的主机与端口',true);
  $('remoteHost').value='';
  saveRemotes([...(state.remotes||[]).filter(r=>r.host!==host||r.port!==port),{host,port,enabled:true,usage:$('remoteUsage').checked,search:$('remoteSearch').checked,words:$('remoteWords').checked}],{host,port});
});
let lengthTimer;
for(const id of ['minLen','maxLen']) $(id).addEventListener('input',()=>{
  clearTimeout(lengthTimer);
  lengthTimer=setTimeout(()=>{state.page=1;state.term='';load();},350);
});
for(const id of ['minLen','maxLen']) $(id).addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();state.page=1;state.term='';load();}});
$('remoteWords').addEventListener('change',()=>{if($('remoteWords').checked)$('remoteSearch').checked=true;});
$('remoteSearch').addEventListener('change',()=>{if(!$('remoteSearch').checked)$('remoteWords').checked=false;});
metadata().then(load).catch(error=>notice(error.message,true));
setInterval(checkFresh,60000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)checkFresh();});
