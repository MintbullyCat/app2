// ======================= app.js (FULL) =======================

// --- 간단 스토리지
const S = {
  get code() { return localStorage.getItem('code') || ''; },
  set code(v) { localStorage.setItem('code', v || ''); },
  get pid() { return localStorage.getItem('pid') || ''; },
  set pid(v) { localStorage.setItem('pid', v || ''); },
  get nickname() { return localStorage.getItem('nickname') || ''; },
  set nickname(v) { localStorage.setItem('nickname', v || ''); },
  get hostSecret() { return localStorage.getItem('hostSecret') || ''; },
  set hostSecret(v) { localStorage.setItem('hostSecret', v || ''); },
  get joinUrl() { return localStorage.getItem('joinUrl') || ''; },
  set joinUrl(v) { localStorage.setItem('joinUrl', v || ''); },
};
const el = (id) => document.getElementById(id);
const sleep = (ms)=>new Promise(r=>setTimeout(r,ms));

// ===== 초기 헬스 핑 (init과 분리해서 먼저 수행) =====
(async function earlyHealthPing(){
  const dot = document.getElementById('health-dot');
  const txt = document.getElementById('health-text');
  try {
    const r = await fetch('/api/health', { cache: 'no-store' });
    if (!r.ok) throw new Error('HTTP '+r.status);
    const j = await r.json();
    if (dot) dot.className = 'dot green';
    if (txt) txt.textContent = '서버 연결 OK';
    console.log('[health]', j);
  } catch (e) {
    if (dot) dot.className = 'dot red';
    if (txt) txt.textContent = '서버 응답 없음';
    console.error('[health] failed:', e);
  }
})();

// ===== 유틸
function escapeHtml(s){ return (s||'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m])); }
async function apiGet(url){ const r=await fetch(url); if(!r.ok) throw new Error(await r.text()); return r.json(); }
async function apiPost(url, body){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});
  const t=await r.text(); if(!r.ok) throw new Error(t); try{return JSON.parse(t);}catch{return t;}
}

// ===== 모달 시트
function openSheet(id){ el(id)?.setAttribute('aria-hidden','false'); }
function closeSheet(id){ el(id)?.setAttribute('aria-hidden','true'); }

// ===== 지도/마커
let map, myMarker=null, pickMode=false;
const marks = { participants: [], centroid:null, best:null };

function initMap(){
  map = L.map('map').setView([37.5665, 126.9780], 12);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {maxZoom:19, attribution:'&copy; OpenStreetMap'}).addTo(map);
  setTimeout(()=>map.invalidateSize(), 0);
  window.addEventListener('resize', ()=>map.invalidateSize());

  map.on('click', (e)=>{
    if(!pickMode) return;
    const {lat,lng}=e.latlng;
    el('myLat').value = lat.toFixed(6);
    el('myLng').value = lng.toFixed(6);
    const gs=el('geoStatus'); if(gs) gs.textContent='지도에서 선택됨';
    upsertMyMarker(lat,lng,true);
    pickMode=false; el('map')?.classList.remove('picking');
  });
}
function clearMarks(){
  marks.participants.forEach(m=>map.removeLayer(m)); marks.participants=[];
  if(marks.centroid){map.removeLayer(marks.centroid); marks.centroid=null;}
  if(marks.best){map.removeLayer(marks.best); marks.best=null;}
}
function addParticipantMarker(p){
  const color = p.mode==='car' ? '#2563eb' : p.mode==='bus' ? '#f59e0b' : p.mode==='subway' ? '#8b5cf6' : '#10b981';
  const m=L.circleMarker([p.lat,p.lng],{radius:8,color,fillColor:color,fillOpacity:.9}).addTo(map);
  m.bindTooltip(`${escapeHtml(p.nickname||'')} (${p.mode})\n${p.pid||''}`);
  marks.participants.push(m);
}
function setCentroidMarker(c){
  if(!c) return;
  marks.centroid=L.circleMarker([c.lat,c.lng],{radius:10,color:'#111827',fillColor:'#111827',fillOpacity:.9})
    .addTo(map).bindTooltip('Centroid');
}
function setBestMarker(b){
  if(!b) return;
  marks.best=L.circleMarker([b.lat,b.lng],{radius:10,color:'#e11d48',fillColor:'#e11d48',fillOpacity:.9})
    .addTo(map).bindTooltip('ETA Midpoint');
}
function fitToPoints(points){
  const coords = points.filter(p=>p&&isFinite(p.lat)&&isFinite(p.lng)).map(p=>[p.lat,p.lng]);
  if(coords.length) map.fitBounds(L.latLngBounds(coords),{padding:[24,24]});
}
function upsertMyMarker(lat,lng,pan=false){
  if(!map) return;
  if(!myMarker){
    myMarker=L.marker([lat,lng],{draggable:true}).addTo(map).bindTooltip('내 위치');
    myMarker.on('dragend', (e)=>{
      const {lat:nlat,lng:nlng}=e.target.getLatLng();
      el('myLat').value=nlat.toFixed(6);
      el('myLng').value=nlng.toFixed(6);
      const gs=el('geoStatus'); if(gs) gs.textContent='마커 이동됨';
    });
  }else{
    myMarker.setLatLng([lat,lng]);
  }
  if(pan) map.setView([lat,lng], Math.max(14,map.getZoom()));
}

