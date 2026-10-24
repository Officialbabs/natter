"""tools/report.sh, the problem report a person runs and sends back: run whole in a scratch home,
with stand-ins for journalctl, coredumpctl, systemctl, gdbus, flatpak, pacman, nvidia-smi and the
rest that answer from canned replies, and a made-up /proc, /sys and cgroup tree, so nothing reads
this computer's logs, nothing reaches the network, and no real Natter Den is looked at."""

import json
import os
import re
import sys
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "report.sh"
SHELLS = [shell for shell in ("dash", "bash") if shutil.which(shell)] or ["sh"]
# What the script needs from the system; the rest are stand-ins below.
TOOLS = ("sh", "awk", "sed", "grep", "cat", "head", "tail", "wc", "tr", "cut", "sort", "date", "mktemp", "rm",
         "mkdir", "chmod", "mv", "id", "uname", "du", "find", "sleep", "kill", "getconf", "timeout", "readlink", "ls")
AWKS = {name: shutil.which(name) for name in ("mawk", "gawk", "nawk", "original-awk")}
AWKS = {name: path for name, path in AWKS.items() if path}
if shutil.which("busybox"):
    AWKS["busybox"] = shutil.which("busybox")

USER = "friendly"
HOST = "friendbox"
SSID = "HomeWifi-5G"
UNIT = "app-io.github.Officialbabs.Natter-4242.scope"
CGROUP = "/user.slice/user-1000.slice/user@1000.service/app.slice/" + UNIT
SECRETS = ("Secret Person", "hello there", "secret console", "abcdef123456", "447700900123", "friend@example.com",
           "192.168.1.20", "3c:22:fb:12:34:56", "SECRET-SESSION-DATA", "Chat With Secret", "org.other.App",
           "Firefox thing", "Message words", HOST, SSID, "7700 900123", "2a02:c7f", "otherapp", "Portal words",
           "Alice Smith", "Isolated Web Co", "signal-desktop", "keepassxc", "thunderbird", "SECRET_TOKEN",
           "disk-uuid-secret", "0123456789abcdef0123456789abcdef", "storage.db")
BOOT_IDS = ("0123456789abcdef0123456789abcdef", "1123456789abcdef0123456789abcdef", "2123456789abcdef0123456789abcdef",
            "3123456789abcdef0123456789abcdef")


def kernel_line(stamp, text):
    return f"{stamp}.000001+02:00 {HOST} kernel: {text}\n"


# The boot before: a freeze, with GPU errors from Natter's web process and from other apps.
PREVIOUS_KERNEL = "".join(kernel_line(f"2026-10-22T23:{58 + i // 60:02d}:{i % 60:02d}", text) for i, text in enumerate([
    "NVRM: Xid (PCI:0000:01:00): 13, pid=2345, name=Isolated Web Co, Graphics SM Warp Exception",
    "NVRM: Xid (PCI:0000:01:00): 109, pid=4250, name=WebKitWebProces, Ch 00000010, errorString CTX SWITCH TIMEOUT",
    "amdgpu 0000:c4:00.0: [drm:amdgpu_job_timedout [amdgpu]] *ERROR* ring gfx_0.0.0 timeout, signaled seq=1234, emitted seq=1236",
    "amdgpu 0000:c4:00.0: amdgpu: Process information: process signal-desktop pid 3456 thread signal-de:cs0 pid 3460",
    "amdgpu 0000:c4:00.0: amdgpu: Process WebKitWebProces pid 4250 thread SkiaGPU pid 4270",
    "amdgpu 0000:c4:00.0: amdgpu: GPU reset begin!",
    "[drm:drm_atomic_helper_wait_for_flip_done] *ERROR* [CRTC:79:crtc-0] flip_done timed out",
    "INFO: task keepassxc:5555 blocked for more than 122 seconds.",
    "task:keepassxc       state:D stack:0     pid:5555  tgid:5555  ppid:1      flags:0x00004006",
    "watchdog: BUG: soft lockup - CPU#3 stuck for 26s! [Isolated Web Co:2345]",
    "i915 0000:00:02.0: [drm] GPU HANG: ecode 12:1:85dffffb, in thunderbird [6666]",
    "i915 0000:00:02.0: [drm] thunderbird[6666] context reset due to GPU hang",
    "usb 1-2: new device",
    "INFO: task kworker/u64:3:123 blocked for more than 122 seconds.",
]))

