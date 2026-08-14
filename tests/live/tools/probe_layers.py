#!/usr/bin/env python3
"""Find out *which layer* stalled when the live suite says musica is down.

The suite's session fixture aborts with "musica is not answering at
http://localhost:8092 after 90s". That message names musica because musica
is the only thing it probes — but the host's view of musica is the end of a
chain, and every link in it can produce exactly the same symptom:

    pytest -> localhost:8092 -> com.docker.backend (Docker Desktop's
    userspace port forwarder) -> the VM -> the bridge network -> uvicorn

A single py-spy dump inside the container cannot tell those apart. On
2026-08-13 it actively misled: it showed every app thread idle, container
CPU at 0.20%, and no Python frame anywhere near beets, slskd or the DB —
all of which is consistent with "the app is fine" *and* with "the app is
wedged somewhere py-spy can't see", and gives no way to choose.

This probes all the layers at once, at 1Hz, each on its own thread so a
stall in one never delays another:

  host   -> musica /api/system/status   the exact probe the fixture uses
  host   -> musica /                    same app, no health checks, no DB
  host   -> navidrome, slskd            unrelated containers, same forwarder
  cont   -> 127.0.0.1:8000              the app seen from inside the netns
  docker -> `docker inspect` latency    the daemon path, not the port path
  host   -> CPU + socket census         who is actually burning the core

The discrimination that matters:

  - musica slow, musica / fast              -> the app (health checks, DB)
  - musica slow, in-container fast          -> NOT the app; below it
  - navidrome and slskd slow in lockstep    -> the shared host-side forwarder
  - `docker inspect` slow too               -> Docker Desktop itself, since
                                               the CLI never touches the
                                               published-port path

Usage:

    python3 tests/live/tools/probe_layers.py record probe.jsonl   # ctrl-c to stop
    python3 tests/live/tools/probe_layers.py summarize probe.jsonl

Run `record` in one terminal for the whole live suite, then `summarize`
afterwards. It is cheap: four HTTP GETs and a socket count per second.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

#: Published ports from docker-compose.yml. All three share one host-side
#: forwarder, which is exactly why probing all three is informative: an app
#: bug cannot slow down a container it has nothing to do with.
HOST_TARGETS: tuple[tuple[str, str], ...] = (
    ("musica_status", "http://localhost:8092/api/system/status"),
    ("musica_root", "http://localhost:8092/"),
    ("navidrome", "http://localhost:8090/ping"),
    ("slskd", "http://localhost:8091/health"),
)

#: Deliberately far above the fixture's 5s. The question is never "did it
#: exceed 5s" — we already know it did — but "by how much, and did the
#: neighbours exceed it by the same amount at the same second".
PROBE_TIMEOUT = 20.0

#: The in-container half. Written to /app/data because that is bind-mounted
#: to ./app_data on the host, so the record stays readable even while
#: `docker exec` is too congested to return — which is the exact window we
#: care about. (A 2026-08-13 `docker compose exec py-spy dump` took 2.5
#: minutes to come back.)
CONTAINER_PROBE = '''
import json, socket, threading, time, urllib.request, os
fh = open("/app/data/layer_probe_container.jsonl", "a", buffering=1)
lock = threading.Lock()
# Proxies off explicitly: the container has http_proxy pointing at
# musica-proxy, and a probe that silently routed through a SOCKS5 upstream
# would be measuring the proxy rather than the target.
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def emit(**f):
    f["t"] = time.time(); f["ts"] = time.strftime("%H:%M:%S", time.localtime(f["t"]))
    with lock: fh.write(json.dumps(f) + "\\n")
def probe(name, url):
    host = url.split("://", 1)[1].split("/", 1)[0]
    hostname, _, port = host.partition(":")
    while True:
        rec = {"probe": name, "src": "cont"}
        s = time.monotonic()
        try:
            opener.open(url, timeout=20).read(200); rec["code"] = 200
        except Exception as e:
            rec["code"] = 0; rec["err"] = "%s: %s" % (type(e).__name__, e)
        rec["total_ms"] = round((time.monotonic() - s) * 1000, 1)
        emit(**rec); time.sleep(1.0)
def proc1():
    # Thread states of the app process, read from /proc because this is a
    # separate process. State D (uninterruptible I/O) is the one py-spy
    # cannot show you: it prints a Python frame, not the fact that the
    # thread is wedged in a read that will never return.
    while True:
        rec = {"probe": "proc1", "src": "cont"}
        try:
            st = {}
            for tid in os.listdir("/proc/1/task"):
                try:
                    with open("/proc/1/task/%s/stat" % tid) as f:
                        s = f.read().rsplit(")", 1)[1].split()[0]
                    st[s] = st.get(s, 0) + 1
                except OSError: pass
            rec["thread_states"] = st; rec["threads"] = sum(st.values())
        except Exception as e:
            rec["err"] = "%s: %s" % (type(e).__name__, e)
        emit(**rec); time.sleep(5.0)
for n, u in (("in_status", "http://127.0.0.1:8000/api/system/status"),
             ("in_root", "http://127.0.0.1:8000/")):
    threading.Thread(target=probe, args=(n, u), daemon=True).start()
threading.Thread(target=proc1, daemon=True).start()
while True: time.sleep(3600)
'''


class Recorder:
    def __init__(self, path: Path) -> None:
        self.fh = path.open("a", buffering=1)
        self.lock = threading.Lock()
        self.stop = threading.Event()

    def emit(self, **fields) -> None:
        fields["t"] = time.time()
        fields["ts"] = time.strftime("%H:%M:%S", time.localtime(fields["t"]))
        with self.lock:
            self.fh.write(json.dumps(fields) + "\n")

    # -- probes ------------------------------------------------------------

    def http(self, name: str, url: str) -> None:
        host = url.split("://", 1)[1].split("/", 1)[0]
        hostname, _, port = host.partition(":")
        port_num = int(port or 80)
        while not self.stop.is_set():
            rec: dict = {"probe": name, "src": "host"}
            # Connect time separately from total: a dead container refuses
            # the connection instantly, a congested forwarder accepts it and
            # then makes you wait. Same failed request, opposite causes.
            started = time.monotonic()
            try:
                socket.create_connection((hostname, port_num), timeout=PROBE_TIMEOUT).close()
                rec["connect_ms"] = round((time.monotonic() - started) * 1000, 1)
            except OSError as e:
                rec["connect_ms"] = round((time.monotonic() - started) * 1000, 1)
                rec["connect_err"] = f"{type(e).__name__}: {e}"
            started = time.monotonic()
            try:
                with urllib.request.urlopen(url, timeout=PROBE_TIMEOUT) as resp:
                    resp.read(200)
                    rec["code"] = resp.status
            except urllib.error.HTTPError as e:
                rec["code"] = e.code
            except Exception as e:  # noqa: BLE001 - every failure mode is data
                rec["code"] = 0
                rec["err"] = f"{type(e).__name__}: {e}"
            rec["total_ms"] = round((time.monotonic() - started) * 1000, 1)
            self.emit(**rec)
            self.stop.wait(1.0)

    def docker_cli(self) -> None:
        """Latency of the docker CLI is a first-class signal, not overhead.

        The CLI talks to the daemon over a unix socket; it never traverses a
        published port. So a slow `docker inspect` *and* a slow
        localhost:8092 at the same second rules out anything inside the
        container and points at Docker Desktop's host-side process.
        """
        while not self.stop.is_set():
            started = time.monotonic()
            rec: dict = {"probe": "docker_cli", "src": "host"}
            try:
                out = subprocess.run(
                    ["docker", "inspect", "musica", "--format",
                     "{{.State.Health.Status}}|{{.RestartCount}}|{{.State.OOMKilled}}"],
                    capture_output=True, text=True, timeout=120, check=False,
                )
                rec["out"] = out.stdout.strip()
            except subprocess.TimeoutExpired:
                rec["out"] = "TIMEOUT"
            rec["total_ms"] = round((time.monotonic() - started) * 1000, 1)
            self.emit(**rec)
            self.stop.wait(5.0)

    def census(self) -> None:
        """CPU of the usual suspects, plus a socket census by remote port.

        The socket census is what turns "something is slow" into a named
        cause: Soulseek peer traffic shows up as thousands of established
        connections to peer ports, all of them proxied in userspace by
        com.docker.backend, which is also what forwards localhost:8092.
        """
        while not self.stop.is_set():
            rec: dict = {"probe": "census", "src": "host"}
            try:
                ps = subprocess.run(
                    ["ps", "-Ao", "%cpu,comm"], capture_output=True, text=True, check=False
                ).stdout.splitlines()
                cpu: dict[str, float] = {}
                for line in ps[1:]:
                    pct, _, comm = line.strip().partition(" ")
                    try:
                        val = float(pct)
                    except ValueError:
                        continue
                    for key in ("com.docker.backend", "Virtualization.VirtualMachine"):
                        if key in comm:
                            cpu[key] = max(cpu.get(key, 0.0), val)
                    if val > 40.0:
                        cpu.setdefault(comm.strip()[-32:], val)
                rec["cpu_pct"] = cpu

                net = subprocess.run(
                    ["netstat", "-an", "-p", "tcp"], capture_output=True, text=True, check=False
                ).stdout.splitlines()
                established = 0
                by_remote_port: dict[str, int] = {}
                for line in net:
                    parts = line.split()
                    if len(parts) < 6 or parts[5] != "ESTABLISHED":
                        continue
                    established += 1
                    by_remote_port[parts[4].rsplit(".", 1)[-1]] = (
                        by_remote_port.get(parts[4].rsplit(".", 1)[-1], 0) + 1
                    )
                rec["established"] = established
                rec["top_remote_ports"] = dict(
                    sorted(by_remote_port.items(), key=lambda kv: -kv[1])[:6]
                )
            except Exception as e:  # noqa: BLE001
                rec["err"] = f"{type(e).__name__}: {e}"
            self.emit(**rec)
            self.stop.wait(5.0)


def install_container_probe() -> bool:
    """Start the in-container half. Best effort — the host half is still
    worth having on its own if the container isn't reachable."""
    try:
        subprocess.run(
            ["docker", "exec", "-d", "musica", "python", "-c", CONTAINER_PROBE],
            capture_output=True, timeout=60, check=True,
        )
    except (subprocess.SubprocessError, OSError) as e:
        print(f"[probe] could not start in-container probe: {e}", file=sys.stderr)
        return False
    return True


