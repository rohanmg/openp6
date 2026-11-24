/* openP6 web client — P6-style scheduler with editing */
const S = {
  projects: [], key: null, data: null, sel: null,
  ppd: 8, criticalOnly: false, q: '', tab: 'general', navtab: 'projects',
};
const COLS = 640;
const fmt = s => s || '';
const day = s => s ? Math.floor(new Date(s.replace(' ', 'T') + ':00').getTime() / 864e5) : 0;
const MON = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const $ = id => document.getElementById(id);
const el = (t, c, h) => { const e = document.createElement(t); if (c) e.className = c;
  if (h != null) e.innerHTML = h; return e; };
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const TYPE_LABEL = {TT_Task:'Task', TT_Mile:'Start Milestone', TT_FinMile:'Finish Milestone',
  TT_Rsrc:'Resource Dependent', TT_LOE:'Level of Effort', TT_WBS:'WBS Summary'};
const CSTR = {'':'— none —', CS_MSO:'Start On', CS_MSOA:'Start On or After', CS_SNET:'Start No Earlier Than',
  CS_MFO:'Finish On', CS_MEO:'Finish On or Before', CS_FNLT:'Finish No Later Than',
  CS_ALAP:'As Late As Possible', CS_MANDSTART:'Mandatory Start', CS_MANDFIN:'Mandatory Finish'};

function toast(m, bad){ const t = el('div','toast' + (bad?' bad':''), esc(m));
  document.body.append(t); requestAnimationFrame(()=>t.classList.add('show'));
  setTimeout(()=>{ t.classList.remove('show'); setTimeout(()=>t.remove(),300); },2600); }

async function api(path, opts={}){
  const o = Object.assign({}, opts, {headers: Object.assign({}, opts.headers||{})});
  if (o.body && typeof o.body === 'object' && !(o.body instanceof FormData)){
    o.headers['Content-Type'] = 'application/json';
    o.body = JSON.stringify(o.body);
  }
  if (S.data && S.data.rev != null && (o.method || 'GET') !== 'GET')
    o.headers['X-OpenP6-Rev'] = String(S.data.rev);
  const r = await fetch(path, o);
  if (r.status === 401){ location.href = '/login'; throw new Error('auth'); }
  if (!r.ok){ let m = await r.text(); try { m = JSON.parse(m).detail || m; } catch(e){}
    throw new Error(String(m).slice(0,200)); }
  return r.headers.get('content-type')?.includes('json') ? r.json() : r.text();
}

/* -------- projects -------- */
async function loadProjects(){
  S.projects = await api('/api/projects');
  renderNav();
  if (!S.key && S.projects.length) selectProject(S.projects[0].key);
}
async function selectProject(key, keepSel){
  S.key = key;
  if (!keepSel) S.sel = null;
  setStatus('Loading…');
  S.data = await api('/api/projects/' + encodeURIComponent(key));
  const gw = document.querySelector('.gridwrap');
  S.ppd = Math.max(1.2, Math.max(gw.clientWidth - COLS - 10, 260) / Math.max(spanDays(), 1));
  render(); renderDetails();
}
async function refresh(){ if (S.key) await selectProject(S.key, true); }
async function mutate(fn, okMsg){ try { await fn(); await loadProjects(); await refresh();
  if (okMsg) toast(okMsg); }
  catch(e){ toast(e.message, true); await refresh().catch(()=>{}); } }

async function doUndo(){ if (!S.key) return;
  try { S.data = await api(`/api/projects/${encodeURIComponent(S.key)}/undo`, {method:'POST'});
    await loadProjects(); render(); renderDetails(); toast('Undone'); }
  catch(e){ toast(e.message, true); await refresh().catch(()=>{}); } }
async function doRedo(){ if (!S.key) return;
  try { S.data = await api(`/api/projects/${encodeURIComponent(S.key)}/redo`, {method:'POST'});
    await loadProjects(); render(); renderDetails(); toast('Redone'); }
  catch(e){ toast(e.message, true); await refresh().catch(()=>{}); } }

