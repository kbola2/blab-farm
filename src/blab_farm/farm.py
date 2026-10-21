#!/usr/bin/env python3
"""The farm: one Boundary Lab Server address that spreads each solve across several Boundary Lab servers.

The Boundary Lab GUI (Preferences > Application > Solver = Boundary Lab Server) or CLI (``--server-url``)
talks to the farm exactly as to a single server (remote protocol ``blab-remote`` v2). The farm cuts a
job's frequency list into small batches and feeds them to workers as they become free, so a fast machine
simply takes more. Workers are stock ``blab server`` processes; they know nothing about each other.
Per-frequency results are independent, so splitting changes nothing numerically.
"""
from __future__ import annotations

import argparse
import hmac
import socket
import io
import json
import logging
import math
import os
import re
import sys
import threading
import time
import uuid
import zipfile
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

LOGGER = logging.getLogger("blab_farm")
PROTOCOL = "blab-remote"
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
TERMINAL = {"completed", "failed", "cancelled"}


class WorkerError(RuntimeError):
    def __init__(self, code, detail):
        super().__init__(f"HTTP {code}: {detail}")
        self.code = code


class Worker:
    """One Boundary Lab server, addressed like the GUI addresses it."""

    def __init__(self, url, *, name=None, token=None, max_gib=None, host_gib=None, shared=True, backend_id=None):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError(f"worker needs an http(s) base URL: {url}")
        self.url = url.rstrip("/")
        self.host = parsed.hostname  # workers on one host share that machine's memory
        self.name = name or parsed.hostname
        self.token = token
        self.opener = build_opener(ProxyHandler({}))
        self.down = False
        self.backend_id = backend_id  # stated by the worker or its spec; else guessed at join, confirmed by its first batch
        self.solved = 0
        self.seconds = 0.0
        self.failures = 0
        self.current = None  # batch indices being solved right now
        self.current_gib = 0.0  # estimate of the job that batch belongs to (for per-machine headroom)
        self.last_error = ""
        self.solved_total = 0
        self.seconds_total = 0.0
        self.max_gib = max_gib  # dense-BEM memory this worker may take on (None = unlimited)
        self.host_gib = host_gib  # memory budget of the whole machine, shared by every worker on it (None = unknown)
        self.shared = shared  # False: this worker ignores the other workers on its machine (its own cap only)

    def need(self, gib):
        """What a job estimated at ``gib`` (Metal/CUDA, complex64) costs on this worker: the CPU backend solves
        in complex128, so it needs twice the memory for the same mesh."""
        return gib * (2.0 if (self.backend_id or "").endswith("cpu") else 1.0)

    def new_job(self):
        """Per-job counters drive the throughput estimate; lifetime totals are kept separately."""
        self.solved_total += self.solved
        self.seconds_total += self.seconds
        self.solved, self.seconds, self.current = 0, 0.0, None
        if self.down and self.failures < 3:
            self.down = False  # give a worker that failed earlier another chance on the next job

    def snapshot(self):
        return {"name": self.name, "url": self.url, "backend_id": self.backend_id, "solved": self.solved,
                "seconds": round(self.seconds, 2), "seconds_per_frequency": round(self.seconds / self.solved, 2) if self.solved else None,
                "solved_total": self.solved_total + self.solved, "down": self.down, "busy": self.current is not None, "max_gib": self.max_gib,
                "host": self.host, "host_gib": self.host_gib, "memory": "shared" if self.shared else "independent",
                "current_batch": list(self.current or []), "failures": self.failures, "last_error": self.last_error}

    def call(self, path, *, data=None, method="GET", timeout=30):
        headers = {"Content-Type": "application/zip", "User-Agent": "BoundaryLabFarm/0.1"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=timeout) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error", "")
            except (ValueError, AttributeError):
                detail = ""
            finally:
                exc.close()  # release the socket now rather than when the garbage collector gets to it
            raise WorkerError(exc.code, detail or exc.reason) from exc
        except (URLError, OSError, ValueError) as exc:
            raise WorkerError(0, str(exc)) from exc

    def capabilities(self):
        try:
            info = self.call("/v1/capabilities", timeout=5)
            self.down = False
            return info
        except WorkerError as exc:
            self.down = True
            return {"state": "unavailable", "error": str(exc)}


def read_bundle(bundle):
    """Return (envelope dict, {asset name: bytes}) after the same structural checks a server makes."""
    if len(bundle) > MAX_BUNDLE_BYTES:
        raise ValueError("Remote bundle exceeds upload limit.")
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        names = [entry.filename for entry in archive.infolist()]
        if "job.json" not in names or len(names) != len(set(names)):
            raise ValueError("Bundle must contain exactly one job.json and its assets.")
        envelope = json.loads(archive.read("job.json"))
        if envelope.get("protocol") != PROTOCOL or type(envelope.get("version")) is not int:
            raise ValueError("Unsupported remote job protocol/version.")
        assets = {}
        for asset in envelope.get("assets", []):
            name = asset["name"]
            if not re.fullmatch(r"assets/[0-9a-f]{64}\.msh", name):
                raise ValueError("Invalid asset name.")
            assets[name] = archive.read(name)
    frequencies = envelope["request"]["frequencies_hz"]
    if not isinstance(frequencies, list) or not frequencies:
        raise ValueError("Request has no frequencies.")
    return envelope, assets


