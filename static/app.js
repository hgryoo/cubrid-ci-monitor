"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private window */ } },
};
const REPO_URL = "https://github.com/CUBRID/cubrid";
const RECENT_PAGE = 20;

const S = {
  queue: null, recent: null, mine: null,
  view: store.get("gham-view") || "all",        // all | mine
  recentView: store.get("gham-recent") || "all", // all | fail
  recentLimit: RECENT_PAGE,
  me: new URLSearchParams(location.search).get("me") || store.get("gham-me") || null,
  loadedAt: 0,
};

const KIND = {
  pr: ["PR", "k-pr"], batch: ["야간", "k-batch"], push: ["push", "k-push"],
  dispatch: ["수동", "k-dispatch"],
};
const STATE_ICON = {
  success: "✓", failure: "✗", timed_out: "✗", cancelled: "–", skipped: "–",
  in_progress: "●", queued: "◌", waiting: "◌", pending: "◌", error: "!", unknown: "?",
};
const STATE_WORD = {
  success: "성공", failure: "실패", timed_out: "시간 초과", cancelled: "취소", skipped: "건너뜀",
  in_progress: "실행 중", queued: "러너 대기", waiting: "대기", pending: "진행 중",
  error: "판정 없음", unknown: "판정 미확인",
};

// ------------------------------------------------------------------ format
function dur(s) {
  if (s == null) return "—";
  s = Math.max(0, s | 0);
  const h = (s / 3600) | 0, m = ((s % 3600) / 60) | 0;
  if (h) return `${h}시간 ${m}분`;
  if (m) return `${m}분`;
  return `${s}초`;
}
const since = (iso) => iso ? (Date.now() - new Date(iso).getTime()) / 1000 : null;
function ago(iso) {
  const s = since(iso);
  if (s == null) return "—";
  if (s < 10) return "방금";
  if (s < 60) return `${s | 0}초 전`;
  if (s < 3600) return `${(s / 60) | 0}분 전`;
  if (s < 86400) return `${(s / 3600) | 0}시간 전`;
  return `${(s / 86400) | 0}일 전`;
}
const hhmm = (iso) => new Date(iso).toLocaleTimeString("ko-KR",
  { hour: "2-digit", minute: "2-digit", hour12: false });
const full = (iso) => new Date(iso).toLocaleString("ko-KR");
const live = (iso) => `<span data-since="${esc(iso || "")}">${dur(since(iso))}</span>`;

// ------------------------------------------------------------------ model
const isMine = (e) => !!S.me && (e.author === S.me || e.actor === S.me ||
  (e.assignees || []).includes(S.me));
const visible = (list) => S.view === "mine" ? list.filter(isMine) : list;
const isFail = (e) => e.conclusion === "failure" || e.conclusion === "timed_out" ||
  (e.verdicts || []).some((v) => v.state === "failure" || v.state === "error");
const activeRuns = () => S.queue ? S.queue.running.concat(S.queue.waiting) : [];

// a cancelled PR run with a newer run of the same PR was superseded by a new /run
function superseded(e) {
  if (e.kind !== "pr") return false;
  const newer = (x) => x.pr === e.pr && x.id !== e.id && x.created_at > e.created_at;
  return activeRuns().some(newer) || (S.recent ? S.recent.runs : []).some(newer);
}

// ------------------------------------------------------------------ pieces
function kindBadge(e) {
  const [label, cls] = KIND[e.kind] || [e.kind, "k-push"];
  return `<span class="kind ${cls}">${esc(label)}</span>`;
}

function headline(e) {
  let link, text;
  if (e.kind === "pr" && e.pr) {
    link = `<a class="pr" href="${REPO_URL}/pull/${e.pr}" target="_blank" rel="noopener">#${e.pr}</a>`;
    text = e.title;
  } else if (e.kind === "batch" || e.kind === "dispatch") {
    const sha = (e.sha || "").slice(0, 7);
    link = sha ? `<a class="pr" href="${REPO_URL}/commit/${esc(e.sha)}" target="_blank" rel="noopener">${esc(sha)}</a>` : "";
    text = e.kind === "batch" ? "develop 커밋 야간 검증" : e.title;
  } else {
    link = `<span class="pr plain">${esc(e.branch || "")}</span>`;
    text = e.title;
  }
  const tags = (e.rerun ? `<span class="tag">재실행</span>` : "") +
    (isMine(e) ? `<span class="tag-mine">내 PR</span>` : "");
  return `<div class="title-row">${kindBadge(e)}${link}<span class="ttl" title="${esc(text)}">${esc(text)}</span>${tags}</div>`;
}

