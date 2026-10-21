"""Command line: ``blab-farm serve`` (coordinator), ``blab-farm worker`` (each machine), ``blab-farm status``."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from urllib.request import urlopen

from . import __version__
from .farm import Farm, Worker, create_http_server, local_ip, parse_worker
from .worker import install_service, run_worker, uninstall_service

LOGGER = logging.getLogger("blab_farm")


def cmd_serve(args):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    worker_token = os.environ.get(args.worker_token_env) or None
    workers = [parse_worker(spec, worker_token) for spec in args.worker]
    farm = Farm(workers, args.root, batch_size=args.batch, ordered=args.ordered)
    farm.worker_token = worker_token
    farm.discovered_max_gib = args.discovered_max_gib
    server = create_http_server(farm, host=args.host, port=args.port, token=os.environ.get(args.token_env) or None)
    if not args.no_discover:
        ports = tuple(int(p) for p in args.discover_ports.split(",") if p)
        own = local_ip()
        LOGGER.info("scanning %s.0/24 ports %s every %.0fs; workers may also register themselves",
                    own.rsplit(".", 1)[0] if own else "?", list(ports), args.discover_interval)
        farm.start_discovery(args.discover_interval, ports)
        time.sleep(2.5)
    for info in farm.capabilities()["farm"]["workers"]:
        LOGGER.info("worker %-14s %s  state=%s  backends=%s", info["name"], info["url"], info["state"], info["backend_ids"])
    LOGGER.info("farm listening on http://%s:%d  (set Boundary Lab's Solver to Boundary Lab Server at this address; "
                "open it in a browser for the dashboard)", own or args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        farm.closing = True
        server.server_close()


def cmd_worker(args):
    if args.uninstall_service:
        return uninstall_service(args.port)
    if args.install_service:
        return install_service(args)
    return run_worker(args)


def cmd_status(args):
    with urlopen(args.farm.rstrip("/") + "/v1/farm/status", timeout=5) as response:
        data = json.loads(response.read())
    if args.json:
        print(json.dumps(data, indent=1))
        return
    print("workers:")
    for w in data["workers"]:
        state = "down" if w["down"] else ("solving" if w["busy"] else "idle")
        cap = f"  cap {w['max_gib']:.0f} GiB" if w.get("max_gib") else ""
        rate = f"  {w['seconds_per_frequency']:.1f} s/freq" if w.get("seconds_per_frequency") else ""
        print(f"  {w['name']:<20} {w['url']:<28} {state:<8} solved {w['solved_total']}{rate}{cap}")
    jobs = data.get("jobs") or ([data["job"]] if data.get("job") else [])
    if not jobs:
        print("jobs: none")
    for j in jobs:
        eta = f", about {j['eta_s']:.0f} s left" if j.get("eta_s") else ""
        print(f"job {j['job_id'][:8]}: {j['state']}, {j['done']}/{j['frequency_count']} frequencies, "
              f"{j['elapsed_s']:.0f} s{eta}, about {j.get('gib_estimate', 0):.1f} GiB, eligible: {', '.join(j.get('eligible_workers', []))}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="blab-farm", description="Schedule Boundary Lab solves across several machines.")
    parser.add_argument("--version", action="version", version=f"blab-farm {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the coordinator: one Boundary Lab Server address backed by many workers")
    p.add_argument("--worker", action="append", default=[], metavar="URL[=name][@max_gib[/host_gib]]",
                   help="a Boundary Lab server to use (repeat); workers can also join by themselves")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--root", default=os.path.expanduser("~/.blab-farm/jobs"), help="where job journals are kept")
    p.add_argument("--batch", type=int, default=None, help="frequencies per batch (default adaptive, at most 8)")
    p.add_argument("--ordered", action="store_true", help="stream results in request order (buffers)")
    p.add_argument("--token-env", default="BLAB_FARM_TOKEN", help="env var with the key clients must send (optional)")
    p.add_argument("--worker-token-env", default="BLAB_SERVER_TOKEN", help="env var with the key sent to workers (optional)")
    p.add_argument("--no-discover", action="store_true", help="do not scan the LAN for Boundary Lab servers")
    p.add_argument("--discover-interval", type=float, default=30.0)
    p.add_argument("--discover-ports", default="8765")
    p.add_argument("--discovered-max-gib", type=float, default=None,
                   help="memory cap for workers that join by discovery and do not state their own")
    p.set_defaults(func=cmd_serve)

    w = sub.add_parser("worker", help="run this machine as a worker (wraps the stock 'blab server')")
    w.add_argument("--farm", default=os.environ.get("BLAB_FARM_URL", ""), help="coordinator address to register with, e.g. http://192.168.1.10:8766")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--backend", default="auto", help="auto, cpu, metal, cuda or rocm (passed to blab server)")
    w.add_argument("--name", default=None, help="name shown on the dashboard (default: this computer's name)")
    w.add_argument("--max-gib", type=float, default=None,
                   help="largest job this worker takes, in GiB as the farm estimates it (default: the machine budget; half of it for a CPU worker)")
    w.add_argument("--host-gib", type=float, default=None,
                   help="memory budget of this whole machine, shared by all its workers (default: 70%% of RAM)")
    w.add_argument("--blab", default=os.environ.get("BLAB_CLI", ""), help="path to the 'blab' command or a python with blab installed")
    w.add_argument("--julia", default=os.environ.get("BLAB_JULIA_EXE", ""), help="Julia executable for the engine (optional)")
    w.add_argument("--root", default=os.path.expanduser("~/.blab-farm/server-jobs"))
    w.add_argument("--no-caffeinate", action="store_true", help="do not keep the machine awake while serving")
    w.add_argument("--install-service", action="store_true", help="install as a login service (launchd / systemd user / Task Scheduler) and start it")
    w.add_argument("--uninstall-service", action="store_true", help="remove the service for this --port")
    w.set_defaults(func=cmd_worker)

    s = sub.add_parser("status", help="show the farm's workers and jobs")
    s.add_argument("--farm", default=os.environ.get("BLAB_FARM_URL", "http://127.0.0.1:8766"))
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