// ===== 위치 검색 (OSM Nominatim)
async function searchLocation(q){
  const url = `https://nominatim.openstreetmap.org/search?format=json&q=${encodeURIComponent(q)}&limit=8`;
  const r = await fetch(url, {headers:{'Accept':'application/json'}});
  if(!r.ok) throw new Error('search failed');
  return r.json();
}
function showLocResults(list){
  const box = el('locResults'); if(!box) return;
  if(!list || !list.length){ box.classList.remove('show'); box.innerHTML=''; return; }
  box.innerHTML = list.map(item=>{
    const name = escapeHtml(item.display_name || '');
    return `<div class="item" data-lat="${item.lat}" data-lng="${item.lon}">${name}</div>`;
  }).join('');
  box.classList.add('show');
}
function attachLocResultClick(){
  const box = el('locResults'); if(!box) return;
  box.addEventListener('click', (e)=>{
    const t = e.target.closest('.item'); if(!t) return;
    const lat = parseFloat(t.dataset.lat), lng = parseFloat(t.dataset.lng);
    el('myLat').value = lat.toFixed(6);
    el('myLng').value = lng.toFixed(6);
    upsertMyMarker(lat,lng,true);
    const gs=el('geoStatus'); if(gs) gs.textContent='검색 위치 적용됨';
    box.classList.remove('show'); box.innerHTML='';
  });
  document.addEventListener('click', (e)=>{
    if(!box.contains(e.target) && e.target !== el('locSearch') && e.target !== el('btnLocSearch')){
      box.classList.remove('show');
    }
  });
}

