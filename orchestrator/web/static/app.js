'use strict';

const $ = (id) => document.getElementById(id);
const STATE_LABEL = { idle: '대기', prep: '준비', round: '진행중', break: '브레이크', finished: '종료' };

let latest = null;
let localRemaining = 0;
let localPull = null;

function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

function clock(sec) {
  if (sec == null) return '—';
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  const mm = String(m).padStart(2, '0');
  const ss = String(s).padStart(2, '0');
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

function localTime(ts) {
  if (!ts) return '—';
  const d = typeof ts === 'number' ? new Date(ts * 1000) : new Date(ts);
  if (isNaN(d)) return String(ts);
  return d.toLocaleString('ko-KR', { hour12: false });
}

function bytes(n) {
  if (!n && n !== 0) return '';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(1) + ' MB';
}

function pct(x) {
  return x == null ? '—' : (x * 100).toFixed(1) + '%';
}

// ── 렌더 ────────────────────────────────────────────────────────────────
function render(d) {
  const st = d.status;

  $('compName').firstChild.textContent = d.competition.name;
  $('compSub').textContent = d.competition.subtitle || '';
  $('teamBadge').textContent = `${d.team.slug} · ${d.team.name}`;
  $('nodePlatform').textContent = d.competition.node_platform;

  const badge = $('stateBadge');
  badge.textContent = STATE_LABEL[st.state] || st.state;
  badge.className = 'badge ' + (st.state === 'round' ? 'live' : st.state === 'break' || st.state === 'prep' ? 'warn' : 'off');

  $('phaseLabel').textContent = st.state === 'finished'
    ? '대회 종료'
    : `${st.phase} Round ${st.round || '—'}`;
  $('globalRound').textContent = st.global_round
    ? `전역 R${st.global_round} / ${st.total_rounds}` : '';
  $('timerNote').textContent = st.state === 'round' ? '남음'
    : st.state === 'break' || st.state === 'prep' ? '다음 라운드까지' : '';
  $('activeLayers').textContent = (st.active_layers || []).map((n) => 'L' + n).join(', ') || '—';
  $('submitOpen').textContent = st.state === 'round' ? '열림' : '닫힘';
  $('roundProgress').textContent = `${st.rounds_done} / ${st.total_rounds}`;

  const rs = d.round_stats;
  $('roundTag').textContent = st.global_round ? `아티팩트 태그 ${tagOf(st)}` : '';
  $('slaValue').textContent = rs.sla_total ? pct(rs.sla_ratio) : '—';
  $('slaValue').className = 'v ' + (rs.sla_ratio == null ? '' : rs.sla_ratio >= 0.99 ? 'ok' : rs.sla_ratio >= 0.9 ? 'warn' : 'bad');
  $('slaSub').textContent = rs.sla_total ? `${rs.sla_ok} / ${rs.sla_total} 성공` : '검사 대기';
  $('stolenValue').textContent = rs.stolen_layers.length;
  $('stolenValue').className = 'v ' + (rs.stolen_layers.length ? 'bad' : 'ok');
  $('stolenSub').textContent = rs.stolen_layers.length
    ? rs.stolen_layers.map((n) => 'L' + n).join(', ') : '방어 유지';
  $('capturedValue').textContent = rs.captured;
  $('acceptValue').textContent = rs.accept.toLocaleString();
  $('dropValue').textContent = rs.drop.toLocaleString();
  $('timeoutValue').textContent = rs.timeout.toLocaleString();
  $('agentValue').textContent = rs.agent_connected ? '연결' : '미연결';
  $('agentValue').className = 'v ' + (rs.agent_connected ? 'ok' : 'bad');
  $('agentSub').textContent = rs.agent_connected
    ? `재시작 ${rs.restarts}회` : 'fail-open · 전 패킷 ACCEPT';

  // 진입점
  $('entryRows').innerHTML = d.entrypoints.map((e) => `
    <tr>
      <td>L${e.layer}</td>
      <td>${esc(e.title)}</td>
      <td class="mono">${esc(e.host)}</td>
      <td class="mono">${esc(e.ip)}</td>
      <td class="num">${e.port}</td>
      <td><span class="chip ${e.active ? 'on' : 'off'}">${e.active ? '활성' : '비활성'}</span></td>
    </tr>`).join('');

  // 이미지
  const roles = ['defender', 'attacker'];
  $('imageRows').innerHTML = roles.map((role) => {
    const img = d.images[role] || {};
    const applied = img.applied
      ? '<span class="chip on">반영</span>'
      : `<span class="chip ${img.warning ? 'bad' : 'off'}">미반영</span>`;
    const plat = img.platform || '—';
    const platBad = img.platform && img.platform !== d.competition.node_platform;
    return `<tr>
      <td>${role}</td>
      <td>${esc(localTime(img.pushed_at))}</td>
      <td>${esc(localTime(img.pulled_at))}</td>
      <td class="mono" style="${platBad ? 'color:var(--danger-fg)' : ''}">${esc(plat)}</td>
      <td>${applied}</td>
    </tr>`;
  }).join('');

  $('imageDigests').innerHTML = roles.map((role) => {
    const img = d.images[role] || {};
    if (!img.digest) return '';
    return `<div class="meta"><b>${role}</b> <span class="mono" style="font-size:11px">${esc(img.digest)}</span></div>`;
  }).join('');

  const warns = roles
    .filter((r) => (d.images[r] || {}).warning)
    .map((r) => `<div class="warnbox"><b>${r}</b> — ${esc(d.images[r].warning)}</div>`);
  $('imageWarnings').innerHTML = warns.join('');

  // Flags captured
  $('captureRows').innerHTML = d.captures.length ? d.captures.map((c) => `
    <tr>
      <td class="mono">${esc(localTime(c.ts).slice(-8))}</td>
      <td>team${c.victim}</td>
      <td>L${c.layer}</td>
      <td class="num">R${c.round}</td>
      <td><span class="chip on">Accepted</span></td>
    </tr>`).join('') : '<tr><td colspan="5" class="none">아직 없음</td></tr>';

  // 라운드 히스토리
  $('roundRows').innerHTML = d.rounds.length ? d.rounds.slice().reverse().map((r) => `
    <tr>
      <td>${esc(r.label)} <span class="mono" style="color:var(--faint);font-size:11px">${esc(r.tag)}</span></td>
      <td class="num" style="${r.stolen ? 'color:var(--danger-fg)' : 'color:var(--green-fg)'}">${r.stolen}</td>
      <td>${(r.stolen_layers || []).map((n) => 'L' + n).join(', ') || '—'}</td>
      <td class="num">${pct(r.sla_ratio)}</td>
      <td class="num">${(r.drop || 0).toLocaleString()}</td>
      <td class="num">${r.score}</td>
    </tr>`).join('') : '<tr><td colspan="6" class="none">종료된 라운드 없음</td></tr>';

  // 스코어보드
  $('scoreRows').innerHTML = d.scoreboard.map((row) => `
    <tr class="${row.id === d.team.id ? 'me' : ''}">
      <td class="num">${row.rank}</td>
      <td>${esc(row.slug)} <span style="color:var(--faint)">${esc(row.name)}</span></td>
      <td class="num">${row.score}</td>
      <td class="num">${row.captured}</td>
      <td class="num" style="${row.stolen ? 'color:var(--danger-fg)' : ''}">${row.stolen}</td>
      <td class="num">${pct(row.sla_ratio)}</td>
    </tr>`).join('');

  // 아티팩트
  const byRound = {};
  d.artifacts.forEach((a) => { (byRound[a.tag] = byRound[a.tag] || []).push(a); });
  const tags = Object.keys(byRound);
  $('artifactList').innerHTML = tags.length ? tags.map((tag) => `
    <div style="margin-bottom:12px">
      <div class="meta" style="margin-bottom:6px"><b>${esc(tag)}</b></div>
      ${byRound[tag].map((a) => `<a class="dl" href="/artifacts/${encodeURIComponent(a.name)}">${esc(a.name)} <span style="color:var(--faint)">${bytes(a.size)}</span></a>`).join('')}
    </div>`).join('') : '<span class="none">라운드 종료 후 제공됩니다</span>';

  // LLM
  $('llmUsed').textContent = '$' + (d.llm.used || 0).toFixed(2);
  $('llmBudget').textContent = '$' + (d.llm.budget || 0).toFixed(2);
  const left = (d.llm.budget || 0) - (d.llm.used || 0);
  $('llmLeft').textContent = `잔여 $${left.toFixed(2)}`;

  // 자격 증명
  $('submitToken').textContent = d.credentials.submit_token;
  $('registryHost').textContent = d.credentials.registry;
  $('pushCmd').textContent = [
    `docker buildx build --platform ${d.competition.node_platform} \\`,
    '  --provenance=false --sbom=false \\',
    `  -t ${d.credentials.defender_image} --push .`,
    '',
    `docker pull --platform ${d.competition.node_platform} ${d.credentials.defender_image}`,
    `docker image inspect ${d.credentials.defender_image} --format '{{.Os}}/{{.Architecture}}'`,
  ].join('\n');

  // 이벤트
  $('events').textContent = d.events.length
    ? d.events.map((e) => `${localTime(e.ts).slice(-8)}  ${e.message}`).join('\n')
    : '—';
}

function tagOf(st) {
  return st.phase_index ? `P${st.phase_index}-R${st.global_round}` : '—';
}

function tick() {
  if (!latest) return;
  const st = latest.status;
  if (localRemaining > 0) localRemaining -= 1;
  $('remain').textContent = clock(localRemaining);
  const total = st.state === 'round' ? st.round_seconds : null;
  $('barFill').style.width = total
    ? (100 * Math.max(0, localRemaining) / total) + '%'
    : '100%';
  if (localPull != null) {
    if (localPull > 0) localPull -= 1;
    $('nextPull').textContent = clock(localPull);
  }
}

async function refresh() {
  let data;
  try {
    const res = await fetch('/api/state', { credentials: 'same-origin' });
    if (res.status === 401) { location.reload(); return; }
    data = await res.json();
  } catch (_) { return; }
  latest = data;
  localRemaining = data.status.remaining || 0;
  localPull = data.status.next_pull_in;
  if (localPull == null) $('nextPull').textContent = data.status.state === 'round' ? '라운드 진행 중' : '—';
  render(data);
}

document.getElementById('logout').addEventListener('click', async (e) => {
  e.preventDefault();
  await fetch('/logout', { method: 'POST' });
  location.reload();
});

setInterval(refresh, 2500);
setInterval(tick, 1000);
refresh();
