# CUBRID gha-ci · Runner-Queue Monitor

Live dashboard for the GitHub Actions workflow `gha-ci` of `CUBRID/cubrid`, which
replaced the CircleCI `build_test` pipeline for PR testing (CUBRIDQA-1501/1526).
Successor of `../circle_ci_monitor/`, which stays as it was.

The old monitor watched one `test_shell` slot. gha-ci has no such slot: every run's
jobs queue on two self-hosted runner lanes, and a single shell run is 50 shard jobs.
So this monitor answers two questions: who holds each lane, and where your `/run`
stands in the lane queue.

## Run

```bash
./run.sh                 # http://<host>:7070
# or
PORT=7070 python3 server.py
```

Python 3, stdlib only. The browser talks only to `/api/*`; the token stays on the server.
It shares port 7070 with `circle_ci_monitor`, so run one or the other.

## Credentials

One GitHub token, read-only use: `GITHUB_TOKEN` / `GH_TOKEN`, else `gh auth token`.
No CircleCI token.

## What it shows

- **Lanes**: `cubrid-arc` (build, shell/sql/sql_by_cci shards) and `cubrid-arc-light` (plan,
  collect, medium, a rerun's shard). Each lane shows the running count, colored by which run
  holds the runners (nightly batch, PR, push). Queued work is counted in runs, not jobs, because
  every run queues about the same number of jobs; the job count is in the tooltip. Runs are split
  into **기동 중** (all their queued jobs fit in free slots and wait for a runner pod) and **대기**
  (at least one job waits for a slot). The lane also shows who owns the waiting jobs and the
  oldest wait. 진행 중 and 대기 below are grouped by lane, then 레인 밖 (runs with no lane job).
- **진행 중**: runs with a job running. Each card shows the stage strip gate › build ›
  plan › shards per suite › collect › status, and alerts: shards that failed on cases, shards
  that died, and shards running past 1.5× the median of finished ones or past 90 min (3/4 of the
  shard timeout), whichever comes first.
- **대기**: runs holding no runner, in lane FIFO order, numbered per lane. Each card shows the runs ahead and
  whose they are, this wait's time, and the time since the request.
- **최근 완료**: runs finished in the last `RECENT_HOURS` (24), excluding comment runs that
  asked for nothing. Each row shows a verdict per suite with the failed-case count; a failure
  chip links to the collect job. A summary line and a failures-only filter sit on top.
- **내 PR 판정** (the 내 PR view): for each open PR you author or are assigned, the
  latest `gha-ci:` commit status per context on its head. This is what branch protection sees.

The 내 PR view filters every section to one login. The default is the token owner; change it
with `?me=<login>` or the footer link.

## Where the numbers come from

| Shown | Source | Caveat |
|---|---|---|
| Lane running / queued | jobs of active runs, by runner label | — |
| Lane cap | the most jobs running in the lane while a job waited for a runner over 10 min, kept 24 h; `LANES=cubrid-arc=N` fixes it | The runners API needs admin, so the cap cannot be read. The estimate restarts with the server and does not see other workflows on the same runners |
| Lane of a run | the lane where it holds the most runners, else where its jobs queue | A run can hold runners in both lanes |
| Queue order | job `created_at` per lane | The workflow calls the queue FIFO; GitHub does not guarantee it |
| Shard failed vs died | the failing step: `Verdict` = cases failed, anything earlier or a timeout = died | Follows the step names in `gha-ci.yml` |
| Suite verdict | commit statuses whose `target_url` is the run | Searched on the PR head, then the last 8 PR commits. `?` if not found; looked up again after 15 min |
| Failed-case count | collect job annotations `<suite>: N failed` | — |
| Rerun tag | a shell/sql shard on `cubrid-arc-light` | Plan sends only a rerun's shard there |

## Cost

`/api/queue`: about 10–15 API calls, 3 s server cache. `/api/recent`: 2 pages of runs plus,
for each run not seen before, its jobs, annotations and statuses. That takes about 30 s on the
first load and about 6 s after, with a 30 s server cache. Auto refresh (default 1 min) uses the
server cache and pauses in hidden tabs.

## Files

| file | purpose |
|---|---|
| `server.py` | stdlib HTTP server, GitHub API proxy, lane/queue model |
| `index.html`, `static/*` | frontend |