function subline(e) {
  const bits = [];
  if (e.author) bits.push(`<span class="who">${esc(e.author)}</span>`);
  if (e.actor && e.actor !== e.author && !e.actor.endsWith("[bot]"))
    bits.push(`<span>요청 <span class="who">${esc(e.actor)}</span></span>`);
  if (e.suites && e.suites.length)
    bits.push(`<span class="suites">${e.suites.map((s) => `<span class="suite">${esc(s)}</span>`).join("")}</span>`);
  bits.push(`<a class="runlink" href="${esc(e.url)}" target="_blank" rel="noopener">런 ${e.id} ↗</a>`);
  return `<div class="sub">${bits.join("")}</div>`;
}

function shardBar(sh) {
  const pct = (n) => (100 * n / sh.total).toFixed(2) + "%";
  return `<span class="bar" aria-hidden="true">` +
    `<i class="b-ok" style="width:${pct(sh.done)}"></i>` +
    `<i class="b-fail" style="width:${pct(sh.failed)}"></i>` +
    `<i class="b-died" style="width:${pct(sh.died)}"></i>` +
    `<i class="b-run" style="width:${pct(sh.running)}"></i>` +
    `<i class="b-wait" style="width:${pct(sh.queued)}"></i></span>`;
}

function stageStrip(stages) {
  if (!stages || !stages.length) return "";
  return `<ol class="stages">` + stages.map((s) => {
    const st = s.state;
    if (s.shards) {
      const sh = s.shards;
      const fin = sh.done + sh.failed + sh.died + sh.cancelled;
      const tip = `${s.label} 샤드 ${sh.total}개 — 통과 ${sh.done}, 케이스 실패 ${sh.failed}, ` +
        `이상 종료 ${sh.died}, 실행 ${sh.running}, 대기 ${sh.queued}`;
      const bad = (sh.failed ? `<span class="bad">✗${sh.failed}</span>` : "") +
        (sh.died ? `<span class="bad died">!${sh.died}</span>` : "");
      return `<li class="stage shard st-${esc(st)}" title="${esc(tip)}">` +
        `<span class="nm">${esc(s.label)}</span>${shardBar(sh)}` +
        `<span class="num">${fin}/${sh.total}</span>${bad}</li>`;
    }
    return `<li class="stage st-${esc(st)}" title="${esc(s.label)}: ${esc(STATE_WORD[st] || st)}">` +
      `<span class="ic">${STATE_ICON[st] || "·"}</span>${esc(s.label)}</li>`;
  }).join(`<li class="sep" aria-hidden="true">›</li>`) + `</ol>`;
}

function alertChips(e) {
  const out = (e.alerts || []).map((a) => `<span class="alert a-${esc(a.level)}">${esc(a.text)}</span>`);
  if (e.jobs_error) out.push(`<span class="alert a-warn">잡 목록을 읽지 못했다</span>`);
  return out.length ? `<div class="alerts">${out.join("")}</div>` : "";
}

const owners = (list) => (list || []).map((o) => `${esc(o.label)} ${o.n}`).join(", ");
// runs ahead: names only, since the count beside them is already in runs
const labels = (list, total) => (list || []).map((o) => esc(o.label)).join(", ") +
  (total > (list || []).length ? " 외" : "");

// ------------------------------------------------------------------ lanes
const LANE_ROLE = {
  "cubrid-arc": "build · shell/sql/sql_by_cci 샤드",
  "cubrid-arc-light": "plan · collect · medium · 재실행 샤드",
};

