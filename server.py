#!/usr/bin/env python3
"""CUBRID gha-ci runner-queue monitor.

Serves a dashboard on http://<host>:7070 for the GitHub Actions `gha-ci`
workflow of CUBRID/cubrid. The bottleneck there is not one test slot but two
self-hosted runner lanes (cubrid-arc, cubrid-arc-light) that every run's jobs
queue on, first in first out. The GitHub token stays on the server; the
browser only talks to /api/queue and /api/recent.
"""
import concurrent.futures as cf
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = os.path.dirname(os.path.abspath(__file__))
GH_API = "https://api.github.com"
REPO = os.environ.get("REPO", "CUBRID/cubrid")
WORKFLOW = "gha-ci.yml"

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "7070"))


def _parse_lanes(spec):
    # "cubrid-arc=100,cubrid-arc-light=" -> {"cubrid-arc": 100, "cubrid-arc-light": None}
    lanes = {}
    for part in spec.split(","):
        name, _, cap = part.strip().partition("=")
        if name:
            lanes[name] = int(cap) if cap.strip().isdigit() else None
    return lanes


# maxRunners is not readable through the API (the runners endpoint needs admin). A lane with
# no cap in LANES takes the most runners it held while a job had waited for one past
# SATURATED_AFTER_S, far longer than a pod boots; that peak is kept for CAP_TTL_S.
LANES = _parse_lanes(os.environ.get("LANES", "cubrid-arc=,cubrid-arc-light="))
SATURATED_AFTER_S = 600
CAP_TTL_S = 24 * 3600
_cap_seen = {}  # lane -> (peak runners, epoch seconds it was last reached)


def lane_cap(lane, running, waited_s, now_s):
    """(cap, "fixed" | "observed"), or (None, None) when no full lane was seen lately."""
    if LANES[lane] is not None:
        return LANES[lane], "fixed"
    peak, at = _cap_seen.get(lane, (None, 0.0))
    if peak is not None and now_s - at > CAP_TTL_S:
        peak = None
    full = waited_s is not None and waited_s > SATURATED_AFTER_S and running > 0
    # running never exceeds the cap, so a higher count raises the peak even off a full lane
    if (full and (peak is None or running >= peak)) or (peak is not None and running > peak):
        peak, at = running, now_s
        _cap_seen[lane] = (peak, at)
    return (peak, "observed") if peak is not None else (None, None)
SUITES = ("shell", "sql", "medium", "sql_by_cci")
ACTIVE_STATUSES = ("in_progress", "queued", "pending", "waiting", "requested")
RECENT_HOURS = int(os.environ.get("RECENT_HOURS", "24"))
RECENT_CAP = 300     # a safety net, not a window: the window is RECENT_HOURS
COMMIT_WALK = 8      # PR commits to search back for a run's statuses once the head moved
SHARD_TIMEOUT_S = 120 * 60        # timeout-minutes of the shard job in gha-ci.yml
STALL_ABS_S = SHARD_TIMEOUT_S * 3 // 4
CACHE_TTL = 3.0      # seconds; ?force=1 bypasses
PR_META_TTL = 60.0

_lock = threading.Lock()
_cache = {}          # key -> (at, data)
_run_detail = {}     # (run id, attempt, updated_at) -> (expires or None, detail)


# --------------------------------------------------------------------------- #
# credentials / http
# --------------------------------------------------------------------------- #
_token = None


def gh_token():
    global _token
    if _token:
        return _token
    t = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not t:
        try:
            t = subprocess.check_output(["gh", "auth", "token"], text=True,
                                        stderr=subprocess.DEVNULL)
        except Exception:
            raise RuntimeError("No GitHub token: set GITHUB_TOKEN or run `gh auth login`")
    _token = t.strip()
    return _token