# Stand-ins. Each logs how it was called; FAKE_PIDS gets the pid of each long-running one.
STUBS = {
    "journalctl": r"""#!/bin/sh
echo "journalctl $*" >> "$FAKE_LOG"
case " $* " in
    *" -F _SYSTEMD_USER_UNIT "*) printf '%s\n' "$FAKE_UNIT" app-org.kde.konsole@abc.service; exit 0 ;;
    *" -F USER_UNIT "*) printf '%s\n' "$FAKE_UNIT"; exit 0 ;;
    *" -f "*)
        echo $$ >> "$FAKE_PIDS"
        case " $* " in
            *" -k "*)
                echo "2026-10-23T23:59:58.000001+02:00 $FAKE_HOST kernel: Out of memory: Killed process 4250 (WebKitWebProces)"
                echo "2026-10-23T23:59:58.000003+02:00 $FAKE_HOST kernel: amdgpu 0000:c4:00.0: amdgpu: ring gfx_0.0.0 timeout, signaled seq=9, emitted seq=11"
                echo "2026-10-23T23:59:58.000004+02:00 $FAKE_HOST kernel: amdgpu 0000:c4:00.0: amdgpu: Process signal-desktop pid 3456 thread signal-de:cs0 pid 3460"
                echo "2026-10-23T23:59:58.000005+02:00 $FAKE_HOST kernel: usb 1-2: during the watch" ;;
            *) echo "2026-10-23T23:59:58.000002+02:00 $FAKE_HOST natter[4242]: natter: during the watch" ;;
        esac
        exec sleep 60 ;;
    *" -k -n 1 "*) echo "2026-10-23T08:00:00.000001+02:00 $FAKE_HOST kernel: Linux version" ;;
    *" --list-boots "*)
        echo "IDX BOOT ID                          FIRST ENTRY                  LAST ENTRY"
        if [ -z "${FAKE_ONE_BOOT-}" ]; then
            echo " -3 0123456789abcdef0123456789abcdef Mon 2026-10-20 08:00:01 CEST Mon 2026-10-20 22:00:00 CEST"
            echo " -2 1123456789abcdef0123456789abcdef Tue 2026-10-21 08:00:01 CEST Tue 2026-10-21 23:00:00 CEST"
            echo " -1 2123456789abcdef0123456789abcdef Wed 2026-10-22 08:00:01 CEST Wed 2026-10-22 23:59:13 CEST"
        fi
        echo "  0 3123456789abcdef0123456789abcdef Thu 2026-10-23 08:00:00 CEST Thu 2026-10-23 10:00:00 CEST" ;;
    *" -k -b -1 "*) cat "$FAKE_KERNEL_PREV" ;;
    *" -k -b 0 "*) cat "$FAKE_KERNEL" ;;
    *" -b -1 -n 80 "*)
        echo "2026-10-22T23:59:12.000001+02:00 $FAKE_HOST natter[4242]: natter: memory: fine"
        echo "2026-10-22T23:59:13.000001+02:00 $FAKE_HOST kernel: watchdog: BUG: soft lockup" ;;
    *" -b -2 -n 80 "*)
        echo "2026-10-21T22:59:59.000001+02:00 $FAKE_HOST systemd[1]: Reached target poweroff.target - System Power Off."
        echo "2026-10-21T23:00:00.000001+02:00 $FAKE_HOST systemd-journald[300]: Journal stopped" ;;
    *" -b -3 -n 80 "*)
        echo "2026-10-20T21:59:59.000001+02:00 $FAKE_HOST systemd-sleep[900]: Performing sleep operation 'suspend'..."
        echo "2026-10-20T22:00:00.000001+02:00 $FAKE_HOST kernel: PM: suspend entry (s2idle)" ;;
    *" -k "*) cat "$FAKE_KERNEL" ;;
    *" -t systemd-oomd "*) echo "2026-10-23T09:30:00.000001+02:00 $FAKE_HOST systemd-oomd[300]: Killed $FAKE_UNIT due to memory pressure (WebKitWebProces)" ;;
    *" -t rtkit-daemon "*) echo "2026-10-23T09:31:00.000001+02:00 $FAKE_HOST rtkit-daemon[400]: The canary thread is apparently starving. Taking action." ;;
    *" --user "*) cat "$FAKE_JOURNAL" ;;
esac
""",
    "coredumpctl": r"""#!/bin/sh
echo "coredumpctl $*" >> "$FAKE_LOG"
case " $* " in
    *" list "*)
        echo "Thu 2026-10-23 09:20:00 CEST 4250 1000 1000 SIGSEGV present /usr/lib/webkitgtk-6.0/WebKitWebProcess 12.3M"
        echo "Thu 2026-10-23 09:21:00 CEST 6000 1000 1000 SIGABRT present /usr/bin/gnome-calculator 1.2M"
        echo "Thu 2026-10-23 09:22:00 CEST 6100 1000 1000 SIGSEGV present /usr/bin/python3.13 45.6M"
        echo "Thu 2026-10-23 09:23:00 CEST 6200 1000 1000 SIGABRT present /usr/bin/python3.13 7.8M" ;;
    *" COREDUMP_PID=6100 "*)
        echo "           PID: 6100 (python3)"
        echo "  Command Line: /usr/bin/python3.13 -m natter.callwindow --socket /run/user/1000/x"
        echo " Control Group: /user.slice/user-1000.slice/user@1000.service/app.slice/$FAKE_UNIT" ;;
    *" COREDUMP_PID=6200 "*)
        echo "           PID: 6200 (python3)"
        echo "  Command Line: /usr/bin/python3.13 /opt/otherapp/main.py"
        echo " Control Group: /user.slice/user-1000.slice/user@1000.service/app.slice/app-otherapp.scope" ;;
    *" COREDUMP_COMM=WebKitWebProces "*)
        echo "           PID: 4250 (WebKitWebProces)"
        echo "        Signal: 11 (SEGV)"
        echo "     Timestamp: Thu 2026-10-23 09:20:00 CEST"
        echo "  Command Line: /usr/lib/webkitgtk-6.0/WebKitWebProcess 12 Chat With Secret"
        echo "    Executable: /usr/lib/webkitgtk-6.0/WebKitWebProcess"
        echo "      Hostname: $FAKE_HOST"
        echo "       Message: Process 4250 (WebKitWebProces) of user 1000 dumped core."
        echo ""
        echo "                Stack trace of thread 4250:"
        i=0
        while [ $i -lt 30 ]; do echo "                #$i  0x00007f00deadbe$i frame_$i (libwebkitgtk-6.0.so.4 + 0x1234)"; i=$((i + 1)); done
        echo ""
        echo "                Stack trace of thread 4251:"
        echo "                #0  0x00007f00cafe other_thread (libc.so.6 + 0x99)" ;;
    *) exit 1 ;;
esac
""",
    "systemctl": r"""#!/bin/sh
echo "systemctl $*" >> "$FAKE_LOG"
case " $* " in
    *" show "*"$FAKE_UNIT"*) printf '%s\n' "Id=$FAKE_UNIT" ActiveState=active OOMPolicy=continue MemoryMax=4294967296 \
        MemoryHigh=infinity MemorySwapMax=268435456 MemoryCurrent=1073741824 ;;
    *" is-active "*) echo inactive; exit 3 ;;
    *" show-environment "*) printf '%s\n' HOME=/home/x SECRET_TOKEN=zzz MESA_VK_DEVICE_SELECT=1002:150e ;;
esac
""",
    "gdbus": r"""#!/bin/sh
echo "gdbus $*" >> "$FAKE_LOG"
case " $* " in
    *"NameHasOwner "*) echo "(true,)" ;;
    *"GetConnectionUnixProcessID org.freedesktop.Notifications "*) echo "(uint32 777,)" ;;
    *"GetConnectionUnixProcessID :1.5 "*) echo "(uint32 4242,)" ;;
    *"GetConnectionUnixProcessID :1.9 "*) echo "(uint32 5001,)" ;;
    *"GetConnectionUnixProcessID :1.7 "*) echo "(uint32 4260,)" ;;
    *"GetConnectionUnixProcessID :1.8 "*) echo "(uint32 5002,)" ;;
    *"GetServerInformation "*) echo "('Plasma', 'KDE', '6.5.0', '1.2')" ;;
    *"GetCapabilities "*) echo "(['body', 'actions', 'persistence'],)" ;;
    *" Inhibited "*) echo "(<false>,)" ;;
    *" IsStatusNotifierHostRegistered "*) echo "(<true>,)" ;;
    *" RegisteredStatusNotifierItems "*) echo "(<[':1.5/StatusNotifierItem', 'org.kde.foo']>,)" ;;
    *"PermissionStore.Lookup "*) echo "({'io.github.Officialbabs.Natter': ['yes'], 'org.other.App': ['no']}, <byte 0x00>)" ;;
    *" version "*) echo "(<uint32 2>,)" ;;
    *) exit 1 ;;
esac
""",
    "dbus-monitor": r"""#!/bin/sh
echo "dbus-monitor $*" >> "$FAKE_LOG"
echo $$ >> "$FAKE_PIDS"
cat <<'EOF'
signal time=1729700000.0 sender=org.freedesktop.DBus -> destination=:1.30 serial=2 path=/org/freedesktop/DBus; interface=org.freedesktop.DBus; member=NameAcquired
   string ":1.30"
method call time=1729700001.1 sender=:1.5 -> destination=:1.20 serial=12 path=/org/freedesktop/Notifications; interface=org.freedesktop.Notifications; member=Notify
   string "Natter Den"
   uint32 0
   string "io.github.Officialbabs.Natter"
   string "Secret Person"
   string "Message words +44 7700 900123"
method call time=1729700002.1 sender=:1.9 -> destination=:1.20 serial=40 path=/org/freedesktop/Notifications; interface=org.freedesktop.Notifications; member=Notify
   string "Firefox thing"
method call time=1729700003.1 sender=:1.7 -> destination=:1.21 serial=7 path=/org/freedesktop/portal/desktop; interface=org.freedesktop.portal.Notification; member=AddNotification
   string "chat-1"
   array [
      dict entry(
         string "title"
         variant             string "Portal words"
      )
   ]
method call time=1729700004.1 sender=:1.8 -> destination=:1.21 serial=9 path=/org/freedesktop/portal/desktop; interface=org.freedesktop.portal.Notification; member=AddNotification
   string "other"
EOF
exec sleep 60
""",
    "flatpak": r"""#!/bin/sh
echo "flatpak $*" >> "$FAKE_LOG"
case " $* " in
    *" permission-show "*) printf 'Table      Object     App                           Permissions Data\nbackground background io.github.Officialbabs.Natter yes         0x00\n' ;;
    *) echo "error: io.github.Officialbabs.Natter/*unspecified*/*unspecified* not installed" >&2; exit 1 ;;
esac
""",
    "pacman": r"""#!/bin/sh
echo "pacman $*" >> "$FAKE_LOG"
case "$1" in
    -Qqo) [ -n "${FAKE_AUR-}" ] && echo natter-den-git && exit 0; exit 1 ;;
    -Qi) exit 1 ;;
    -Q)
        shift
        status=0
        for pkg; do
            case $pkg in
                webkitgtk-6.0) echo "webkitgtk-6.0 2.50.1-1" ;;
                natter-den-git) if [ -n "${FAKE_AUR-}" ]; then echo "natter-den-git r6.ab3e527-1"; else
                    echo "error: package '$pkg' was not found" >&2; status=1; fi ;;
                gstreamer) echo "gstreamer 1.26.7-1" ;;
                *) echo "error: package '$pkg' was not found" >&2; status=1 ;;
            esac
        done
        exit $status ;;
esac
exit 1
""",
    # Never to be run: it logs if it is. Its NATTER_DIR is filled in by run_report.
    "natter": "#!/bin/sh\necho \"natter $*\" >> \"$FAKE_LOG\"\necho 'Natter Den 9.9.9'\nexit 0\nNATTER_DIR = \"{source}\"\n",
    "lspci": r"""#!/bin/sh
printf '00:02.0 VGA compatible controller: Intel Corporation Meteor Lake-P [Intel Graphics]\n\tSubsystem: HP Device 8c2f\n\tKernel driver in use: i915\n00:14.0 USB controller: Intel Corporation Device 7e7d\n\tKernel driver in use: xhci_hcd\n'
""",
    "gst-inspect-1.0": r"""#!/bin/sh
case "$1" in
    --version) echo "gst-inspect-1.0 version 1.26.7"; echo "GStreamer 1.26.7" ;;
    webrtcbin | opusdec | avdec_h264) exit 0 ;;
    *) exit 1 ;;
esac
""",
    "nvidia-smi": r"""#!/bin/sh
echo "nvidia-smi $*" >> "$FAKE_LOG"
case " $* " in
    *" --query-gpu=driver_version,name "*) echo "580.95.05, NVIDIA GeForce RTX 5070 Laptop GPU" ;;
    *) exit 1 ;;
esac
""",
    "modinfo": "#!/bin/sh\necho \"modinfo $*\" >> \"$FAKE_LOG\"\nexit 0\n",
    "swapon": "#!/bin/sh\nprintf 'NAME       TYPE      SIZE USED PRIO\\n/dev/zram0 partition 7.6G 1.2G  100\\n'\n",
    "zramctl": "#!/bin/sh\nprintf 'NAME       ALGORITHM DISKSIZE  DATA COMPR TOTAL STREAMS MOUNTPOINT\\n/dev/zram0 zstd          7.6G  1.2G  300M  320M      16 [SWAP]\\n'\n",
    "hostname": "#!/bin/sh\necho \"$FAKE_HOST\"\n",
    "nmcli": "#!/bin/sh\nprintf '%s\\n' \"$FAKE_SSID:802-11-wireless\" 'Wired connection 1:802-3-ethernet'\n",
}