// ===== 방/참가자 상태 렌더
function renderParticipants(plist){
  const tb=el('participantsTable').querySelector('tbody'); tb.innerHTML='';
  plist.forEach(p=>{
    const tr=document.createElement('tr');
    tr.innerHTML=`
      <td class="small">${escapeHtml(p.pid||'')}</td>
      <td>${escapeHtml(p.nickname||'')}</td>
      <td>${p.mode}</td>
      <td class="small">${p.updated_at?new Date(p.updated_at).toLocaleTimeString():''}</td>`;
    tb.appendChild(tr);
  });
}
function fillRoomInfo(res){
  if(!res) return;
  if(res.code){ S.code=res.code; const c=res.code; el('code').value=c; el('codeLabel').textContent=c; }
  if(res.hostSecret){ S.hostSecret=res.hostSecret; el('hostSecretLabel').textContent=res.hostSecret; el('hostSecretInput').value=res.hostSecret; }
  if(res.joinUrl){
    S.joinUrl=res.joinUrl; const a=el('joinUrl'); a.href=res.joinUrl; a.textContent=res.joinUrl;
  } else if(S.code){
    const u=location.origin+'/?code='+S.code; S.joinUrl=u; el('joinUrl').href=u; el('joinUrl').textContent=u;
  }
  el('pidLabel').textContent = S.pid || '-';
}
function renderSuggest(list, centroid){
  el('suggestCount').textContent=`추천 ${list.length}개`;
  const wrap=el('suggestList'); wrap.innerHTML='';
  list.forEach(d=>{
    const lat = parseFloat(d.y), lng = parseFloat(d.x);
    const dist = (d._centroid_dist_km!=null)? `${d._centroid_dist_km}km` : '';
    const open = (d._open_minutes_left!=null) ? `영업 ${d._open_minutes_left}분 남음 (마감 ${d._closes_at||''})` : '';
    const phone = d._phone || d.phone || '';
    const addr = d.road_address_name || d.address_name || '';
    const tags = [
      d.category_name ? `<span class="tag">${escapeHtml(d.category_name)}</span>` : '',
      d._open_enough===true ? `<span class="badge">충분히 영업</span>` : '',
      dist ? `<span class="tag">${dist}</span>` : ''
    ].join(' ');
    const img = d._photo_url ? `<img src="${d._photo_url}" alt="">` : `<div style="width:100%;height:100px;border-radius:10px;background:#111827;border:1px solid #1f2937;"></div>`;
    const html = `
      <div class="card" data-lat="${lat}" data-lng="${lng}">
        <div>${img}</div>
        <div>
          <div style="font-weight:700;margin-bottom:4px">${escapeHtml(d.place_name||'')}</div>
          <div class="small">${escapeHtml(addr)}</div>
          ${phone? `<div class="small">${escapeHtml(phone)}</div>`:''}
          ${open? `<div class="small">${escapeHtml(open)}</div>`:''}
          <div style="margin-top:6px">${tags}</div>
        </div>
      </div>`;
    wrap.insertAdjacentHTML('beforeend', html);
  });
  // 카드 클릭 → 지도 이동
  wrap.querySelectorAll('.card').forEach(c=>{
    c.addEventListener('click',()=>{
      const lat=parseFloat(c.dataset.lat), lng=parseFloat(c.dataset.lng);
      map.setView([lat,lng], Math.max(15,map.getZoom()));
      L.circleMarker([lat,lng],{radius:9,color:'#22d3ee',fillColor:'#22d3ee',fillOpacity:.9}).addTo(map);
    });
  });
}

// ===== 상태 갱신
async function refreshState(showToast=false){
  if(!S.code) return;
  try{
    const st = await apiGet(`/api/room/state?code=${encodeURIComponent(S.code)}`);
    // 우측 패널
    el('metaText').textContent = JSON.stringify(st.meta||{}, null, 0);
    el('centroidText').textContent = st.centroid ? `${st.centroid.lat.toFixed(5)}, ${st.centroid.lng.toFixed(5)}` : '-';
    renderParticipants(st.participants||[]);
    // 지도
    clearMarks();
    (st.participants||[]).forEach(p=>{
      if(isFinite(p.lat)&&isFinite(p.lng)) addParticipantMarker(p);
    });
    if(st.centroid) setCentroidMarker(st.centroid);
    if(st.eta && st.eta.best) setBestMarker(st.eta.best);
    fitToPoints([
      ...(st.participants||[]).filter(p=>isFinite(p.lat)&&isFinite(p.lng)),
      st.centroid, st.eta?.best
    ]);
    // 추천 결과 표시(저장된 결과)
    if(st.results && Array.isArray(st.results.items)){
      renderSuggest(st.results.items, st.results.centroid);
    }
    if(showToast) console.log('[state] refreshed');
  }catch(e){
    console.error('state error', e);
  }
}