GIB_PER_DOF2 = 2.1 * 8 / 2 ** 30   # measured: peak RSS of a Metal solve is ~2.1 dense complex64 matrices
GIB_FIXED = 0.5                     # engine, caches, Krylov space and scratch


def estimate_gib(assets):
    """Dense-BEM memory a job needs on a Metal/CUDA worker, from its mesh assets: P1 unknowns are about
    half the triangle count (Gmsh 2.2 assets, quarter or full as bundled), and the engine's peak was
    measured at 2.1 complex64 N x N matrices plus half a GiB whether it factors (LU) or iterates (GMRES).
    A routing estimate, not a guarantee; CPU workers need twice this (see ``Worker.need``)."""
    triangles = 0
    for data in assets.values():
        text = data.decode("ascii", "ignore")
        if "$Elements" not in text:
            continue
        body = text.split("$Elements", 1)[1].split("$EndElements", 1)[0].splitlines()[2:]
        triangles += sum(1 for line in body if line.split()[1:2] == ["2"])
    dofs = triangles / 2
    return GIB_PER_DOF2 * dofs * dofs + GIB_FIXED


def sub_bundle(envelope, assets, frequencies):
    """The same job with only ``frequencies`` requested; assets unchanged."""
    part = json.loads(json.dumps(envelope))
    part["request"]["frequencies_hz"] = list(frequencies)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("job.json", json.dumps(part, allow_nan=False))
        for name, data in assets.items():
            archive.writestr(name, data)
    return stream.getvalue()


def make_batches(count, workers, batch_size=None):
    """Index batches: small enough that a fast worker takes more, large enough to amortise uploads."""
    size = batch_size or max(1, min(8, math.ceil(count / (3 * max(1, workers)))))
    return [list(range(start, min(count, start + size))) for start in range(0, count, size)]


class Job:
    def __init__(self, job_id, envelope, assets, root, batches):
        self.id = job_id
        self.envelope = envelope
        self.assets = assets
        self.frequencies = [float(f) for f in envelope["request"]["frequencies_hz"]]
        self.root = root
        self.lock = threading.RLock()
        self.events = []
        self.pending = deque(batches)
        self.attempts = {tuple(batch): 0 for batch in batches}
        self.received = {}  # frequency index -> result dict (ordered mode buffers here)
        self.emitted = set()
        self.next_ordered = 0
        self.active = {}  # worker name -> remote sub job id
        self.cancelled = False
        self.terminal = False
        self.accepted = False
        self.started = time.monotonic()
        self.started_wall = time.time()
        self.finished = None
        self.state = [{"index": i, "freq_hz": f, "state": "queued", "worker": None, "batch": None,
                       "assigned_at": None, "done_at": None} for i, f in enumerate(self.frequencies)]
        self.batch_number = {tuple(batch): n for n, batch in enumerate(batches)}
        self.timeline = []  # {"worker", "batch", "indices", "start", "end", "outcome"}
        (self.root / self.id).mkdir(parents=True)

    def mark(self, indices, worker, state, batch=None):
        now = time.monotonic() - self.started
        with self.lock:
            for i in indices:
                entry = self.state[i]
                if state == "assigned":
                    entry.update(state="assigned", worker=worker, batch=batch, assigned_at=round(now, 2), done_at=None)
                elif state == "running" and entry["state"] != "done":
                    entry["state"] = "running"
                elif state == "done":
                    entry.update(state="done", worker=worker, done_at=round(now, 2))
                elif state == "queued" and entry["state"] != "done":
                    entry.update(state="queued", worker=None, batch=None, assigned_at=None)

    def summary(self):
        with self.lock:
            done = sum(1 for e in self.state if e["state"] == "done")
            elapsed = (self.finished or time.monotonic()) - self.started
            kind = self.events[-1]["type"] if self.events and self.events[-1]["type"] in TERMINAL else "running"
            return {"job_id": self.id, "state": kind, "frequency_count": len(self.frequencies), "done": done,
                    "frequencies_hz": self.frequencies, "started_wall": self.started_wall, "elapsed_s": round(elapsed, 1),
                    "frequencies": [dict(e) for e in self.state], "timeline": [dict(t) for t in self.timeline],
                    "batches": len(self.batch_number), "solved_by": dict(getattr(self, "solved_by", {})),
                    "gib_estimate": round(getattr(self, "gib", 0.0), 2)}

    def append(self, event):
        with self.lock:
            self.events.append(event)
            with (self.root / self.id / "events.ndjson").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, allow_nan=False) + "\n")

    def index_of(self, freq_hz):
        best = min(range(len(self.frequencies)), key=lambda i: abs(self.frequencies[i] - freq_hz))
        tolerance = max(1e-5, abs(freq_hz) * 2e-6)
        if abs(self.frequencies[best] - freq_hz) > tolerance:
            raise ValueError(f"worker returned unrequested frequency {freq_hz:g} Hz")
        return best


