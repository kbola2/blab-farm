# blab-farm

Schedule [Boundary Lab](https://github.com/JWSound/boundary-lab) solves across several machines.

Boundary Lab's server runs on one machine and solves one job at a time. `blab-farm` puts **one
address** in front of any number of stock Boundary Lab servers ("workers"). Boundary Lab's GUI or CLI
talks to that address exactly as to a single server; the farm splits each solve's frequency list into
small batches and feeds them to whichever workers are free. A fast machine simply takes more batches.
Nothing changes numerically: every frequency is an independent solve, and results stream back tagged
with their frequency.

- **Drop-in.** Speaks Boundary Lab's remote protocol (v2, Boundary Lab 0.5.0). In the GUI: Preferences >
  Application > Solver = *Boundary Lab Server*, address `http://<coordinator>:8766`. CLI: `--server-url`.
- **Workers are dumb.** Each runs the stock `blab server`; `blab-farm worker` wraps it, keeps the machine
  awake, registers with the farm, and can install itself as a login service (macOS, Linux, Windows).
- **Memory-aware.** Dense BEM needs memory proportional to the square of the mesh size; the farm estimates
  each job from its mesh and routes it only to workers that can hold it. Jobs few machines can hold go first.
- **Robust.** Queue of jobs, failover when a worker dies, retry when a worker is busy, an idle fast worker
  duplicates a straggler's last batch, LAN discovery, per-frequency provenance, event journals on disk.
- **Visible.** A live dashboard at the farm address: every frequency as a block coloured by the machine
  solving it, per-worker throughput, batch lanes over time, remaining-time estimate. `blab-farm status`
  prints the same (`--json` for scripts).
- **No dependencies.** Python 3.10+ standard library only. Boundary Lab itself is only needed on workers.

## Quickstart (humans)

On every machine that should solve (Boundary Lab already installed there):

```bash
pip install blab-farm
blab-farm worker --farm http://192.168.1.10:8766 --install-service   # or without --install-service to run in a terminal
```

On the coordinator (any machine, can also be a worker):

```bash
pip install blab-farm
blab-farm serve
```

Then set Boundary Lab's solver to *Boundary Lab Server* at `http://192.168.1.10:8766` and press Solve.
Open that address in a browser to watch. Workers on the same LAN are found automatically; workers
started with `--farm` also announce themselves every minute, so order does not matter.

## Quickstart (agents)

The same two commands. Verification: `blab-farm status --json` returns the workers, the active jobs with
per-frequency state (`queued`, `assigned`, `running`, `done`) and the machine that solved each frequency.
A worker is healthy when `curl http://<worker>:8765/v1/capabilities` reports `"state": "ready"`.
See [AGENTS.md](AGENTS.md) for the conventions a session should follow when changing this package.

## Does Boundary Lab change? No.

Nothing is recompiled, patched or reinstalled. The farm is a separate Python process that speaks the
network protocol Boundary Lab already uses for its own *Boundary Lab Server* mode, and every worker runs
Boundary Lab's own `blab server`. A user keeps their existing Boundary Lab install on each worker
(source checkout or packaged installer, with its solver engine prepared as for any server), installs
this package, and starts one process per machine. Removing the farm is `pip uninstall` and switching
the GUI's solver back to local.

For a complete, checked, step-by-step procedure (for agents or careful humans) see
[docs/AGENT_SETUP.md](docs/AGENT_SETUP.md).

## How it works

```
GUI / blab CLI --(blab-remote v2)--> blab-farm serve --(blab-remote v2)--> blab server (worker A)
                                           |----------------------------> blab server (worker B)
                                           |----------------------------> blab server (worker C)
```

1. The client uploads a job (meshes + request). The farm estimates its dense-BEM memory from the mesh
   assets, picks the workers whose cap covers it, and cuts the frequency list into batches
   (adaptive, at most 8 per batch; `--batch N` to fix it).
2. Each worker loop pulls the next batch from the highest-priority job it can hold and submits it to its
   Boundary Lab server as a normal job with only those frequencies. Results are forwarded to the client's
   event stream as they arrive. Priority: fewest eligible workers first, then submission order.
3. Failures requeue the batch for other workers; HTTP 409 (worker busy with someone else's job) retries;
   when the queue is empty an idle fast worker duplicates the slowest running batch and the first copy wins.
4. The job completes when every frequency has a result. Provenance (which machine solved what, seconds
   per frequency) is in the terminal event and in the journal under `~/.blab-farm/jobs/<job>/`.

## Options worth knowing

| Option | Meaning |
|---|---|
| `serve --worker URL[=name][@max_gib[/host_gib]][+backend]` | pin a worker instead of discovery; `@12/14`: jobs up to 12 GiB, and all workers on that machine together stay within 14 GiB; `@12` alone: independent; `+cpu` or `+metal`: which backend the server runs (its jobs count double on CPU) |
| `serve --discovered-max-gib N` | cap for workers that join without stating their own |
| `worker --host-gib N` | memory budget of the whole machine (default 80 % of RAM; 90 % with `--dedicated`) |
| `worker --dedicated` | this machine does nothing else (no desktop, no GUI): budget 90 % of RAM |
| `worker --memory shared\|independent` | `shared` (default): the workers on this machine stay within its budget together; `independent`: this worker is limited only by its own cap, even if a sibling is busy |
| `worker --max-gib N` | largest job this worker takes (default: the machine budget; the farm already counts a CPU worker's jobs at twice their size) |
| `worker --backend metal|cpu|cuda|rocm|auto` | passed to `blab server` |
| `worker --blab PATH` | the `blab` command or a python with Boundary Lab installed, if not on PATH |
| `worker --port N` | one worker per port; run a GPU worker and a CPU worker on the same machine on two ports, each with its own service; the machine budget keeps them from overcommitting the RAM together |
| `serve --ordered` | stream results in request order (buffers; the GUI does not need this) |
| `BLAB_FARM_TOKEN`, `BLAB_SERVER_TOKEN` | optional bearer keys: clients to the farm, farm to workers |

## Limits

- One mesh per job, so a wideband sweep that needs a finer mesh at high frequency is several jobs
  (one study per band); the farm routes each to machines with enough memory.
- Observation planes (the pressure animation) are not solved through a Boundary Lab Server, so not
  through the farm either; polars, balloons and probes are.
- The memory estimate is measured, not guessed: a Metal/CUDA solve peaks at about 2.1 dense complex64
  matrices on about half the triangle count, plus 0.5 GiB (Boundary Lab 0.5.0, LU or GMRES alike). CPU
  workers solve in complex128 and are counted at twice that. It is still an estimate: keep caps below
  what the machine can really spare. Two machines cannot share one matrix; see
  [docs/POOLED_MEMORY.md](docs/POOLED_MEMORY.md) for what that would take.
- Trusted-LAN tool: it binds all interfaces on purpose and has no accounts. Use the bearer keys or a
  VPN beyond a home or lab network.
- Tested against Boundary Lab 0.5.0 / remote protocol v2. A protocol change upstream will need a
  release here; the farm checks versions on every worker.

## Development

```bash
pip install -e ".[dev]"
pytest            # fake-worker tests: splitting, merging, failover, speculation, queueing, caps, discovery
```

## Status

Early. Used daily on a three-Mac LAN (an Apple Silicon laptop, a MacBook Pro and a Mac mini) with the
macOS service installer; the Linux and Windows installers follow the same pattern but have had less use.
Issues and reports from other setups are welcome.

## Licence

MIT. `blab-farm` talks to Boundary Lab over HTTP and imports nothing from it; Boundary Lab itself is
GPL-3.0 and is not included.
