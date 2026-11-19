(() => {
'use strict';
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let state = null, authenticated = false, editUntil = 0;
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
 const cal = ch.calibration, linked = cal?.fans?.length || 0;
 const threshold = cal && linked ? cal.safe_pwm : 180;
 const disabled = !ch.writable;
 const pct = typeof ch.value === 'number' ? Math.round(ch.value/255*100) : 100;
 const min = Math.ceil(threshold / 255 * 100);
 const badge = disabled ? 'READ ONLY' : (ch.active ? 'MANUAL' : 'FIRMWARE');
 const sub = linked ? `${linked} RPM-сенсор(а) · нижняя граница ${min}%` : 'Связанные тахометры ещё не определены';
 const mode = ch.mode === 0 ? 'DC' : ch.mode === 1 ? 'PWM' : 'N/A';
 return `<article class="fan-card" data-channel="${esc(ch.id)}">
 <div class="card-header"><div><div class="fan-title">${esc(ch.name)}</div><div class="fan-sub">${esc(ch.chip)} · ${esc(mode)} · ${disabled ? 'мониторинг' : sub}</div></div><span class="chip ${ch.active ? '' : 'neutral'}">${badge}</span></div>
 <div class="speed-row"><div class="speed-big">${ch.value === null ? '—' : pct}<small>%</small></div><span class="small-label">${ch.value === null ? 'NO VALUE' : ch.value + '/255'}</span></div>
 <input type="range" class="track fan-slider" min="${min}" max="100" value="${Math.max(min, pct)}" step="1" aria-label="Мощность ${esc(ch.name)}" ${disabled?'disabled':''}>
 <div class="card-actions"><button class="mini-btn accent apply-speed" ${disabled?'disabled':''}>Применить</button><button class="mini-btn calibrate" ${disabled?'disabled':''}>Автотест ↗</button>${ch.dc_available ? `<button class="mini-btn switch-mode" ${disabled?'disabled':''}>${ch.mode===0?'PWM':'DC'} режим</button>` : ''}${ch.active || ch.recoverable ? '<button class="mini-btn firmware">Вернуть BIOS</button>':''}</div>
 <div class="cal-note">${cal ? `Калибровка: ${min}% безопасный минимум · ${linked} RPM-сенсор(а).` : (disabled ? 'Режим read-only: разъём недоступен для управления.' : 'Автотест сопоставит канал с тахометром и проверит нижнюю границу.')}</div>
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
 $('#channels').innerHTML = data.channels.length ? data.channels.map(card).join('') : `<div class="empty">PWM-каналы не обнаружены. Проверьте, поддерживает ли ваш контроллер Linux hwmon и загружен ли драйвер датчиков. Панель работает в режиме мониторинга.</div>`;
 $('#groups').innerHTML = data.groups.length ? data.groups.map(g => { const min = Math.max(...g.channels.map(cid => {const ch=data.channels.find(x=>x.id===cid);return ch?.calibration?.fans?.length ? Math.ceil(ch.calibration.safe_pwm/255*100) : 71;})); return `<article class="group-card" data-group="${esc(g.id)}"><div class="group-top"><div><h3>${esc(g.name)}</h3><div class="group-sub">${g.channels.length} канал(а)</div></div><button class="mini-btn delete-group">Удалить</button></div><div class="group-controls"><input type="range" class="track group-slider" min="${min}" max="100" value="100" aria-label="Мощность группы ${esc(g.name)}"><span class="group-pct">100%</span><button class="mini-btn accent apply-group">Применить</button></div></article>`; }).join('') : `<div class="empty">Групп пока нет. Объедините, например, вентиляторы корзины HDD и регулируйте их вместе.</div>`;
 const sensors = [
   ...data.temperatures.map(t=>({name:t.name,chip:t.chip,value:t.celsius+' °C'})),
   ...data.fans.map(f=>({name:f.name,chip:f.chip,value:f.rpm === null?'—':f.rpm+' RPM'}))];
 $('#sensors').innerHTML = sensors.length ? sensors.map(s=>`<div class="sensor-row"><div><div class="sensor-name">${esc(s.name)}</div><span class="sensor-chip">${esc(s.chip)}</span></div><div class="sensor-val">${esc(s.value)}</div></div>`).join('') : `<div class="empty">Датчики не найдены.</div>`;
 const notice = $('#notice');
 if (data.conflict) {notice.hidden=false; notice.textContent = 'Найден другой активный fan daemon. Управление заблокировано.';}
 else if (data.alarm) {notice.hidden=false; notice.textContent = `Защита: ${data.alarm}`;}
 else if (data.job.state === 'running' || data.job.state === 'error' || data.job.state === 'complete') {notice.hidden=false; notice.textContent=`Автотест · ${data.job.progress}% · ${data.job.message}`;}
 else notice.hidden=true;
}
async function refresh() {
 if (!authenticated) return;
 try {
  const result = await api('/api/state');
  if (Date.now() >= editUntil) render(result);
  else { state = result; if (result.job.state==='running') {$('#notice').hidden=false; $('#notice').textContent=`Автотест · ${result.job.progress}% · ${result.job.message}`;} }
 } catch (e) { $('#online').classList.remove('ok'); $('#online').lastChild.textContent = ' Offline'; }
}
const action = async (path,data,success) => {
 try { await api(path,data); if(success)toast(success); editUntil=0; await refresh(); }
 catch(e) {toast(e.message);}
};
$('#login-form').addEventListener('submit', async e=>{ e.preventDefault(); try { await api('/api/login',{password:$('#password').value}); $('#password').value=''; $('#login-error').textContent=''; showDashboard(); await refresh(); }catch(e){ $('#login-error').textContent=e.message; } });
$('#logout').addEventListener('click', async()=>{try {await api('/api/logout',{});}finally{showLogin();}});
$('#channels').addEventListener('input',e=>{if(e.target.matches('.fan-slider')) {editUntil=Date.now()+5000; const card=e.target.closest('.fan-card'); card.querySelector('.speed-big').innerHTML = e.target.value+'<small>%</small>';}});
$('#channels').addEventListener('click', async e=>{
 const card = e.target.closest('.fan-card'); if(!card) return;
 const cid = card.dataset.channel, ch=state.channels.find(c=>c.id===cid);
 if(e.target.closest('.apply-speed')) return action('/api/speed',{channel:cid,percent:Number(card.querySelector('.fan-slider').value)},'Скорость применена');
 if(e.target.closest('.calibrate')) {if(confirm('Автотест временно изменит скорость вентилятора. Запускайте при низкой нагрузке, убедившись, что охлаждение исправно. Продолжить?')) return action('/api/calibrate',{channel:cid},'Калибровка запущена');}
 if(e.target.closest('.switch-mode')) {const mode = ch.mode===0?1:0; if(confirm(`Изменить режим питания на ${mode?'PWM':'DC'}? Это допустимо только для совместимого разъёма и вентилятора. Скорость будет сначала увеличена до 100%.`)) return action('/api/mode',{channel:cid,mode},'Режим изменён. Повторите автотест.');}
 if(e.target.closest('.firmware')) return action('/api/firmware',{channel:cid},'Исходное управление восстановлено');
});
$('#groups').addEventListener('input',e=>{if(e.target.matches('.group-slider')){editUntil=Date.now()+5000; e.target.closest('.group-card').querySelector('.group-pct').textContent=e.target.value+'%';}});
$('#groups').addEventListener('click',e=>{
 const card=e.target.closest('.group-card');if(!card)return;
 if(e.target.closest('.apply-group'))return action('/api/group/speed',{id:card.dataset.group,percent:Number(card.querySelector('input').value)},'Группа обновлена');
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