function laneCard(ln) {
  const cap = ln.max;
  const full = cap != null && ln.running >= cap;
  const state = cap == null ? "" : full
    ? `<span class="lane-state hot">포화</span>`
    : `<span class="lane-state ok">여유 ${cap - ln.running}</span>`;
  // no known cap: a fixed scale of 40 so one job does not fill the bar
  const scale = cap || 40;
  const mineIds = new Set(activeRuns().filter(isMine).map((e) => e.id));
  const cls = (h) => `seg-${esc(h.kind || "push")}${mineIds.has(h.run) ? " mine" : ""}`;
  const segs = ln.holders.map((h) =>
    `<i class="${cls(h)}" style="width:${(100 * h.n / scale).toFixed(2)}%" title="${esc(h.label)} · ${h.n}"></i>`).join("");
  const top = ln.holders.slice(0, 4).map((h) =>
    `<span class="holder"><i class="sw ${cls(h)}"></i>${esc(h.label)} <b>${h.n}</b></span>`).join("");
  const rest = ln.holders.length > 4
    ? `<span class="holder muted">외 ${ln.holders.length - 4}개 런 ${ln.holders.slice(4).reduce((a, h) => a + h.n, 0)}</span>` : "";
  // counted in runs: every run queues about the same number of jobs; the job count goes in the tooltip
  const starting = ln.starting || 0, waiting = ln.queued - starting;
  const wr = ln.waiting_runs || 0, sr = ln.starting_runs || 0;
  let q;
  if (!ln.queued) q = `<span class="q-chip zero">대기 0</span>`;
  else if (!waiting) q = `<span class="q-chip boot" title="빈 슬롯이 있다. 러너 파드가 뜨기를 기다린다 (잡 ${starting}개)">기동 중 런 <b>${sr}</b></span>`;
  else q = `<span class="q-chip" title="슬롯이 비기를 기다리는 런 ${wr}개 · 잡 ${waiting}개">대기 런 <b>${wr}</b>${sr ? ` <small>+ 기동 중 ${sr}</small>` : ""}</span>`;
  const waitLine = ln.queued
    ? `<div class="lane-wait">대기 잡: ${owners(ln.waiters.slice(0, 4))}${ln.waiters.length > 4 ? " 외" : ""} · 최장 ${live(ln.oldest_since)}</div>` : "";
  return `<article class="lane${full ? " full" : ""}">
    <div class="lane-head"><h3>${esc(ln.name)}</h3>${state}</div>
    <div class="lane-num"><b>${ln.running}</b><span${ln.max_source === "observed" ? ` title="관측 상한: 잡이 러너를 오래 기다릴 때 이 레인에서 동시에 돈 최대 잡 수"` : ""}>${cap != null ? ` / ${cap}` : ""} 실행</span>${q}</div>
    <div class="meter${cap == null ? " nocap" : ""}" role="img" aria-label="${esc(ln.name)} 실행 ${ln.running}${cap != null ? ` / ${cap}` : ""}, 대기 런 ${ln.waiters.length}">${segs}</div>
    <div class="holders">${top}${rest}${ln.holders.length ? "" : `<span class="holder muted">비어 있음</span>`}</div>
    ${waitLine}
    <div class="lane-role">${esc(LANE_ROLE[ln.name] || "")}${cap == null ? " · 상한 미확인" : ln.max_source === "observed" ? " · 상한은 관측값" : ""}</div>
  </article>`;
}

// ------------------------------------------------------------------ cards
function runningCard(e) {
  let extra = "";
  if (e.queued_jobs && e.wait_lane) {
    const boot = e.starting_jobs ? `<span class="muted">잡 ${e.starting_jobs}개는 러너 파드가 뜨는 중</span>` : "";
    const slot = e.slot_jobs ? `<span class="warn">잡 ${e.slot_jobs}개 슬롯 대기` +
      (e.ahead ? ` — 앞에 런 ${e.ahead}개 (${labels(e.ahead_by, e.ahead)})` : "") + `</span>` : "";
    extra = [slot, boot].filter(Boolean).join(`<span class="dotsep">·</span>`);
  }
  return `<article class="card running${isMine(e) ? " mine" : ""}">
    <div class="meta">${headline(e)}${subline(e)}
      <div class="phase"><span class="live" aria-hidden="true"></span><span>${esc(e.phase)}</span>${extra ? `<span class="dotsep">·</span>${extra}` : ""}</div>
      ${alertChips(e)}
      ${stageStrip(e.stages)}
    </div>
    <div class="right"><div class="time run">${live(e.created_at)}</div><div class="time-l">요청 후</div></div>
  </article>`;
}