class Farm:
    """One active job, like a single Boundary Lab server; fans batches out to the workers."""

    def __init__(self, workers, root, *, batch_size=None, ordered=False, busy_retry_s=3.0, max_attempts=3, poll_s=0.2):
        if not workers:
            raise ValueError("at least one --worker is required")
        self.workers = workers
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.batch_size = batch_size
        self.ordered = ordered
        self.busy_retry_s = busy_retry_s
        self.max_attempts = max_attempts
        self.poll_s = poll_s
        self.jobs = {}
        self.threads = []
        self.history = deque(maxlen=12)
        self.current_job = None
        self.worker_token = None
        self.discovered_max_gib = None
        self.join_lock = threading.Lock()
        self.sched_lock = threading.RLock()
        self.loops = {}  # worker name -> thread
        self.closing = False
        self.learn_backends()
        for worker in self.workers:
            self._start_loop(worker)

    def learn_backends(self):
        """Ask each pinned worker which backend it runs, so CPU workers are counted at twice the memory from the start."""
        for worker in self.workers:
            if worker.backend_id is None:
                worker.backend_id = guess_backend(worker.capabilities())

    def _start_loop(self, worker):
        if worker.name in self.loops:
            return
        thread = threading.Thread(target=self._worker_loop, args=(worker,), daemon=True, name=f"farm-{worker.name}")
        self.loops[worker.name] = thread
        thread.start()

    # ----- scheduling: every worker pulls from the queue of jobs it can hold
    def active_jobs(self):
        return [j for j in self.jobs.values() if not j.terminal]

    def eligible(self, worker, job):
        return worker.max_gib is None or worker.max_gib >= worker.need(job.gib)

    def host_headroom(self, worker, job):
        """A worker on a machine with a known budget may start a batch only if the batches already running
        on that machine's other workers leave room for this one."""
        if worker.host_gib is None or not worker.shared:
            return True
        in_use = sum(w.need(w.current_gib) for w in self.workers
                     if w is not worker and w.shared and w.host == worker.host and w.current is not None)
        return in_use == 0 or in_use + worker.need(job.gib) <= worker.host_gib  # alone on the machine: the cap decides

    def _priority(self, job):
        """Jobs few machines can hold go first, then by submission order."""
        return (sum(1 for w in self.workers if self.eligible(w, job) and not w.down), job.started_wall)

    def _pick(self, worker):
        with self.sched_lock:
            for job in sorted(self.active_jobs(), key=self._priority):
                if job.cancelled or not self.eligible(worker, job) or not self.host_headroom(worker, job):
                    continue
                batch = self._take(job, worker)
                if batch is not None:
                    worker.current = batch
                    worker.current_gib = job.gib
                    return job, batch
        return None, None

    def _worker_loop(self, worker):
        while not self.closing:
            job, batch = self._pick(worker)
            if job is None:
                time.sleep(0.3)
                continue
            self._serve_batch(job, worker, batch)
            self._settle(job)

    def _settle(self, job):
        """Emit the terminal event once nothing is pending or in flight for a job."""
        with job.lock:
            if job.terminal:
                return
            in_flight = any(e["end"] is None for e in job.timeline)
            received = len(job.received)
            if job.cancelled and not in_flight:
                job.append({"type": "cancelled", "worker": self._provenance(job)})
            elif received == len(job.frequencies):
                if self.ordered:
                    self._flush_ordered(job, final=True)
                now = round(time.monotonic() - job.started, 2)
                for e in job.timeline:  # duplicates still in flight are no longer needed
                    if e["end"] is None:
                        e.update(end=now, outcome="superseded")
                job.append({"type": "completed", "worker": self._provenance(job)})
            elif not job.pending and not in_flight:
                job.append({"type": "failed", "worker": self._provenance(job),
                            "error": f"Farm solved {received} of {len(job.frequencies)} frequencies; "
                                     f"workers down: {[w.name for w in job.workers if w.down]}"})
            else:
                return
            job.terminal = True
            job.finished = time.monotonic()
        self.history.appendleft({k: v for k, v in job.summary().items() if k not in ("frequencies", "timeline")}
                                | {"per_worker": [w.snapshot() for w in job.workers]})
        elapsed = time.monotonic() - job.started
        LOGGER.info("Farm job %s | job=%s | frequencies=%d | elapsed=%.1fs | per worker: %s",
                    job.events[-1]["type"], job.id, len(job.received), elapsed,
                    ", ".join(f"{w.name}={job.solved_by.get(w.name, 0)}" for w in job.workers))
        if self.current_job is job:
            self.current_job = next(iter(self.active_jobs()), job)

    # ----- workers joining on their own
    def add_worker(self, url, *, name=None, token=None, max_gib=None, host_gib=None, shared=True, backend_id=None,
                   stated=False):
        """Add a server if it speaks the protocol; idempotent by URL. Returns (worker, added). A worker's own
        registration (``stated``) overrides the name and caps discovery guessed for it."""
        url = url.rstrip("/")
        with self.join_lock:
            for worker in self.workers:
                if worker.url == url:
                    if name and worker.name != name:  # a self-registration knows its real computer name
                        LOGGER.info("worker %s is %s", worker.name, name)
                        worker.name = name
                    if stated and (worker.max_gib, worker.host_gib, worker.shared) != (max_gib, host_gib, shared):
                        LOGGER.info("worker %s caps: %s GiB, machine %s GiB, %s", worker.name, max_gib, host_gib,
                                    "shared" if shared else "independent")
                        worker.max_gib, worker.host_gib, worker.shared = max_gib, host_gib, shared
                    if stated and backend_id and worker.backend_id != backend_id:
                        worker.backend_id = backend_id
                    return worker, False
            worker = Worker(url, name=name, token=token, max_gib=max_gib, host_gib=host_gib, shared=shared,
                            backend_id=backend_id)
            info = worker.capabilities()
            if info.get("protocol") != PROTOCOL:
                raise ValueError(f"{url} is not a Boundary Lab server ({info.get('error', 'no protocol field')})")
            if worker.backend_id is None:
                worker.backend_id = guess_backend(info)
            self.workers.append(worker)
        LOGGER.info("worker joined: %-12s %s  state=%s  backends=%s", worker.name, url, info.get("state"), info.get("backend_ids"))
        self._start_loop(worker)
        return worker, True

    DISCOVERY_CONCURRENCY = 24  # a /24 scan must not open 250 sockets at once (macOS defaults to 256 descriptors)

    def discover(self, hosts, ports=(8765,), *, timeout=0.4):
        """Probe host:port pairs for Boundary Lab servers and add the ones found, a few at a time."""
        known = {w.url for w in self.workers}
        found = []
        slots = threading.BoundedSemaphore(self.DISCOVERY_CONCURRENCY)

        def probe(host, port):
            url = f"http://{host}:{port}"
            if url in known:
                return
            try:
                with slots, socket.create_connection((host, port), timeout=timeout):
                    pass
            except OSError:
                return
            try:
                name = socket.gethostbyaddr(host)[0].split(".")[0]
            except OSError:
                name = host
            try:
                worker, added = self.add_worker(url, name=name, token=self.worker_token, max_gib=self.discovered_max_gib)
                if added:
                    found.append(worker)
            except ValueError:
                pass

        threads = [threading.Thread(target=probe, args=(host, port), daemon=True)
                   for host in dict.fromkeys(hosts) for port in dict.fromkeys(ports)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=timeout * (len(threads) / self.DISCOVERY_CONCURRENCY + 2) + 6)
        return found


    def start_discovery(self, interval_s=30.0, ports=(8765,)):
        """Scan this Mac's /24 LAN for Boundary Lab servers every ``interval_s`` seconds."""
        def loop():
            while True:
                try:
                    own = local_ip()
                    if own:
                        prefix = own.rsplit(".", 1)[0]
                        hosts = [f"{prefix}.{i}" for i in range(1, 255) if f"{prefix}.{i}" != own]
                        self.discover(hosts, ports)
                        for worker in self.workers:
                            if worker.current is None:  # do not probe a worker mid-batch
                                worker.capabilities()
                except Exception as exc:  # discovery must never kill the farm
                    LOGGER.warning("discovery error: %s", exc)
                time.sleep(interval_s)

        threading.Thread(target=loop, daemon=True).start()

    # ----- capabilities
    def capabilities(self):
        infos = {}
        threads = []
        for worker in self.workers:
            def probe(w=worker):
                infos[w.name] = w.capabilities()
            thread = threading.Thread(target=probe, daemon=True)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join(timeout=6)
        states = [info.get("state", "unavailable") for info in infos.values()]
        state = "ready" if "ready" in states else ("starting" if "starting" in states else "unavailable")
        template = next((info for info in infos.values() if info.get("state") == "ready"), None) or {}
        backends = {}
        for info in infos.values():
            for key, record in (info.get("backends") or {}).items():
                if record.get("available") or key not in backends:
                    backends[key] = record
        return {
            "protocol": PROTOCOL,
            "version": template.get("version", 2),
            "state": state,
            "request_schema_version": template.get("request_schema_version"),
            "result_schema_version": template.get("result_schema_version"),
            "backend_ids": sorted({key for key, record in backends.items() if record.get("available")}),
            "backends": backends,
            "phasor_conventions": template.get("phasor_conventions", []),
            "observation_planes": False,
            "max_bundle_bytes": MAX_BUNDLE_BYTES,
            "runtime_check": "Farm of Boundary Lab servers; each worker rechecks runtime compatibility per batch.",
            "farm": {
                "workers": [
                    {"name": w.name, "url": w.url, "state": infos.get(w.name, {}).get("state", "unavailable"),
                     "backend_ids": infos.get(w.name, {}).get("backend_ids", [])}
                    for w in self.workers
                ]
            },
        }

    def status(self):
        """Live picture for dashboards: workers, every active job with its per-frequency assignment, recent jobs."""
        active = sorted(self.active_jobs(), key=self._priority)
        summaries = []
        for job in active:
            summary = job.summary()
            rate = sum(w.solved / w.seconds for w in job.workers if w.solved and w.seconds and not w.down)
            remaining = summary["frequency_count"] - summary["done"]
            summary["eta_s"] = round(remaining / rate, 1) if rate and remaining else None
            summary["eligible_workers"] = [w.name for w in job.workers]
            summaries.append(summary)
        latest = self.current_job.summary() if self.current_job and not summaries else None
        return {"farm_time": time.time(), "workers": [w.snapshot() for w in self.workers],
                "job": summaries[0] if summaries else latest, "jobs": summaries,
                "history": list(self.history), "batch_size": self.batch_size, "ordered": self.ordered}

    # ----- jobs
    def submit(self, bundle):
        envelope, assets = read_bundle(bundle)
        count = len(envelope["request"]["frequencies_hz"])
        job = Job(uuid.uuid4().hex, envelope, assets, self.root, [])
        job.gib = estimate_gib(assets)
        eligible = [w for w in self.workers if self.eligible(w, job)]
        if not eligible:
            raise ValueError(f"No worker has room for this model (about {job.gib:.1f} GiB dense BEM on a GPU worker, "
                             f"twice that on a CPU worker); caps: {[(w.name, w.max_gib) for w in self.workers]}")
        batches = make_batches(count, len(eligible), self.batch_size)
        job.pending = deque(batches)
        job.attempts = {tuple(b): 0 for b in batches}
        job.batch_number = {tuple(b): n for n, b in enumerate(batches)}
        job.workers = eligible
        job.solved_by = {}
        with self.sched_lock:
            self.jobs[job.id] = job
            if self.current_job is None or self.current_job.terminal:
                self.current_job = job
        LOGGER.info("Farm job queued | job=%s | frequencies=%d | batches=%d | workers=%d of %d (about %.1f GiB) | queue=%d",
                    job.id, count, len(batches), len(eligible), len(self.workers), job.gib, len(self.active_jobs()))
        return job.id

    def events(self, job_id, cursor, *, limit=32):
        job = self.jobs.get(job_id)
        if job is None or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise FileNotFoundError("Unknown job.")
        with job.lock:
            if cursor < 0 or cursor > len(job.events):
                raise ValueError("Invalid event cursor.")
            return list(job.events[cursor: None if limit is None else cursor + limit])

    def cancel(self, job_id):
        job = self.jobs.get(job_id)
        if job is None:
            raise FileNotFoundError("Unknown job.")
        with job.lock:
            job.cancelled = True
            active = dict(job.active)
        for worker in getattr(job, "workers", self.workers):
            sub = active.get(worker.name)
            if sub:
                try:
                    worker.call(f"/v1/jobs/{sub}", method="DELETE")
                except WorkerError as exc:
                    LOGGER.warning("Cancel on %s failed: %s", worker.name, exc)
        with job.lock:
            job.pending.clear()
        self._settle(job)

    # ----- dispatch
    def _provenance(self, job):
        return {"farm": [{"name": w.name, "url": w.url, "backend_id": w.backend_id, "solved": job.solved_by.get(w.name, 0)}
                         for w in getattr(job, "workers", self.workers)]}

    def _take(self, job, worker=None):
        with job.lock:
            if job.cancelled:
                return None
            if job.pending:
                return job.pending.popleft()
            return self._speculate(job, worker)

    def _speculate(self, job, worker):
        """Queue empty: an idle fast worker duplicates the batch of the slowest running worker when it
        would finish well before that worker does. Results are deduplicated per frequency, so the first
        copy wins and the straggler's late copy is ignored. At most one duplicate per batch."""
        if worker is None or not worker.solved:
            return None
        mine = worker.seconds / worker.solved
        now = time.monotonic() - job.started
        best, best_gain = None, 0.0
        for entry in job.timeline:
            if entry["end"] is not None or entry["worker"] == worker.name or entry.get("speculated"):
                continue
            other = next((w for w in job.workers if w.name == entry["worker"]), None)
            remaining_indices = [i for i in entry["indices"] if i not in job.received]
            if not remaining_indices:
                continue
            theirs = (other.seconds / other.solved) if other and other.solved else None
            their_remaining = (len(remaining_indices) * theirs - max(0.0, now - entry["start"])) if theirs else float("inf")
            my_time = len(remaining_indices) * mine
            if their_remaining > 2 * my_time and their_remaining - my_time > best_gain:
                best, best_gain = entry, their_remaining - my_time
        if best is None:
            return None
        best["speculated"] = True
        batch = [i for i in best["indices"] if i not in job.received]
        job.attempts.setdefault(tuple(batch), 0)
        job.batch_number.setdefault(tuple(batch), best["batch"])
        LOGGER.info("%s duplicates batch %d of %s (straggler)", worker.name, best["batch"], best["worker"])
        return batch

    def _give_back(self, job, batch):
        with job.lock:
            job.pending.appendleft(batch)

    def _serve_batch(self, job, worker, batch):
        """Run one batch on one worker; requeue on failure; benign if a duplicate finished first."""
        key = tuple(batch)
        with job.lock:
            job.attempts[key] = job.attempts.get(key, 0) + 1
            attempts = job.attempts[key]
        if attempts > self.max_attempts:
            LOGGER.error("Batch %s failed %d times; giving up", batch, attempts - 1)
            with job.lock:
                job.pending.clear()
            return
        frequencies = [job.frequencies[i] for i in batch]
        started = time.monotonic()
        job.mark(batch, worker.name, "assigned", batch=job.batch_number.get(key))
        worker.current = batch
        entry = {"worker": worker.name, "batch": job.batch_number.get(key), "indices": list(batch),
                 "start": round(started - job.started, 2), "end": None, "outcome": "running"}
        with job.lock:
            job.timeline.append(entry)
        try:
            self._run_batch(job, worker, batch, frequencies)
            entry.update(end=round(time.monotonic() - job.started, 2), outcome="done")
        except WorkerError as exc:
            worker.current = None
            with job.lock:
                already = all(i in job.received for i in batch)
            if already:
                entry.update(end=round(time.monotonic() - job.started, 2), outcome="superseded")
                return
            entry.update(end=round(time.monotonic() - job.started, 2), outcome="failed")
            if exc.code == 409:  # busy with someone else's job: put the batch back and let any worker retry
                with job.lock:
                    job.attempts[key] -= 1
                self._give_back(job, batch)
                LOGGER.info("%s busy; batch %s requeued", worker.name, batch)
                time.sleep(self.busy_retry_s)
                return
            worker.failures += 1
            worker.down = True
            worker.last_error = str(exc)
            job.mark(batch, None, "queued")
            self._give_back(job, batch)
            LOGGER.warning("%s failed batch %s: %s; other workers take it", worker.name, batch, exc)
            job.append({"type": "status", "message": f"[{worker.name}] batch failed ({exc}); redistributing"})
            return
        except Exception as exc:
            worker.current = None
            entry.update(end=round(time.monotonic() - job.started, 2), outcome="failed")
            worker.failures += 1
            worker.last_error = str(exc)
            job.mark(batch, None, "queued")
            self._give_back(job, batch)
            LOGGER.warning("%s error on batch %s: %s", worker.name, batch, exc)
            job.append({"type": "status", "message": f"[{worker.name}] error ({exc}); redistributing"})
            if worker.failures >= 2:
                worker.down = True
            return
        worker.seconds += time.monotonic() - started
        worker.current = None
        with job.lock:
            stragglers = [(e["worker"], job.active.get(e["worker"])) for e in job.timeline
                          if e is not entry and e["end"] is None and set(e["indices"]) <= set(job.received)]
        for name, sub in stragglers:
            other = next((w for w in job.workers if w.name == name), None)
            if other and sub:
                try:
                    other.call(f"/v1/jobs/{sub}", method="DELETE")
                    LOGGER.info("cancelled %s's copy of a batch already solved elsewhere", name)
                except WorkerError:
                    pass
        if job.cancelled:
            entry["outcome"] = "cancelled"

    def _run_batch(self, job, worker, batch, frequencies):
        reply = worker.call("/v1/jobs", data=sub_bundle(job.envelope, job.assets, frequencies), method="POST", timeout=120)
        sub = reply["job_id"]
        with job.lock:
            job.active[worker.name] = sub
        LOGGER.info("%s <- batch %s (%d freqs, %g..%g Hz) job=%s", worker.name, batch[0], len(batch),
                    frequencies[0], frequencies[-1], sub)
        cursor = 0
        got = set()
        try:
            while True:
                if job.cancelled:
                    try:
                        worker.call(f"/v1/jobs/{sub}", method="DELETE")
                    except WorkerError:
                        pass
                    return
                reply = worker.call(f"/v1/jobs/{sub}?cursor={cursor}")
                cursor = reply["next_cursor"]
                for event in reply["events"]:
                    kind = event["type"]
                    if kind == "accepted":
                        worker.backend_id = event.get("backend_id")
                        job.mark(batch, worker.name, "running")
                        with job.lock:
                            first = not job.accepted
                            job.accepted = True
                        if first:
                            job.append(dict(event))
                    elif kind == "status":
                        job.append({"type": "status", "message": f"[{worker.name}] {event.get('message', '')}"})
                    elif kind == "result":
                        index = job.index_of(float(event["result"]["freq_hz"]))
                        got.add(index)
                        if self._deliver(job, index, event, worker):  # duplicates from a straggler do not count
                            worker.solved += 1
                            with job.lock:
                                job.solved_by[worker.name] = job.solved_by.get(worker.name, 0) + 1
                            job.mark([index], worker.name, "done")
                    elif kind in TERMINAL:
                        if kind != "completed":
                            raise WorkerError(0, event.get("error", kind))
                        missing = set(batch) - got
                        if missing:
                            raise WorkerError(0, f"batch ended without frequencies {sorted(missing)}")
                        return
                if not reply["events"]:
                    time.sleep(self.poll_s)
        finally:
            with job.lock:
                job.active.pop(worker.name, None)

    def _deliver(self, job, index, event, worker):
        tagged = dict(event)
        tagged["farm_worker"] = worker.name
        with job.lock:
            if index in job.received:
                return False  # duplicate from a retried or speculated batch
            job.received[index] = tagged
            if not self.ordered:
                job.append(tagged)
                job.emitted.add(index)
            else:
                self._flush_ordered(job)
        return True

    def _flush_ordered(self, job, final=False):
        with job.lock:
            while job.next_ordered in job.received:
                job.append(job.received[job.next_ordered])
                job.emitted.add(job.next_ordered)
                job.next_ordered += 1