function counts(){
  const a = S.data?.activities || [];
  return { activities:a.length, critical:a.filter(x=>x.critical).length,
    milestones:a.filter(x=>x.milestone).length,
    relationships:a.reduce((n,x)=>n+(x.depends?.length||0),0),
    resources:(S.data?.resources||[]).length };
}
function spanDays(){
  const a = S.data?.activities || [];
  const ds = a.map(x=>day(x.start)).filter(Boolean), fs = a.map(x=>day(x.finish)).filter(Boolean);
  if (!ds.length) return 30;
  return Math.max(...fs) - Math.min(...ds) + 1;
}
function origin(){ const ds = (S.data?.activities||[]).map(x=>day(x.start)).filter(Boolean);
  return ds.length ? Math.min(...ds) : (day(S.data?.start||'') || 0); }
function wbsName(id){ const w=(S.data?.wbs||[]).find(w=>w.id===id); return w? w.name : '—'; }

/* -------- navigator -------- */
function renderNav(){
  const b = $('navbody'); b.innerHTML = '';
  if (S.navtab === 'projects'){
    const ul = el('ul','tree');
    S.projects.forEach(p => {
      const li = el('li', p.key===S.key?'sel':'',
        `<span class="tcode">${esc(p.short_name||'')}</span>${esc(p.name)}` +
        `<span class="pcount">${p.counts?.activities ?? ''}</span>`);
      li.onclick = () => { selectProject(p.key); closeNav(); };
      ul.append(li);
    });
    if (!S.projects.length) b.append(el('div','empty','No schedules yet — import a .xer or create one'));
    b.append(ul);
  } else if (S.navtab === 'resources'){
    const ul = el('ul','tree');
    (S.data?.resources||[]).forEach(r => ul.append(el('li','',
      `<span class="tcode">${esc(r.code||r.id)}</span>${esc(r.name)}`)));
    b.append(S.data? ul : el('div','empty','Open a schedule first'));
  } else {
    const c = S.data?.calendar;
    b.append(c ? el('ul','tree', `<li><span class="tcode">${esc(c.hpd)}h</span>${esc(c.name)}</li>`)
               : el('div','empty','Open a schedule first'));
  }
}

/* -------- grid -------- */
const HEADH = 38, H_BAND = 21, H_ROW = 23, BUFFER = 400;
const ROWH = {band: H_BAND, row: H_ROW};

function render(){
  if (!S.data){ $('grid').innerHTML=''; $('crumbs').textContent='No project selected'; return; }
  const d = S.data, c = counts();
  $('crumbs').innerHTML = `<b>${esc(d.short_name)}</b> — ${esc(d.name)} · data date ${esc(fmt(d.data_date))} · finish ${esc(fmt(d.finish))}`;
  const g = $('grid'); g.innerHTML='';
  const o = origin(); const tlw = spanDays() * S.ppd;
  g.style.width = (COLS + tlw) + 'px';

  const head = el('div','ghead');
  head.append(hcell('c-id','Activity ID'), hcell('c-name','Activity Name'),
    hcell('c-dur','Dur (d)'), hcell('c-date','Start'), hcell('c-date','Finish'),
    hcell('c-tf','Total Float'), hcell('c-pct','% Comp'));
  const htl = el('div','htimeline');
  ticks(o, tlw).forEach(t => { const l = el('div','tlabel', t.label); l.style.left = t.x+'px'; htl.append(l); });
  head.append(htl); g.append(head);

  // ---- flat item list (WBS band, then its activities) ----
  let acts = d.activities;
  if (S.criticalOnly) acts = acts.filter(a => a.critical);
  if (S.q) acts = acts.filter(a => `${a.code} ${a.name}`.toLowerCase().includes(S.q));
  const items = [];
  groupBy(acts).forEach(grp => {
    items.push({type:'band', grp});
    grp.acts.forEach(a => items.push({type:'row', act:a}));
  });
  const offs = []; let y = 0;
  for (const it of items){ offs.push(y); y += ROWH[it.type]; }
  S.items = items; S.offs = offs; S.origin = o; S.range = null;

  const body = el('div','gridbody');
  body.style.height = Math.max(y, 1) + 'px';
  g.append(body); S.body = body;
  if (items.length) paint(true);
  else body.append(el('div','empty', S.q ? 'No activity matches that search.'
                                           : 'No activities yet — use “+ Activity”.'));
  const shown = acts.length !== d.activities.length ? ` · showing ${acts.length}/${d.activities.length}` : '';
  setStatus(`${c.activities} activities · ${c.critical} critical · ${c.relationships} relationships · ${d.calendar?.hpd||8}h/day · rev ${d.rev||1}${shown}`);
}