function waitingCard(e) {
  let where = `<span>${esc(e.phase)}</span>`;
  if (e.wait_lane) {
    const lane = `<code>${esc(e.wait_lane)}</code>`;
    const boot = e.starting_jobs && !e.starting ? ` <span class="muted">— ${e.starting_jobs}개는 러너 파드가 뜨는 중</span>` : "";
    if (e.starting) where += `<span class="dotsep">·</span><span>${lane}에 빈 슬롯이 있다 — 러너 파드가 뜨는 중</span>`;
    else if (e.ahead === 0) where += `<span class="dotsep">·</span><span>${lane} <b class="next">다음 차례</b>${boot}</span>`;
    else where += `<span class="dotsep">·</span><span>${lane} 앞에 런 <b>${e.ahead}</b>개` +
      (e.ahead_by && e.ahead_by.length ? ` <span class="muted">(${labels(e.ahead_by, e.ahead)})</span>` : "") + `${boot}</span>`;
  }
  const next = e.ahead === 0 || e.starting;
  return `<article class="card waiting${isMine(e) ? " mine" : ""}${next ? " is-next" : ""}">
    <div class="pos" title="대기 순번"><b>${e.position}</b><small>순번</small></div>
    <div class="meta">${headline(e)}${subline(e)}
      <div class="phase">${where}</div>
      ${alertChips(e)}
      ${stageStrip(e.stages)}
    </div>
    <div class="right"><div class="time wait">${live(e.queued_since || e.created_at)}</div>
      <div class="time-l">이번 대기 · 요청 후 ${live(e.created_at)}</div></div>
  </article>`;
}

function verdictChips(e) {
  if (e.detail_error) return `<span class="vchip st-unknown" title="이 런의 상세를 읽지 못했다"><span class="ic">?</span>상세 조회 실패</span>`;
  if (e.rejected) {
    return e.rejected === "gate_failed"
      ? `<span class="vchip st-failure"><span class="ic">✗</span>gate 실패</span>`
      : `<span class="vchip st-cancelled" title="gate가 요청을 받지 않았다 (/run 문법이 아니거나 권한 없음)"><span class="ic">–</span>요청 무시</span>`;
  }
  if (e.conclusion === "cancelled") {
    return `<span class="vchip st-cancelled"><span class="ic">–</span>${superseded(e) ? "새 /run 으로 취소" : "취소"}</span>`;
  }
  if (e.verdicts && e.verdicts.length) {
    return e.verdicts.map((v) => {
      const n = v.state === "failure" && v.failed ? ` <b>${v.failed}</b>` : "";
      const died = v.died ? ` <span class="died">!${v.died}</span>` : "";
      const tip = `${v.suite}: ${v.description || STATE_WORD[v.state] || v.state}` +
        (v.failed ? ` — 케이스 ${v.failed}건 실패` : "") + (v.died ? ` — 이상 종료 샤드 ${v.died}` : "");
      const body = `<span class="ic">${STATE_ICON[v.state] || "?"}</span>${esc(v.suite)}${n}${died}`;
      return v.state === "failure" && e.collect_url
        ? `<a class="vchip st-failure" href="${esc(e.collect_url)}" target="_blank" rel="noopener" title="${esc(tip)} — 눌러서 실패 목록">${body}</a>`
        : `<span class="vchip st-${esc(v.state)}" title="${esc(tip)}">${body}</span>`;
    }).join("");
  }
  if (e.build) {
    return `<span class="vchip st-${esc(e.build)}" title="빌드만 실행"><span class="ic">${STATE_ICON[e.build] || "?"}</span>build</span>`;
  }
  return `<span class="vchip st-unknown"><span class="ic">?</span>${esc(e.conclusion)}</span>`;
}