// ===== 이벤트 핸들러 묶음
async function handleCreate(){
  try{
    const purpose = el('purpose').value.trim();
    const meetingTime = el('meetingTime').value.trim();
    const res = await apiPost('/api/room/create', { purpose, meetingTime, ttlMinutes: 120 });
    fillRoomInfo(res);
    closeSheet('sheetCreate');
    // 방을 만든 직후 호스트도 참가 처리(닉네임 없으면 기본값)
    const nickname = S.nickname || '호스트';
    const jr = await apiPost('/api/room/join', { code: res.code, nickname });
    S.pid = jr.pid;
    el('pidLabel').textContent = jr.pid;
    await refreshState(true);
  }catch(e){
    alert('방 생성 실패: '+e.message);
  }
}
async function handleJoin(){
  try{
    const code = (el('code').value||'').trim().toUpperCase();
    const nickname = (el('nickname').value||'').trim() || '게스트';
    const res = await apiPost('/api/room/join', { code, nickname, pid: S.pid || undefined });
    S.code = code; S.nickname = nickname; S.pid = res.pid;
    fillRoomInfo({code});
    closeSheet('sheetJoin');
    await refreshState(true);
  }catch(e){
    alert('방 참가 실패: '+e.message);
  }
}
async function handleUpdate(){
  if(!S.code || !S.pid){ alert('먼저 방에 참가하세요.'); return; }
  let lat = parseFloat(el('myLat').value), lng = parseFloat(el('myLng').value);
  if(!isFinite(lat)||!isFinite(lng)){
    if(myMarker){
      const ll = myMarker.getLatLng(); lat=ll.lat; lng=ll.lng;
    }else{
      alert('위치를 먼저 지정하세요. (내 위치/검색/지도 선택)');
      return;
    }
  }
  const mode = el('myMode').value;
  try{
    await apiPost('/api/room/update', { code: S.code, pid: S.pid, lat, lng, mode });
    el('geoStatus').textContent = `서버 저장됨 (${mode})`;
    await refreshState();
  }catch(e){
    alert('업데이트 실패: '+e.message);
  }
}
async function handleLeave(){
  if(!S.code || !S.pid){ alert('참가 상태가 아닙니다.'); return; }
  try{
    await apiPost('/api/room/leave', { code: S.code, pid: S.pid });
    S.pid=''; el('pidLabel').textContent='-';
    await refreshState(true);
  }catch(e){ alert('나가기 실패: '+e.message); }
}
async function handleClose(){
  if(!S.code){ alert('닫을 방 코드가 없습니다.'); return; }
  const hostSecret = (el('hostSecretInput').value||'').trim();
  if(!hostSecret){ alert('HostSecret을 입력하세요.'); return; }
  try{
    await apiPost('/api/room/close', { code: S.code, hostSecret });
    alert('방이 닫혔습니다.');
    S.code=''; S.pid=''; S.hostSecret=''; S.joinUrl='';
    location.href='/';
  }catch(e){ alert('방 닫기 실패: '+e.message); }
}
async function handleSuggest(){
  if(!S.code){ alert('먼저 방을 만들거나 참가하세요.'); return; }
  const category = el('cat').value;
  const radius = parseInt(el('radius').value||'2000',10);
  const query = el('q').value.trim();
  try{
    const r = await apiPost('/api/meeting-suggest', { roomCode:S.code, category, radius, query });
    renderSuggest(r.items||[], r.centroid);
  }catch(e){ alert('추천 실패: '+e.message); }
}
async function handleEta(){
  if(!S.code){ alert('먼저 방을 만들거나 참가하세요.'); return; }
  const searchRadius = parseInt(el('etaRadius').value||'2000',10);
  const includeTopN = parseInt(el('topN').value||'5',10);
  try{
    const r = await apiPost('/api/eta-centroid', { roomCode:S.code, searchRadius, includeTopN, twoStage:true });
    const sum = r.participants_eta?.map(p=>`${escapeHtml(p.nickname||'')||p.index}: ${p.eta_min}분`).join(' · ') || '';
    el('etaSummary').textContent = `중간지점 ETA 계산 완료. 후보(1단계 ${r.candidate_count_stage1} / 2단계 ${r.candidate_count_stage2}) ${sum? ' | '+sum:''}`;
    // 지도 표시
    clearMarks(); await refreshState(); // state에 best 저장됨
  }catch(e){ alert('ETA 계산 실패: '+e.message); }
}
function handleShowSaved(){ refreshState(true); }
function handleCopyLink(){
  if(!S.joinUrl){ alert('링크가 없습니다.'); return; }
  navigator.clipboard.writeText(S.joinUrl).then(()=>{ alert('초대링크 복사됨'); });
}