/* virtual scrolling: only the visible slice gets DOM nodes */
function firstVisible(top){
  let lo = 0, hi = S.items.length;
  while (lo < hi){ const m = (lo + hi) >> 1;
    if (S.offs[m] + ROWH[S.items[m].type] <= top) lo = m + 1; else hi = m; }
  return lo;
}
function paint(force){
  const gw = document.querySelector('.gridwrap');
  if (!S.body || !S.items || !S.items.length) return;
  const top = Math.max(0, gw.scrollTop - HEADH - BUFFER);
  const bot = gw.scrollTop + gw.clientHeight + BUFFER;
  const start = firstVisible(top);
  let end = start;
  while (end < S.items.length && S.offs[end] < bot) end++;
  if (!force && S.range && S.range[0] === start && S.range[1] === end) return;
  S.range = [start, end];
  const frag = document.createDocumentFragment();
  for (let i = start; i < end; i++) frag.append(vrow(i));
  S.body.replaceChildren(frag);
}
function vrow(i){
  const it = S.items[i];
  const node = it.type === 'band' ? bandRow(it.grp, S.origin) : actRow(it.act, S.origin);
  node.style.position = 'absolute'; node.style.left = '0'; node.style.right = '0';
  node.style.top = S.offs[i] + 'px'; node.style.height = ROWH[it.type] + 'px';
  node.style.overflow = 'hidden';
  return node;
}
function hcell(c,t){ return el('div','hcell cell ' + c, t); }
function ticks(o, tlw){
  const out=[]; const step = S.ppd < 3.2 ? 28 : 7;
  let cur = new Date(o*864e5);
  if (step===7) cur.setDate(cur.getDate() - ((cur.getDay()+6)%7));
  else cur = new Date(cur.getFullYear(), cur.getMonth(), 1);
  let x=0, guard=0;
  while (x <= tlw + 40 && guard++ < 250){
    const diff = Math.round(cur.getTime()/864e5) - o; x = diff * S.ppd;
    if (x >= -20) out.push({x, label: step===7 ? `${cur.getDate()} ${MON[cur.getMonth()]}`
                                               : `${MON[cur.getMonth()]} ${String(cur.getFullYear()).slice(2)}`});
    const n = new Date(cur); n.setDate(n.getDate()+step); cur = n;
  }
  return out;
}
function groupBy(acts){
  const order=[], map=new Map(); const seq={}; (S.data.wbs||[]).forEach((w,i)=>seq[w.id]=i);
  acts.forEach(a=>{ const k=a.wbs_id||'none';
    if(!map.has(k)){ const w=(S.data.wbs||[]).find(x=>x.id===k)||{};
      map.set(k,{id:k, code:w.code||'', name:wbsName(k), acts:[]}); order.push(k); }
    map.get(k).acts.push(a); });
  return order.map(k=>map.get(k)).sort((x,y)=>(seq[x.id]??99)-(seq[y.id]??99));
}
function cell(c,v){ return el('div','cell '+c, esc(v)); }
function actRow(a,o){
  const r = el('div','arow'+(S.sel===a.id?' sel':''));
  r.onclick = () => { S.sel = a.id; render(); renderDetails(); };
  r.append(cell('c-id'+(a.critical?' crit':''), a.code), cell('c-name', a.name),
    cell('c-dur', (a.dur_d||0) ? (+a.dur_d.toFixed(1)) : '0'),
    cell('c-date', fmt(a.start)), cell('c-date', fmt(a.finish)),
    cell('c-tf'+(a.critical?' crit':''), (+(a.total_float_h||0).toFixed(0))),
    cell('c-pct', a.pct ? a.pct + '%' : ''));
  r.append(track(a,o)); return r;
}
function track(a,o){
  const t = el('div','track');
  const x0=(day(a.start)-o)*S.ppd, x1=(day(a.finish)-o)*S.ppd, xf=(day(a.late_finish)-o)*S.ppd;
  if (xf > x1+1){ const f=el('div','floatbar'); f.style.left=x1+'px'; f.style.width=(xf-x1)+'px'; t.append(f); }
  if (a.milestone){ const b=el('div','bar mile'+(a.critical?' crit':'')); b.style.left=x0+'px'; b.title=a.name; t.append(b); }
  else { const b=el('div','bar'+(a.critical?' crit':'')); b.style.left=x0+'px';
    b.style.width=Math.max(x1-x0,2)+'px'; b.title=`${a.code} ${a.name}`;
    const pct=parseFloat(a.pct)||0; if (pct>0){ const p=el('div','prog'); p.style.width=pct+'%'; b.append(p); }
    t.append(b); }
  return t;
}
function bandRow(grp,o){
  const r = el('div','band');
  const ds=grp.acts.map(a=>day(a.start)), fs=grp.acts.map(a=>day(a.finish));
  const x0=(Math.min(...ds)-o)*S.ppd, x1=(Math.max(...fs)-o)*S.ppd;
  r.append(el('div','cell c-id',esc(grp.code)), el('div','cell c-name',esc(grp.name)),
    el('div','cell c-dur'), el('div','cell c-date'), el('div','cell c-date'),
    el('div','cell c-tf'), el('div','cell c-pct'));
  const t=el('div','track'); const b=el('div','bw');
  b.style.left=x0+'px'; b.style.width=Math.max(x1-x0,2)+'px'; t.append(b); r.append(t);
  return r;
}