function recentRow(e) {
  const notes = (e.notes || []).length
    ? `<div class="notes" title="${esc(e.notes.join("\n"))}">${esc(e.notes[0])}</div>` : "";
  return `<article class="row c-${esc(e.conclusion)}${isMine(e) ? " mine" : ""}">
    <div class="when" title="${esc(full(e.updated_at))}">${esc(hhmm(e.updated_at))}<small>${esc(ago(e.updated_at))}</small></div>
    <div class="what">${headline(e)}${subline(e)}${notes}</div>
    <div class="verdicts">${verdictChips(e)}</div>
    <div class="took" title="요청부터 끝날 때까지">${dur(e.duration_s)}<small>걸림</small></div>
  </article>`;
}

function summary(rows) {
  const n = rows.length;
  if (!n) return "";
  const fail = rows.filter(isFail).length;
  const bySuite = {}, st = { error: 0, unknown: 0, died: 0 };
  rows.forEach((e) => (e.verdicts || []).forEach((v) => {
    if (v.state === "failure") bySuite[v.suite] = (bySuite[v.suite] || 0) + 1;
    if (v.state === "error") st.error++;
    if (v.state === "unknown") st.unknown++;
    st.died += v.died || 0;
  }));
  const guards = rows.filter((e) => (e.notes || []).length).length;
  const cancelled = rows.filter((e) => e.conclusion === "cancelled").length;
  const suites = Object.entries(bySuite).map(([k, v]) => `${k} ${v}`).join(" · ");
  const bits = [`런 <b>${n}</b>개`, `실패 <b class="f">${fail}</b>${suites ? ` (${suites})` : ""}`];
  if (st.died) bits.push(`이상 종료 샤드 <b class="f">${st.died}</b>`);
  if (guards) bits.push(`가드 실패 <b class="f">${guards}</b>`);
  if (st.error) bits.push(`판정 없음 ${st.error}`);
  if (st.unknown) bits.push(`판정 미확인 ${st.unknown}`);
  if (cancelled) bits.push(`취소 ${cancelled}`);
  return bits.join(`<span class="dotsep">·</span>`);
}

const runIdOf = (url) => ((url || "").match(/\/runs\/(\d+)/) || [])[1];
const recentById = (id) => S.recent && S.recent.runs.find((r) => String(r.id) === String(id));

function mineRow(p) {
  const active = activeRuns().find((e) => e.pr === p.pr);
  const now = active
    ? `<span class="tag ${active.category === "waiting" ? "t-wait" : "t-run"}">${active.category === "waiting" ? `대기 ${active.position}번째` : "진행 중"}</span>` : "";
  const ctx = {};
  p.contexts.forEach((c) => { ctx[c.name] = c; });
  const chip = (c, label) => {
    const st = c.state === "pending" ? "in_progress" : c.state;
    const run = recentById(runIdOf(c.url));
    const v = run && (run.verdicts || []).find((x) => `test_${x.suite}` === c.name);
    const n = st === "failure" && v && v.failed ? ` <b>${v.failed}</b>` : "";
    const href = st === "failure" && run && run.collect_url ? run.collect_url : c.url;
    const body = `<span class="ic">${STATE_ICON[st] || "?"}</span>${esc(label)}${n}`;
    return `<a class="vchip st-${esc(st)}" href="${esc(href || "#")}" target="_blank" rel="noopener" title="${esc(c.description || "")} · ${esc(ago(c.updated_at))}">${body}</a>`;
  };
  const chips = [];
  const bd = ctx["build (debug)"], br = ctx["build (release)"];
  if (bd && br && bd.state === br.state) chips.push(chip(bd, "build"));
  else { if (bd) chips.push(chip(bd, "build d")); if (br) chips.push(chip(br, "build r")); }
  ["shell", "sql", "medium", "sql_by_cci"].forEach((s) => { const c = ctx[`test_${s}`]; if (c) chips.push(chip(c, s)); });
  return `<article class="row">
    <div class="when">${p.updated_at ? esc(ago(p.updated_at)) : "—"}</div>
    <div class="what"><div class="title-row"><a class="pr" href="${REPO_URL}/pull/${p.pr}" target="_blank" rel="noopener">#${p.pr}</a>` +
    `<span class="ttl" title="${esc(p.title)}">${esc(p.title)}</span>${p.draft ? `<span class="tag">draft</span>` : ""}${now}</div></div>
    <div class="verdicts">${chips.length ? chips.join("") : earlierHead(p)}</div>
    <div class="took"></div>
  </article>`;
}

