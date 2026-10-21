# Notes for agents working on blab-farm

- Purpose: one Boundary Lab Server address backed by many stock Boundary Lab servers. Keep it stdlib-only.
- Protocol: Boundary Lab remote protocol v2 (`/v1/capabilities`, `POST /v1/jobs`, `GET /v1/jobs/<id>?cursor=`,
  `DELETE /v1/jobs/<id>`). The farm must stay a faithful server to clients and a faithful client to workers.
  Farm-only endpoints are under `/v1/farm/` (`status`, `register`) and the dashboard at `/`.
- Run tests with `pytest`; they use in-process fake workers (`tests/test_farm.py::FakeWorker`). Every behaviour
  change needs a test there. Timing-sensitive tests use `poll_s=0.01` and `busy_retry_s=0.05` fixtures.
- Never count a result twice: `_deliver` returns whether a frequency was new; duplicates from retried or
  speculated batches are ignored.
- Per-job state lives on `Job`; per-worker lifetime stats on `Worker`. A job's `workers` are the eligible ones
  at submission (memory caps); `_priority` orders jobs by fewest eligible workers, then age.
- Do not add dependencies, accounts, or anything that needs Boundary Lab installed on the coordinator.
- Report numbers honestly: the memory estimate is a heuristic; say so in any docs you touch.