/* -------- details (editable) -------- */
const TABS = ['General','Status','Relationships','Resources','Codes','Project'];
function renderDetails(){
  const tabs = $('dtabs'); tabs.innerHTML='';
  TABS.forEach(t => { const b = el('button', t.toLowerCase()===S.tab?'active':'', t);
    b.onclick = () => { S.tab = t.toLowerCase(); renderDetails(); }; tabs.append(b); });
  const a = S.data?.activities.find(x=>String(x.id)===String(S.sel));
  const d = $('dbody');
  if (S.tab === 'project'){ d.innerHTML = projectForm(); wireProject(); return; }
  if (!a){ d.innerHTML = '<div class="empty">Select an activity to see and edit its details.</div>'; return; }
  if (S.tab==='general')            d.innerHTML = generalForm(a);
  else if (S.tab==='status')        d.innerHTML = statusForm(a);
  else if (S.tab==='relationships') d.innerHTML = relTab(a);
  else if (S.tab==='resources')     d.innerHTML = resTab(a);
  else if (S.tab==='codes')         d.innerHTML = codesTab(a);
  wire(a);
}
const inp = (id,val,ph) => `<input class="fin" id="${id}" value="${esc(val??'')}" placeholder="${esc(ph||'')}">`;
const sel = (id,opts,val) => `<select class="fin" id="${id}">` +
  opts.map(([v,l])=>`<option value="${esc(v)}"${String(v)===String(val)?' selected':''}>${esc(l)}</option>`).join('') + `</select>`;

