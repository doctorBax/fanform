(() => {
'use strict';
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state = null, authenticated = false, editUntil = 0;
const drafts = new Map(), groupDrafts = new Map();
// Show the actual hwmon sensor/threshold instead of an unhelpful generic error.
const visibleError = (message) => String(message || 'Неизвестная ошибка');
const toast = (message) => { const t = $('#toast'); t.textContent = message; t.classList.add('visible'); setTimeout(() => t.classList.remove('visible'), 4000); };
async function api(path, body) {
  const options = body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json','X-Fanform-Request':'1'}, body:JSON.stringify(body)};
  const result = await fetch(path, {...options, credentials:'same-origin'});
  const value = await result.json();
  if (!result.ok) { if (result.status === 401 && path !== '/api/login') showLogin(); throw new Error(value.error || `HTTP ${result.status}`); }
  return value;
}
function showLogin() { authenticated = false; $('#login').hidden = false; $('#dashboard').hidden = true; $('#logout').hidden = true; $('#online').classList.remove('ok'); $('#online').lastChild.textContent = ' Locked'; }
function showDashboard() { authenticated = true; $('#login').hidden = true; $('#dashboard').hidden = false; $('#logout').hidden = false; $('#online').classList.add('ok'); $('#online').lastChild.textContent = ' Live'; }
function card(ch) {
 const cal = ch.calibration, linked = ch.linked_fans || [];
 const threshold = cal && cal.fans?.length ? cal.safe_pwm : 180;
 const disabled = !ch.writable;
 const requested = typeof ch.target_pwm === 'number' ? ch.target_pwm : ch.value;
 const actualPct = typeof ch.value === 'number' ? Math.round(ch.value/255*100) : null;
 const pct = drafts.has(ch.id) ? drafts.get(ch.id) : (typeof requested === 'number' ? Math.round(requested/255*100) : 100);
 const min = Math.ceil(threshold/255*100), value = Math.max(min,Math.min(100,pct));
 const mode = ch.mode === 0 ? 'DC' : ch.mode === 1 ? 'PWM' : 'FAN';
 const mainFan = linked.find(f => Number.isFinite(f.rpm)) || linked[0];
 const rpm = mainFan && Number.isFinite(mainFan.rpm) ? Number(mainFan.rpm).toLocaleString('ru-RU') : '—';
 const active = !!ch.active;
 const stateText = disabled ? 'Только мониторинг' : active ? 'Ручное управление' : 'Управление BIOS';
 const stateBadge = disabled ? 'Нет доступа' : active ? 'Активно' : 'Авто';
 const drift = typeof ch.target_pwm === 'number' && actualPct !== null && Math.abs(ch.target_pwm-ch.value)>4;
 const warning = drift ? `<div class="fan-warning" role="status">Скорость меняется извне · сейчас ${actualPct}%</div>` : '';
 const otherFans = linked.length > 1 ? `<div class="fan-extra">${linked.slice(1).map(f=>`${esc(f.name)}: ${Number.isFinite(f.rpm) ? Number(f.rpm).toLocaleString('ru-RU') : '—'} RPM`).join(' · ')}</div>` : '';
 const hasSettings = ch.dc_available || active || ch.recoverable || !!cal || !!ch.control_drift || !!linked.length;
 return `<article class="fan-card" data-channel="${esc(ch.id)}">
  <div class="card-header"><div class="fan-heading"><h3 class="fan-title">${esc(ch.name)}</h3><p class="fan-sub">${esc(ch.chip)}</p></div><span class="mode-badge">${mode}</span></div>
  <div class="speed-row"><div class="speed-big" aria-label="Мощность ${esc(ch.name)}">${pct}<small>%</small></div><div class="rpm-reading"><strong>${rpm}<span> RPM</span></strong><span class="rpm-caption">${mainFan ? esc(mainFan.name) : 'Тахометр не привязан'}</span></div></div>
  <input type="range" class="track fan-slider" style="--fill:${value}%" min="${min}" max="100" value="${value}" step="1" aria-label="Мощность ${esc(ch.name)}" ${disabled?'disabled':''}>
  <div class="fan-control-row"><span class="control-state"><span class="state-dot ${active ? 'on' : ''}"></span>${stateText}</span><span class="state-badge ${active ? 'is-active' : ''}">${stateBadge}</span></div>
  ${warning}
  <div class="card-actions"><button type="button" class="mini-btn accent apply-speed" ${disabled?'disabled':''}>Применить <span aria-hidden="true">↗</span></button>${hasSettings ? `<details class="fan-options"><summary>Настройки <span aria-hidden="true">⌄</span></summary><div class="advanced-actions">${ch.dc_available ? `<button type="button" class="mini-btn switch-mode" ${disabled?'disabled':''}>Переключить на ${ch.mode === 0 ? 'PWM' : 'DC'}</button>` : ''}${ch.active || ch.recoverable ? '<button type="button" class="mini-btn firmware">Вернуть BIOS</button>' : ''}${cal?.fans?.length ? `<span class="advanced-caption">Привязано ${cal.fans.length} RPM · минимум ${min}%</span>` : ''}${otherFans}</div></details>` : ''}</div>
 </article>`;
}
function render(data) {
 state = data;
 $('#online').classList.add('ok'); $('#online').lastChild.textContent = ' Live';
 const maxTemp = data.temperatures.length ? Math.max(...data.temperatures.map(t=>t.celsius)) : null;
 $('#hero-temp').innerHTML = `${maxTemp === null ? '—' : Math.round(maxTemp)}<span>°C</span>`;
 $('#temp-count').textContent = `${data.temperatures.length} температурных датчиков`;
 const rpms = data.fans.filter(f => f.rpm !== null);
 $('#metrics').innerHTML = `<div class="metric"><div class="metric-label">Контролируемые каналы</div><div class="metric-value">${data.channels.filter(c=>c.writable).length}<span class="metric-unit">/ ${data.channels.length}</span></div></div><div class="metric"><div class="metric-label">RPM датчики</div><div class="metric-value">${rpms.length}<span class="metric-unit">шт.</span></div></div><div class="metric"><div class="metric-label">Средние обороты</div><div class="metric-value">${rpms.length? Math.round(rpms.reduce((a,f)=>a+f.rpm,0)/rpms.length).toLocaleString('ru-RU') : '—'}<span class="metric-unit">RPM</span></div></div>`;
 $('#fan-count').textContent = `${data.channels.length} TOTAL`;
 const openOptions = new Set([...$('#channels').querySelectorAll('.fan-card:has(.fan-options[open])')].map(el => el.dataset.channel));
 $('#channels').innerHTML = data.channels.length ? data.channels.map(card).join('') : `<div class="empty">PWM-каналы не обнаружены. Проверьте, поддерживает ли ваш контроллер Linux hwmon и загружен ли драйвер датчиков. Панель работает в режиме мониторинга.</div>`;
 $('#channels').querySelectorAll('.fan-card').forEach(el => { if (openOptions.has(el.dataset.channel)) { const settings = el.querySelector('.fan-options'); if (settings) settings.open = true; } });
 $('#groups').innerHTML = data.groups.length ? data.groups.map(g => {
  const min = Math.max(...g.channels.map(cid => {const ch=data.channels.find(x=>x.id===cid);return ch?.calibration?.fans?.length ? Math.ceil(ch.calibration.safe_pwm/255*100) : 71;}));
  const targets = g.channels.map(cid => data.channels.find(x=>x.id===cid)?.target_pwm);
  const applied = targets.length && targets.every(v=>typeof v==='number' && v===targets[0]) ? Math.round(targets[0]/255*100) : 100;
  const pct = groupDrafts.has(g.id) ? groupDrafts.get(g.id) : applied;
  const value = Math.max(min,Math.min(100,pct));
  return `<article class="group-card" data-group="${esc(g.id)}">
   <div class="group-top"><div><h3>${esc(g.name)}</h3><p class="group-sub">${g.channels.length} канал(а)</p></div><button type="button" class="quiet-btn delete-group" aria-label="Удалить группу ${esc(g.name)}">Удалить</button></div>
   <div class="group-power"><span class="group-pct">${pct}<small>%</small></span><span class="group-hint">Общая мощность</span></div>
   <input type="range" class="track group-slider" style="--fill:${value}%" min="${min}" max="100" value="${value}" aria-label="Мощность группы ${esc(g.name)}">
   <div class="group-actions"><span class="control-state"><span class="state-dot"></span>Группа</span><button type="button" class="mini-btn accent apply-group">Применить ↗</button></div></article>`;
 }).join('') : `<div class="empty">Групп пока нет. Объедините несколько каналов для общего управления.</div>`;
 const sensors = [
   ...data.temperatures.map(t=>({name:t.name,chip:t.chip,value:t.celsius+' °C'})),
   ...data.fans.map(f=>({name:f.name,chip:f.chip,value:f.rpm === null?'—':f.rpm+' RPM'}))];
 $('#sensors').innerHTML = sensors.length ? sensors.map(s=>`<div class="sensor-row"><div><div class="sensor-name">${esc(s.name)}</div><span class="sensor-chip">${esc(s.chip)}</span></div><div class="sensor-val">${esc(s.value)}</div></div>`).join('') : `<div class="empty">Датчики не найдены.</div>`;
 renderCalibration(data.job, data);
 const notice = $('#notice');
 // The sensor temperatures remain available in the metrics; no temperature popups.
 if (data.conflict) {notice.hidden=false; notice.textContent='Другой контроллер вентиляторов уже работает. Fanform не будет конкурировать с ним.';}
 else if (data.alarm && !/температур|перегрев|порог|тест остановлен/i.test(data.alarm)) {notice.hidden=false; notice.textContent=`Защита: ${data.alarm}`;}
 else notice.hidden=true;
}
function renderCalibration(job, data) {
 const running = job?.state === 'running';
 const button = $('#calibrate-all');
 const eligible = (data.channels || []).filter(ch => ch.writable && !ch.active).length;
 const blocked = data.cooling_check && !data.cooling_check.can_start;
 button.disabled = running || !!data.conflict || eligible === 0 || blocked;
 button.textContent = running ? 'Автотест выполняется…' : 'Автотест всех вентиляторов ↗';
 const panel = $('#calibration-panel');
 panel.hidden = (!job || job.state === 'idle') && !blocked;
 if (panel.hidden) return;
 if ((!job || job.state === 'idle') && blocked) {
   $('#calibration-progress').value = 0;
   $('#calibration-percent').textContent = '0%';
   $('#calibration-title').textContent = 'Автотест пока недоступен';
   $('#calibration-message').textContent = data.cooling_check.reason;
   $('#calibration-results').replaceChildren();
   return;
 }
 const percent = Math.max(0, Math.min(100, Number(job.progress) || 0));
 $('#calibration-progress').value = percent;
 $('#calibration-percent').textContent = `${percent}%`;
 $('#calibration-title').textContent = running ? `Канал ${job.current || 1} из ${job.total || 1}` :
    (job.state === 'complete' ? 'Автотест завершён' : 'Автотест прерван');
 const message = String(job.message || '');
 $('#calibration-message').textContent = message;
 $('#calibration-results').innerHTML = (job.results || []).map(item => {
   const label = item.status === 'mapped' ? `${(item.fans || []).length} RPM · минимум ${Math.ceil(item.safe_pwm / 255 * 100)}%` :
     (item.message || 'Привязка не выполнена');
   const details = Object.entries(item.response_curve || {}).map(([id, r]) =>
     `<div>${esc(id)} · ${r.rpm_at_100 ?? '—'} → ${r.rpm_at_84 ?? '—'} → ${r.rpm_at_69 ?? '—'} → ${r.rpm_at_59 ?? '—'} → ${r.rpm_recovered ?? '—'} RPM · ${esc(r.reason || (r.matched ? 'подтверждено' : 'не подтверждено'))}</div>`
   ).join('');
   return `<div class="calibration-result"><div><strong>${esc(item.name)}</strong><p class="${item.status === 'mapped' ? 'calibration-ok' : 'calibration-miss'}">${esc(label)}</p>${details ? `<details class="calibration-details"><summary>Измерения RPM (100% → 84% → 69% → 59% → 100%)</summary>${details}</details>` : ''}</div></div>`;
 }).join('');
}
async function refresh() {
 if (!authenticated) return;
 try {
  const result = await api('/api/state');
  if (Date.now() >= editUntil) render(result);
  else { state = result; renderCalibration(result.job, result); }
 } catch (e) { $('#online').classList.remove('ok'); $('#online').lastChild.textContent = ' Offline'; }
}
const action = async (path,data,success,onSuccess) => {
 try { const result=await api(path,data); if(onSuccess)onSuccess(result); if(success)toast(success); editUntil=0; await refresh(); }
 catch(e) {toast(visibleError(e.message));}
};
$('#login-form').addEventListener('submit', async e=>{ e.preventDefault(); try { await api('/api/login',{password:$('#password').value}); $('#password').value=''; $('#login-error').textContent=''; showDashboard(); await refresh(); }catch(e){ $('#login-error').textContent=e.message; } });
$('#logout').addEventListener('click', async()=>{try {await api('/api/logout',{});}finally{showLogin();}});
$('#calibrate-all').addEventListener('click', () => {
 if(confirm('Fanform последовательно проверит доступные вентиляторы, измерит RPM, автоматически привяжет тахометры к ползункам и найдёт рабочий минимум. Во время теста обеспечьте минимальную нагрузку на хост. Начать?')) {
   action('/api/calibrate/all', {}, 'Автотест запущен');
 }
});
$('#channels').addEventListener('input',e=>{if(e.target.matches('.fan-slider')) {editUntil=Date.now()+5000; const card=e.target.closest('.fan-card'); drafts.set(card.dataset.channel, Number(e.target.value)); card.querySelector('.speed-big').innerHTML = e.target.value+'<small>%</small>'; e.target.style.setProperty('--fill',e.target.value+'%');}});
$('#channels').addEventListener('click', async e=>{
 const card = e.target.closest('.fan-card'); if(!card) return;
 const cid = card.dataset.channel, ch=state.channels.find(c=>c.id===cid);
 if(e.target.closest('.apply-speed')) return action('/api/speed',{channel:cid,percent:Number(card.querySelector('.fan-slider').value)},'Скорость применена',()=>drafts.delete(cid));
 if(e.target.closest('.switch-mode')) {const mode = ch.mode===0?1:0; if(confirm(`Изменить режим питания на ${mode?'PWM':'DC'}? Это допустимо только для совместимого разъёма и вентилятора. Скорость будет сначала увеличена до 100%.`)) return action('/api/mode',{channel:cid,mode},'Режим изменён. Повторите автотест.');}
 if(e.target.closest('.firmware')) return action('/api/firmware',{channel:cid},'Исходное управление восстановлено');
});
$('#groups').addEventListener('input',e=>{if(e.target.matches('.group-slider')){editUntil=Date.now()+5000; const card=e.target.closest('.group-card'); groupDrafts.set(card.dataset.group,Number(e.target.value)); card.querySelector('.group-pct').innerHTML=e.target.value+'<small>%</small>'; e.target.style.setProperty('--fill',e.target.value+'%');}});
$('#groups').addEventListener('click',e=>{
 const card=e.target.closest('.group-card');if(!card)return;
 if(e.target.closest('.apply-group'))return action('/api/group/speed',{id:card.dataset.group,percent:Number(card.querySelector('input').value)},'Группа обновлена',()=>groupDrafts.delete(card.dataset.group));
 if(e.target.closest('.delete-group')&&confirm('Удалить эту группу?'))return action('/api/group/delete',{id:card.dataset.group},'Группа удалена');
});
$('#group-open').addEventListener('click',()=>{
 $('#group-options').innerHTML = (state?.channels||[]).filter(c=>c.writable).map(c=>`<label class="check-item"><input type="checkbox" value="${esc(c.id)}"> ${esc(c.name)}</label>`).join('');
 $('#modal').hidden=false;$('#group-name').focus();
});
$('#modal-close').addEventListener('click',()=>$('#modal').hidden=true);
$('#modal').addEventListener('click',e=>{if(e.target.id==='modal')$('#modal').hidden=true;});
$('#group-form').addEventListener('submit',async e=>{
 e.preventDefault(); const channels=[...$('#group-options').querySelectorAll('input:checked')].map(n=>n.value);
 await action('/api/group/create',{name:$('#group-name').value,channels},'Группа создана');
 $('#modal').hidden=true; $('#group-name').value='';
});
$('#panic').addEventListener('click',()=>{if(confirm('Включить аварийный сброс и вывести ранее управляемые каналы в безопасный режим?')) action('/api/failsafe',{},'Аварийный режим активирован');});
(async()=>{ try { const r=await api('/api/session'); if(r.authenticated){showDashboard();await refresh();}else showLogin();}catch(e){showLogin();} setInterval(refresh,2500); })();
})();