def _headers():
    return {"Authorization": f"Bearer {gh_token()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def gh(path, **params):
    url = f"{GH_API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def age_s(now, ts):
    t = parse_ts(ts)
    return round((now - t).total_seconds()) if t else None


def cached(key, ttl, fn, force=False):
    with _lock:
        hit = _cache.get(key)
        if hit and not force and time.time() - hit[0] < ttl:
            return hit[1]
    data = fn()
    with _lock:
        _cache[key] = (time.time(), data)
    return data


# --------------------------------------------------------------------------- #
# GitHub fetch
# --------------------------------------------------------------------------- #
def fetch_runs(max_pages=5, **params):
    # page 1 says how many there are; the rest go out together
    path = f"repos/{REPO}/actions/workflows/{WORKFLOW}/runs"
    params.update(per_page=100, exclude_pull_requests="true")
    first = gh(path, page=1, **params)
    runs = first.get("workflow_runs", [])
    pages = min(max_pages, -(-first.get("total_count", 0) // 100))
    if pages > 1:
        with cf.ThreadPoolExecutor(max_workers=pages - 1) as ex:
            for d in ex.map(lambda n: gh(path, page=n, **params), range(2, pages + 1)):
                runs += d.get("workflow_runs", [])
    return runs


def fetch_jobs(run_id):
    jobs, page = [], 1
    while True:
        d = gh(f"repos/{REPO}/actions/runs/{run_id}/jobs",
               per_page=100, page=page, filter="latest")
        jobs += d.get("jobs", [])
        if len(jobs) >= d.get("total_count", 0) or not d.get("jobs"):
            return jobs
        page += 1


def fetch_pr_meta():
    meta = {}
    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        pages = list(ex.map(lambda n: gh(f"repos/{REPO}/pulls", state="open",
                                         per_page=100, page=n), (1, 2, 3)))
    for rows in pages:
        for pr in rows:
            meta[pr["number"]] = {
                "title": pr["title"],
                "author": pr["user"]["login"],
                "assignees": [a["login"] for a in pr.get("assignees") or []],
                "head_sha": pr["head"]["sha"],
                "draft": pr.get("draft", False),
            }
    return meta


def fetch_viewer():
    try:
        return gh("user").get("login")
    except Exception:
        return None


def run_statuses(sha, run_id):
    """The newest gha-ci commit status per context that this run posted on sha."""
    if not sha:
        return {}
    out = {}
    for st in gh(f"repos/{REPO}/commits/{sha}/statuses", per_page=100):
        ctx = st.get("context", "")
        if not ctx.startswith("gha-ci: ") or ctx in out:
            continue
        if (st.get("target_url") or "").rstrip("/").endswith(f"/runs/{run_id}"):
            out[ctx] = {"state": st["state"], "description": st.get("description")}
    return out


def pr_commits(pr):
    shas = []
    for page in (1, 2, 3):  # the API stops at 250
        rows = gh(f"repos/{REPO}/pulls/{pr}/commits", per_page=100, page=page)
        shas += [c["sha"] for c in rows]
        if len(rows) < 100:
            break
    return shas


def find_run_statuses(e, pr_meta):
    """Statuses a run posted. A PR run posts on the head it saw, which may no longer be
    the head, or the PR may be closed: then search back through the PR's commits."""
    shas = [e["sha"]] if e["sha"] else []
    if e["kind"] == "pr":
        if e["pr"] not in pr_meta:
            shas.append(cached(f"pr:{e['pr']}", 600,
                               lambda: gh(f"repos/{REPO}/pulls/{e['pr']}"))["head"]["sha"])
        commits = cached(f"prc:{e['pr']}", 300, lambda: pr_commits(e["pr"]))
        shas += [c for c in reversed(commits[-COMMIT_WALK:]) if c not in shas]
    for sha in shas:
        sts = run_statuses(sha, e["id"])
        if sts:
            return sts
    return {}


# --------------------------------------------------------------------------- #
# run model
# --------------------------------------------------------------------------- #
SHA_RE = re.compile(r"\b([0-9a-f]{40})\b")


def classify(run, pr_meta):
    """kind, pr number, title, author and the engine SHA the run's statuses land on."""
    title = run.get("display_title") or ""
    ev = run.get("event")
    actor = (run.get("triggering_actor") or run.get("actor") or {}).get("login")
    m = re.match(r"PR #(\d+) · (.*)", title)
    pr = int(m.group(1)) if m else None
    meta = pr_meta.get(pr, {}) if pr else {}
    sha_m = SHA_RE.search(title)
    if pr:
        kind = "pr"
        sha = sha_m.group(1) if sha_m else meta.get("head_sha")
        title = meta.get("title") or m.group(2)
    elif ev == "workflow_dispatch" and actor and actor.endswith("[bot]"):
        kind, sha = "batch", sha_m.group(1) if sha_m else None
    elif ev == "workflow_dispatch":
        kind, sha = "dispatch", sha_m.group(1) if sha_m else None
    elif ev == "push":
        kind, sha = "push", run.get("head_sha")
    else:
        kind, sha = ev or "other", None
    return {
        "id": run["id"],
        "attempt": run.get("run_attempt", 1),
        "kind": kind,
        "event": ev,
        "pr": pr,
        "title": title,
        "author": meta.get("author"),
        "assignees": meta.get("assignees", []),
        "actor": actor,
        "branch": run.get("head_branch"),
        "sha": sha,
        "url": run.get("html_url"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
    }


def holder_label(e):
    if not e:
        return "?"
    if e["kind"] == "pr":
        return f"#{e['pr']}"
    if e["kind"] == "batch":
        return f"야간 {(e['sha'] or '')[:7]}"
    if e["kind"] == "push":
        return f"push {e['branch']}"
    return e["kind"]


def lane_of(job):
    labels = job.get("labels") or []
    for lane in LANES:
        if lane in labels:
            return lane
    return None  # GitHub-hosted (gate, status): never waits long enough to matter


def job_state(j):
    if j is None:
        return "none"
    if j["status"] == "completed":
        return j.get("conclusion") or "completed"
    return j["status"]  # queued | in_progress | waiting | pending


QUEUED = ("queued", "waiting", "pending")
SHARD_RE = re.compile(r"shard (\w+) (\d+)$")
STATUS_RE = re.compile(r"status (\w+)$")


def shard_fault(j):
    """A shard's Verdict step exits 1 when any case failed or went missing; a failure
    in any step before it means the shard itself died."""
    if j.get("conclusion") == "timed_out":
        return "died"
    for st in j.get("steps") or []:
        if st.get("conclusion") == "failure":
            return "failed" if st.get("name") == "Verdict" else "died"
    return "died"


def _secs(a, b):
    ta, tb = parse_ts(a), parse_ts(b)
    return (tb - ta).total_seconds() if ta and tb else None


def summarize_jobs(jobs, now, run_cancelled=False):
    """Stage strip, per-suite shard counts and whether this is a /run rerun."""
    by = {j["name"]: j for j in jobs}
    shards, suites, rerun = {}, set(), False
    took, running_for = {}, {}
    for j in jobs:
        m = SHARD_RE.match(j["name"])
        if m:
            suite = m.group(1)
            s = shards.setdefault(suite, {"total": 0, "done": 0, "failed": 0, "died": 0,
                                          "cancelled": 0, "running": 0, "queued": 0,
                                          "lane": lane_of(j)})
            s["total"] += 1
            st = job_state(j)
            if st == "success":
                s["done"] += 1
            elif st == "in_progress":
                s["running"] += 1
                running_for.setdefault(suite, []).append(
                    (now - parse_ts(j["started_at"])).total_seconds() if j.get("started_at") else 0)
            elif st in QUEUED:
                s["queued"] += 1
            elif st == "cancelled":
                s["cancelled" if run_cancelled else "died"] += 1
            elif st != "skipped":
                s[shard_fault(j)] += 1
            # the median counts shards that ran their share to the end, not ones that died early
            if st == "success" or (st == "failure" and shard_fault(j) == "failed"):
                d = _secs(j.get("started_at"), j.get("completed_at"))
                if d:
                    took.setdefault(suite, []).append(d)
            suites.add(suite)
            # plan sends a rerun's single shard to the light lane; a full shell or
            # sql suite never goes there
            if suite != "medium" and lane_of(j) == "cubrid-arc-light":
                rerun = True
        m = STATUS_RE.match(j["name"])
        if m and m.group(1) in SUITES:
            suites.add(m.group(1))

    # a shard still running at 1.5x the median of the finished ones, or at 3/4 of the
    # job timeout whatever the others did, is worth a look
    for suite, sh in shards.items():
        runs_for = running_for.get(suite, [])
        if not runs_for:
            continue
        done = sorted(took.get(suite, []))
        med = done[len(done) // 2] if len(done) >= max(3, sh["total"] // 3) else None
        limit = min(1.5 * med if med else STALL_ABS_S, STALL_ABS_S)
        slow = [r for r in runs_for if r > limit]
        if slow:
            sh["slow"] = len(slow)
            sh["median_s"] = round(med) if med else None
            sh["longest_s"] = round(max(slow))

    def stage(key, label, *names):
        js = [by.get(n) for n in names if by.get(n)]
        if not js:
            return None
        states = [job_state(j) for j in js]
        for s in ("failure", "timed_out", "cancelled", "in_progress") + QUEUED:
            if s in states:
                agg = s
                break
        else:
            agg = "skipped" if all(s == "skipped" for s in states) else "success"
        lane = next((lane_of(j) for j in js if lane_of(j)), None)
        return {"key": key, "label": label, "state": agg, "lane": lane}

    stages = [s for s in (stage("gate", "gate", "gate"),
                          stage("build", "build", "build (debug)", "build (release)"),
                          stage("plan", "plan", "plan")) if s]
    for suite in SUITES:
        if suite in shards:
            sh = shards[suite]
            st = ("in_progress" if sh["running"] else "queued" if sh["queued"] else
                  "failure" if sh["failed"] or sh["died"] else
                  "cancelled" if sh["cancelled"] and not sh["done"] else "success")
            stages.append({"key": f"shard:{suite}", "label": suite, "state": st,
                           "lane": sh["lane"], "shards": sh})
    for s in (stage("collect", "collect", "collect"),
              stage("status", "status", *[f"status {x}" for x in SUITES])):
        if s:
            stages.append(s)
    # a build-only run shows skipped plan/collect; drop them so the strip ends at build
    stages = [s for s in stages if s["state"] != "skipped" or s["key"] == "gate"]
    return stages, shards, sorted(suites, key=SUITES.index), by, rerun


def alerts_of(shards):
    out = []
    for suite in SUITES:
        sh = shards.get(suite)
        if not sh:
            continue
        if sh["failed"]:
            out.append({"level": "fail", "text": f"{suite} 케이스 실패 샤드 {sh['failed']}개"})
        if sh["died"]:
            out.append({"level": "fail", "text": f"{suite} 이상 종료 샤드 {sh['died']}개"})
        if sh.get("slow"):
            base = (f"완료 중앙값 {sh['median_s'] // 60}분" if sh["median_s"]
                    else f"타임아웃 {SHARD_TIMEOUT_S // 60}분")
            out.append({"level": "warn",
                        "text": f"{suite} 샤드 {sh['slow']}개가 오래 돈다 — 최장 "
                                f"{sh['longest_s'] // 60}분, {base}"})
    return out


def phase_of(run, stages):
    """(sentence, lane the run waits in or None, word for that stage)."""
    if run["status"] == "pending":
        return "동시성 그룹 대기 — 같은 PR 의 이전 런이 끝나기를 기다린다", None, None
    active = next((s for s in stages if s["state"] in ("in_progress",) + QUEUED), None)
    if not active:
        return ("잡 생성 전" if not stages else "다음 단계 준비 중"), None, None
    word = {"gate": "요청 확인", "build": "빌드", "plan": "샤드 분할",
            "collect": "결과 집계", "status": "판정 게시"}.get(active["key"])
    if active["key"].startswith("shard:"):
        sh = active["shards"]
        word = f"{active['label']} 테스트"
        if active["state"] == "in_progress":
            fin = sh["done"] + sh["failed"] + sh["died"] + sh["cancelled"]
            return f"{word} 중 — 샤드 {fin}/{sh['total']} 끝남", None, word
    lane = active.get("lane")
    if active["state"] == "in_progress" or not lane:
        return f"{word} 중", None, word
    return f"{word} 러너 대기", lane, word


def build_queue():
    now = datetime.now(timezone.utc)
    t0 = time.time()
    errors, fatal = [], []

    def safe(label, fn, default, sink=errors):
        try:
            return fn()
        except Exception as e:
            sink.append(f"{label}: {e}")
            return default

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        f_meta = ex.submit(safe, "pulls",
                           lambda: cached("pr_meta", PR_META_TTL, fetch_pr_meta), {})
        f_viewer = ex.submit(safe, "user", lambda: cached("viewer", 3600, fetch_viewer), None)
        lists = list(ex.map(lambda st: safe(f"runs {st}", lambda: fetch_runs(status=st), [],
                                            fatal), ACTIVE_STATUSES))
        pr_meta, viewer = f_meta.result(), f_viewer.result()
    if fatal:
        # an empty lane drawn from a failed call reads as "nothing is running"
        causes = sorted({f.split(": ", 1)[-1] for f in fatal})
        raise RuntimeError("런 목록을 읽지 못했다 — " + "; ".join(causes))
    seen, runs = set(), []
    for r in (r for lst in lists for r in lst):
        if r["id"] not in seen:
            seen.add(r["id"])
            runs.append(r)

    jobs_err = set()

    def jobs_or_mark(r):
        try:
            return fetch_jobs(r["id"])
        except Exception as e:
            errors.append(f"jobs {r['id']}: {e}")
            jobs_err.add(r["id"])
            return []

    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        jobs_of = dict(zip([r["id"] for r in runs], ex.map(jobs_or_mark, runs)))

    entries = {r["id"]: classify(r, pr_meta) for r in runs}
    entries = {k: e for k, e in entries.items() if e["kind"] != "schedule"}  # the batch launcher

    # lanes: who holds the runners, and the FIFO order of what waits for one
    lanes = {name: {"name": name, "max": cap, "running": 0, "queued": 0, "holders": {},
                    "waiters": {}, "oldest_since": None} for name, cap in LANES.items()}
    queued = []  # (lane, created_at, job id, run id)
    slot_since = {}  # lane -> created_at of the oldest job waiting for a runner
    for rid, jobs in jobs_of.items():
        for j in jobs:
            lane = lane_of(j)
            if not lane:
                continue
            if j["status"] == "in_progress":
                lanes[lane]["running"] += 1
                lanes[lane]["holders"][rid] = lanes[lane]["holders"].get(rid, 0) + 1
            elif j["status"] in QUEUED:
                lanes[lane]["queued"] += 1
                lanes[lane]["waiters"][rid] = lanes[lane]["waiters"].get(rid, 0) + 1
                queued.append((lane, j.get("created_at") or "", j["id"], rid))
                if j["status"] == "queued" and j.get("created_at"):
                    slot_since[lane] = min(slot_since.get(lane, j["created_at"]), j["created_at"])
    queued.sort()
    order = {}      # lane -> [run id of each queued job, FIFO]
    first_pos = {}  # (run id, lane) -> (0-based position in lane, created_at)
    for lane, created, _jid, rid in queued:
        order.setdefault(lane, []).append(rid)
        first_pos.setdefault((rid, lane), (len(order[lane]) - 1, created))
        if not lanes[lane]["oldest_since"]:
            lanes[lane]["oldest_since"] = created

    for name, ln in lanes.items():
        since = parse_ts(slot_since.get(name))
        ln["max"], ln["max_source"] = lane_cap(
            name, ln["running"], (now - since).total_seconds() if since else None, now.timestamp())

    def free(lane):
        cap = lanes[lane]["max"]
        return None if cap is None else max(0, cap - lanes[lane]["running"])

    def who(rids):
        tally = {}
        for rid in rids:
            tally[rid] = tally.get(rid, 0) + 1
        return [{"label": holder_label(entries.get(rid)), "kind": (entries.get(rid) or {}).get("kind"),
                 "run": rid, "n": n} for rid, n in sorted(tally.items(), key=lambda kv: -kv[1])]

    for rid, e in entries.items():
        jobs = jobs_of.get(rid, [])
        stages, shards, suites, _, rerun = summarize_jobs(jobs, now)
        e["stages"], e["suites"], e["rerun"] = stages, suites, rerun
        e["alerts"] = alerts_of(shards)
        e["jobs_error"] = rid in jobs_err
        phase, wait_lane, word = phase_of(e, stages)
        stage_waits = wait_lane is not None  # the current stage itself is what queues
        e["running_jobs"] = sum(1 for j in jobs if j["status"] == "in_progress")
        e["queued_jobs"] = sum(1 for j in jobs if j["status"] in QUEUED)
        own = {ln: p for (r, ln), p in first_pos.items() if r == rid}
        if wait_lane not in own and own:
            wait_lane = min(own, key=lambda ln: own[ln][0])
        if wait_lane in own:
            _, created = own[wait_lane]
            fr = free(wait_lane)
            # a job that fits a free slot waits only for its runner pod, not for anyone
            # to finish; the rest wait for a slot behind everything queued before them
            mine_pos = [i for i, r in enumerate(order[wait_lane]) if r == rid]
            boot = [i for i in mine_pos if fr is not None and i < fr]
            slot = [i for i in mine_pos if not (fr is not None and i < fr)]
            e["wait_lane"] = wait_lane
            e["starting_jobs"], e["slot_jobs"] = len(boot), len(slot)
            e["starting"] = not slot
            pos = slot[0] if slot else mine_pos[0]
            # every run queues about the same number of jobs, so count runs, not jobs
            others = [r for r in order[wait_lane][:pos] if r != rid]
            e["ahead"] = len(set(others))
            by = {o["run"]: o for o in who(others)}
            e["ahead_by"] = [by[r] for r in dict.fromkeys(others)][:3]  # front of the queue first
            e["queued_since"] = created
            if stage_waits and e["starting"]:
                phase = f"{word} 러너 기동 대기"
        e["phase"] = phase
        # the lane a run is listed under: where it holds the most runners, else where it waits
        held = {}
        for j in jobs:
            if j["status"] == "in_progress" and lane_of(j):
                held[lane_of(j)] = held.get(lane_of(j), 0) + 1
        e["lane"] = max(held, key=held.get) if held else e.get("wait_lane")
        # waiting = holds no runner and stands in a lane queue (or has no job yet);
        # a run whose only open jobs are GitHub-hosted is finishing, not waiting
        if e["running_jobs"] or (not own and len(stages) > 1 and e["status"] != "pending"):
            e["category"] = "running"
        else:
            e["category"] = "waiting"

    # suites of a PR run that has not reached plan yet: the pending statuses name them
    need = [e for e in entries.values() if e["kind"] == "pr" and not e["suites"] and e["sha"]]
    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        for e, sts in zip(need, ex.map(
                lambda e: safe(f"statuses {e['pr']}", lambda: run_statuses(e["sha"], e["id"]), {}),
                need)):
            e["suites"] = [s for s in SUITES if f"gha-ci: test_{s}" in sts]
    for e in entries.values():
        if e["kind"] == "batch" and not e["suites"]:
            e["suites"] = list(SUITES)

    lane_rank = {name: i for i, name in enumerate(LANES)}

    def rank(e):
        return lane_rank.get(e["lane"], len(lane_rank))  # runs outside a lane go last

    running = sorted([e for e in entries.values() if e["category"] == "running"],
                     key=lambda e: (rank(e), e["created_at"] or ""))
    waiting = sorted([e for e in entries.values() if e["category"] == "waiting"],
                     key=lambda e: (rank(e), e.get("ahead") is None, e.get("ahead") or 0,
                                    e["created_at"] or ""))
    pos = {}  # the queue order is per lane, so is the position
    for e in waiting:
        pos[e["lane"]] = e["position"] = pos.get(e["lane"], 0) + 1

    lane_out = []
    for ln in lanes.values():
        fr = free(ln["name"])
        ln["starting"] = None if fr is None else min(ln["queued"], fr)
        # in runs: a run with any job past the free slots waits, even if some of its jobs boot
        q = order.get(ln["name"], [])
        cut = 0 if fr is None else min(len(q), fr)
        slot_runs = set(q[cut:])
        ln["waiting_runs"] = len(slot_runs)
        ln["starting_runs"] = len(set(q[:cut]) - slot_runs)
        ln["holders"] = who([rid for rid, n in ln["holders"].items() for _ in range(n)])
        ln["waiters"] = who([rid for rid, n in ln["waiters"].items() for _ in range(n)])
        lane_out.append(ln)

    return {
        "generated_at": now.isoformat(),
        "duration_ms": round((time.time() - t0) * 1000),
        "viewer": viewer,
        "lanes": lane_out,
        "cap_rule": {"waited_min": SATURATED_AFTER_S // 60, "kept_h": CAP_TTL_S // 3600},
        "running": running,
        "waiting": waiting,
        "partial": bool(jobs_err),
        "errors": errors,
    }


# --------------------------------------------------------------------------- #
# recent completed runs
# --------------------------------------------------------------------------- #
FAIL_RE = re.compile(r"^(shell|sql|medium): (\d+) failed")
NOISE = ("Process completed with exit code", "Executing the custom container",
         "failed to run script step")


def run_detail(e, pr_meta):
    """Verdict per suite for one completed run: statuses first, collect annotations for counts."""
    jobs = fetch_jobs(e["id"])
    now = datetime.now(timezone.utc)
    stages, shards, suites, by, rerun = summarize_jobs(jobs, now,
                                                       e["conclusion"] == "cancelled")
    collect = by.get("collect")
    notes, fails = [], {}
    if collect and collect.get("conclusion") == "failure":
        for a in gh(f"repos/{REPO}/check-runs/{collect['id']}/annotations", per_page=50):
            msg = (a.get("message") or "").strip()
            m = FAIL_RE.match(msg)
            if m:
                fails[m.group(1)] = int(m.group(2))
            elif a.get("annotation_level") == "failure" and not any(n in msg for n in NOISE):
                notes.append(msg.splitlines()[0][:200])
    sts = find_run_statuses(e, pr_meta) if suites else {}

    verdicts = []
    for s in suites:
        st = sts.get(f"gha-ci: test_{s}")
        desc = st and st.get("description")
        if e["conclusion"] == "cancelled":
            state = "cancelled"
        elif st and st["state"] != "pending":
            state = st["state"]
        elif collect and collect.get("conclusion") == "success":
            state = "success"
        elif s in fails:
            state = "failure"
        else:
            state, desc = "unknown", "이 런이 게시한 commit status 를 찾지 못했다"
        verdicts.append({"suite": s, "state": state, "failed": fails.get(s),
                         "died": shards.get(s, {}).get("died", 0), "description": desc})
    build = next((x for x in stages if x["key"] == "build"), None)
    gate = by.get("gate")
    rejected = None
    if not build and not suites:
        # the gate ran and turned the comment away, or failed deciding
        rejected = "gate_failed" if gate and gate.get("conclusion") == "failure" else "ignored"
    return {"suites": suites, "verdicts": verdicts, "notes": notes[:3],
            "build": build and build["state"], "rerun": rerun, "rejected": rejected,
            "collect_url": collect and collect.get("html_url")}


def build_recent():
    now = datetime.now(timezone.utc)
    t0 = time.time()
    errors = []
    try:
        pr_meta = cached("pr_meta", PR_META_TTL, fetch_pr_meta)
    except Exception as ex:
        pr_meta = {}
        errors.append(f"pulls: {ex}")
    since = (now - timedelta(hours=RECENT_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    runs = fetch_runs(status="completed", created=f">={since}")
    rows = [classify(r, pr_meta) for r in runs
            if r.get("conclusion") not in ("skipped", None) and r.get("event") != "schedule"]
    rows = rows[:RECENT_CAP]

    def detail(e):
        key = (e["id"], e["attempt"], e["updated_at"])
        with _lock:
            hit = _run_detail.get(key)
            if hit and (hit[0] is None or hit[0] > time.time()):
                return hit[1]
        try:
            d = run_detail(e, pr_meta)
        except Exception as ex:
            errors.append(f"run {e['id']}: {ex}")
            return {"suites": [], "verdicts": [], "notes": [], "build": None,
                    "rerun": False, "rejected": None, "collect_url": None, "detail_error": True}
        # a verdict not found yet may turn up (a slow status post), so look again later
        unsure = not pr_meta or any(v["state"] == "unknown" for v in d["verdicts"])
        with _lock:
            _run_detail[key] = (time.time() + 900 if unsure else None, d)
        return d

    def closed_pr_author(e):
        # the open-PR list does not know a merged PR, and "내 PR" needs its author
        try:
            pr = cached(f"pr:{e['pr']}", 600, lambda: gh(f"repos/{REPO}/pulls/{e['pr']}"))
            e["author"] = pr["user"]["login"]
            e["assignees"] = [a["login"] for a in pr.get("assignees") or []]
        except Exception as ex:
            errors.append(f"pull {e['pr']}: {ex}")

    with cf.ThreadPoolExecutor(max_workers=10) as ex:
        for e, d in zip(rows, ex.map(detail, rows)):
            e.update(d)
            e["duration_s"] = round(_secs(e["created_at"], e["updated_at"]) or 0)
        list(ex.map(closed_pr_author, [e for e in rows if e["kind"] == "pr" and not e["author"]]))
    return {"generated_at": now.isoformat(),
            "duration_ms": round((time.time() - t0) * 1000),
            "hours": RECENT_HOURS, "runs": rows, "errors": errors}


# --------------------------------------------------------------------------- #
# the viewer's open PRs: the verdict branch protection sees, per context
# --------------------------------------------------------------------------- #
CONTEXTS = ("build (debug)", "build (release)") + tuple(f"test_{s}" for s in SUITES)


def build_mine(login):
    now = datetime.now(timezone.utc)
    pr_meta = cached("pr_meta", PR_META_TTL, fetch_pr_meta)
    mine = [(n, m) for n, m in pr_meta.items()
            if m["author"] == login or login in m["assignees"]]

    def one(item):
        n, m = item
        sha = m["head_sha"]
        d = cached(f"combined:{sha}", 30, lambda: gh(f"repos/{REPO}/commits/{sha}/status",
                                                      per_page=100))
        ctx = {}
        for st in d.get("statuses", []):
            name = st.get("context", "")
            if name.startswith("gha-ci: "):
                ctx[name[len("gha-ci: "):]] = {
                    "state": st["state"], "description": st.get("description"),
                    "url": st.get("target_url"), "updated_at": st.get("updated_at")}
        contexts = [dict(name=c, **ctx[c]) for c in CONTEXTS if c in ctx]
        return {"pr": n, "title": m["title"], "author": m["author"], "draft": m["draft"],
                "head_sha": sha, "contexts": contexts,
                "updated_at": max((c["updated_at"] or "" for c in contexts), default=None)}

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        rows = list(ex.map(one, mine))
    rows.sort(key=lambda r: r["updated_at"] or "", reverse=True)
    return {"generated_at": now.isoformat(), "login": login, "prs": rows}


# --------------------------------------------------------------------------- #
# http server
# --------------------------------------------------------------------------- #
CT = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
      ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml"}
API = {"/api/queue": ("queue", CACHE_TTL, build_queue),
       "/api/recent": ("recent", 30.0, build_recent)}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser navigated away mid-response

    def _file(self, rel):
        path = os.path.normpath(os.path.join(BASE, rel))
        if not path.startswith(BASE) or not os.path.isfile(path):
            self._send(404, "not found", "text/plain")
            return
        with open(path, "rb") as f:
            self._send(200, f.read(), CT.get(os.path.splitext(path)[1],
                                             "application/octet-stream"))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path in ("/", "/index.html"):
            self._file("index.html")
        elif u.path.startswith("/static/"):
            self._file(u.path.lstrip("/"))
        elif u.path in API or u.path == "/api/mine":
            q = urllib.parse.parse_qs(u.query)
            force = q.get("force", ["0"])[0] in ("1", "true")
            if u.path == "/api/mine":
                login = re.sub(r"[^A-Za-z0-9-]", "", q.get("login", [""])[0])
                key, ttl, fn = f"mine:{login}", 30.0, lambda: build_mine(login)
            else:
                key, ttl, fn = API[u.path]
            try:
                self._send(200, json.dumps(cached(key, ttl, fn, force)))
            except urllib.error.HTTPError as e:
                self._send(502, json.dumps({"error": f"GitHub {e.code}: {e.reason}"}))
            except Exception as e:
                self._send(500, json.dumps({"error": str(e)}))
        else:
            self._send(404, "not found", "text/plain")


def main():
    gh_token()  # fail fast if missing
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"CUBRID gha-ci monitor → http://{HOST}:{PORT}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