// the head moved after the last run: say so, and show what the earlier head got
function earlierHead(p) {
  const last = S.recent && S.recent.runs
    .filter((r) => r.pr === p.pr && (r.verdicts || []).length && r.conclusion !== "cancelled")
    .sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""))[0];
  if (!last) return `<span class="vchip st-cancelled">이 head에 판정 없음</span>`;
  return `<span class="prev"><small>이 head에 판정 없음 · 이전 head (${esc(ago(last.updated_at))})</small>${verdictChips(last)}</span>`;
}

// ------------------------------------------------------------------ render
function empty(msg) { return `<div class="empty">${msg}</div>`; }
function count(el, shown, total) {
  $(el).textContent = S.view === "mine" && shown !== total ? `${shown} / ${total}` : String(total);
}

// runs grouped by lane, in lane order; runs with no lane job (GitHub-hosted only, or none yet) last
function byLane(list, card, outside) {
  const groups = new Map(S.queue.lanes.map((ln) => [ln.name, []]));
  const rest = [];
  list.forEach((e) => (groups.get(e.lane) || rest).push(e));
  const head = (name, n, hint) => `<div class="lane-group"><h3>${name}</h3><span class="n">${n}</span>` +
    (hint ? `<span class="hint">${hint}</span>` : "") + `</div>`;
  return [...groups].filter(([, es]) => es.length)
    .map(([name, es]) => head(`<code>${esc(name)}</code>`, es.length) + es.map(card).join(""))
    .concat(rest.length ? [head("레인 밖", rest.length, outside) + rest.map(card).join("")] : [])
    .join("");
}

function renderQueue() {
  const d = S.queue;
  if (!d) return;
  $("lanes").innerHTML = d.lanes.map(laneCard).join("");
  const run = visible(d.running), wait = visible(d.waiting);
  count("c-running", run.length, d.running.length);
  count("c-waiting", wait.length, d.waiting.length);
  const scope = S.view === "mine" ? "내 PR 중 " : "";
  $("running").innerHTML = run.length ? byLane(run, runningCard, "GitHub 호스트 잡만 돌고 있다")
    : empty(`${scope}진행 중인 런이 없다.`);
  $("waiting").innerHTML = wait.length ? byLane(wait, waitingCard, "러너 잡이 아직 없다 (동시성 그룹 대기 등)")
    : empty(`${scope}러너를 기다리는 런이 없다.`);
  const mineRun = d.running.filter(isMine).length, mineWait = d.waiting.filter(isMine).length;
  document.title = (mineRun || mineWait)
    ? `${mineRun ? `▶${mineRun} ` : ""}${mineWait ? `⏸${mineWait} ` : ""}· gha-ci 러너 큐` : "gha-ci 러너 큐";
}

function renderRecent() {
  const d = S.recent;
  if (!d) return;
  const base = visible(d.runs).slice().sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
  const rows = S.recentView === "fail" ? base.filter(isFail) : base;
  $("c-recent").textContent = rows.length === d.runs.length ? String(d.runs.length)
    : `${rows.length} / ${d.runs.length}`;
  $("recent-hint").textContent = `지난 ${d.hours}시간 · 요청 없는 코멘트 런 제외`;
  $("recent-sum").innerHTML = summary(base);
  const scope = S.view === "mine" ? "내 PR 중 " : "";
  if (!rows.length) {
    $("recent").innerHTML = empty(scope + (S.recentView === "fail" && base.length ? "실패한 런이 없다." : "완료된 런이 없다."));
    return;
  }
  const shown = rows.slice(0, S.recentLimit);
  const more = rows.length - shown.length;
  const unknown = shown.some((e) => (e.verdicts || []).some((v) => v.state === "unknown"));
  $("recent").innerHTML = `<div class="rows">${shown.map(recentRow).join("")}</div>` +
    (more ? `<button class="more" id="more">${Math.min(more, RECENT_PAGE)}건 더 보기 <small>(남은 ${more}건)</small></button>` : "") +
    `<p class="legend">✗ 뒤 숫자는 실패 케이스 수, !는 이상 종료 샤드 수다. 실패 칩을 누르면 collect 잡의 실패 목록으로 간다.` +
    (unknown ? ` ?는 이 런이 게시한 commit status를 찾지 못한 경우다.` : "") + `</p>`;
  if (more) $("more").onclick = () => { S.recentLimit += RECENT_PAGE; renderRecent(); };
}