function generalForm(a){
  const wbsOpts = (S.data.wbs||[]).map(w=>[w.id, `${w.code} — ${w.name}`]);
  return `<div class="editgrid">
    <label>Activity ID ${inp('f_code', a.code)}</label>
    <label class="wide">Activity Name ${inp('f_name', a.name)}</label>
    <label>Duration (h) ${inp('f_dur', a.dur_h, '8 = 1 day')}</label>
    <label>% Complete ${inp('f_pct', a.pct, '0–100')}</label>
    <label>Type ${sel('f_type', Object.entries(TYPE_LABEL), a.type)}</label>
    <label>WBS ${sel('f_wbs', wbsOpts.length?wbsOpts:[['','—']], a.wbs_id)}</label>
    <label>Constraint ${sel('f_cstr', Object.entries(CSTR), a.cstr_type)}</label>
    <label>Constraint date ${inp('f_cstrdate', a.cstr_date, 'YYYY-MM-DD HH:MM')}</label>
  </div>
  <div class="computed">Start <b>${esc(fmt(a.start))}</b> · Finish <b>${esc(fmt(a.finish))}</b>
    · Total float <b>${esc((+(a.total_float_h||0).toFixed(0)))}h</b>
    · ${a.critical?'<span class="crit">Critical</span>':'Not critical'}</div>
  <div class="actions"><button class="tb primary" id="saveAct">Save changes</button>
    <button class="tb danger" id="delAct">Delete activity</button></div>`;
}
function statusForm(a){
  return `<div class="editgrid">
    <label>Status ${sel('f_status', [['TK_NotStart','Not Started'],['TK_Active','Active'],['TK_Complete','Complete']], a.status)}</label>
    <label>% Complete ${inp('f_pct', a.pct, '0–100')}</label>
    <label>Duration (h) ${inp('f_dur', a.dur_h)}</label>
  </div>
  <div class="computed">Start <b>${esc(fmt(a.start))}</b> · Finish <b>${esc(fmt(a.finish))}</b>
    · Late finish <b>${esc(fmt(a.late_finish))}</b></div>
  <div class="actions"><button class="tb primary" id="saveAct">Save changes</button>
    <button class="tb danger" id="delAct">Delete activity</button></div>`;
}
function relTab(a){
  const succs=[]; (S.data.activities||[]).forEach(x=>(x.depends||[]).forEach(dd=>{
    if (String(dd.id)===String(a.id)) succs.push({code:x.code, id:x.id, type:dd.type, lag_h:dd.lag_h}); }));
  const opts = (S.data.activities||[]).filter(x=>String(x.id)!==String(a.id)).map(x=>[x.id, `${x.code} ${x.name}`]);
  const rows = (list, kind) => list.length ? `<table class="reltab"><thead><tr>
      <th>Activity</th><th>Type</th><th>Lag (h)</th><th></th></tr></thead><tbody>` +
      list.map(r=>`<tr><td>${esc(r.code)}</td><td>${esc(r.type)}</td><td>${(+r.lag_h||0).toFixed(0)}</td>
        <td>${kind==='pred'?`<button class="mini danger" data-del="${esc(r.id)}">remove</button>`:''}</td></tr>`).join('') +
      `</tbody></table>` : '<div class="empty">None</div>';
  return `<b>Predecessors</b>${rows(a.depends||[], 'pred')}
    <b>Successors</b>${rows(succs, 'succ')}
    <div class="addrel">
      <span>Add predecessor</span>
      <select class="fin" id="r_pred">${opts.map(([v,l])=>`<option value="${esc(v)}">${esc(l)}</option>`).join('')}</select>
      <select class="fin" id="r_type">${['FS','SS','FF','SF'].map(t=>`<option>${t}</option>`).join('')}</select>
      <input class="fin" id="r_lag" value="0" placeholder="lag h">
      <button class="tb" id="addRel">Add</button>
    </div>`;
}
function resTab(a){
  const asg = (S.data.assignments||[]).filter(x=>String(x.act_id)===String(a.id));
  if (!asg.length) return '<div class="empty">No resource assignments in this schedule.</div>';
  return `<table class="reltab"><thead><tr><th>Resource</th><th>Name</th><th>Qty</th><th>Cost</th></tr></thead><tbody>` +
    asg.map(r=>{ const rr=(S.data.resources||[]).find(x=>String(x.id)===String(r.rsrc_id))||{};
      return `<tr><td>${esc(rr.code||r.rsrc_id)}</td><td>${esc(rr.name||'')}</td><td>${esc(r.qty)}</td><td>${esc(r.cost)}</td></tr>`;}).join('') +
    `</tbody></table>`;
}
function codesTab(a){
  return `<div class="editgrid"><label>Activity ID ${inp('f_code', a.code)}</label>
    <label class="wide">Activity Name ${inp('f_name', a.name)}</label></div>
    <div class="actions"><button class="tb primary" id="saveAct">Save changes</button></div>`;
}
function projectForm(){
  const d=S.data||{}, c=d.calendar||{};
  return `<div class="editgrid">
    <label>Project ID / Short name ${inp('p_short', d.short_name)}</label>
    <label class="wide">Project name ${inp('p_name', d.name)}</label>
    <label>Plan start ${inp('p_start', d.start, 'YYYY-MM-DD HH:MM')}</label>
    <label>Data date ${inp('p_dd', d.data_date, 'YYYY-MM-DD HH:MM')}</label>
    <label>Hours per day ${inp('p_hpd', c.hpd)}</label>
    <label class="wide">Calendar name ${inp('p_cal', c.name)}</label>
  </div>
  <div class="computed">Finish <b>${esc(fmt(d.finish))}</b> · Holidays: ${esc((c.holidays||[]).join(', ')||'none')}</div>
  <div class="actions"><button class="tb primary" id="saveProj">Save project</button>
    <button class="tb" id="exportXer2">Export .xer</button></div>`;
}