def stat_line(pid, comm, ppid, ticks, start=50000):
    """A /proc/<pid>/stat line: fields after the name are state, ppid, …, utime (12th), stime, …,
    starttime (20th)."""
    rest = ["S", str(ppid)] + ["0"] * 9 + [str(ticks), "0"] + ["0"] * 6 + [str(start)] + ["0"] * 10
    return f"{pid} ({comm}) " + " ".join(rest) + "\n"


def make_proc(base: Path) -> Path:
    """Natter (4242) with a web and a network process, another app's WebKit process, Firefox, and
    the notification server."""
    proc = base / "proc"
    proc.mkdir()
    (proc / "uptime").write_text("1000.00 2000.00\n")
    processes = [
        (4242, "natter", 1, 500, CGROUP, 200 << 10, 10 << 10),
        (4250, "WebKitWebProces", 4242, 900, CGROUP, 600 << 10, 0),
        (4251, "WebKitNetworkPr", 4242, 100, CGROUP, 80 << 10, 0),
        (5000, "WebKitWebProces", 1, 100, "/user.slice/app-gnome-org.gnome.Epiphany-5000.scope", 300 << 10, 0),
        (5001, "firefox", 1, 100, "/user.slice/app-firefox.scope", 900 << 10, 0),
        (777, "plasmashell", 1, 100, "/user.slice/plasma-plasmashell.service", 400 << 10, 0),
    ]
    for pid, comm, ppid, ticks, cgroup, rss, swap in processes:
        folder = proc / str(pid)
        folder.mkdir()
        (folder / "stat").write_text(stat_line(pid, comm, ppid, ticks))
        (folder / "comm").write_text(comm + "\n")
        (folder / "cgroup").write_text(f"0::{cgroup}\n")
        (folder / "status").write_text(f"Name:\t{comm}\nVmRSS:\t{rss} kB\nVmSwap:\t{swap} kB\n")
        (folder / "limits").write_text("Limit                     Soft Limit           Hard Limit           Units\n"
                                       "Max realtime priority     0                    0\n"
                                       "Max realtime timeout      200000               200000               us\n")
    # Graphics: the kernel's options, NVIDIA's driver, natter's environment and the devices open.
    (proc / "cmdline").write_text("BOOT_IMAGE=/vmlinuz root=UUID=disk-uuid-secret ro quiet nvidia-drm.modeset=1 "
                                  "amdgpu.dcdebugmask=0x10\n")
    (proc / "driver/nvidia").mkdir(parents=True)
    (proc / "driver/nvidia/version").write_text(
        "NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  580.95.05  Release Build  (dvs-builder@U16-I3-B03-4-3)\n"
        "GCC version:  gcc version 14.2.0\n")
    (proc / "driver/nvidia/params").write_text("DynamicPowerManagement: 3\nEnableGpuFirmware: 18\n"
                                               'TemporaryFilePath: "/home/friendly/SECRET_TOKEN"\n')
    (proc / "4242/environ").write_bytes(b"HOME=/home/friendly\0GSK_RENDERER=ngl\0SECRET_TOKEN=abc\0"
                                         b"WEBKIT_DISABLE_DMABUF_RENDERER=1\0")
    for pid, targets in ((4242, ["/dev/dri/renderD128", "/home/friendly/.local/share/natter/storage.db", "socket:[99]"]),
                         (4250, ["/dev/dri/renderD128", "/dev/nvidiactl", "/dev/nvidia0", "/dev/dri/renderD128"])):
        (proc / str(pid) / "fd").mkdir()
        for number, target in enumerate(targets, 3):
            (proc / str(pid) / "fd" / str(number)).symlink_to(target)
    for pid, cgroup in ((4260, "/user.slice/app-flatpak-io.github.Officialbabs.Natter-77.scope"),
                        (5002, "/user.slice/app-flatpak-org.other.Chat-78.scope")):
        folder = proc / str(pid)
        folder.mkdir()
        (folder / "comm").write_text("xdg-dbus-proxy\n")
        (folder / "cgroup").write_text(f"0::{cgroup}\n")
    cgroups = base / "cgroup"
    unit = cgroups / CGROUP.lstrip("/")
    unit.mkdir(parents=True)
    (unit / "memory.current").write_text(f"{900 << 20}\n")
    (unit / "memory.swap.current").write_text(f"{10 << 20}\n")
    (unit / "memory.events").write_text("low 0\nhigh 3\nmax 1\noom 1\noom_kill 1\noom_group_kill 0\n")
    return proc


def make_sys(base: Path) -> Path:
    """A laptop's two GPUs: AMD's (the one the panel is on, started on at boot) and NVIDIA's,
    asleep, with no screen."""
    sys = base / "sys"
    for pci, vendor, boot, power, card, render, driver in (
            ("0000:c4:00.0", "0x1002", "1", "active", "card1", "renderD128", "amdgpu"),
            ("0000:01:00.0", "0x10de", "0", "suspended", "card0", "renderD129", "nvidia")):
        device = sys / "devices" / pci
        (device / "power").mkdir(parents=True)
        (device / "vendor").write_text(vendor + "\n")
        (device / "boot_vga").write_text(boot + "\n")
        (device / "power/runtime_status").write_text(power + "\n")
        (device / "drm" / card).mkdir(parents=True)
        (device / "drm" / render).mkdir()
        (sys / "bus/pci/drivers" / driver).mkdir(parents=True)
        (device / "driver").symlink_to(f"../../bus/pci/drivers/{driver}")
        for node in (card, render):
            (sys / "class/drm" / node).mkdir(parents=True)
            (sys / "class/drm" / node / "device").symlink_to(f"../../../devices/{pci}")
    for connector, status in (("card1-eDP-1", "connected"), ("card1-HDMI-A-1", "disconnected"),
                              ("card0-DP-1", "disconnected")):
        (sys / "class/drm" / connector).mkdir()
        (sys / "class/drm" / connector / "status").write_text(status + "\n")
    (sys / "module/nvidia").mkdir(parents=True)
    (sys / "module/nvidia/version").write_text("580.95.05\n")
    (sys / "module/nvidia_drm/parameters").mkdir(parents=True)
    (sys / "module/nvidia_drm/parameters/modeset").write_text("Y\n")
    (sys / "module/amdgpu").mkdir()
    return sys


