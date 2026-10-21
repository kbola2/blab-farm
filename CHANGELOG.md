# Changelog

## 0.1.0 (unreleased)

- First extraction from a horn-design workflow: coordinator (`serve`), worker wrapper (`worker`) with
  macOS/Linux/Windows login-service installers, `status`, dashboard, LAN discovery and self-registration,
  memory caps, job queue with priority, failover, busy retry, straggler duplication, event journals.
- Services are per port, so a machine can run a GPU worker and a CPU worker side by side.
- Memory model measured on Boundary Lab 0.5.0: 2.1 dense matrices + 0.5 GiB (was a 4-matrix guess, about
  twice too high); CPU workers counted at twice the memory (complex128); per-machine budget (`--host-gib`,
  `@max/host`) so two workers on one machine do not overcommit it; a worker's own registration now updates
  the caps discovery guessed; design note on pooling RAM across machines (`docs/POOLED_MEMORY.md`).