/* -------- wiring -------- */
function wire(a){
  const save = $('saveAct');
  if (save) save.onclick = () => {
    const patch = {};
    const set = (id,key,num) => { const e=$(id); if (e) patch[key] = num ? parseFloat(e.value||'0') : e.value; };
    set('f_code','code'); set('f_name','name'); set('f_dur','dur_h',true); set('f_pct','pct',true);
    set('f_type','type'); set('f_wbs','wbs_id'); set('f_cstr','cstr_type'); set('f_cstrdate','cstr_date');
    set('f_status','status');
    mutate(()=>api(`/api/projects/${encodeURIComponent(S.key)}/activities/${encodeURIComponent(a.id)}`,
      {method:'PATCH', body:patch}), 'Saved');
  };
  const del = $('delAct');
  if (del) del.onclick = () => { if (!confirm('Delete activity '+a.code+'?')) return;
    S.sel = null;
    mutate(()=>api(`/api/projects/${encodeURIComponent(S.key)}/activities/${encodeURIComponent(a.id)}`,
      {method:'DELETE'}), 'Deleted activity'); };
  const add = $('addRel');
  if (add) add.onclick = () => { if (!$('r_pred').value) return toast('No other activity to link to', true);
    mutate(()=>api(`/api/projects/${encodeURIComponent(S.key)}/activities/${encodeURIComponent(a.id)}/depends`,
      {method:'POST', body:{id:$('r_pred').value, type:$('r_type').value, lag_h:parseFloat($('r_lag').value||'0')}}),
      'Relationship added'); };
  $('dbody').querySelectorAll('[data-del]').forEach(b => b.onclick = () =>
    mutate(()=>api(`/api/projects/${encodeURIComponent(S.key)}/activities/${encodeURIComponent(a.id)}/depends/${encodeURIComponent(b.dataset.del)}`,
      {method:'DELETE'}), 'Relationship removed'));
}
function wireProject(){
  const sp = $('saveProj');
  if (sp) sp.onclick = () => mutate(()=>api(`/api/projects/${encodeURIComponent(S.key)}`,
    {method:'PATCH', body:{short_name:$('p_short').value, name:$('p_name').value, start:$('p_start').value,
      data_date:$('p_dd').value, calendar:{hpd:parseFloat($('p_hpd').value||'8'), name:$('p_cal').value}}}), 'Project saved');
  const ex = $('exportXer2'); if (ex) ex.onclick = () => { if (S.key) location.href = `/api/projects/${encodeURIComponent(S.key)}/export/xer`; };
}