// ===== 초기화
function parseQuery(){
  const u = new URL(location.href);
  const code=u.searchParams.get('code');
  if(code){ S.code=code.toUpperCase(); el('code').value=S.code; el('codeLabel').textContent=S.code; openSheet('sheetJoin'); }
}
async function init(){
  initMap();
  attachLocResultClick();
  parseQuery();

  // 버튼/이벤트
  el('btnOpenCreate').onclick = ()=> openSheet('sheetCreate');
  el('btnOpenJoin').onclick   = ()=> openSheet('sheetJoin');
  el('closeCreate').onclick   = ()=> closeSheet('sheetCreate');
  el('closeJoin').onclick     = ()=> closeSheet('sheetJoin');

  el('btnCreate').onclick     = handleCreate;
  el('btnJoin').onclick       = handleJoin;

  el('btnLeave').onclick      = handleLeave;
  el('btnClose').onclick      = handleClose;
  el('btnCopyLink').onclick   = handleCopyLink;

  el('btnSuggest').onclick    = handleSuggest;
  el('btnShowSaved').onclick  = handleShowSaved;
  el('btnRefresh').onclick    = ()=>refreshState(true);

  el('btnEta').onclick        = handleEta;
  el('btnUpdate').onclick     = handleUpdate;

  // 위치 검색
  el('btnLocSearch').onclick = async()=>{
    const q = el('locSearch').value.trim(); if(!q) return;
    try{ const list = await searchLocation(q); showLocResults(list); }catch(e){ alert('검색 실패'); }
  };
  el('locSearch').addEventListener('keydown', (e)=>{ if(e.key==='Enter'){ e.preventDefault(); el('btnLocSearch').click(); }});
  el('btnPick').onclick = ()=>{
    pickMode = !pickMode;
    if(pickMode){ el('map').classList.add('picking'); } else { el('map').classList.remove('picking'); }
  };
  el('btnGeo').onclick = ()=>{
    if(!navigator.geolocation){ alert('브라우저에서 위치를 지원하지 않습니다.'); return; }
    navigator.geolocation.getCurrentPosition((pos)=>{
      const {latitude:lat, longitude:lng}=pos.coords;
      el('myLat').value=lat.toFixed(6); el('myLng').value=lng.toFixed(6);
      upsertMyMarker(lat,lng,true);
      const gs=el('geoStatus'); if(gs) gs.textContent='내 GPS 위치 적용됨';
    },(err)=>{ alert('위치 권한 필요: '+err.message); },{enableHighAccuracy:true,timeout:8000});
  };

  // 저장된 코드 정보 반영
  if(S.code){ fillRoomInfo({code:S.code, hostSecret:S.hostSecret, joinUrl:S.joinUrl}); await refreshState(); }
}

// ===== 안전한 DOMContentLoaded
window.addEventListener('DOMContentLoaded', () => {
  try { init(); } catch (e) {
    console.error('[init] error:', e);
    const dot = document.getElementById('health-dot');
    const txt = document.getElementById('health-text');
    if (dot) dot.className = 'dot red';
    if (txt) txt.textContent = '프론트 스크립트 오류';
  }
});

// ===================== END app.js =====================
