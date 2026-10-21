"""blab-farm: splits a remote job over fake Boundary Lab workers, merges, fails over, speculates, queues, cancels."""
from __future__ import annotations

import hashlib
import io
import json
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

import pytest

from blab_farm import farm as blab_farm  # noqa: E402


class FakeWorker:
    """Minimal Boundary Lab server protocol: one job at a time, result event per frequency."""

    def __init__(self, name, *, delay=0.01, fail_frequencies=(), fail_once=True):
        self.name, self.delay = name, delay
        self.fail_frequencies, self.fail_once = set(fail_frequencies), fail_once
        self.jobs, self.lock, self.busy = {}, threading.Lock(), threading.Lock()
        self.batches = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, code, value):
                data = json.dumps(value).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path.startswith("/v1/capabilities"):
                    self.reply(200, {"protocol": "blab-remote", "version": 2, "state": "ready", "request_schema_version": 3,
                                     "result_schema_version": 2, "backend_ids": ["beat_cpu"],
                                     "backends": {"beat_cpu": {"available": True, "state": "ready"}},
                                     "phasor_conventions": ["positive_time"], "observation_planes": False})
                    return
                job_id, _, query = self.path[len("/v1/jobs/"):].partition("?")
                cursor = int(query.partition("=")[2] or 0)
                with outer.lock:
                    events = outer.jobs[job_id][cursor:cursor + 32]
                self.reply(200, {"events": events, "next_cursor": cursor + len(events)})

            def do_POST(self):
                if not outer.busy.acquire(blocking=False):
                    self.reply(409, {"error": "busy"})
                    return
                bundle = self.rfile.read(int(self.headers["Content-Length"]))
                with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
                    job = json.loads(archive.read("job.json"))
                    assert set(archive.namelist()) == {"job.json", *(a["name"] for a in job["assets"])}
                frequencies = job["request"]["frequencies_hz"]
                job_id = hashlib.md5(f"{outer.name}{time.time()}{frequencies}".encode()).hexdigest()
                outer.batches.append(list(frequencies))
                with outer.lock:
                    outer.jobs[job_id] = [{"type": "accepted", "backend_id": "beat_cpu", "execution_backend": "cpu"}]
                threading.Thread(target=outer.run, args=(job_id, frequencies), daemon=True).start()
                self.reply(202, {"job_id": job_id})

            def do_DELETE(self):
                job_id = self.path[len("/v1/jobs/"):]
                with outer.lock:
                    outer.jobs[job_id].append({"type": "cancelled"})
                self.reply(202, {"job_id": job_id})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def run(self, job_id, frequencies):
        try:
            for f in frequencies:
                time.sleep(self.delay)
                with self.lock:
                    if self.jobs[job_id][-1]["type"] == "cancelled":
                        return
                    if f in self.fail_frequencies:
                        if self.fail_once:
                            self.fail_frequencies.discard(f)
                        self.jobs[job_id].append({"type": "failed", "error": f"boom at {f}"})
                        return
                    self.jobs[job_id].append({"type": "result", "result": {"freq_hz": f, "quantities": [],
                                                                            "diagnostics": {"phasor_convention": "positive_time", "worker": self.name}}})
            with self.lock:
                self.jobs[job_id].append({"type": "completed", "worker": {"name": self.name}})
        finally:
            self.busy.release()

    def close(self):
        self.server.shutdown()


def bundle(frequencies):
    mesh = b"$MeshFormat\n2.2 0 8\n$EndMeshFormat\n$Elements\n2\n1 2 2 1 1 1 2 3\n2 2 2 1 1 2 3 4\n$EndElements\n"
    name = f"assets/{hashlib.sha256(mesh).hexdigest()}.msh"
    envelope = {"protocol": "blab-remote", "version": 2, "observation_planes": False,
                "request": {"schema_version": 3, "compiled_system": {"meshes": [{"file": name}]}, "frequencies_hz": frequencies,
                            "excitation_port_ids": ["e"], "outputs": [], "solver_options": {}},
                "assets": [{"name": name, "size": len(mesh), "sha256": hashlib.sha256(mesh).hexdigest()}]}
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("job.json", json.dumps(envelope))
        archive.writestr(name, mesh)
    return stream.getvalue()