/* -------- toolbar -------- */
function setStatus(m){ $('status').innerHTML = m; }
$('btnOpen').onclick = () => $('file').click();
$('btnUpload2').onclick = () => $('file').click();
$('file').onchange = async e => {
  const f = e.target.files[0]; if (!f) return;
  const fd = new FormData(); fd.append('file', f);
  setStatus('Importing ' + esc(f.name) + '…');
  try { const r = await api('/api/projects', {method:'POST', body:fd});
    toast('Imported ' + (r.projects[0]?.short_name || f.name));
    await loadProjects(); if (r.projects[0]) selectProject(r.projects[0].key);
  } catch(err){ toast('Import failed: ' + err.message, true); }
  e.target.value='';
};
$('btnNew').onclick = async () => {
  const name = prompt('New schedule name?', 'New schedule'); if (name === null) return;
  try { const r = await api('/api/projects/new', {method:'POST',
      body:{name, short_name:name.slice(0,12).toUpperCase()}});
    await loadProjects(); selectProject(r.key); toast('Created ' + r.short_name);
  } catch(e){ toast(e.message, true); }
};
$('btnAddAct').onclick = () => { if (!S.key) return toast('Open a schedule first', true);
  mutate(()=>api(`/api/projects/${encodeURIComponent(S.key)}/activities`,
    {method:'POST', body:{name:'New activity', dur_h:8}}), 'Activity added'); };
$('btnSchedule').onclick = () => { if (S.key) mutate(()=>api(`/api/projects/${encodeURIComponent(S.key)}/schedule`,
  {method:'POST'}), 'Schedule complete'); };
document.addEventListener('keydown', e => {
  if (e.key === 'F9'){ e.preventDefault(); $('btnSchedule').click(); }
  else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z'){
    e.preventDefault(); e.shiftKey ? doRedo() : doUndo(); }
});
$('btnUndo').onclick = doUndo;
$('btnRedo').onclick = doRedo;
$('btnCsv').onclick = () => { if (S.key) location.href = `/api/projects/${encodeURIComponent(S.key)}/export/csv`; };
$('btnXer').onclick = () => { if (S.key) location.href = `/api/projects/${encodeURIComponent(S.key)}/export/xer`; };
$('btnPrint').onclick = () => window.print();
$('btnCo').onclick = e => { S.criticalOnly = !S.criticalOnly; e.currentTarget.classList.toggle('on', S.criticalOnly); render(); };
$('btnZoomIn').onclick = () => { S.ppd = Math.min(S.ppd*1.5, 60); render(); };
$('btnZoomOut').onclick = () => { S.ppd = Math.max(S.ppd/1.5, 0.8); render(); };
$('btnFit').onclick = () => { const gw=document.querySelector('.gridwrap');
  S.ppd = Math.max(1.2, (gw.clientWidth - COLS - 10)/Math.max(spanDays(),1)); render(); };
$('search').oninput = e => { S.q = e.target.value.toLowerCase().trim();
  clearTimeout(S.qT);
  S.qT = setTimeout(() => { const gw = document.querySelector('.gridwrap'); if (gw) gw.scrollTop = 0; render(); }, 180); };
$('btnDelete').onclick = async () => {
  if (!S.key || !confirm('Delete this schedule from the server?')) return;
  await api('/api/projects/'+encodeURIComponent(S.key), {method:'DELETE'});
  S.key=null; S.data=null; await loadProjects();
  if (!S.projects.length){ $('grid').innerHTML=''; renderNav(); renderDetails(); }
};
document.querySelectorAll('.nt').forEach(b => b.onclick = () => {
  document.querySelectorAll('.nt').forEach(x=>x.classList.remove('active'));
  b.classList.add('active'); S.navtab = b.dataset.tab; renderNav(); });

function openNav(){ $('nav').classList.add('open'); $('scrim').classList.add('show'); }
function closeNav(){ $('nav').classList.remove('open'); $('scrim').classList.remove('show'); }
$('burger').onclick = openNav; $('scrim').onclick = closeNav;
window.addEventListener('resize', () => { if (S.data) render(); });

/* virtual-scroll: repaint the visible window, throttled to one frame */
document.querySelector('.gridwrap').addEventListener('scroll', () => {
  if (S.rafP) return;
  S.rafP = requestAnimationFrame(() => { S.rafP = 0; paint(false); });
}, {passive:true});

loadProjects().catch(e => toast('Load failed: ' + e.message, true));