def record(path: Path) -> None:
    rec = Recorder(path)
    in_container = install_container_probe()
    rec.emit(probe="start", in_container=in_container)
    threads = [threading.Thread(target=rec.http, args=t, daemon=True) for t in HOST_TARGETS]
    threads += [
        threading.Thread(target=rec.docker_cli, daemon=True),
        threading.Thread(target=rec.census, daemon=True),
    ]
    for t in threads:
        t.start()
    print(f"[probe] recording to {path} (ctrl-c to stop)"
          f"{'' if in_container else ' — host vantage only'}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        rec.stop.set()
        rec.emit(probe="stop")
        print("\n[probe] stopped")


def summarize(path: Path, slow_ms: float = 1000.0) -> None:
    records = []
    for source in (path, Path("app_data/layer_probe_container.jsonl")):
        if not source.exists():
            continue
        for line in source.read_text().splitlines():
            try:
                records.append(json.loads(line))
            except ValueError:
                continue
    if not records:
        raise SystemExit(f"no probe records in {path}")
    records.sort(key=lambda r: r.get("t", 0))
    # The container's clock is UTC and the host's is local, so the two halves
    # record different `ts` strings for the same instant. Both record the
    # same epoch `t`, so re-render every label from that — otherwise the two
    # vantages appear hours apart and cannot be read side by side, which is
    # the entire point of collecting them.
    for r in records:
        if "t" in r:
            r["ts"] = time.strftime("%H:%M:%S", time.localtime(r["t"]))

    print(f"window {records[0].get('ts')} .. {records[-1].get('ts')}  "
          f"({len(records)} samples)\n")

    print("=== per-probe latency ===")
    stats: dict[str, list[float]] = {}
    codes: dict[str, dict] = {}
    for r in records:
        if "total_ms" not in r:
            continue
        # `src` is inferred when absent so that records written by an older
        # or ad-hoc prober still land in the right bucket. Getting this wrong
        # is not cosmetic: an unrecognised key means the verdict below sees no
        # samples for a probe and would otherwise announce "nothing stalled"
        # over a file full of stalls.
        src = r.get("src") or ("cont" if str(r.get("probe", "")).startswith("in_") else "host")
        p = f"{src}/{r.get('probe')}"
        stats.setdefault(p, []).append(r["total_ms"])
        if "code" in r:
            codes.setdefault(p, {})[r["code"]] = codes.setdefault(p, {}).get(r["code"], 0) + 1
    for p, vals in sorted(stats.items()):
        vals.sort()
        print(f"  {p:24s} n={len(vals):5d} med={vals[len(vals) // 2]:8.1f}ms "
              f"p95={vals[int(len(vals) * 0.95)]:9.1f}ms max={vals[-1]:9.1f}ms "
              f"codes={codes.get(p, {})}")

    print(f"\n=== stalls (>{slow_ms:.0f}ms or non-200) ===")
    stalls = [
        r for r in records
        if (r.get("total_ms") or 0) > slow_ms or r.get("code") not in (None, 200)
    ]
    for r in stalls[:80]:
        print(f"  {r.get('ts')} [{r.get('src', '?')}] {str(r.get('probe')):16s} "
              f"{r.get('total_ms', 0):9.1f}ms code={r.get('code', '-')} "
              f"{str(r.get('err', ''))[:60]}")
    if len(stalls) > 80:
        print(f"  ... and {len(stalls) - 80} more")

    # -- the verdict -------------------------------------------------------
    # A missing probe is reported as missing, never as 0ms. Absent evidence
    # and evidence of health look identical once they are both a number, and
    # a diagnostic that confidently says "fine" when it simply has no data is
    # worse than no diagnostic at all.
    def worst(*names: str) -> float | None:
        vals = [v for n in names for v in stats.get(n, [])]
        return max(vals) if vals else None

    def show(label: str, value: float | None) -> None:
        rendered = "  no samples" if value is None else f"{value:9.1f}ms"
        print(f"  {label:34s}{rendered}")

    host_app = worst("host/musica_status", "host/musica_root")
    cont_app = worst("cont/in_status", "cont/in_root")
    neighbours = worst("host/navidrome", "host/slskd")
    cli = worst("host/docker_cli")

    print("\n=== verdict ===")
    show("worst musica, from the host:", host_app)
    show("worst musica, inside the netns:", cont_app)
    show("worst navidrome/slskd, from host:", neighbours)
    show("worst `docker inspect`:", cli)
    if host_app is None:
        print("  -> no host-side samples; cannot say anything. Was `record` running?")
    elif host_app < slow_ms:
        print("  -> nothing stalled in this window.")
    elif cont_app is None:
        print("  -> musica was slow from the host, but the in-container probe never\n"
              "     ran, so the app cannot be cleared or blamed. Re-run `record`\n"
              "     with the container reachable.")
    elif cont_app > slow_ms:
        print("  -> the APP was slow: it is slow from inside its own netns too.")
    elif (neighbours or 0) > slow_ms or (cli or 0) > slow_ms:
        print("  -> NOT the app. Unrelated containers and/or the docker CLI stalled\n"
              "     by the same amount at the same time, so the shared host-side\n"
              "     path stalled: com.docker.backend. Check `cpu_pct` and\n"
              "     `top_remote_ports` in the census samples for the traffic\n"
              "     saturating it.")
    else:
        print("  -> musica alone was unreachable from the host while healthy inside.\n"
              "     Suspect its published-port mapping specifically.")

    census = [r for r in records if r.get("probe") == "census"]
    if census:
        peak = max(census, key=lambda r: r.get("cpu_pct", {}).get("com.docker.backend", 0))
        print(f"\n  peak com.docker.backend: "
              f"{peak.get('cpu_pct', {}).get('com.docker.backend', 0):.1f}% CPU at "
              f"{peak.get('ts')}, {peak.get('established')} established sockets, "
              f"top remote ports {peak.get('top_remote_ports')}")


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[1] not in ("record", "summarize"):
        raise SystemExit(__doc__)
    if sys.argv[1] == "record":
        record(Path(sys.argv[2]))
    else:
        summarize(Path(sys.argv[2]))