def journal(home: Path) -> str:
    lines = [
        "systemd[1500]: Started app-io.github.Officialbabs.Natter-4242.scope - Natter Den.",
        "natter[4242]: natter: memory: a page may hold 1200 MB, 1600 MB holding still",
        "natter[4242]: some stdout line naming Secret Person",
        "natter[4242]: (WebKitWebProcess:4250): GStreamer-WARNING **: 09:00:03.123: pipeline stalled",
        "natter[4242]: Traceback (most recent call last):",
        f'natter[4242]:   File "{home}/.local/lib/natter/source/natter/app.py", line 12, in on_notify',
        "natter[4242]:     chats[name]",
        "natter[4242]: KeyError: 'Secret Person'",
        "natter[4242]: Traceback (most recent call last):",
        f'natter[4242]:   File "{home}/.local/lib/natter/source/natter/app.py", line 40, in open_chat',
        "natter[4242]: ValueError: can't find chat 'Alice Smith'",
        "natter[4242]: Traceback (most recent call last):",
        f'natter[4242]:   File "{home}/.local/lib/natter/source/natter/app.py", line 50, in open_chat',
        "natter[4242]: gi.repository.GLib.GError: g-file-error-quark: Alice Smith.jpg: No such file (4)",
        "natter[4242]: (WebKitWebProcess:4250): libnice-WARNING **: 09:00:04.000: candidate 2a02:c7f:1234:5678:abcd:ef01:2345:6789 failed",
        "natter[4242]: natter: reply: sent to 447700900123@s.whatsapp.net for friend@example.com from 192.168.1.20 "
        f"by {USER} on {HOST} over {SSID} via 3c:22:fb:12:34:56",
        'natter[4242]: summary="Secret Person" body="hello there"',
        "natter[4242]: CONSOLE LOG secret console message",
        'natter[4242]: natter: eval → {"chat": "Secret Person"}',
        "natter[4242]: natter: debug token abcdef123456",
        "systemd[1500]: app-io.github.Officialbabs.Natter-4242.scope: A process of this unit has been killed by the OOM killer.",
        "natter[4242]: natter: quitting with 0 of WebKit's web processes still running",
    ]
    return "".join(f"2026-10-23T09:00:{i:02d}.000001+02:00 {HOST} {line}\n" for i, line in enumerate(lines))