function renderMine() {
  $("mine-sec").hidden = S.view !== "mine";
  if (S.view !== "mine") return;
  const d = S.mine;
  if (!d || d.login !== S.me) { $("mine").innerHTML = empty("불러오는 중…"); return; }
  $("c-mine").textContent = d.prs.length || "";
  if (!d.prs.length) { $("mine").innerHTML = empty(`작성하거나 담당한 열린 PR이 없다 (@${esc(d.login)}).`); return; }
  // PRs with no verdict anywhere in view fold into one line, so live runs stay near the top
  const quiet = (p) => !p.contexts.length && !activeRuns().some((e) => e.pr === p.pr) &&
    !(S.recent && S.recent.runs.some((r) => r.pr === p.pr));
  const shown = d.prs.filter((p) => !quiet(p)), folded = d.prs.filter(quiet);
  const fold = folded.length ? `<article class="row norun"><div class="when">—</div><div>판정 없는 PR ${folded.length}개: ` +
    folded.map((p) => `<a class="pr" href="${REPO_URL}/pull/${p.pr}" target="_blank" rel="noopener" title="${esc(p.title)}">#${p.pr}</a>`).join(" ") +
    `</div></article>` : "";
  $("mine").innerHTML = `<div class="rows">${shown.map(mineRow).join("")}${fold}</div>`;
}

function renderChrome() {
  $("f-all").setAttribute("aria-pressed", S.view === "all");
  $("f-mine").setAttribute("aria-pressed", S.view === "mine");
  $("r-all").setAttribute("aria-pressed", S.recentView === "all");
  $("r-fail").setAttribute("aria-pressed", S.recentView === "fail");
  $("me").textContent = S.me ? `@${S.me}` : "";
  const d = S.queue;
  const errs = [].concat(d ? d.errors : [], S.recent ? S.recent.errors : []);
  const warn = errs.length
    ? ` · <span class="warn" title="${esc(errs.join("\n"))}">API 경고 ${errs.length}건</span>` : "";
  $("footer").innerHTML = (d ? `큐 ${d.duration_ms}ms` : "") +
    (S.recent ? ` · 최근 완료 ${S.recent.duration_ms}ms` : "") + warn +
    ` · 내 PR 기준 <button class="linkish" id="set-me">@${esc(S.me || "?")}</button><br>` +
    `대기 순번은 레인마다 잡이 큐에 들어간 시각 순이다. GitHub가 FIFO를 보장하지는 않는다. ` +
    (d && d.cap_rule ? `레인 상한은 API로 읽을 수 없어, 잡이 러너를 ${d.cap_rule.waited_min}분 넘게 기다릴 때 ` +
      `동시에 돈 최대 잡 수로 잡고 ${d.cap_rule.kept_h}시간 유지한다.` : "") + `<br>` +
    `데이터: GitHub Actions API · 토큰은 서버에만 둔다 · <a href="${REPO_URL}/actions/workflows/gha-ci.yml" target="_blank" rel="noopener">gha-ci 런 목록 ↗</a>`;
  $("set-me").onclick = () => {
    const v = prompt("'내 PR'로 볼 GitHub 로그인", S.me || "");
    if (v != null && v.trim()) {
      S.me = v.trim().replace(/^@/, ""); store.set("gham-me", S.me);
      rerender(); if (S.view === "mine") loadMine(false);
    }
  };
}

function rerender() { renderMine(); renderQueue(); renderRecent(); renderChrome(); }

// ------------------------------------------------------------------ load
let busy = 0;
function spin(delta) {
  busy += delta;
  $("refresh").classList.toggle("spin", busy > 0);
  $("refresh").disabled = busy > 0;
}
async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  const d = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
  if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}
