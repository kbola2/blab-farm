"""``blab-farm worker``: run the stock Boundary Lab server, keep the machine awake, register with the farm."""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.request import Request, urlopen

SERVICE_LABEL = "com.blab-farm.worker"  # one service per port: label gets "-<port>" appended


def service_label(port):
    return f"{SERVICE_LABEL}-{port}"


def total_ram_gib():
    try:
        if sys.platform == "darwin":
            return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"])) / 2 ** 30
        if sys.platform.startswith("linux"):
            for line in open("/proc/meminfo"):
                if line.startswith("MemTotal"):
                    return int(line.split()[1]) * 1024 / 2 ** 30
        if sys.platform == "win32":
            import ctypes
            class Status(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = Status(); st.dwLength = ctypes.sizeof(st); ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return st.ullTotalPhys / 2 ** 30
    except Exception:
        pass
    return None


def computer_name():
    if sys.platform == "darwin":
        try:
            return subprocess.check_output(["scutil", "--get", "ComputerName"], text=True).strip()
        except Exception:
            pass
    return platform.node().split(".")[0] or "worker"


def local_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def blab_command(spec):
    """How to run Boundary Lab's CLI: a 'blab' executable, or a python that has blab installed."""
    if spec:
        path = Path(spec).expanduser()
        if path.name.startswith("python"):
            return [str(path), "-m", "blab.cli"]
        return [str(path)]
    found = shutil.which("blab")
    if found:
        return [found]
    for candidate in ("~/Development/boundary-lab/.venv/bin/blab", "~/boundary-lab/.venv/bin/blab"):
        path = Path(candidate).expanduser()
        if path.exists():
            return [str(path)]
    raise SystemExit("Boundary Lab's 'blab' command was not found. Install Boundary Lab first, then pass --blab <path to blab or python>.")


def register_loop(farm_url, body, interval=60.0):
    data = json.dumps(body).encode()
    while True:
        try:
            request = Request(farm_url.rstrip("/") + "/v1/farm/register", data=data, method="POST",
                              headers={"Content-Type": "application/json"})
            with urlopen(request, timeout=5) as response:
                reply = json.loads(response.read())
                if reply.get("added"):
                    print(f"registered with farm {farm_url} as {body['name']}", flush=True)
        except Exception:
            pass
        time.sleep(interval)


def run_worker(args):
    name = args.name or computer_name()
    max_gib = args.max_gib if args.max_gib is not None else (round(0.7 * total_ram_gib(), 1) if total_ram_gib() else None)
    url = f"http://{local_ip()}:{args.port}"
    Path(args.root).expanduser().mkdir(parents=True, exist_ok=True)
    cmd = blab_command(args.blab) + ["server", "--root", str(Path(args.root).expanduser()), "--mode", "private-network",
                                    "--host", "0.0.0.0", "--port", str(args.port), "--backend", args.backend]
    if args.julia:
        cmd += ["--julia-executable", args.julia]
    if sys.platform == "darwin" and not args.no_caffeinate and shutil.which("caffeinate"):
        cmd = ["caffeinate", "-is"] + cmd
    print(f"worker ready: {url}  name={name}  backend={args.backend}  max_gib={max_gib}", flush=True)
    if args.farm:
        threading.Thread(target=register_loop, args=(args.farm, {"url": url, "name": name, "max_gib": max_gib}), daemon=True).start()
    else:
        print("no --farm given: the coordinator will still find this worker by LAN scan on its port", flush=True)
    env = dict(os.environ)
    if args.julia:
        env["BLAB_JULIA_EXE"] = args.julia
    return subprocess.call(cmd, env=env)


def _worker_args(args):
    out = ["worker", "--port", str(args.port), "--backend", args.backend]
    if args.farm: out += ["--farm", args.farm]
    if args.name: out += ["--name", args.name]
    if args.max_gib is not None: out += ["--max-gib", str(args.max_gib)]
    if args.blab: out += ["--blab", args.blab]
    if args.julia: out += ["--julia", args.julia]
    if args.no_caffeinate: out += ["--no-caffeinate"]
    return out


def install_service(args):
    label = service_label(args.port)
    exe = shutil.which("blab-farm") or f"{sys.executable} -m blab_farm.cli"
    program = exe.split() + _worker_args(args)
    log_dir = Path.home() / ("Library/Logs" if sys.platform == "darwin" else ".blab-farm")
    log_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
        plist.parent.mkdir(parents=True, exist_ok=True)
        items = "".join(f"<string>{a}</string>" for a in program)
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>{items}</array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string><key>HOME</key><string>{Path.home()}</string></dict>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>15</integer>
  <key>StandardOutPath</key><string>{log_dir}/blab-farm-worker-{args.port}.log</string>
  <key>StandardErrorPath</key><string>{log_dir}/blab-farm-worker-{args.port}.log</string>
</dict></plist>
""")
        uid = os.getuid()
        subprocess.call(["launchctl", "bootout", f"gui/{uid}/{label}"], stderr=subprocess.DEVNULL)
        subprocess.check_call(["launchctl", "bootstrap", f"gui/{uid}", str(plist)])
        subprocess.check_call(["launchctl", "kickstart", "-k", f"gui/{uid}/{label}"])
        print(f"installed launchd agent {label}; log: {log_dir}/blab-farm-worker-{args.port}.log")
        print("For an unattended Mac also enable automatic login (System Settings > Users & Groups) and prevent sleep in Energy settings.")
    elif sys.platform.startswith("linux"):
        unit = Path.home() / ".config/systemd/user" / f"blab-farm-worker-{args.port}.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(f"""[Unit]
Description=blab-farm worker (Boundary Lab server)
After=network-online.target

[Service]
ExecStart={' '.join(program)}
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
""")
        subprocess.check_call(["systemctl", "--user", "daemon-reload"])
        subprocess.check_call(["systemctl", "--user", "enable", "--now", f"blab-farm-worker-{args.port}.service"])
        subprocess.call(["loginctl", "enable-linger", os.environ.get("USER", "")])
        print(f"installed systemd user service blab-farm-worker-{args.port} (lingering enabled so it runs without a login session)")
    elif sys.platform == "win32":
        task = " ".join(f'"{a}"' if " " in a else a for a in program)
        subprocess.check_call(["schtasks", "/Create", "/F", "/SC", "ONLOGON", "/TN", f"blab-farm worker {args.port}", "/TR", task])
        subprocess.check_call(["schtasks", "/Run", "/TN", f"blab-farm worker {args.port}"])
        print("installed a Task Scheduler task 'blab-farm worker' that runs at logon (untested path: please report issues)")
    else:
        raise SystemExit(f"no service installer for {sys.platform}")
    return 0


def uninstall_service(port):
    label = service_label(port)
    if sys.platform == "darwin":
        plist = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
        subprocess.call(["launchctl", "bootout", f"gui/{os.getuid()}/{label}"], stderr=subprocess.DEVNULL)
        plist.unlink(missing_ok=True)
    elif sys.platform.startswith("linux"):
        subprocess.call(["systemctl", "--user", "disable", "--now", f"blab-farm-worker-{port}.service"])
        (Path.home() / ".config/systemd/user" / f"blab-farm-worker-{port}.service").unlink(missing_ok=True)
    elif sys.platform == "win32":
        subprocess.call(["schtasks", "/Delete", "/F", "/TN", f"blab-farm worker {port}"])
    print(f"worker service for port {port} removed")
    return 0