def create_http_server(farm, *, host="0.0.0.0", port=8766, token=None):
    if token is not None and (len(token) < 32 or not token.isascii() or any(c.isspace() for c in token)):
        raise ValueError("Access key must contain at least 32 ASCII characters without whitespace.")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self, code, value):
            data = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def handle_request(self):
            self.connection.settimeout(30)
            if token and not hmac.compare_digest(self.headers.get("Authorization", "").encode(), f"Bearer {token}".encode()):
                self.reply(401, {"error": "Authentication required."})
                return
            path = urlsplit(self.path)
            try:
                if self.command == "GET" and path.path == "/v1/capabilities":
                    self.reply(200, farm.capabilities())
                elif self.command == "GET" and path.path == "/v1/farm/status":
                    self.reply(200, farm.status())
                elif self.command == "POST" and path.path == "/v1/farm/register":
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096:
                        raise ValueError("A small JSON body is required.")
                    body = json.loads(self.rfile.read(length))
                    worker, added = farm.add_worker(str(body["url"]), name=body.get("name") or None, token=farm.worker_token,
                                                    max_gib=body.get("max_gib", farm.discovered_max_gib),
                                                    host_gib=body.get("host_gib"),
                                                    shared=body.get("memory", "shared") != "independent",
                                                    backend_id=normalize_backend(body.get("backend")), stated=True)
                    self.reply(200, {"registered": True, "added": added, "name": worker.name, "url": worker.url})
                elif self.command == "GET" and path.path in {"/", "/dashboard"}:
                    data = (Path(__file__).with_name("dashboard.html")).read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                elif self.command == "POST" and path.path == "/v1/jobs":
                    if self.headers.get_content_type() != "application/zip":
                        self.reply(415, {"error": "Expected application/zip."})
                        return
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_BUNDLE_BYTES or self.headers.get("Transfer-Encoding"):
                        raise ValueError("A bounded Content-Length is required.")
                    bundle = self.rfile.read(length)
                    if len(bundle) != length:
                        raise ValueError("Incomplete upload.")
                    self.reply(202, {"job_id": farm.submit(bundle)})
                elif match := re.fullmatch(r"/v1/jobs/([0-9a-f]{32})", path.path):
                    job_id = match[1]
                    if self.command == "GET":
                        cursor = int(parse_qs(path.query).get("cursor", ["0"])[0])
                        events = farm.events(job_id, cursor)
                        self.reply(200, {"events": events, "next_cursor": cursor + len(events)})
                    elif self.command == "DELETE":
                        farm.cancel(job_id)
                        self.reply(202, {"job_id": job_id})
                    else:
                        self.reply(405, {"error": "Unsupported method."})
                else:
                    self.reply(404, {"error": "Unknown endpoint."})
            except BlockingIOError as exc:
                self.reply(409, {"error": str(exc)})
            except FileNotFoundError as exc:
                self.reply(404, {"error": str(exc)})
            except (ValueError, KeyError, TypeError, zipfile.BadZipFile) as exc:
                LOGGER.warning("Client request rejected | %s | %s: %s", self.command, type(exc).__name__, exc)
                self.reply(400, {"error": str(exc)})
            except (ConnectionError, TimeoutError):
                pass

        do_GET = handle_request
        do_POST = handle_request
        do_DELETE = handle_request

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            if isinstance(sys.exception(), (ConnectionError, TimeoutError)):
                return
            super().handle_error(request, client_address)

    return Server((host, port), Handler)