const banners = {};
function banner(key, html, level) {
  if (html) banners[key] = `<div class="banner ${level || "err"}">${html}</div>`;
  else delete banners[key];
  $("error").innerHTML = Object.values(banners).join("");
}
function dim(on) {
  ["lanes", "running", "waiting"].forEach((id) => $(id).classList.toggle("dim", on));
}

function loadMine(force) {
  if (S.view !== "mine" || !S.me) return Promise.resolve();
  const login = S.me;
  return getJSON(`/api/mine?login=${encodeURIComponent(login)}${force ? "&force=1" : ""}`)
    .then((d) => { if (login === S.me) { S.mine = d; renderMine(); banner("mine"); } })
    .catch((e) => banner("mine", `내 PR 판정 불러오기 실패: ${esc(e.message)}`));
}

function load(force) {
  if (busy) return;
  const q = force ? "?force=1" : "";
  spin(3);
  getJSON("/api/queue" + q)
    .then((d) => {
      S.queue = d; S.loadedAt = Date.now();
      if (!S.me && d.viewer) { S.me = d.viewer; loadMine(false); }
      dim(false); banner("queue");
      banner("partial", d.partial ? "일부 런의 잡 목록을 읽지 못했다 — 그 런의 단계와 레인 수치가 빠져 있다." : "", "warn");
      rerender();
    })
    .catch((e) => {
      dim(!!S.queue);
      banner("partial");
      banner("queue", `큐 불러오기 실패: ${esc(e.message)}` +
        (S.queue ? ` — 흐린 값은 ${esc(ago(new Date(S.loadedAt).toISOString()))} 것이다.` : ""));
    })
    .finally(() => { spin(-1); tick(); });
  getJSON("/api/recent" + q)
    .then((d) => { S.recent = d; banner("recent"); renderMine(); renderRecent(); renderChrome(); })
    .catch((e) => banner("recent", `최근 완료 불러오기 실패: ${esc(e.message)}`))
    .finally(() => spin(-1));
  loadMine(force).finally(() => spin(-1));
}

// live clocks: durations tick between refreshes
function tick() {
  document.querySelectorAll("[data-since]").forEach((el) => {
    if (el.dataset.since) el.textContent = dur(since(el.dataset.since));
  });
  if (!S.loadedAt) return;
  const age = (Date.now() - S.loadedAt) / 1000;
  $("updated").textContent = ago(new Date(S.loadedAt).toISOString());
  const auto = parseInt($("auto").value, 10);
  $("updated-wrap").classList.toggle("stale", age > Math.max(120, 2 * auto));
}
setInterval(tick, 1000);

// auto refresh shares the server cache with every other open tab
let timer = null;
function setAuto() {
  if (timer) clearInterval(timer);
  const s = parseInt($("auto").value, 10);
  store.set("gham-auto", String(s));
  if (s > 0) timer = setInterval(() => { if (!document.hidden) load(false); }, s * 1000);
}
document.addEventListener("visibilitychange", () => {
  const s = parseInt($("auto").value, 10);
  if (!document.hidden && s > 0 && Date.now() - S.loadedAt > s * 1000) load(false);
});

// ------------------------------------------------------------------ theme / wiring
function setTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  store.set("gham-theme", t);
}
(function init() {
  const t = store.get("gham-theme");
  if (t) setTheme(t);
  else if (window.matchMedia("(prefers-color-scheme: light)").matches) setTheme("light");
  const a = store.get("gham-auto");
  if (a && [...$("auto").options].some((o) => o.value === a)) $("auto").value = a;
})();

function setView(v) {
  S.view = v; S.recentLimit = RECENT_PAGE; store.set("gham-view", v);
  rerender(); loadMine(false);
}
function setRecentView(v) {
  S.recentView = v; S.recentLimit = RECENT_PAGE; store.set("gham-recent", v);
  renderRecent(); renderChrome();
}
$("f-all").onclick = () => setView("all");
$("f-mine").onclick = () => setView("mine");
$("r-all").onclick = () => setRecentView("all");
$("r-fail").onclick = () => setRecentView("fail");
$("refresh").onclick = () => load(true);
$("auto").onchange = setAuto;
$("theme").onclick = () =>
  setTheme(document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light");

renderChrome();
renderMine();
load(false);
setAuto();
