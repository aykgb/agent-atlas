'use strict';
const $ = id => document.getElementById(id);
const colors = ['#6484d8', '#71b3a2', '#b49acb', '#e2b274', '#de8999', '#87acc8', '#a5b677', '#9299ad'];
const state = {tab: 'usage', page: 1, term: '', usage: null, request: 0};
const number = n => Number(n || 0).toLocaleString('en-US');
const compact = n => n >= 1e9 ? (n / 1e9).toFixed(2) + 'B' : n >= 1e6 ? (n / 1e6).toFixed(2) + 'M' : n >= 1e3 ? (n / 1e3).toFixed(1) + 'K' : number(n);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const empty = text => '<div class="empty">' + esc(text) + '</div>';
function notice(text = '', error = false) {
  $('notice').textContent = text;
  $('notice').classList.toggle('error', error);
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
async function metadata() {
  const data = await api('/api/meta');
  for (const [id, values, label] of [['agent', data.agents, '全部 agent'], ['model', data.models, '全部模型']]) {
    const selected = $(id).value;
    $(id).innerHTML = '<option value="">' + label + '</option>' + values.map(v => '<option value="' + esc(v) + '">' + esc(v) + '</option>').join('');
    $(id).value = selected;
  }
  $('updated').textContent = '更新于 ' + new Date(data.updated).toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
  $('coverage').textContent = number(data.sessions) + ' 个会话 · ' + number(data.turns) + ' 轮输入 · 数据仅保存在本机';
  $('warningList').textContent = data.warnings.length ? data.warnings.join('\n') : '各数据源读取完成，无解析异常。';
  if (data.warnings.length) $('warnings').querySelector('summary').textContent = '数据口径与读取提示（' + data.warnings.length + '）';
}
function renderUsage(data) {
  state.usage = data;
  const sum = key => data.rows.reduce((s, r) => s + r[key], 0);
  for (const [id, value] of [['total',sum('total')],['input',sum('new')],['cache',sum('cc')+sum('cr')],['output',sum('out')]]) {
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
  renderCharts();
}
function renderCharts() {
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
    let y=bottom;
    keys.forEach((key,j) => {
      const value=g.values[key], h=value/max*plot;
      if (!value) return;
      y-=h;
      const label=g.bucket+' · '+key+' · '+number(value)+' tokens';
      svg += '<rect tabindex="0" aria-label="'+esc(label)+'" x="'+(left+stride*i+(stride-bar)/2)+'" y="'+y+'" width="'+bar+'" height="'+h+'" fill="'+colors[j]+'"><title>'+esc(label)+'</title></rect>';
    });
    svg += '<text x="'+(left+stride*(i+.5))+'" y="251" text-anchor="middle">'+esc(g.bucket.length===7 ? g.bucket.slice(2) : g.bucket.slice(5))+'</text>';
  });
  $('bars').innerHTML = svg+'</svg>';
  $('barLegend').innerHTML = keys.map((k,i) => '<span><i class="swatch" style="background:'+colors[i]+'"></i>'+esc(k)+'</span>').join('');
  let degrees=0;
  const stops=keys.map((key,i) => {
    const value=key==='其他' ? ranked.slice(7).reduce((s,[,v])=>s+v,0) : totals.get(key);
    const start=degrees; degrees+=value/total*360;
    return colors[i]+' '+start+'deg '+degrees+'deg';
  });
  $('pie').innerHTML = '<div class="pie-wrap"><div class="donut" role="img" aria-label="用量占比，数值见下方图例" style="background:conic-gradient('+stops.join(',')+')"></div><div class="donut-center"><b>'+ranked.length+'</b><small>活跃分组</small></div></div>';
  $('pieLegend').innerHTML = keys.map((k,i) => {
    const v=k==='其他' ? ranked.slice(7).reduce((s,[,v])=>s+v,0) : totals.get(k);
    return '<div class="pie-row" title="'+number(v)+' tokens"><i class="swatch" style="background:'+colors[i]+'"></i><span class="pie-name">'+esc(k)+'</span><span>'+compact(v)+'</span><strong>'+(v/total*100).toFixed(1)+'%</strong></div>';
  }).join('');
}
async function loadTurn(detail, id) {
  if (!detail.open || detail.dataset.loaded) return;
  const body = detail.querySelector('.turn-body');
  body.textContent = '正在加载完整会话…';
  detail.dataset.loaded = 'loading';
  try {
    const turn = await api('/api/turn',{id});
    const categories = new Map();
    turn.output.forEach((part,index) => {
      if (!categories.has(part.kind)) categories.set(part.kind,[]);
      categories.get(part.kind).push({text:part.text, index:index+1});
    });
    body.innerHTML = '<details open><summary>用户输入</summary><pre>'+highlight(turn.input)+'</pre></details>' +
      [...categories.entries()].map(([kind,parts]) => '<details><summary>'+esc(kind)+' · '+parts.length+' 段</summary>' +
      parts.map(p => '<details><summary>第 '+p.index+' 段 · '+number(p.text.length)+' 字符</summary><pre>'+highlight(p.text)+'</pre></details>').join('')+'</details>').join('') +
      (!turn.output.length ? '<p>该轮尚无可见输出。</p>' : '') +
      '<div class="source">会话 '+esc(turn.session)+'<br>来源 '+esc(turn.source)+'</div>';
    detail.dataset.loaded = 'yes';
  } catch(error) {
    body.textContent = error.message + '；收起后重新展开可重试。'; delete detail.dataset.loaded;
  }
}
function renderSearch(data) {
  $('searchCount').textContent = number(data.total)+' 轮匹配'+(state.term ? ' · 输入词：'+state.term : ' · 空格分隔的关键词需全部命中');
  $('clearTerm').hidden = !state.term;
  $('results').innerHTML = data.rows.map(r => '<details class="turn" data-id="'+r.id+'"><summary><div class="turn-meta"><span class="badge">'+esc(r.agent)+'</span><span>'+esc(r.model)+'</span><span>'+esc(new Date(r.timestamp).toLocaleString('zh-CN'))+'</span></div><div class="preview">'+highlight(r.preview)+'</div>'+(r.match ? '<div class="match">'+highlight(r.match)+'</div>' : '')+'</summary><div class="turn-body"></div></details>').join('') || empty('没有找到匹配轮次。试试更短的关键词或扩大时间范围。');
  $('results').querySelectorAll('.turn').forEach(el => el.addEventListener('toggle',()=>loadTurn(el,el.dataset.id)));
  $('page').textContent = data.total ? data.page+' / '+data.pages : '0 / 0';
  $('prev').disabled = data.page <= 1; $('next').disabled = data.page >= data.pages;
}
function renderWords(data) {
  const maximum = Math.max(...data.words.map(w=>w.count),1);
  $('wordChart').innerHTML = data.words.map(w=>'<button class="word-row" data-term="'+esc(w.term)+'" title="'+esc(w.term)+'：'+number(w.count)+' 次 / '+number(w.turns)+' 轮"><span class="word-label">'+esc(w.term)+'</span><div class="word-track"><div class="word-fill" style="width:'+(w.count/maximum*100)+'%"></div></div><span class="word-count">'+number(w.count)+' / '+number(w.turns)+'</span></button>').join('') || empty('当前筛选下没有可统计的输入词语。');
  $('wordChart').querySelectorAll('button').forEach(button=>button.addEventListener('click',()=>{
    state.term=button.dataset.term; $('q').value=''; state.page=1; setTab('search');
  }));
}
async function load() {
  const request=++state.request, tab=state.tab;
  notice('正在读取…');
  try {
    if ($('start').value && $('end').value && $('start').value > $('end').value) throw new Error('开始日期不能晚于结束日期');
    let result;
    if (tab==='usage') result=await api('/api/usage',{agent:$('agent').value,model:$('model').value,n:$('n').value,unit:$('unit').value});
    else if (tab==='search') result=await api('/api/search',{...filters(),q:$('q').value,page:state.page,term:state.term});
    else result=await api('/api/words',{...filters(),limit:$('wordLimit').value});
    if (request!==state.request) return;
    ({usage:renderUsage,search:renderSearch,words:renderWords}[tab])(result);
    notice();
  } catch(error) {if(request===state.request) notice(error.message,true);}
}
function setTab(tab) {
  state.tab=tab;
  document.querySelectorAll('nav button').forEach(b=>{
    b.classList.toggle('active',b.dataset.tab===tab);
    if(b.dataset.tab===tab) b.setAttribute('aria-current','page'); else b.removeAttribute('aria-current');
  });
  for(const name of ['usage','search','words']) $(name).hidden=name!==tab;
  $('periodControls').hidden=tab!=='usage'; $('dateControls').hidden=tab==='usage';
  load();
}
document.querySelectorAll('nav button').forEach(b=>b.addEventListener('click',()=>setTab(b.dataset.tab)));
for(const id of ['agent','model','n','unit','start','end','wordLimit']) $(id).addEventListener('change',()=>{state.page=1;load();});
$('group').addEventListener('change',renderCharts);
$('searchForm').addEventListener('submit',e=>{e.preventDefault();state.page=1;state.term='';load();});
$('prev').addEventListener('click',()=>{state.page--;load();});
$('next').addEventListener('click',()=>{state.page++;load();});
$('clearTerm').addEventListener('click',()=>{state.term='';state.page=1;load();});
$('allDates').addEventListener('click',()=>{$('start').value='';$('end').value='';state.page=1;load();});
$('reset').addEventListener('click',()=>{
  for(const id of ['agent','model','start','end','q']) $(id).value='';
  $('n').value=14;$('unit').value='day';state.page=1;state.term='';load();
});
$('refresh').addEventListener('click',async()=>{
  $('refresh').disabled=true; $('refresh').textContent='正在更新…'; notice('正在重建本地索引，完成后更新页面…');
  try {await api('/api/refresh',{}, {method:'POST',headers:{'X-Stats-Request':'1'}});await metadata();state.page=1;await load();}
  catch(error){notice(error.message,true);}
  finally{$('refresh').disabled=false;$('refresh').textContent='↻ 刷新数据';}
});
metadata().then(load).catch(error=>notice(error.message,true));
