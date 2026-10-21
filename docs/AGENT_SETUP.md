# Setting up blab-farm: step-by-step for an agent (or a careful human)

This guide assumes nothing except shell access to each machine. Follow it in order; every step has a
check. Nothing here modifies or rebuilds Boundary Lab: the farm is a separate process that speaks
Boundary Lab's own network protocol, and workers run Boundary Lab's own server command.

## 0. What you are building

```
GUI / blab CLI  -->  coordinator (blab-farm serve, port 8766)  -->  worker 1 (blab server, port 8765)
                                                                -->  worker 2
                                                                -->  worker N
```

One machine runs the coordinator (it can also be a worker). Every machine that should solve runs a
worker. The user's Boundary Lab GUI points at the coordinator as if it were a single server.

## 1. On each worker machine: confirm Boundary Lab can serve

Boundary Lab must already be installed with its solver engine ready. Find its Python environment or
`blab` command:

```bash
which blab                                   # source installs put it on PATH or in <checkout>/.venv/bin/blab
ls ~/Development/boundary-lab/.venv/bin/blab # common source layout (adjust to the real path)
```

If only a packaged installer is present, use the Python that installer bundled and pass it later as
`--blab /path/to/python` (the worker then runs `python -m blab.cli server`). Verify the engine:

```bash
<python> -m beat_engine doctor --backend cpu --threads 2     # expect a JSON block, no traceback
<python> -m beat_engine doctor --backend metal --threads 2   # Apple Silicon; cuda / rocm on other GPUs
```

If `doctor` fails, stop: fix Boundary Lab's installation first (its Installation and Setup guide). The farm
cannot help a machine whose solver does not run.

Check: `curl -s http://127.0.0.1:8765/v1/capabilities` returns nothing yet (no server running).

## 2. Install blab-farm (every machine)

Into the same Python environment as Boundary Lab, or any Python 3.10+:

```bash
<python> -m pip install blab-farm            # or: pip install /path/to/blab_farm-0.1.0-py3-none-any.whl
blab-farm --version                          # or: <python> -m blab_farm.cli --version
```

Check: prints `blab-farm 0.1.0`.

## 3. Start a worker (each solving machine)

Decide the coordinator's LAN address first (step 4 shows how to find it); the worker needs it only to
announce itself, and the coordinator also scans the LAN, so the order does not matter.

Run in a terminal to test:

```bash
blab-farm worker --farm http://<coordinator-ip>:8766 --backend metal \
    --blab <path to blab or python> --julia <julia executable if Boundary Lab needs it>
```

Expected output within about a minute: a `worker ready: http://<this-ip>:8765 ...` line, Boundary Lab's own
server log line, and `registered with farm ...` once the coordinator is up.

Check from this machine: `curl -s "http://127.0.0.1:8765/v1/capabilities?connection_test=1"` contains
`"state": "ready"` (it says `"starting"` for the first minute while the engine warms up).

Then make it permanent (set and forget):

```bash
blab-farm worker --farm http://<coordinator-ip>:8766 --backend metal --blab <...> --julia <...> --install-service
```

This installs a login service (launchd on macOS, systemd user unit with lingering on Linux, Task
Scheduler on Windows), starts it, restarts it on failure, keeps the machine awake, and logs to
`~/Library/Logs/blab-farm-worker.log` (macOS) or `~/.blab-farm/` (Linux). On an unattended Mac the user
must also enable automatic login and prevent sleep when the display is off; say so.

Memory: the worker announces a machine budget of 70 percent of its RAM (`--host-gib N` to change it, for
example on the machine that also runs the GUI) and takes jobs up to that budget (`--max-gib N`); a CPU
worker defaults to half the budget because it solves in complex128 and is counted at twice the memory.
Two workers on one machine never run jobs that together exceed the budget.

## 4. Start the coordinator (one machine)

```bash
blab-farm serve
```

It prints its address (`farm listening on http://<ip>:8766`), scans the LAN for Boundary Lab servers
on port 8765 every 30 s, and accepts registrations. Pin workers explicitly when discovery is not wanted:

```bash
blab-farm serve --no-discover --worker http://192.168.1.86:8765=pro@48 --worker http://127.0.0.1:8765=air@3
```

`@N` caps that worker at N GiB of dense BEM. `--discovered-max-gib N` caps workers that join without
stating their own.

Check: `blab-farm status` lists every worker as `idle` with its cap, and `curl http://<ip>:8766/v1/capabilities`
reports `"state": "ready"`.

Make it permanent the same way as a worker if the coordinator machine is unattended: run it under the
operating system's service manager (the worker installer is a template; a `serve` service is a one-line
change of the command).

## 5. Point Boundary Lab at the farm

In the GUI: Preferences > Application > Solver = **Boundary Lab Server**, Address `http://<coordinator-ip>:8766`,
Check connection, OK. From the CLI: `blab project solve <project> --request <request.json> --server-url http://<coordinator-ip>:8766 --output <dir>`.

If an agent must set the GUI preference without the dialog: Boundary Lab stores it in its settings under
`preferences/solve_backend = beat_remote` and `preferences/solve_server_url = <address>`; write them with
Boundary Lab's own `QSettings` names while the GUI is closed. Prefer the dialog when a human is present.

Check: press Solve on a small project (a few frequencies). `blab-farm status` shows the job with
`done` climbing and names the machine for each frequency; the dashboard at `http://<coordinator-ip>:8766/`
shows the same with colours per machine.

## 6. Verify numerically once

Solve the same small project once through the farm and once with the local solver, and compare a few
frequencies. They must agree to float precision (Metal uses complex64, CPU complex128, so differences of
order 1e-3 relative between backends are expected; between the farm and a local solve on the same backend,
zero). Splitting a sweep across machines cannot change results: each frequency is an independent solve.

## 7. Troubleshooting

| Symptom | Meaning | Action |
|---|---|---|
| GUI: "server busy" or HTTP 409 | a worker is busy with a job submitted directly to it, not through the farm | the farm retries that worker automatically; nothing to do |
| `No worker has room for this model (about N GiB)` | every worker's cap is below the job's estimate | raise a cap on a machine with enough RAM, or solve that band on a coarser mesh |
| a worker shows `down` | its server failed a batch twice | read that machine's worker log; the farm resumes using it on the next job |
| results stop at some count, job `failed` | all eligible workers down | restart the workers; resubmit (the client decides what to resubmit) |
| `matrix contains Infs or NaNs` from a worker | the mesh has degenerate (sliver or near-coincident) triangles | fix the mesh; this is not a farm or machine problem |
| worker not found by the coordinator | different subnet, firewall, or port other than 8765 | start the worker with `--farm` so it registers itself, or pin it with `--worker` |
| macOS asks about incoming connections | the server binds all interfaces on purpose | allow it on the trusted LAN; set `BLAB_SERVER_TOKEN` / `BLAB_FARM_TOKEN` for keys |

## 8. Uninstall

```bash
blab-farm worker --uninstall-service
<python> -m pip uninstall blab-farm
```

Boundary Lab is untouched by either command. Set the GUI's solver back to a local backend in Preferences.