def local_ip():
    """This Mac's LAN address (the interface that would route to the internet)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0]
    except OSError:
        return None


def normalize_backend(text):
    """'cpu' / 'metal' / 'beat_metal' -> 'beat_metal'; 'auto', '' and None -> None (unknown)."""
    text = (text or "").strip().lower()
    if not text or text == "auto":
        return None
    return text if text.startswith("beat_") else f"beat_{text}"


def guess_backend(info):
    """Which backend a server probably runs, from its capabilities. They list every backend it *could* run,
    not the one it was started with, so prefer a GPU: counting a GPU worker as CPU would double its jobs
    and keep big meshes off it. The worker's first accepted batch replaces the guess with the truth."""
    ids = list(info.get("backend_ids") or [])
    for preferred in ("beat_metal", "beat_cuda", "beat_rocm"):
        if preferred in ids:
            return preferred
    return ids[0] if ids else None


def raise_descriptor_limit(wanted=4096):
    """Lift this process's soft open-file limit (macOS ships 256); the farm holds a socket per worker poll,
    per client connection and per discovery probe. Best effort; returns the limit in force."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        target = min(wanted, hard) if hard != resource.RLIM_INFINITY else wanted
        if soft < target:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        return resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except (ImportError, ValueError, OSError):
        return None


def parse_worker(spec, token):
    """URL[=name][@max_gib[/host_gib]][+backend], e.g. http://192.168.1.86:8765=mbp@48 or
    http://127.0.0.1:8772=air-cpu@18/18+cpu (jobs up to 18 GiB; all workers on that machine together stay within
    18 GiB; it runs the CPU backend, so its jobs count double). Without ``/host_gib`` the worker is independent:
    only its own cap limits it. Without ``+backend`` the farm guesses (GPU first) until the first batch tells."""
    spec, _, backend = spec.partition("+")
    url, _, rest = spec.partition("=")
    name, _, cap = rest.partition("@")
    if "@" in url:
        url, _, cap = url.partition("@")
    cap, _, host_cap = cap.partition("/")
    return Worker(url, name=name or None, token=token, max_gib=float(cap) if cap else None,
                  host_gib=float(host_cap) if host_cap else None, shared=bool(host_cap),
                  backend_id=normalize_backend(backend))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worker", action="append", default=[], metavar="URL[=name][@max_gib[/host_gib]][+backend]",
                        help="a Boundary Lab server (repeat); e.g. http://192.168.1.20:8765=mbp@48 (cap in GiB of dense BEM)")
    parser.add_argument("--discovered-max-gib", type=float, default=None,
                        help="memory cap applied to workers found by discovery or registration")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--root", default=os.path.expanduser("~/Library/Application Support/Boundary Lab/farm-jobs"))
    parser.add_argument("--batch", type=int, default=None, help="frequencies per batch (default: adaptive, at most 8)")
    parser.add_argument("--ordered", action="store_true", help="stream results in request order (buffers)")
    parser.add_argument("--token-env", default="BLAB_FARM_TOKEN", help="env var holding the key clients must send")
    parser.add_argument("--worker-token-env", default="BLAB_SERVER_TOKEN", help="env var holding the workers' key")
    parser.add_argument("--no-discover", action="store_true", help="do not scan the LAN for Boundary Lab servers")
    parser.add_argument("--discover-interval", type=float, default=30.0, help="seconds between LAN scans")
    parser.add_argument("--discover-ports", default="8765", help="comma-separated ports to scan")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    worker_token = os.environ.get(args.worker_token_env) or None
    workers = [parse_worker(spec, worker_token) for spec in args.worker]
    farm = Farm(workers, args.root, batch_size=args.batch, ordered=args.ordered) if workers else Farm.__new__(Farm)
    if not workers:  # allowed when discovery/registration will supply the workers
        Farm.__init__(farm, [Worker("http://127.0.0.1:1")], args.root, batch_size=args.batch, ordered=args.ordered)
        farm.workers.clear()
    farm.worker_token = worker_token
    farm.discovered_max_gib = args.discovered_max_gib
    if not args.no_discover:
        ports = tuple(int(p) for p in args.discover_ports.split(",") if p)
        own = local_ip()
        LOGGER.info("scanning %s.0/24 ports %s for Boundary Lab servers every %.0fs (workers may also POST /v1/farm/register)",
                    own.rsplit(".", 1)[0] if own else "?", list(ports), args.discover_interval)
        farm.start_discovery(args.discover_interval, ports)
    server = create_http_server(farm, host=args.host, port=args.port, token=os.environ.get(args.token_env) or None)
    time.sleep(0 if args.no_discover else 2.5)  # let the first scan land before printing the roster
    caps = farm.capabilities()
    for info in caps["farm"]["workers"]:
        LOGGER.info("worker %-8s %s  state=%s  backends=%s", info["name"], info["url"], info["state"], info["backend_ids"])
    LOGGER.info("Boundary Lab farm listening on http://%s:%d  (point the GUI's Boundary Lab Server address here; "
                "open it in a browser for the live dashboard)", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

