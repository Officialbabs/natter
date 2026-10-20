#!/usr/bin/python3
"""Natter Den, ZapZap and Whatsie side by side: the memory and CPU of every process each app runs.

Each app is found by its systemd scopes: ZapZap's Flatpak runs its web engine in extra
scopes, which count too, and Whatsie runs as a Snap or a Flatpak. Memory is PSS from
/proc/<pid>/smaps_rollup plus what is swapped out; CPU is user and system time over the
measuring window.

  python3 tools/compare-memory.py              30 s, every app that's running
  python3 tools/compare-memory.py --seconds 60
"""

from __future__ import annotations

import argparse
import glob
import os
import time

SLICE = f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service/app.slice"
APPS = {
    "ZapZap": ["app-flatpak-com.rtosta.zapzap-*.scope"],
    "Whatsie": ["snap.whatsie.*.scope", "app-flatpak-com.ktechpit.whatsie-*.scope"],
    "Natter Den": ["app-*io.github.Officialbabs.Natter-*.scope"],
}
HZ = os.sysconf("SC_CLK_TCK")


def scopes(patterns: list[str]) -> list[str]:
    return [d for pattern in patterns for d in glob.glob(f"{SLICE}/{pattern}") if read(f"{d}/cgroup.procs").split()]


def read(path: str) -> str:
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
        return ""


def pids(dirs: list[str]) -> list[int]:
    return [int(p) for d in dirs for p in read(f"{d}/cgroup.procs").split()]


def memory(pid: int) -> dict[str, int]:
    """PSS, its private part and swap, in MB."""
    found = {}
    for line in read(f"/proc/{pid}/smaps_rollup").splitlines():
        key, *rest = line.split()
        if key in ("Pss:", "Pss_Anon:", "Swap:"):
            found[key[:-1]] = int(rest[0]) // 1024
    return found


def cpu_ticks(pid: int) -> int:
    fields = read(f"/proc/{pid}/stat").rsplit(")", 1)[-1].split()
    return int(fields[11]) + int(fields[12]) if len(fields) > 12 else 0


def name(pid: int) -> str:
    return read(f"/proc/{pid}/comm").strip() or "?"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seconds", type=int, default=30, help="how long to measure CPU over")
    seconds = parser.parse_args().seconds

    found = {app: scopes(patterns) for app, patterns in APPS.items()}
    before = {app: {p: cpu_ticks(p) for p in pids(dirs)} for app, dirs in found.items()}
    time.sleep(seconds)
    for app, dirs in found.items():
        processes = pids(dirs)
        if not processes:
            print(f"{app}: not running")
            continue
        per = {p: memory(p) for p in processes}
        pss = sum(m.get("Pss", 0) for m in per.values())
        swap = sum(m.get("Swap", 0) for m in per.values())
        cpu = sum(cpu_ticks(p) - before[app].get(p, cpu_ticks(p)) for p in processes) / HZ / seconds * 100
        biggest = sorted(((m.get("Pss", 0), name(p)) for p, m in per.items()), reverse=True)[:3]
        print(f"{app}: {len(processes)} processes in {len(dirs)} scope(s)")
        print(f"  in memory (PSS) {pss} MB + swapped out {swap} MB = {pss + swap} MB")
        print(f"  CPU {cpu:.1f}% of one core over {seconds} s")
        print("  biggest: " + ", ".join(f"{n} {m} MB" for m, n in biggest if m))


if __name__ == "__main__":
    main()