def call(url, path, data=None, method="GET"):
    request = Request(url + path, data=data, method=method, headers={"Content-Type": "application/zip"})
    with urlopen(request, timeout=10) as response:
        return response.status, json.loads(response.read())


def drain(url, job_id, timeout=20):
    events, cursor, deadline = [], 0, time.time() + timeout
    while time.time() < deadline:
        _, reply = call(url, f"/v1/jobs/{job_id}?cursor={cursor}")
        events += reply["events"]
        cursor = reply["next_cursor"]
        if events and events[-1]["type"] in blab_farm.TERMINAL:
            return events
        time.sleep(0.02)
    raise AssertionError(f"no terminal event: {[e['type'] for e in events]}")


@pytest.fixture
def farm(tmp_path):
    started = []

    def start(workers, **options):
        farm = blab_farm.Farm([blab_farm.Worker(w.url, name=w.name) for w in workers], tmp_path / "farm", busy_retry_s=0.05, poll_s=0.01, **options)
        server = blab_farm.create_http_server(farm, host="127.0.0.1", port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append((server, workers))
        return f"http://127.0.0.1:{server.server_address[1]}", farm

    yield start
    for server, workers in started:
        server.shutdown()
        for worker in workers:
            worker.close()


def test_batches_adapt_to_count_and_workers():
    assert blab_farm.make_batches(4, 2) == [[0], [1], [2], [3]]
    assert [len(b) for b in blab_farm.make_batches(200, 3)] == [8] * 25
    assert blab_farm.make_batches(5, 1, batch_size=2) == [[0, 1], [2, 3], [4]]


def test_sub_bundle_keeps_assets_and_only_changes_frequencies():
    envelope, assets = blab_farm.read_bundle(bundle([100.0, 200.0, 300.0]))
    part = blab_farm.sub_bundle(envelope, assets, [200.0])
    with zipfile.ZipFile(io.BytesIO(part)) as archive:
        job = json.loads(archive.read("job.json"))
        assert job["request"]["frequencies_hz"] == [200.0]
        assert set(archive.namelist()) == {"job.json", *assets}
        assert job["assets"] == envelope["assets"]


def test_two_workers_share_the_sweep_and_the_client_sees_one_job(farm):
    fast, slow = FakeWorker("fast", delay=0.002), FakeWorker("slow", delay=0.05)
    url, _ = farm([fast, slow])
    status, caps = call(url, "/v1/capabilities")
    assert status == 200 and caps["state"] == "ready" and caps["protocol"] == "blab-remote"
    assert [w["name"] for w in caps["farm"]["workers"]] == ["fast", "slow"]
    frequencies = [float(f) for f in range(100, 2500, 100)]  # 24 frequencies
    status, reply = call(url, "/v1/jobs", data=bundle(frequencies), method="POST")
    assert status == 202
    events = drain(url, reply["job_id"])
    assert events[0]["type"] == "accepted" and events[-1]["type"] == "completed"
    results = [e for e in events if e["type"] == "result"]
    assert sorted(r["result"]["freq_hz"] for r in results) == frequencies
    assert {r["farm_worker"] for r in results} == {"fast", "slow"}
    assert len(fast.batches) > len(slow.batches), "the faster worker takes more batches"
    provenance = events[-1]["worker"]["farm"]
    assert sum(w["solved"] for w in provenance) == 24


def test_failed_worker_batch_is_redistributed(farm):
    good, flaky = FakeWorker("good"), FakeWorker("flaky", fail_frequencies={300.0})
    url, live = farm([good, flaky], batch_size=2)
    frequencies = [100.0, 200.0, 300.0, 400.0, 500.0, 600.0]
    _, reply = call(url, "/v1/jobs", data=bundle(frequencies), method="POST")
    events = drain(url, reply["job_id"])
    assert events[-1]["type"] == "completed"
    assert sorted(e["result"]["freq_hz"] for e in events if e["type"] == "result") == frequencies
    timeline = live.jobs[reply["job_id"]].summary()["timeline"]
    if any(300.0 in b for b in flaky.batches):  # only then did a failure actually happen
        redistributed = any("redistributing" in e.get("message", "") for e in events if e["type"] == "status")
        superseded = any(t["outcome"] in ("superseded", "failed") for t in timeline if t["worker"] == "flaky")
        assert redistributed or superseded, "the flaky worker's batch was either redistributed or overtaken by a duplicate"


def test_ordered_mode_streams_in_request_order(farm):
    url, _ = farm([FakeWorker("a", delay=0.02), FakeWorker("b", delay=0.001)], ordered=True, batch_size=1)
    frequencies = [100.0, 200.0, 300.0, 400.0, 500.0, 600.0, 700.0, 800.0]
    _, reply = call(url, "/v1/jobs", data=bundle(frequencies), method="POST")
    events = drain(url, reply["job_id"])
    assert [e["result"]["freq_hz"] for e in events if e["type"] == "result"] == frequencies


def test_cancel_stops_workers_and_reports_cancelled(farm):
    worker = FakeWorker("w", delay=0.2)
    url, _ = farm([worker], batch_size=1)
    _, reply = call(url, "/v1/jobs", data=bundle([100.0, 200.0, 300.0, 400.0]), method="POST")
    time.sleep(0.1)
    status, _ = call(url, f"/v1/jobs/{reply['job_id']}", method="DELETE")
    assert status == 202
    events = drain(url, reply["job_id"])
    assert events[-1]["type"] == "cancelled"
    assert len([e for e in events if e["type"] == "result"]) < 4


def test_second_job_queues_and_both_complete(farm):
    url, _ = farm([FakeWorker("w", delay=0.05)])
    _, first = call(url, "/v1/jobs", data=bundle([100.0, 200.0]), method="POST")
    status, second = call(url, "/v1/jobs", data=bundle([300.0]), method="POST")
    assert status == 202
    assert drain(url, first["job_id"])[-1]["type"] == "completed"
    assert drain(url, second["job_id"])[-1]["type"] == "completed"


def test_big_job_starts_on_the_big_worker_while_small_jobs_use_the_rest(farm):
    small, big = FakeWorker("small", delay=0.05), FakeWorker("big", delay=0.05)
    url, live = farm([small, big], batch_size=1)
    live.workers[0].max_gib = 0.0      # "small" cannot hold any real mesh (the fake meshes are tiny but > 0)
    live.workers[1].max_gib = 100.0
    _, small_job = call(url, "/v1/jobs", data=bundle([100.0, 200.0, 300.0, 400.0]), method="POST")
    live.workers[0].max_gib = 100.0    # from here both can hold everything; the big-only job is submitted next
    _, big_job = call(url, "/v1/jobs", data=bundle([1000.0, 2000.0, 3000.0, 4000.0]), method="POST")
    live.jobs[big_job["job_id"]].workers = [live.workers[1]]
    live.jobs[big_job["job_id"]].gib = 50.0; live.workers[0].max_gib = 10.0   # only "big" may hold it
    a = drain(url, small_job["job_id"]); b = drain(url, big_job["job_id"])
    assert a[-1]["type"] == "completed" and b[-1]["type"] == "completed"
    assert {e["farm_worker"] for e in b if e["type"] == "result"} == {"big"}, "the restricted job ran only on the big worker"
    assert "small" in {e["farm_worker"] for e in a if e["type"] == "result"}, "the small worker kept the other job moving"


def test_status_reports_assignment_per_frequency_and_dashboard_is_served(farm):
    a, b = FakeWorker("alpha", delay=0.01), FakeWorker("beta", delay=0.01)
    url, live = farm([a, b], batch_size=2)
    frequencies = [100.0, 200.0, 300.0, 400.0]
    _, reply = call(url, "/v1/jobs", data=bundle(frequencies), method="POST")
    drain(url, reply["job_id"])
    status, data = call(url, "/v1/farm/status")
    assert status == 200 and data["job"]["state"] == "completed" and data["job"]["done"] == 4
    assert all(e["state"] == "done" and e["worker"] in {"alpha", "beta"} and e["done_at"] is not None for e in data["job"]["frequencies"])
    assert {t["worker"] for t in data["job"]["timeline"]} <= {"alpha", "beta"}
    assert {t["outcome"] for t in data["job"]["timeline"]} <= {"done", "superseded"}, "a speculative duplicate may be superseded"
    assert sum(w["solved"] for w in data["workers"]) == 4 and data["history"][0]["done"] == 4
    with urlopen(url + "/", timeout=5) as page:
        assert page.status == 200 and b"Boundary Lab farm" in page.read()


def test_workers_can_register_and_be_discovered(farm):
    seed = FakeWorker("seed")
    url, live = farm([seed])
    late = FakeWorker("late")
    body = json.dumps({"url": late.url, "name": "late"}).encode()
    request = Request(url + "/v1/farm/register", data=body, method="POST", headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=5) as response:
        assert json.loads(response.read())["added"] is True
    with urlopen(request, timeout=5) as response:
        assert json.loads(response.read())["added"] is False, "registering twice is idempotent"
    assert [w.name for w in live.workers] == ["seed", "late"]
    third = FakeWorker("third")
    host, port = third.url[len("http://"):].split(":")
    found = live.discover([host, "127.0.0.1", host], ports=(int(port), 1))
    assert len(found) == 1 and found[0].url == third.url, "one new worker, no duplicates, closed ports ignored"
    assert live.discover([host], ports=(int(port),)) == [], "already known"
    _, reply = call(url, "/v1/jobs", data=bundle([100.0, 200.0, 300.0]), method="POST")
    events = drain(url, reply["job_id"])
    assert events[-1]["type"] == "completed"
    # a discovered worker is named by reverse DNS (localhost here), a registered one by what it sent
    assert {e["farm_worker"] for e in events if e["type"] == "result"} <= {"seed", "late", found[0].name}
    late.close(); third.close()


def test_memory_caps_route_big_models_only_to_big_workers(farm):
    small, big = FakeWorker("small"), FakeWorker("big")
    url, live = farm([small, big], batch_size=1)
    live.workers[0].max_gib = 0.0   # "small" cannot take any real mesh
    live.workers[1].max_gib = 100.0
    _, reply = call(url, "/v1/jobs", data=bundle([100.0, 200.0, 300.0]), method="POST")
    events = drain(url, reply["job_id"])
    assert events[-1]["type"] == "completed"
    assert {e["farm_worker"] for e in events if e["type"] == "result"} == {"big"}
    assert blab_farm.parse_worker("http://h:1=mbp@48", None).max_gib == 48.0
    assert blab_farm.parse_worker("http://h:1@8", None).max_gib == 8.0 and blab_farm.parse_worker("http://h:1=x", None).max_gib is None
    mesh = b"$MeshFormat\n2.2 0 8\n$EndMeshFormat\n$Elements\n3\n1 2 2 1 1 1 2 3\n2 2 2 1 1 2 3 4\n3 15 2 1 1 1\n$EndElements\n"
    assert blab_farm.estimate_gib({"assets/x.msh": mesh}) == pytest.approx(4 * 1 * 1 * 8 / 2 ** 30)


def test_idle_fast_worker_duplicates_a_straggler_batch(farm):
    fast, slow = FakeWorker("fast", delay=0.005), FakeWorker("slow", delay=0.6)
    url, live = farm([fast, slow], batch_size=2)
    frequencies = [100.0, 200.0, 300.0, 400.0, 500.0, 600.0]
    t0 = time.time()
    _, reply = call(url, "/v1/jobs", data=bundle(frequencies), method="POST")
    events = drain(url, reply["job_id"], timeout=30)
    assert events[-1]["type"] == "completed"
    assert sorted(e["result"]["freq_hz"] for e in events if e["type"] == "result") == frequencies
    timeline = live.jobs[reply["job_id"]].summary()["timeline"]
    assert any(t.get("speculated") for t in timeline), "the fast worker should have duplicated the slow worker's batch"
    assert time.time() - t0 < 0.6 * 2 * 3 + 5, "the job must not wait for the straggler"
