"""tools/report.sh, the problem report a person runs and sends back: run whole in a scratch home,
with stand-ins for journalctl, coredumpctl, systemctl, gdbus, flatpak, pacman and the rest that
answer from canned replies, and a made-up /proc and cgroup tree, so nothing reads this computer's
logs, nothing reaches the network, and no real Natter Den is looked at."""

import os
import re
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
         "mkdir", "chmod", "mv", "id", "uname", "du", "find", "sleep", "kill", "getconf", "timeout", "readlink")
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
           "Alice Smith")

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
            *" -k "*) echo "2026-10-23T23:59:58.000001+02:00 $FAKE_HOST kernel: Out of memory: Killed process 4250 (WebKitWebProces)" ;;
            *) echo "2026-10-23T23:59:58.000002+02:00 $FAKE_HOST natter[4242]: natter: during the watch" ;;
        esac
        exec sleep 60 ;;
    *" -k -n 1 "*) echo "2026-10-23T08:00:00.000001+02:00 $FAKE_HOST kernel: Linux version" ;;
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
        (home / ".config/natter/settings.json").write_text('{"x": "SECRET-SESSION-DATA"}\n')
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

    def run_report(self, *args, shell=None, seconds=None, during=None, env=None, awk=None, journal_text=None):
        """report.sh with `args` in a new scratch home. `during(folder)` runs while it does."""
        folder = Path(tempfile.mkdtemp(dir=self.tmp.name))
        home = self.setup_home(folder)
        fake = folder / "fake-bin"
        fake.mkdir()
        for name, text in STUBS.items():
            (fake / name).write_text(text.replace("{source}", str(home / ".local/lib/natter/source")))
            (fake / name).chmod(0o755)
        (folder / "fake-link").symlink_to(fake)  # the same folder twice on PATH, as /bin and /usr/bin
        if awk:
            (fake / "awk").write_text(f'#!/bin/sh\nexec {AWKS[awk]} {"awk " if awk == "busybox" else ""}"$@"\n')
            (fake / "awk").chmod(0o755)
        proc = make_proc(folder)
        (folder / "journal").write_text(journal(home) if journal_text is None else journal_text)
        (folder / "kernel").write_text(
            f"2026-10-23T09:10:00.000001+02:00 {HOST} kernel: Out of memory: Killed process 4250 (WebKitWebProces) "
            "total-vm:12345678kB, anon-rss:1234567kB, UID:1000\n"
            f"2026-10-23T09:10:01.000001+02:00 {HOST} kernel: usb 1-2: new device\n")
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
            "NATTER_REPORT_TEST_PROC": str(proc), "NATTER_REPORT_TEST_CGROUP": str(folder / "cgroup"),
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
        """Natter's own folders (WhatsApp's data among them) appear once, handed to sizes_only,
        which only runs du and find on them."""
        folders = re.compile(r"(\.local/share|\.cache|\.config|DATA_HOME|CACHE_HOME|CONFIG_HOME)/natter\b|\.var/app")
        uses = [line.strip() for line in self.script.splitlines()
                if folders.search(line) and not line.strip().startswith("#")]
        self.assertEqual(len(uses), 1, uses)
        self.assertTrue(uses[0].startswith("sizes_only "), uses)
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
                for number in range(1, 7):
                    self.assertIn(f"===== {number}. ", text)
                self.assertNotIn("===== 7. ", text)
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
        self.assertIn("===== 7. Watch: 1 minutes =====", run.text)
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

    # ---- The watch ---------------------------------------------------------------------------

    def test_watch(self):
        def page_reloads(folder, _process):
            time.sleep(1.5)  # the web process goes, as when the page reloads
            shutil.rmtree(folder / "proc" / "4250")

        run = self.run_report("--watch", "99", during=page_reloads, seconds=4)
        self.assertEqual(run.code, 0, run.err + run.out)
        self.assertIn("Watching Natter Den for 60 minutes", run.out)
        self.assertIn("Minimise Natter Den's window, or close it.", run.out)
        self.assertIn("Read it over, then send it to whoever asked for it.", run.out)
        text = run.text
        self.assertPrivate(text, run.home)
        self.assertPrivate(run.out.replace(str(run.reports[0]), ""), run.home)  # its own path is for the person
        self.assertIn("The watch was cut to 60 minutes", text)
        watch = text.split("===== 7. Watch: 60 minutes =====", 1)[1]
        self.assertRegex(watch, r"sample: 3 processes, \d+ MB, swap 10 MB: natter 4242: 200 MB, swap 10 MB, cpu -")
        self.assertRegex(watch, r"sample: 2 processes, .*cpu 0\.0%")
        self.assertIn(f"cgroup {UNIT}: memory 900 MB, swap 10 MB, events low=0 high=3 max=1 oom=1 oom_kill=1", watch)
        self.assertIn("change: ended WebKitWebProces 4250", watch)
        self.assertIn("natter: during the watch", watch)
        self.assertIn("kernel: Out of memory: Killed process 4250", watch)
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
        self.assertIn("===== 7. Watch: 10 minutes =====", run.text)
        self.assertIn("the last 5 hours", run.text)
        self.assertRegex(run.out, r"Watch over after [0-9] seconds")
        for pid in run.pids:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