# Natter's settings as it saves them: the switches, other keys that must not show, and a look-alike.
SETTINGS = json.dumps({"x": "SECRET-SESSION-DATA", "lock_hash": "SECRET-SESSION-DATA", "privacy_blur": True,
                       "blur_when_away": "SECRET-SESSION-DATA", "nested": {"hardware_acceleration": False}},
                      indent=2, sort_keys=True)


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS), "needs " + ", ".join(TOOLS))
class ReportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        cls.system = base / "system-bin"
        cls.system.mkdir()
        for tool in dict.fromkeys(TOOLS + tuple(SHELLS)):
            (cls.system / tool).symlink_to(shutil.which(tool))
        cls.script = SCRIPT.read_text()

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setup_home(self, folder: Path) -> Path:
        home = folder / "home"
        source = home / ".local/lib/natter/source"
        (source / "natter").mkdir(parents=True)
        (source / "natter/__init__.py").write_text('NAME = "Natter Den"\nVERSION = "0.1.2"\n')
        (home / ".local/bin").mkdir(parents=True)
        (home / ".local/bin/natter").write_text(f'#!/usr/bin/python3\nNATTER_DIR = "{source}"\n')
        # WhatsApp's data and Natter's own folders: only their sizes may show.
        account = home / ".local/share/natter/accounts/one"
        account.mkdir(parents=True)
        (account / "storage.db").write_text("SECRET-SESSION-DATA\n")
        (home / ".config/natter").mkdir(parents=True)
        (home / ".config/natter/settings.json").write_text(SETTINGS)
        (home / ".local/share/natter/chats.json").write_text('["Chat With Secret"]\n')
        apps = home / ".local/share/applications"
        apps.mkdir(parents=True)
        (apps / "io.github.Officialbabs.Natter.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=Natter Den\nExec=natter %U\nActions=chat-1;\n\n"
            "[Desktop Action chat-1]\nName=Chat With Secret\nExec=natter --chat-id abc\n")
        (home / ".config/plasmanotifyrc").write_text(
            "[Applications][io.github.Officialbabs.Natter]\nShowPopups=false\n\n"
            "[Applications][org.other.App]\nShowPopups=true\n\n[DoNotDisturb]\nUntil=2026,10,23,10,0,0\n")
        return home

    def run_report(self, *args, shell=None, seconds=None, during=None, env=None, awk=None, journal_text=None,
                   python=False, before=None):
        """report.sh with `args` in a new scratch home. `during(folder)` runs while it does; `before(home)`
        before it starts. `python`: python3 on PATH, as for the settings."""
        folder = Path(tempfile.mkdtemp(dir=self.tmp.name))
        home = self.setup_home(folder)
        if before:
            before(home)
        fake = folder / "fake-bin"
        fake.mkdir()
        for name, text in STUBS.items():
            (fake / name).write_text(text.replace("{source}", str(home / ".local/lib/natter/source")))
            (fake / name).chmod(0o755)
        (folder / "fake-link").symlink_to(fake)  # the same folder twice on PATH, as /bin and /usr/bin
        if awk:
            (fake / "awk").write_text(f'#!/bin/sh\nexec {AWKS[awk]} {"awk " if awk == "busybox" else ""}"$@"\n')
            (fake / "awk").chmod(0o755)
        if python:
            (fake / "python3").symlink_to(sys.executable)
        proc = make_proc(folder)
        make_sys(folder)
        (folder / "kernel-prev").write_text(PREVIOUS_KERNEL)
        (folder / "journal").write_text(journal(home) if journal_text is None else journal_text)
        (folder / "kernel").write_text(
            f"2026-10-23T09:10:00.000001+02:00 {HOST} kernel: Out of memory: Killed process 4250 (WebKitWebProces) "
            "total-vm:12345678kB, anon-rss:1234567kB, UID:1000\n"
            f"2026-10-23T09:10:01.000001+02:00 {HOST} kernel: usb 1-2: new device\n"
            f"2026-10-23T09:10:02.000001+02:00 {HOST} kernel: amdgpu 0000:c4:00.0: amdgpu: GPU reset(2) succeeded!\n")
        log, pids = folder / "log", folder / "pids"
        log.write_text("")
        pids.write_text("")
        environ = {
            "PATH": f"{fake}:{folder / 'fake-link'}:{self.system}", "HOME": str(home), "USER": USER, "LOGNAME": USER, "LANG": "C.UTF-8",
            "XDG_DATA_HOME": str(home / ".local/share"), "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_CACHE_HOME": str(home / ".cache"), "XDG_DATA_DIRS": str(folder / "share"),
            "TMPDIR": str(folder), "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/bus",
            "XDG_CURRENT_DESKTOP": "KDE", "XDG_SESSION_TYPE": "wayland",
            "FAKE_LOG": str(log), "FAKE_PIDS": str(pids), "FAKE_UNIT": UNIT, "FAKE_HOST": HOST, "FAKE_SSID": SSID,
            "FAKE_JOURNAL": str(folder / "journal"), "FAKE_KERNEL": str(folder / "kernel"),
            "FAKE_KERNEL_PREV": str(folder / "kernel-prev"),
            "NATTER_REPORT_TEST_PROC": str(proc), "NATTER_REPORT_TEST_CGROUP": str(folder / "cgroup"),
            "NATTER_REPORT_TEST_SYS": str(folder / "sys"),
            "NATTER_REPORT_TEST_SECONDS": str(seconds if seconds is not None else 3),
            "NATTER_REPORT_TEST_INTERVAL": "1", **(env or {}),
        }
        process = subprocess.Popen([shell or SHELLS[0], str(SCRIPT), *args], stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environ,
                                   preexec_fn=lambda: os.umask(0o022))
        if during:
            during(folder, process)
        out, err = process.communicate(timeout=120)
        reports = sorted(home.glob("natter-report-*.txt"))
        return SimpleNamespace(code=process.returncode, out=out, err=err, home=home, folder=folder,
                               reports=reports, text=reports[0].read_text() if reports else "",
                               logged=log.read_text().splitlines(),
                               pids=[int(p) for p in pids.read_text().split()])

    def redact(self, text, awk=None, env=None):
        folder = Path(tempfile.mkdtemp(dir=self.tmp.name))
        fake = folder / "fake-bin"
        fake.mkdir()
        for name in ("hostname", "nmcli"):
            (fake / name).write_text(STUBS[name])
            (fake / name).chmod(0o755)
        if awk:
            (fake / "awk").write_text(f'#!/bin/sh\nexec {AWKS[awk]} {"awk " if awk == "busybox" else ""}"$@"\n')
            (fake / "awk").chmod(0o755)
        environ = {"PATH": f"{fake}:{self.system}", "HOME": "/home/friendly", "USER": USER, "LANG": "C.UTF-8",
                   "FAKE_HOST": HOST, "FAKE_SSID": SSID, **(env or {})}
        done = subprocess.run([SHELLS[0], str(SCRIPT), "--redact"], input=text, capture_output=True, text=True,
                              env=environ, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return done.stdout

    # ---- The script itself ----------------------------------------------------------------

    def test_parses_in_dash_and_sh(self):
        for shell in ("dash", "sh", "bash"):
            if shutil.which(shell):
                done = subprocess.run([shell, "-n", str(SCRIPT)], capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, f"{shell}: {done.stderr}")

    def test_never_reads_natters_folders(self):
        """Natter's own folders (WhatsApp's data among them) appear twice: handed to sizes_only,
        which only runs du and find on them, and as settings.json for natter_switches, which reads
        three switches from it and nothing else."""
        folders = re.compile(r"(\.local/share|\.cache|\.config|DATA_HOME|CACHE_HOME|CONFIG_HOME)/natter\b|\.var/app")
        uses = [line.strip() for line in self.script.splitlines()
                if folders.search(line) and not line.strip().startswith("#")]
        self.assertEqual(len(uses), 2, uses)
        self.assertTrue(uses[0].startswith("sizes_only "), uses)
        self.assertEqual(uses[1], 'ns_file="$CONFIG_HOME/natter/settings.json"')
        switches = self.script.split("natter_switches() {", 1)[1].split("\n}\n", 1)[0]
        self.assertEqual(switches.count('"$ns_file"'), 4, switches)  # tested, python3's, and the two seds'
        for line in switches.splitlines():
            if '"$ns_file"' in line and "[ ! -f" not in line:
                self.assertTrue(re.search(r"python3 -I -c \"\$SWITCHES\"|sed -n -E 's/\^  \"\(privacy_blur\|blur_when_away\|"
                                          r"hardware_acceleration\)\": ", line), line)
        program = self.script.split("SWITCHES='", 1)[1].split("'\n", 1)[0]
        self.assertIn('defaults = {"privacy_blur": False, "blur_when_away": False, "hardware_acceleration": True}', program)
        self.assertEqual(re.findall(r"print\((.*)\)", program),
                         ['"settings.json: not readable as settings"', 'key + ": not set (" + json.dumps(default) + " by default)"',
                          'key + ": " + json.dumps(values[key])', 'key + ": not true or false"'])
        body = self.script.split("sizes_only() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('du -sh "$so_dir"', body)
        self.assertIn('find "$so_dir" -type f', body)
        for reader in ("cat", "head", "tail", "grep", "sed", "awk", "less", "strings", "od", "xxd", "cp", "tar", "<"):
            self.assertNotRegex(body, rf"(^|[\s(|]){re.escape(reader)}\s", reader)
        for word in ("chats.json", "accounts/", "localStorage", "IndexedDB", "LOCAL_STORAGE"):
            self.assertNotIn(word, self.script)

    def test_sends_nothing(self):
        code = "\n".join(line for line in self.script.splitlines() if not line.lstrip().startswith("#"))
        for word in ("curl", "wget", "nc", "ncat", "socat", "ssh", "scp", "rsync", "sudo", "pkexec", "debuginfod-find"):
            self.assertNotRegex(code, rf"(^|[\s;|&(]){word}\s", word)

    # ---- Redaction ---------------------------------------------------------------------------

    def test_redaction(self):
        cases = {
            "call from +44 7700 900123 ok": "call from <digits> ok",
            "jid 447700900123@s.whatsapp.net": "jid <email>",
            "number 4477009001 here": "number <digits> here",
            "mail friend.name+x@example.co.uk now": "mail <email> now",
            "path /home/friendly/.local/lib/natter": "path ~/.local/lib/natter",
            f"user {USER} on {HOST}": "user <user> on <host>",
            f"wifi {SSID} up": "wifi <ssid> up",
            'connected to SSID "Cafe Free" now': "connected to SSID <ssid> now",
            "mac 3C:22:FB:12:34:56 and 3c-22-fb-12-34-56": "mac <mac> and <mac>",
            "from 192.168.1.20:5000 and 127.0.0.1": "from <ip>:5000 and 127.0.0.1",
            "natter: debug token 0a1b2c": "natter: debug token <hidden>",
            "from 2a02:c7f:1234:5678:abcd:ef01:2345:6789 port": "from <ip> port",
            "candidate fe80::1c2d:3e4f:5a6b:7c8d%wlan0 udp": "candidate <ip>%wlan0 udp",
            "[2001:db8::42]:5000 and 2001:db8::1.": "[<ip>]:5000 and <ip>.",
            "mapped ::ffff:192.168.1.20 ok": "mapped <ip> ok",
            "full 2001:1234:5678:9012:3456:7890:1234:5678 end": "full <ip> end",
            # not IPv6: times, C++ names, loopback
            "at 10:00:00.000 WebCore::Page::load std::move ::1 and ::": "at 10:00:00.000 WebCore::Page::load std::move ::1 and ::",
            "0:00:01.2345 de:ad:be:ef": "0:00:01.2345 de:ad:be:ef",
            # what must stay: units that look like addresses, hex numbers, sizes, versions, times
            "app-io.github.Officialbabs.Natter@0f3a.service and user@1000.service":
                "app-io.github.Officialbabs.Natter@0f3a.service and user@1000.service",
            "#0  0x00007f1234567890 raise (libc.so.6 + 0x12345678)": "#0  0x00007f1234567890 raise (libc.so.6 + 0x12345678)",
            "total-vm:12345678kB WebKitGTK 2.50.1 at 2026-10-23T09:00:00.123456+02:00":
                "total-vm:12345678kB WebKitGTK 2.50.1 at 2026-10-23T09:00:00.123456+02:00",
            "Officialbabs friendlyness": "Officialbabs friendlyness",
        }
        text = "".join(line + "\n" for line in cases)
        for awk in [None, *AWKS]:
            with self.subTest(awk=awk or "awk"):
                self.assertEqual(self.redact(text, awk=awk).splitlines(), list(cases.values()))

    def test_a_host_named_like_the_system_is_left(self):
        """A computer called "localhost" (or after its distribution) keeps the word everywhere else."""
        out = self.redact("localhost is fine\n", env={"FAKE_HOST": "localhost"})
        self.assertEqual(out, "localhost is fine\n")

    def test_everyday_names_are_left(self):
        """A computer called "Desktop", or a user called "user", would garble the report's own words
        (xdg-desktop-portal, systemctl --user) and tell no one who it is: they stay."""
        text = "xdg-desktop-portal-kde: systemctl --user user@1000.service\n"
        out = self.redact(text, env={"FAKE_HOST": "Desktop", "USER": "user", "LOGNAME": "user"})
        self.assertEqual(out, text)

    # ---- The report --------------------------------------------------------------------------

    def assertPrivate(self, text, home):
        for secret in SECRETS:
            self.assertNotIn(secret, text)
        self.assertNotIn(str(home), text)

    def test_report(self):
        for shell in SHELLS:
            with self.subTest(shell=shell):
                run = self.run_report(shell=shell)
                self.assertEqual(run.code, 0, run.err + run.out)
                self.assertEqual(len(run.reports), 1)
                report = run.reports[0]
                self.assertRegex(report.name, r"^natter-report-\d{8}-\d{4}\.txt$")
                self.assertEqual(stat.S_IMODE(report.stat().st_mode), 0o600)
                lines = run.out.strip().splitlines()
                self.assertEqual(lines[-3], str(report))
                self.assertTrue(lines[-2].startswith("Natter Den 0.1.2, get.sh (the latest release); running (3 processes)"),
                                lines[-2])
                self.assertEqual(lines[-1], "Read it over, then send it to whoever asked for it.")
                text = run.text
                self.assertPrivate(text, run.home)
                self.assertPrivate(run.out.replace(str(run.reports[0]), ""), run.home)  # its own path is for the person
                self.assertTrue(text.startswith("Natter Den problem report\n"))
                self.assertLess(text.index("WHAT IT DOES NOT CONTAIN"), text.index("===== 1."))
                self.assertIn("Run this after a freeze too: it reads the previous boot's log.\n", text.split("WHAT", 1)[0])
                for number in range(1, 8):
                    self.assertIn(f"===== {number}. ", text)
                self.assertIn("===== 7. Graphics and freezes =====", text)
                self.assertNotIn("===== 8. ", text)
                # 1: version, install, copies, running
                self.assertIn("NATTER_DIR: ~/.local/lib/natter/source", text)
                self.assertRegex(text, r"natter on PATH: \S+/fake-bin/natter, version 0\.1\.2 \(read from its files")
                self.assertIn("Copies found: 1\n", text)
                self.assertIn("(and 1 menu actions, not shown", text)
                self.assertIn("OOMPolicy=continue", text)
                self.assertIn("MemoryMax=4096 MB", text)
                self.assertIn("MemorySwapMax=256 MB", text)
                self.assertIn("Max realtime timeout", text)
                self.assertRegex(text, r"natter +pid 4242 ")
                self.assertRegex(text, r"WebKitWebProces +pid 4250 ")
                self.assertNotIn("pid 5000", text)  # another app's WebKit
                self.assertNotIn("firefox", text)
                self.assertRegex(text, r"~/\.local/share/natter: \S+, 2 files")
                # 2: the system
                self.assertIn("XDG_CURRENT_DESKTOP=KDE", text)
                self.assertIn("Kernel driver in use: i915", text)
                self.assertNotIn("USB controller", text)
                self.assertIn("gst-plugin-va: not installed", text)
                self.assertIn("webrtcbin: yes", text)
                self.assertIn("nicesrc: missing", text)
                # 3: the log, only the safe kinds
                self.assertIn("natter: memory: a page may hold 1200 MB", text)
                self.assertIn("GStreamer-WARNING **", text)
                self.assertIn("Traceback (most recent call last):", text)
                self.assertIn('File "~/.local/lib/natter/source/natter/app.py", line 12', text)
                self.assertIn("KeyError: <message hidden>", text)
                self.assertIn("ValueError: <message hidden>", text)
                self.assertIn("gi.repository.GLib.GError: <message hidden>", text)
                self.assertIn("libnice-WARNING **: 09:00:04.000: candidate <ip> failed", text)
                self.assertIn("natter: reply: sent to <email> for <email> from <ip> by <user> on <host> over <ssid> "
                              "via <mac>", text)
                self.assertIn("killed by the OOM killer", text)
                self.assertNotIn("some stdout line", text)
                self.assertNotIn("CONSOLE", text)
                self.assertNotIn("eval", text)
                starts = text.split("--- Starts and quits", 1)[1].split("---", 1)[0]
                self.assertIn("Started app-io.github.Officialbabs.Natter-4242.scope", starts)
                self.assertIn("natter: quitting", starts)
                self.assertIn(f"--since=-24h _COMM=natter + SYSLOG_IDENTIFIER=io.github.Officialbabs.Natter", "\n".join(run.logged))
                self.assertIn(f"_SYSTEMD_USER_UNIT={UNIT}", "\n".join(run.logged))
                self.assertNotIn("konsole", "\n".join(line for line in run.logged if "--since" in line))
                # 4: crashes, 25 frames of the first thread
                self.assertIn("SIGSEGV present /usr/lib/webkitgtk-6.0/WebKitWebProcess", text)
                self.assertNotIn("gnome-calculator", text)
                self.assertIn("6100 1000 1000 SIGSEGV present /usr/bin/python3.13", text)  # the call window
                self.assertNotIn(" 6200 ", text)  # another Python program
                self.assertIn("Signal: 11 (SEGV)", text)
                self.assertIn("frame_24 ", text)
                self.assertNotIn("frame_25 ", text)
                self.assertNotIn("other_thread", text)
                self.assertNotIn("Command Line", text)
                # 5: kills
                self.assertIn("Out of memory: Killed process 4250 (WebKitWebProces)", text)
                self.assertNotIn("usb 1-2", text)
                self.assertIn("The canary thread is apparently starving", text)
                # 6: notifications and background
                self.assertIn("Owned by: plasmashell (pid 777)", text)
                self.assertIn("('Plasma', 'KDE', '6.5.0', '1.2')", text)
                self.assertIn("one of them Natter's: yes", text)
                self.assertIn("'io.github.Officialbabs.Natter': ['yes']", text)
                self.assertIn("ShowPopups=false", text)
                # 7: graphics and freezes
                graphics = text.split("===== 7. Graphics and freezes =====", 1)[1]
                self.assertIn("card0: NVIDIA 0000:01:00.0, driver nvidia, render node renderD129, started on at boot: no, "
                              "power: suspended, screens: none\n", graphics)
                self.assertIn("card1: AMD 0000:c4:00.0, driver amdgpu, render node renderD128, started on at boot: yes, "
                              "power: active, screens: eDP-1\n", graphics)
                self.assertIn("nvidia-smi: 580.95.05, NVIDIA GeForce RTX 5070 Laptop GPU\n", graphics)
                self.assertIn("/proc/driver/nvidia/version: NVRM version: NVIDIA UNIX Open Kernel Module for x86_64  580.95.05", graphics)
                self.assertIn("nvidia: loaded, version 580.95.05\n", graphics)
                self.assertRegex(graphics, r"amdgpu: loaded, no version of its own \(kernel \S+\)\n")
                self.assertIn("nvidia_drm modeset: Y\n", graphics)
                self.assertIn("NVIDIA options: DynamicPowerManagement:3 EnableGpuFirmware:18 \n", graphics)
                self.assertIn("Kernel options for graphics: nvidia-drm.modeset=1 amdgpu.dcdebugmask=0x10 \n", graphics)
                self.assertIn("natter 4242: /dev/dri/renderD128 (amdgpu)\n", graphics)
                self.assertIn("WebKitWebProces 4250: /dev/dri/renderD128 (amdgpu), /dev/nvidia0, /dev/nvidiactl\n", graphics)
                self.assertIn("WebKitNetworkPr 4251: no GPU device open\n", graphics)
                self.assertIn("natter 4242: GSK_RENDERER=ngl\nnatter 4242: WEBKIT_DISABLE_DMABUF_RENDERER=1\n", graphics)
                self.assertIn("menu launches: MESA_VK_DEVICE_SELECT=1002:150e\n", graphics)
                self.assertIn("privacy_blur: true\nblur_when_away: not true or false\n"
                              "hardware_acceleration: not set (true by default)\n", graphics)
                boots = graphics.split("--- Boots, the last 5, and how each ended\n", 1)[1].split("\n\n", 1)[0]
                self.assertEqual(boots.splitlines(), [
                    "-3  Mon 2026-10-20 08:00:01 CEST Mon 2026-10-20 22:00:00 CEST  no clean end: its log stops at "
                    "2026-10-20T22:00:00.000001+02:00, while it was going to sleep",
                    "-2  Tue 2026-10-21 08:00:01 CEST Tue 2026-10-21 23:00:00 CEST  ended cleanly",
                    "-1  Wed 2026-10-22 08:00:01 CEST Wed 2026-10-22 23:59:13 CEST  no clean end: its log stops at "
                    "2026-10-22T23:59:13.000001+02:00 (a freeze, a forced power-off or a crash)",
                    "0  Thu 2026-10-23 08:00:00 CEST Thu 2026-10-23 10:00:00 CEST  this boot"])
                now = graphics.split("lockups: this boot\n", 1)[1].split("\n\n", 1)[0]
                self.assertEqual(now.splitlines(), [
                    "1 lines (GPU resets 1)",
                    "2026-10-23T09:10:02.000001+02:00 kernel: amdgpu 0000:c4:00.0: amdgpu: GPU reset(2) succeeded!"])
                before = graphics.split("the boot before (a freeze shows here when the log was saved in time)\n", 1)[1]
                self.assertTrue(before.startswith("12 lines (NVIDIA Xid errors 2, ring timeouts 1, GPU hangs 2, GPU resets 1, "
                                                  "screen update timeouts 1, stuck tasks 2, lockups and stalls 1, others 2)\n"),
                                before)
                for line in ("NVRM: Xid (PCI:0000:01:00): 13, pid=2345, name=<app>, Graphics SM Warp Exception",
                             "name=WebKitWebProces, Ch <digits>,",
                             "*ERROR* ring gfx_0.0.0 timeout, signaled seq=1234",
                             "Process information: process <app> pid 3456 thread <app> pid 3460",
                             "Process WebKitWebProces pid 4250 thread SkiaGPU pid 4270",
                             "flip_done timed out", "INFO: task <app>:5555 blocked",
                             "soft lockup - CPU#3 stuck for 26s! [<app>:2345]", "GPU HANG: ecode 12:1:85dffffb, in <app> [6666]",
                             "[drm] <app>[6666] context reset", "INFO: task kworker/u64:3:123 blocked"):
                    self.assertIn(line, before)
                self.assertNotIn("usb 1-2", graphics)
                self.assertNotIn("UUID", text)
                self.assertTrue(lines[-2].endswith(" Previous boot: no clean end. GPU error lines: 1 this boot, "
                                                   "12 the boot before."), lines[-2])
                # nothing started that's still running, and no real Natter asked for anything
                self.assertEqual(run.pids, [])
                self.assertEqual([line for line in run.logged if line.startswith("natter ")], [])
                self.assertNotIn("9.9.9", text)
                # Natter's folders weren't made: one that wasn't there is still "none"
                self.assertIn("~/.cache/natter: none", text)
                self.assertFalse((run.home / ".cache/natter").exists())

    def test_aur_package_is_one_copy(self):
        """The package's command on PATH and the package itself are the same copy."""
        run = self.run_report(env={"FAKE_AUR": "1"})
        self.assertEqual(run.code, 0, run.err)
        self.assertIn("(package natter-den-git)", run.text)
        self.assertIn("Package: natter-den-git r6.ab3e527-1", run.text)
        self.assertIn("Copies found: 1\n", run.text)

    def test_hours(self):
        run = self.run_report("--hours", "500")
        self.assertEqual(run.code, 0, run.err)
        self.assertIn("The logs were cut to the last 72 hours", run.text)
        self.assertTrue(any("--since=-72h" in line for line in run.logged))
        run = self.run_report("--hours=6")
        self.assertTrue(any("--since=-6h" in line for line in run.logged))
        self.assertIn("the last 6 hours", run.text)

    def test_size(self):
        """A long log keeps its newest lines, and the report stays under 2 MB."""
        text = "".join(f"2026-10-23T09:{i // 600 % 60:02d}:{i // 10 % 60:02d}.{i:06d}+02:00 {HOST} natter[4242]: "
                       f"natter: line {i} " + "x" * 380 + "\n" for i in range(30000))
        run = self.run_report("--watch", "1", seconds=2, journal_text=text)
        self.assertEqual(run.code, 0, run.err)
        size = run.reports[0].stat().st_size
        self.assertLess(size, 2_000_000)
        self.assertIn("natter: line 29999 x", run.text)
        self.assertNotIn("natter: line 26999 x", run.text)
        self.assertIn("(the 27000 oldest lines left out)", run.text)
        self.assertIn("===== 8. Watch: 1 minutes =====", run.text)
        self.assertTrue(run.text.endswith("===== End of the report =====\n"))
        self.assertFalse([line for line in run.text.splitlines() if len(line) > 1010])

    def test_bad_arguments(self):
        for args in (["--watch", "abc"], ["--watch", "0"], ["--watch=-3"], ["--hours", "0"], ["--hours"],
                     ["--hours", "x"], ["--bogus"]):
            with self.subTest(args=args):
                run = self.run_report(*args)
                self.assertEqual(run.code, 2, run.out + run.err)
                self.assertIn("Usage:", run.err)
                self.assertEqual(run.reports, [])
        run = self.run_report("--help")
        self.assertEqual(run.code, 0)
        self.assertIn("--watch [MINUTES]", run.out)
        self.assertEqual(run.reports, [])

    # ---- Graphics and freezes ----------------------------------------------------------------

    def test_switches_with_python(self):
        """With python3 the switches come from the settings parsed whole: the nested look-alike
        and every other key stay out."""
        run = self.run_report(python=True)
        self.assertEqual(run.code, 0, run.err)
        self.assertIn("privacy_blur: true\nblur_when_away: not true or false\n"
                      "hardware_acceleration: not set (true by default)\n", run.text)
        self.assertPrivate(run.text, run.home)

    def test_switches_other_settings(self):
        """Every switch saved, by python3 and by sed; no settings saved; settings that aren't."""
        def saved(home):
            (home / ".config/natter/settings.json").write_text(json.dumps(
                {"blur_when_away": True, "hardware_acceleration": False, "privacy_blur": False,
                 "x": "SECRET-SESSION-DATA"}, indent=2, sort_keys=True))

        for python in (False, True):
            with self.subTest(python=python):
                run = self.run_report(python=python, before=saved)
                self.assertIn("privacy_blur: false\nblur_when_away: true\nhardware_acceleration: false\n", run.text)
                self.assertPrivate(run.text, run.home)
        run = self.run_report(before=lambda home: (home / ".config/natter/settings.json").unlink())
        self.assertIn("No settings saved: privacy_blur and blur_when_away are off, hardware_acceleration on", run.text)
        run = self.run_report(python=True, before=lambda home: (home / ".config/natter/settings.json").write_text(
            '["SECRET-SESSION-DATA"]'))
        self.assertIn("settings.json: not readable as settings\n", run.text)
        self.assertPrivate(run.text, run.home)

    def test_only_this_boot(self):
        """A journal that isn't kept across restarts: says so, and reads no boot before."""
        run = self.run_report(env={"FAKE_ONE_BOOT": "1"})
        self.assertEqual(run.code, 0, run.err)
        self.assertIn("Only this boot is in the journal: the log isn't kept across restarts here", run.text)
        self.assertNotIn("the boot before (a freeze", run.text)
        self.assertFalse([line for line in run.logged if " -b -1" in line])
        self.assertTrue(run.out.strip().splitlines()[-2].endswith(
            " Previous boot: not in the journal. GPU error lines: 1 this boot, ? the boot before."))

    def test_unfinished_watch(self):
        """A watch a freeze cut short: the next run adds its file to the report, then removes it."""
        def froze(home):
            (home / "natter-watch-20261022-2350.txt").write_text(
                "Natter Den watch started 2026-10-22 23:50 CEST, for 10 minutes.\n"
                f"2026-10-22T23:58:00 sample: 3 processes, 2900 MB, from {home}/x on {HOST}\n")

        run = self.run_report(before=froze)
        self.assertEqual(run.code, 0, run.err)
        part = run.text.split("--- Watches that didn't finish (the computer stopped, or the watch was killed)\n", 1)[1]
        self.assertTrue(part.startswith("The watch of 2026-10-22 23:50:\nNatter Den watch started 2026-10-22 23:50"), part)
        self.assertIn("2026-10-22T23:58:00 sample: 3 processes, 2900 MB, from ~/x on <host>\n", part)
        self.assertLess(run.text.index("===== 7. Graphics"), run.text.index("Watches that didn't finish"))
        self.assertEqual(list(run.home.glob("natter-watch-*")), [])

    def test_gpu_lines(self):
        """The kernel's GPU lines, other apps' names hidden, in every awk at hand."""
        text = PREVIOUS_KERNEL + kernel_line("2026-10-22T23:59:30", "amdgpu 0000:c4:00.0: amdgpu: Process natter pid 4242 thread "
                                             "natter:cs0 pid 4243")
        # nouveau's "[instance name[pid]]", an other app's and Natter's web process's.
        text += kernel_line("2026-10-22T23:59:31", "nouveau 0000:01:00.0: fifo: fault 01 [WRITE] at 0000000000123000 engine 00 "
                            "[GR] client 0f [GPC0/PE_0] reason 02 [PTE] on channel 3 [00ff6e9000 firefox[3487]]")
        text += kernel_line("2026-10-22T23:59:32", "nouveau 0000:01:00.0: gr: DATA_ERROR 00000031 [INVALID_ENUM] "
                            "ch 2 [007f5b5000 WebKitWebProces[4250]] subc 0 class a097")
        for awk in [None, *AWKS]:
            with self.subTest(awk=awk or "awk"):
                folder = Path(tempfile.mkdtemp(dir=self.tmp.name))
                path = f"{self.system}"
                if awk:
                    (folder / "awk").write_text(f'#!/bin/sh\nexec {AWKS[awk]} {"awk " if awk == "busybox" else ""}"$@"\n')
                    (folder / "awk").chmod(0o755)
                    path = f"{folder}:{path}"
                done = subprocess.run([SHELLS[0], str(SCRIPT), "--gpu-lines"], input=text, capture_output=True, text=True,
                                      env={"PATH": path, "LANG": "C.UTF-8"}, timeout=30)
                self.assertEqual(done.returncode, 0, done.stderr)
                lines = done.stdout.splitlines()
                self.assertEqual(len(lines), 15, done.stdout)
                for secret in ("Isolated Web Co", "signal-desktop", "signal-de", "keepassxc", "thunderbird", "usb 1-2",
                               "firefox"):
                    self.assertNotIn(secret, done.stdout)
                self.assertIn("process <app> pid 3456 thread <app> pid 3460", done.stdout)
                self.assertIn("Process WebKitWebProces pid 4250 thread SkiaGPU pid 4270", done.stdout)
                self.assertIn("Process natter pid 4242 thread natter:cs0 pid 4243", lines[-3])
                self.assertIn("on channel 3 [00ff6e9000 <app>[3487]]", lines[-2])
                self.assertIn("ch 2 [007f5b5000 WebKitWebProces[4250]] subc", lines[-1])
                self.assertIn("name=WebKitWebProces,", done.stdout)
                self.assertIn("task kworker/u64:3:123 blocked", done.stdout)

    # ---- The watch ---------------------------------------------------------------------------

    def test_watch(self):
        kept = []

        def page_reloads(folder, _process):
            time.sleep(1.5)  # the web process goes, as when the page reloads
            shutil.rmtree(folder / "proc" / "4250")
            time.sleep(1)
            kept.extend((path, path.stat().st_mode, path.read_text()) for path in (folder / "home").glob("natter-watch-*.txt"))

        run = self.run_report("--watch", "99", during=page_reloads, seconds=4)
        self.assertEqual(run.code, 0, run.err + run.out)
        # what the watch saw was in the home folder as it went, private and redacted, and is gone now
        self.assertEqual(len(kept), 1, kept)
        path, mode, seen = kept[0]
        self.assertRegex(path.name, r"^natter-watch-\d{8}-\d{4}\.txt$")
        self.assertEqual(stat.S_IMODE(mode), 0o600)
        self.assertTrue(seen.startswith("Natter Den watch started "), seen)
        self.assertIn("If this file is still here, the watch didn't finish", seen)
        self.assertRegex(seen, r"sample: 3 processes")
        self.assertIn("natter: during the watch", seen)
        self.assertIn("ring gfx_0.0.0 timeout", seen)
        self.assertIn("Natter Den asked the desktop to show one", seen)
        self.assertPrivate(seen, run.home)
        self.assertNotIn(HOST, seen)
        self.assertFalse(path.exists())
        self.assertIn(f"saw is kept in ~/{path.name} as it goes", run.out)
        self.assertRegex(run.out, r"\d\d:\d\d:\d\d kernel: amdgpu 0000:c4:00\.0: amdgpu: ring gfx_0\.0\.0 timeout")
        self.assertIn("Watching Natter Den for 60 minutes", run.out)
        self.assertIn("Minimise Natter Den's window, or close it.", run.out)
        self.assertIn("Read it over, then send it to whoever asked for it.", run.out)
        text = run.text
        self.assertPrivate(text, run.home)
        self.assertPrivate(run.out.replace(str(run.reports[0]), ""), run.home)  # its own path is for the person
        self.assertIn("The watch was cut to 60 minutes", text)
        watch = text.split("===== 8. Watch: 60 minutes =====", 1)[1]
        self.assertRegex(watch, r"sample: 3 processes, \d+ MB, swap 10 MB: natter 4242: 200 MB, swap 10 MB, cpu -")
        self.assertRegex(watch, r"sample: 2 processes, .*cpu 0\.0%")
        self.assertIn(f"cgroup {UNIT}: memory 900 MB, swap 10 MB, events low=0 high=3 max=1 oom=1 oom_kill=1", watch)
        self.assertIn("change: ended WebKitWebProces 4250", watch)
        self.assertIn("natter: during the watch", watch)
        self.assertIn("kernel: Out of memory: Killed process 4250", watch)
        self.assertIn("kernel: amdgpu 0000:c4:00.0: amdgpu: ring gfx_0.0.0 timeout, signaled seq=9, emitted seq=11", watch)
        self.assertIn("amdgpu: Process <app> pid 3456 thread <app> pid 3460", watch)
        self.assertNotIn("usb 1-2", watch)
        notified = [line for line in watch.splitlines() if " notification: " in line]
        self.assertEqual(len(notified), 2, watch)
        self.assertIn("Natter Den asked the desktop to show one (natter 4242)", notified[0])
        self.assertIn("Natter Den asked the desktop to show one through the notification portal (xdg-dbus-proxy 4260)",
                      notified[1])
        self.assertIn("notifications Natter asked for: 2", watch)
        monitor = [line for line in run.logged if line.startswith("dbus-monitor ")]
        self.assertIn("interface='org.freedesktop.portal.Notification',member='AddNotification'", monitor[0])
        # the timeline is in time order
        stamps = [line.split()[0] for line in watch.splitlines() if re.match(r"\d{4}-\d\d-\d\dT", line)]
        self.assertEqual(stamps, sorted(stamps))
        # the followers and dbus-monitor are gone
        self.assertEqual(len(run.pids), 3, run.logged)
        for pid in run.pids:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_watch_default_and_interrupt(self):
        """--watch alone is 10 minutes; Ctrl+C stops it early and the report is still written."""
        def interrupt(folder, process):
            time.sleep(2.5)
            process.send_signal(signal.SIGINT)

        run = self.run_report("--watch", "--hours", "5", seconds=60, during=interrupt)
        self.assertEqual(run.code, 0, run.err + run.out)
        self.assertIn("Watching Natter Den for 10 minutes", run.out)
        self.assertIn("===== 8. Watch: 10 minutes =====", run.text)
        self.assertIn("the last 5 hours", run.text)
        self.assertRegex(run.out, r"Watch over after [0-9] seconds")
        for pid in run.pids:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
