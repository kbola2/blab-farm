# Changelog

## 0.1.4 (2026-10-21)

- Fix: when a job completed (or a batch was finished by a faster duplicate), the slow worker's sub-job
  kept running to the end on its machine, unneeded, and the worker stayed busy for other jobs (a CPU
  worker was seen grinding for 40 minutes on two frequencies that had long been delivered). Leftover
  sub-jobs are now cancelled on the worker the moment they are no longer needed, and the worker is
  free again at once.

## 0.1.3 (2026-10-21)

- Scheduling: a worker takes an open batch from any job before it may duplicate a straggler's batch
  (previously an empty queue on the first job let it duplicate there while a later job still had open
  batches), and a worker whose speed is not yet known is no longer presumed infinitely slow: its batch is
  duplicated only once it has run longer than twice what the duplicate would take. Fast machines still
  take more batches; they just stop stealing batches that were going to finish anyway.

## 0.1.2 (2026-10-21)

- Fix: a server's capabilities list every backend it could run, so the farm took the first (alphabetically
  `beat_cpu`) and counted Metal workers at twice their memory. Workers now state their backend when they
  register, pinned workers can say it in their spec (`URL@18/18+cpu`), and the guess prefers the GPU
  backend until the worker's first accepted batch confirms it.

## 0.1.1 (2026-10-21)

- Fix: LAN discovery opened one socket per host at once (253 on a /24), which on macOS's default limit of
  256 open files took down worker polling and job journals with "Too many open files". Probes now run 24
  at a time, the coordinator raises its own open-file limit at startup (to 4096 where allowed) and warns if
  it stays low, and error responses from workers are closed promptly.

## 0.1.0 (2026-10-21)

- First extraction from a horn-design workflow: coordinator (`serve`), worker wrapper (`worker`) with
  macOS/Linux/Windows login-service installers, `status`, dashboard, LAN discovery and self-registration,
  memory caps, job queue with priority, failover, busy retry, straggler duplication, event journals.
- Services are per port, so a machine can run a GPU worker and a CPU worker side by side.
- Memory model measured on Boundary Lab 0.5.0: 2.1 dense matrices + 0.5 GiB (was a 4-matrix guess, about
  twice too high); CPU workers counted at twice the memory (complex128); per-machine budget (`--host-gib`,
  `@max/host`) so two workers on one machine do not overcommit it; a worker's own registration now updates
  the caps discovery guessed; design note on pooling RAM across machines (`docs/POOLED_MEMORY.md`).
- Workers say how they use their machine's memory: `--memory shared` (default: siblings stay within the
  machine budget together) or `--memory independent` (own cap only); `--dedicated` for a machine that does
  nothing else (budget 90 % of RAM, otherwise 80 %). The dashboard shows each worker's cap and budget.
