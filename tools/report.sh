#!/bin/sh
# Natter Den's problem report: what's needed to find out why Natter Den misbehaves on this
# computer, in one text file in your home folder, for you to read over and then send to whoever
# asked for it.
#
#   curl -fsSL https://raw.githubusercontent.com/Officialbabs/natter/master/tools/report.sh | sh
#   curl -fsSL https://raw.githubusercontent.com/Officialbabs/natter/master/tools/report.sh | sh -s -- --watch 10
#   sh tools/report.sh [--watch [MINUTES]] [--hours HOURS]
#
# --watch also watches Natter Den for MINUTES (10 unless given, 60 at most) while you make the
# problem happen: its memory and CPU every 5 seconds, its log, the kernel's out-of-memory kills,
# and the times it asks the desktop for a notification (never what the notification says).
# --hours: how far back the logs go (24 unless given, 72 at most).
#
# It only looks: no sudo, no settings changed, nothing installed, nothing kept running after it,
# and nothing sent anywhere. It never reads WhatsApp's data: Natter Den's own folders appear only
# as sizes and file counts. Of the logs it keeps Natter Den's own lines, warnings and errors, and
# replaces the home folder, user and computer names, e-mail, IP and MAC addresses, Wi-Fi names and
# long numbers (phone numbers) before anything is written.
#
# Test-only hooks (tests/test_report.py sets them; nothing else should):
#   NATTER_REPORT_TEST_PROC      a folder to read processes from instead of /proc
#   NATTER_REPORT_TEST_CGROUP    a folder to read cgroups from instead of /sys/fs/cgroup
#   NATTER_REPORT_TEST_SECONDS   how long the watch lasts, in seconds
#   NATTER_REPORT_TEST_INTERVAL  seconds between the watch's samples

APP_ID=io.github.Officialbabs.Natter
OLD_APP_ID=io.natter.Natter  # Natter's id before the rename
NAME="Natter Den"
HOURS=24
WATCH=0
WATCH_ARG=""
NOTES=""
R_PROC=${NATTER_REPORT_TEST_PROC:-/proc}
R_CGROOT=${NATTER_REPORT_TEST_CGROUP:-/sys/fs/cgroup}
export R_PROC R_CGROOT
DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}
CACHE_HOME=${XDG_CACHE_HOME:-$HOME/.cache}
CONFIG_HOME=${XDG_CONFIG_HOME:-$HOME/.config}
LIMIT_BYTES=2000000  # the whole report, at most

usage() {
    cat <<EOF
Usage: sh report.sh [--watch [MINUTES]] [--hours HOURS]

Writes ~/natter-report-DATE-TIME.txt about $NAME on this computer, for you to read over and
send to whoever asked for it. It sends nothing itself.

  --watch [MINUTES]  also watch $NAME while you make the problem happen
                     (10 minutes unless given, 60 at most; Ctrl+C stops early)
  --hours HOURS      how far back the logs go (24 unless given, 72 at most)
EOF
}

die_usage() {
    printf 'report.sh: %s\n\n' "$1" >&2
    usage >&2
    exit 2
}

# $1 as a whole number without leading zeros ("" when it isn't one); 9999 for anything longer.
whole_number() {
    case $1 in '' | *[!0-9]*) return 0 ;; esac
    wn=$(printf '%s\n' "$1" | sed 's/^0*//')
    if [ "${#wn}" -gt 4 ]; then wn=9999; fi
    printf '%s\n' "${wn:-0}"
}

parse_args() {
    while [ $# -gt 0 ]; do
        case $1 in
            --watch)
                WATCH_ARG=10
                case ${2-} in '' | -*) ;; *) WATCH_ARG=$2; shift ;; esac ;;
            --watch=*) WATCH_ARG=${1#--watch=} ;;
            --hours)
                [ $# -ge 2 ] || die_usage "--hours needs a number of hours"
                HOURS=$2; shift ;;
            --hours=*) HOURS=${1#--hours=} ;;
            -h | --help) usage; exit 0 ;;
            *) die_usage "unknown option: $1" ;;
        esac
        shift
    done
    if [ -n "$WATCH_ARG" ]; then
        WATCH=$(whole_number "$WATCH_ARG")
        if [ -z "$WATCH" ] || [ "$WATCH" -lt 1 ]; then
            die_usage "--watch takes a number of minutes, 1 to 60"
        fi
    fi
    if [ "$WATCH" -gt 60 ]; then
        WATCH=60
        NOTES="${NOTES}The watch was cut to 60 minutes, the most it takes.
"
    fi
    pa_hours=$(whole_number "$HOURS")
    if [ -z "$pa_hours" ] || [ "$pa_hours" -lt 1 ]; then
        die_usage "--hours takes a number of hours, 1 to 72"
    fi
    HOURS=$pa_hours
    if [ "$HOURS" -gt 72 ]; then
        HOURS=72
        NOTES="${NOTES}The logs were cut to the last 72 hours, the most it takes.
"
    fi
}

have() { command -v "$1" >/dev/null 2>&1; }

# A command with a time limit, so one that hangs can't stop the report.
t() {
    if [ -n "$TIMEOUT" ]; then "$TIMEOUT" 30 "$@"; else "$@"; fi
}

say() { printf '%s\n' "$*" >&3; }

heading() { printf '\n\n===== %s =====\n' "$1"; }
part() { printf '\n--- %s\n' "$1"; }

# The first $1 lines of stdin, then how many were left out.
capped() {
    awk -v n="$1" 'NR <= n { print; next } END { if (NR > n) printf "(%d more lines left out)\n", NR - n }'
}

# The last $1 lines of the file $2, after how many older ones were left out.
newest() {
    nw_total=$(wc -l < "$2" | tr -d ' ')
    if [ "$nw_total" -gt "$1" ]; then printf '(the %d oldest lines left out)\n' $((nw_total - $1)); fi
    tail -n "$1" "$2"
}

# The file $2, or as much of its end as fits in $1 bytes, whole lines only.
newest_bytes() {
    if [ "$(wc -c < "$2")" -gt "$1" ]; then
        printf '(the oldest lines left out, to keep the report small)\n'
        tail -c "$1" "$2" | sed 1d
    else
        cat "$2"
    fi
}

# Runs a long-running command ($@) in place of this (sub)shell, stopped after $LIMITED seconds
# when that's set, so a watch's followers can't outlive it.
limited() {
    if [ -n "$TIMEOUT" ] && [ -n "${LIMITED-}" ]; then exec "$TIMEOUT" "$LIMITED" "$@"; fi
    exec "$@"
}

# Byte counts in systemctl's and the cgroups' answers, in MB.
in_mb() {
    awk -F= '/^[A-Za-z]+=[0-9]+$/ && $1 ~ /^Memory/ { printf "%s=%d MB\n", $1, $2 / 1048576 + 0.5; next } { print }'
}

# The only place Natter Den's own folders (WhatsApp's data among them) are touched: their sizes
# and file counts. Nothing in them is opened or read.
sizes_only() {
    for so_dir; do
        if [ -d "$so_dir" ]; then
            printf '%s: %s, %s files\n' "$so_dir" "$(du -sh "$so_dir" 2>/dev/null | cut -f1)" \
                "$(find "$so_dir" -type f 2>/dev/null | wc -l | tr -d ' ')"
        else
            printf '%s: none\n' "$so_dir"
        fi
    done
}

# ---- Redaction -------------------------------------------------------------------------------

# What gets replaced, from this computer: the home folder, user and computer names, and the
# Wi-Fi networks it's on.
redact_setup() {
    R_HOME=$HOME
    # Names that are everyday words, which replacing would garble (a computer called "desktop"
    # would turn xdg-desktop-portal into xdg-<host>-portal): they tell no one who you are.
    rs_skip=" localhost linux natter user users admin root guest live default desktop laptop pc computer home host server workstation "
    for rs_file in /etc/os-release /usr/lib/os-release; do
        if [ -r "$rs_file" ]; then
            rs_skip="$rs_skip $(sed -n 's/^ID=//p; s/^ID_LIKE=//p' "$rs_file" | tr -d '"' | tr '\n' ' ') "
            break
        fi
    done
    R_USERS=$(printf '%s\n' "${USER-}" "${LOGNAME-}" "$(id -un 2>/dev/null)" | sort -u | while IFS= read -r rs_user; do
        rs_low=$(printf '%s\n' "$rs_user" | tr 'A-Z' 'a-z')
        case "$rs_skip" in *" $rs_low "*) continue ;; esac
        printf '%s\n' "$rs_user"
    done)
    R_HOSTS=$( {
        uname -n 2>/dev/null
        if have hostname; then hostname 2>/dev/null; fi
        if [ -r /etc/hostname ]; then head -n 1 /etc/hostname; fi
    } | sort -u | while IFS= read -r rs_host; do
        rs_short=${rs_host%%.*}
        for rs_name in "$rs_host" "$rs_short"; do
            rs_low=$(printf '%s\n' "$rs_name" | tr 'A-Z' 'a-z')
            case "$rs_skip" in *" $rs_low "*) continue ;; esac
            [ "${#rs_name}" -ge 2 ] && printf '%s\n' "$rs_name"
        done
    done | sort -u)
    R_SSIDS=$( {
        if have nmcli; then
            t nmcli -t -f NAME,TYPE connection show --active 2>/dev/null \
                | awk -F: '$NF ~ /wireless/ { sub(/:[^:]*$/, ""); gsub(/\\:/, ":"); print }'
        fi
        if have iwgetid; then iwgetid -r 2>/dev/null; fi
    } | awk 'length($0) >= 3' | sort -u)
    export R_HOME R_USERS R_HOSTS R_SSIDS
}

# stdin with what could tell who or where you are replaced (see redact_setup).
redact() {
    awk '
function wordch(c) { return c ~ /[A-Za-z0-9_]/ }
# s with each `needle` replaced by `repl`; with `wordy`, only where it stands as a word.
function literal(s, needle, repl, wordy,    out, i, b, a, n) {
    n = length(needle)
    if (n == 0) return s
    out = ""
    while ((i = index(s, needle)) > 0) {
        b = i > 1 ? substr(s, i - 1, 1) : ""
        a = substr(s, i + n, 1)
        if (wordy && (wordch(b) || wordch(a))) out = out substr(s, 1, i + n - 1)
        else out = out substr(s, 1, i - 1) repl
        s = substr(s, i + n)
    }
    return out s
}
function emails(s,    out, m) {
    out = ""
    while (match(s, /[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z][A-Za-z]+/)) {
        m = substr(s, RSTART, RLENGTH)
        # systemd units (app-…@….service, user@1000.service) look like addresses
        if (m ~ /\.(service|scope|slice|socket|target|timer|mount|path|swap|device)$/) out = out substr(s, 1, RSTART + RLENGTH - 1)
        else out = out substr(s, 1, RSTART - 1) "<email>"
        s = substr(s, RSTART + RLENGTH)
    }
    return out s
}
# Phone numbers written with a + and spaces or dashes: eight digits or more.
function plusphones(s,    out, m, d) {
    out = ""
    while (match(s, /\+[0-9][0-9 ().-]*[0-9]/)) {
        m = substr(s, RSTART, RLENGTH); d = m
        gsub(/[^0-9]/, "", d)
        if (length(d) >= 8) out = out substr(s, 1, RSTART - 1) "<digits>"
        else out = out substr(s, 1, RSTART + RLENGTH - 1)
        s = substr(s, RSTART + RLENGTH)
    }
    return out s
}
# Eight digits or more in a row, unless part of a word or a hex number (0x7f12345678, 1234kB).
function digits(s,    out, b, a) {
    out = ""
    while (match(s, /[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]+/)) {
        b = RSTART > 1 ? substr(s, RSTART - 1, 1) : ""
        a = substr(s, RSTART + RLENGTH, 1)
        if (b ~ /[A-Za-z_]/ || a ~ /[A-Za-z_]/) out = out substr(s, 1, RSTART + RLENGTH - 1)
        else out = out substr(s, 1, RSTART - 1) "<digits>"
        s = substr(s, RSTART + RLENGTH)
    }
    return out s
}
function ipv4(s,    out, b, a, m) {
    out = ""
    while (match(s, /[0-9][0-9]?[0-9]?\.[0-9][0-9]?[0-9]?\.[0-9][0-9]?[0-9]?\.[0-9][0-9]?[0-9]?/)) {
        m = substr(s, RSTART, RLENGTH)
        b = RSTART > 1 ? substr(s, RSTART - 1, 1) : ""
        a = substr(s, RSTART + RLENGTH, 1)
        if (b ~ /[0-9.]/ || a ~ /[0-9]/ || m == "127.0.0.1" || m == "0.0.0.0") out = out substr(s, 1, RSTART + RLENGTH - 1)
        else out = out substr(s, 1, RSTART - 1) "<ip>"
        s = substr(s, RSTART + RLENGTH)
    }
    return out s
}
# IPv6 addresses: eight groups, or fewer with one "::". Only standing alone, so C++ names
# (WebCore::Page) and times (10:00:00) stay; so do ::1 and ::, like 127.0.0.1. An IPv4 address
# written the IPv6 way (::ffff:<ip>, its IPv4 part already replaced) becomes one <ip>.
function ipv6(s,    out, m, b, a, a2, n, g, i, ok, full, mapped) {
    out = ""
    while (match(s, /[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:[0-9A-Fa-f:]*/)) {
        m = substr(s, RSTART, RLENGTH)
        b = RSTART > 1 ? substr(s, RSTART - 1, 1) : ""
        a = substr(s, RSTART + RLENGTH, 1)
        a2 = substr(s, RSTART + RLENGTH + 1, 1)
        mapped = substr(s, RSTART + RLENGTH, 4) == "<ip>" && m ~ /:$/
        ok = b !~ /[A-Za-z0-9_.]/ && a !~ /[G-Zg-z_]/ && !(a == "." && a2 ~ /[0-9]/)
        n = split(m, g, ":")
        for (i = 1; i <= n; i++) if (length(g[i]) > 4) ok = 0
        if (ok && index(m, "::")) {
            full = substr(m, index(m, "::") + 2)
            if (index(full, "::") || index(m, ":::") || m == "::" || m == "::1") ok = 0
            if (m ~ /^:[^:]/ || (m ~ /[^:]:$/ && !mapped)) ok = 0
        } else if (ok) {
            if (n != 8) ok = 0
            for (i = 1; i <= n; i++) if (g[i] == "") ok = 0
        }
        if (ok) {
            out = out substr(s, 1, RSTART - 1) "<ip>"
            s = substr(s, RSTART + RLENGTH + (mapped ? 4 : 0))
        } else {
            out = out substr(s, 1, RSTART + RLENGTH - 1)
            s = substr(s, RSTART + RLENGTH)
        }
    }
    return out s
}
BEGIN {
    home = ENVIRON["R_HOME"]
    nu = split(ENVIRON["R_USERS"], users, "\n")
    nh = split(ENVIRON["R_HOSTS"], hosts, "\n")
    ns = split(ENVIRON["R_SSIDS"], ssids, "\n")
    h = "[0-9A-Fa-f][0-9A-Fa-f]"
    mac1 = h ":" h ":" h ":" h ":" h ":" h
    mac2 = h "-" h "-" h "-" h "-" h "-" h
    q = "\047"
    ssidword = "[Ss][Ss][Ii][Dd][\"" q "]?[=: ]+(\"[^\"]*\"|" q "[^" q "]*" q "|[^ ,;)]+)"
}
{
    s = $0
    gsub(/debug token [^ ]+/, "debug token <hidden>", s)
    s = emails(s)
    if (length(home) > 1) s = literal(s, home, "~", 0)
    for (i = 1; i <= nh; i++) if (length(hosts[i]) >= 2) s = literal(s, hosts[i], "<host>", 1)
    for (i = 1; i <= nu; i++) if (length(users[i]) >= 2) s = literal(s, users[i], "<user>", 1)
    for (i = 1; i <= ns; i++) if (length(ssids[i]) >= 3) s = literal(s, ssids[i], "<ssid>", 1)
    gsub(ssidword, "SSID <ssid>", s)
    gsub(mac1, "<mac>", s)
    gsub(mac2, "<mac>", s)
    s = ipv4(s)
    s = ipv6(s)
    s = plusphones(s)
    s = digits(s)
    print s
}'
}

# ---- Natter's processes ----------------------------------------------------------------------

# Natter's processes: each "natter", and every process started under one (WebKit's, the call
# window's), plus WebKit processes in a unit named after Natter (the Flatpak's sandbox starts
# those elsewhere). One line each: pid ppid cpu-ticks start-ticks cgroup comm.
SCAN='
{
    line = $0; o = index(line, "("); c = 0
    for (i = length(line); i > o; i--) if (substr(line, i, 1) == ")") { c = i; break }
    if (!o || !c) next
    pid = substr(line, 1, o - 2) + 0
    split(substr(line, c + 2), f, " ")
    ppid[pid] = f[2]; name[pid] = substr(line, o + 1, c - o - 1)
    ticks[pid] = f[12] + f[13]; start[pid] = f[20]; pids[++np] = pid
}
END {
    for (i = 1; i <= np; i++) if (name[pids[i]] == "natter") root[pids[i]] = 1
    for (i = 1; i <= np; i++) {
        p = pids[i]; q = p
        for (k = 0; q > 1 && k < 64; k++) { if (q in root) { keep[p] = 1; break } q = ppid[q] }
    }
    for (i = 1; i <= np; i++) {
        p = pids[i]; cg = ""
        helper = name[p] ~ /^(WebKit|bwrap|QtWebEngine|xdg-dbus-proxy)/
        if (keep[p] || helper) {
            cgf = ENVIRON["R_PROC"] "/" p "/cgroup"
            while ((getline l < cgf) > 0) if (l ~ /^0::/) cg = substr(l, 4)
            close(cgf)
        }
        if (!keep[p] && helper && cg ~ /[Nn]atter/) keep[p] = 1
        if (keep[p]) print p, ppid[p], ticks[p], start[p], (cg == "" ? "-" : cg), name[p]
    }
}'

scan_natter() {
    cat "$R_PROC"/[0-9]*/stat 2>/dev/null | awk "$SCAN"
}

# The systemd unit a cgroup path belongs to: its last part that's a scope or a service.
UNIT_OF='
function unit_of(cg,    n, parts, i) {
    n = split(cg, parts, "/")
    for (i = n; i > 0; i--) if (parts[i] ~ /\.(scope|service)$/) return parts[i]
    return cg
}'

uptime_s() { cut -d' ' -f1 "${1:-$R_PROC}/uptime" 2>/dev/null || echo 0; }

# One line per Natter process, from scan_natter's lines on stdin.
PROCLIST="$UNIT_OF"'
{
    pid = $1; cg = $5; comm = $6
    rss = swap = 0; f = ENVIRON["R_PROC"] "/" pid "/status"
    while ((getline l < f) > 0) {
        split(l, a, " ")
        if (a[1] == "VmRSS:") rss = a[2]; else if (a[1] == "VmSwap:") swap = a[2]
    }
    close(f)
    age = up - $4 / hz; if (age < 0) age = 0
    printf "%-16s pid %-7s parent %-7s running %dh %02dm  memory %d MB  swap %d MB  unit %s\n", comm, pid, $2, age / 3600, (age % 3600) / 60, rss / 1024 + 0.5, swap / 1024 + 0.5, unit_of(cg)
}'

# A watch sample: Natter's processes (memory, swap, CPU since the last sample), its cgroups and
# the system's memory, then which processes started or ended since the last sample. Its files:
# the last sample's "pid ticks comm" lines, then scan_natter's lines.
SAMPLE="$UNIT_OF"'
function readline(f,    v) { v = ""; if ((getline v < f) <= 0) v = ""; close(f); return v }
function mb(v) { return v ~ /^[0-9]+$/ ? sprintf("%d MB", v / 1048576 + 0.5) : (v == "" ? "?" : v) }
FILENAME == ARGV[1] { prev[$1] = $2; prevname[$1] = $3; next }
{
    pid = $1; cg = $5; comm = $6
    rss = swap = 0; f = ENVIRON["R_PROC"] "/" pid "/status"
    while ((getline l < f) > 0) {
        split(l, a, " ")
        if (a[1] == "VmRSS:") rss = a[2]; else if (a[1] == "VmSwap:") swap = a[2]
    }
    close(f)
    cpu = "-"
    if (dt > 0 && (pid in prev)) cpu = sprintf("%.1f%%", ($3 - prev[pid]) * 100 / hz / dt)
    parts = parts sprintf("%s%s %s: %d MB, swap %d MB, cpu %s", n ? " | " : "", comm, pid, rss / 1024 + 0.5, swap / 1024 + 0.5, cpu)
    totr += rss; tots += swap; n++
    now[pid] = 1
    if (cg != "-" && !(cg in seen)) { seen[cg] = 1; cgs[++ncg] = cg }
    if (first != "yes" && !(pid in prev)) born = born (born == "" ? "" : ", ") comm " " pid
}
END {
    if (n) printf "%s sample: %d processes, %d MB, swap %d MB: %s\n", ts, n, totr / 1024 + 0.5, tots / 1024 + 0.5, parts
    else printf "%s sample: %s is not running\n", ts, name
    for (i = 1; i <= ncg; i++) {
        d = ENVIRON["R_CGROOT"] cgs[i]
        ev = ""; f = d "/memory.events"
        while ((getline l < f) > 0) { split(l, a, " "); ev = ev " " a[1] "=" a[2] }
        close(f)
        printf "%s cgroup %s: memory %s, swap %s, events%s\n", ts, unit_of(cgs[i]), mb(readline(d "/memory.current")), mb(readline(d "/memory.swap.current")), ev == "" ? " ?" : ev
    }
    avail = sfree = 0
    while ((getline l < "/proc/meminfo") > 0) {
        split(l, a, " ")
        if (a[1] == "MemAvailable:") avail = a[2]; else if (a[1] == "SwapFree:") sfree = a[2]
    }
    close("/proc/meminfo")
    psi = ""
    while ((getline l < "/proc/pressure/memory") > 0) { split(l, a, " "); psi = psi " " a[1] " " a[2] }
    close("/proc/pressure/memory")
    printf "%s system: %d MB available, swap free %d MB%s\n", ts, avail / 1024 + 0.5, sfree / 1024 + 0.5, psi == "" ? "" : ", memory pressure" psi
    if (first != "yes") for (p in prevname) if (!(p in now)) gone = gone (gone == "" ? "" : ", ") prevname[p] " " p
    if (born != "") printf "%s change: started %s\n", ts, born
    if (gone != "") printf "%s change: ended %s\n", ts, gone
}'

# ---- Natter's log ----------------------------------------------------------------------------

# Natter's lines from journalctl's short-iso-precise output, without the computer's name, and
# only those of these kinds: Natter's own ("natter: …"), GLib's, WebKit's and GStreamer's
# warnings and errors, Python's tracebacks (the exception's type, never its message, which could
# hold a name), crash and graphics driver errors, and systemd's starts and stops of Natter's units.
JFILTER='
function keep(ts, id, msg,    l) {
    l = ts " " id ": " msg
    if (length(l) > 400) l = substr(l, 1, 400) " ..."
    print l
}
# The line that ends a traceback: the exception type, and its message hidden whole, quoted
# or not, as it could hold a name.
function exception_only(s,    i, t) {
    i = index(s, ": ")
    t = i ? substr(s, 1, i - 1) : s
    if (t !~ /^[A-Za-z_][A-Za-z0-9_.]*$/) return "<message hidden>"
    return i ? t ": <message hidden>" : t
}
!/^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T/ { next }
{
    ts = $1
    rest = $0; sub(/^[^ ]+ [^ ]+ /, "", rest)
    i = index(rest, ": "); if (!i) next
    id = substr(rest, 1, i - 1); msg = substr(rest, i + 2)
    low = tolower(msg)
    if (low ~ /(title|body|summary)["\047]?[ \t]*[:=]/) { tb[id] = 0; next }
    if (msg ~ /^CONSOLE / || low ~ /^console[: ]/ || msg ~ /eval (→|->)/) next
    if (tb[id]) {
        if (msg ~ /^  / || msg == "" || msg ~ /^(During handling of the above exception|The above exception was the direct cause)/) {
            if (tbn[id]++ < 80) keep(ts, id, msg)
            next
        }
        tb[id] = 0
        keep(ts, id, exception_only(msg))
        next
    }
    if (msg == "Traceback (most recent call last):") { tb[id] = 1; tbn[id] = 0; keep(ts, id, msg); next }
    if (msg ~ /^natter: /) { keep(ts, id, msg); next }
    if (msg ~ /^(\*\* )?(\([^)]*:[0-9]+\): )?([A-Za-z0-9_-]+-)?(WARNING|CRITICAL|ERROR) \*\*/) { keep(ts, id, msg); next }
    if (msg ~ /^[0-9]+:[0-9][0-9]:[0-9][0-9]\.[0-9]+ +[0-9]+ +0x[0-9a-fA-F]+ +(WARN|ERROR) /) { keep(ts, id, msg); next }
    if (msg ~ /^(lib)?EGL (warning|error)|^MESA|^Mesa|^libva (error|info: va_openDriver)|^(WARNING|ERROR|FATAL|CRITICAL): |Segmentation fault|Aborted|core dumped|ASSERTION FAILED|SIGXCPU|RLIMIT_RTTIME|^bwrap: |\.py:[0-9]+: [A-Za-z]*Warning: /) { keep(ts, id, msg); next }
    if (id ~ /^systemd(\[[0-9]+\])?$/ && msg ~ /^(Started|Starting|Stopped|Stopping|Finished|Failed to start) |Deactivated successfully|Consumed .*CPU time|Failed with result|Main process exited|killed by the OOM killer|code=killed|status=[0-9]+\//) { keep(ts, id, msg); next }
}'

# The kernel's and memory killers' lines about running out of memory or real-time time.
KILLS='oom-kill|out of memory|oom_reaper|invoked oom-killer|killed process|memory cgroup out of memory|sigxcpu|xcpu|rlimit_rttime|rttime'

# The units Natter has run in, from the user journal, and the ones it runs in now.
natter_units() {
    {
        journalctl --user -F _SYSTEMD_USER_UNIT 2>/dev/null
        journalctl --user -F USER_UNIT 2>/dev/null
    } | grep -E '[Nn]atter' | sort -u | tail -n 400
    if [ -s "$T/procs" ]; then awk "$UNIT_OF"'{ u = unit_of($5); if (u ~ /\.(scope|service)$/) print u }' "$T/procs" | sort -u; fi
}

# Natter's lines in the user journal: from processes called natter, Natter's identifiers, and the
# units in the file $1. $2: "exec" to become journalctl. The rest: journalctl's options.
journal_natter() {
    jn_units=$1 jn_exec=$2
    shift 2
    set -- "$@" _COMM=natter + "SYSLOG_IDENTIFIER=$APP_ID" "SYSLOG_IDENTIFIER=$OLD_APP_ID" SYSLOG_IDENTIFIER=natter
    if [ -s "$jn_units" ]; then
        set -- "$@" +
        while IFS= read -r jn_u; do set -- "$@" "_SYSTEMD_USER_UNIT=$jn_u"; done < "$jn_units"
        set -- "$@" +
        while IFS= read -r jn_u; do set -- "$@" "USER_UNIT=$jn_u"; done < "$jn_units"
    fi
    if [ "$jn_exec" = exec ]; then limited journalctl --user --no-pager -q -o short-iso-precise "$@"; fi
    journalctl --user --no-pager -q -o short-iso-precise "$@"
}

# ---- 1. Natter Den itself --------------------------------------------------------------------

# The main group of a desktop entry: how it starts Natter, and how many menu actions it has (their
# names are chats' names, so they aren't shown).
ENTRY='
/^\[/ { group = $0; if (group ~ /^\[Desktop Action /) actions++; next }
group == "[Desktop Entry]" && /^(Exec|TryExec|NoDisplay|Hidden|DBusActivatable|X-Flatpak|StartupWMClass|X-GNOME-Autostart-enabled|X-KDE-autostart-after|X-GNOME-UsesNotifications)=/ { print "    " $0 }
/^Exec=/ && group == "[D-BUS Service]" { print "    " $0 }
END { if (actions) printf "    (and %d menu actions, not shown: they are named after chats)\n", actions }'

natter_version_in() {
    sed -n 's/^VERSION = "\([^"]*\)".*/\1/p' "$1/natter/__init__.py" 2>/dev/null | head -n 1
}

resolved() {
    if have readlink && readlink -f "$1" 2>/dev/null; then return; fi
    printf '%s\n' "$1"
}

# The version of the natter command $1, from the natter/__init__.py it runs: its NATTER_DIR, or
# the checkout it's in. Natter itself isn't started (even --version would make and read its
# folders before it answers).
command_version() {
    cv_dir=$(sed -n 's/^NATTER_DIR = "\(.*\)".*/\1/p' "$1" 2>/dev/null | head -n 1)
    if [ -z "$cv_dir" ]; then
        cv_dir=$(resolved "$1")
        cv_dir=${cv_dir%/*}
        cv_dir=${cv_dir%/*}
    fi
    natter_version_in "$cv_dir"
}

section_natter() {
    heading "1. $NAME itself"
    part "The natter command"
    launcher="$HOME/.local/bin/natter"
    nd=""
    if [ -f "$launcher" ]; then
        nd=$(sed -n 's/^NATTER_DIR = "\(.*\)".*/\1/p' "$launcher" | head -n 1)
        printf '%s (runs with %s)\n' "$launcher" "$(head -n 1 "$launcher" | sed 's/^#!//')"
        printf 'NATTER_DIR: %s\n' "${nd:-(empty: the folder the command is in)}"
        if [ -n "$nd" ]; then
            if [ "$nd" = "$HOME/.local/lib/natter/source" ]; then
                INSTALL_KIND="get.sh (the latest release)"
            elif [ -e "$nd/.git" ]; then
                INSTALL_KIND="install.sh in a git checkout"
            else
                INSTALL_KIND="install.sh in another folder (an old zip or a copy)"
            fi
            printf 'Installed by: %s\n' "$INSTALL_KIND"
            if [ -d "$nd" ]; then
                sn_version=$(natter_version_in "$nd")
                printf 'Version there: %s\n' "${sn_version:-unknown}"
                [ -n "$sn_version" ] && NATTER_VERSION=$sn_version
                if [ -e "$nd/.git" ] && have git; then
                    printf 'Git: %s, %s\n' "$(t git --no-optional-locks -C "$nd" describe --tags --always --dirty 2>&1 | head -n 1)" \
                        "$(t git --no-optional-locks -C "$nd" log -1 --format='%h %cs %s' 2>&1 | head -n 1)"
                fi
            else
                printf 'That folder is missing: the natter command fails.\n'
            fi
        fi
    else
        printf '%s: none\n' "$launcher"
    fi
    if have natter; then
        sn_cmd=$(command -v natter)
        sn_v=$(command_version "$sn_cmd")
        printf 'natter on PATH: %s, version %s (read from its files: Natter is not started)\n' "$sn_cmd" "${sn_v:-unknown}"
        [ -n "$sn_v" ] && NATTER_VERSION=$sn_v
    else
        printf 'No natter command on PATH.\n'
    fi

    part "Every copy installed"
    sn_copies=0
    sn_seen=""
    sn_owned=" "
    sn_ifs=$IFS; IFS=:
    for sn_dir in $PATH; do
        IFS=$sn_ifs
        [ -n "$sn_dir" ] && [ -f "$sn_dir/natter" ] && [ -x "$sn_dir/natter" ] || continue
        # The same file through a link (/bin and /usr/bin, merged) is one copy.
        sn_real=$(resolved "$sn_dir/natter")
        case "$sn_seen " in *" $sn_real "*) continue ;; esac
        sn_seen="$sn_seen $sn_real"
        sn_copies=$((sn_copies + 1))
        sn_where=$(sed -n 's/^NATTER_DIR = "\(.*\)".*/\1/p' "$sn_dir/natter" 2>/dev/null | head -n 1)
        sn_owner=""
        if have pacman; then sn_owner=$(pacman -Qqo "$sn_dir/natter" 2>/dev/null | head -n 1); fi
        [ -n "$sn_owner" ] && sn_owned="$sn_owned$sn_owner "
        printf 'command %s%s%s\n' "$sn_dir/natter" "${sn_where:+ (NATTER_DIR $sn_where)}" "${sn_owner:+ (package $sn_owner)}"
    done
    IFS=$sn_ifs
    if [ -d "$HOME/.local/lib/natter/source" ]; then
        sn_v=$(natter_version_in "$HOME/.local/lib/natter/source")
        printf 'get.sh copy: ~/.local/lib/natter/source, version %s\n' "${sn_v:-unknown}"
    fi
    for sn_lib in "$HOME/.local/lib/natter/gstreamer-1.0/libnattercamfix.so"; do
        if [ -f "$sn_lib" ]; then printf 'Camera fix: %s (%s bytes)\n' "$sn_lib" "$(wc -c < "$sn_lib" | tr -d ' ')"
        else printf 'Camera fix: not built\n'; fi
    done
    if have flatpak; then
        for sn_inst in --user --system; do
            if sn_info=$(t flatpak info "$sn_inst" "$APP_ID" 2>/dev/null) && [ -n "$sn_info" ]; then
                sn_copies=$((sn_copies + 1))
                printf 'Flatpak (%s):\n' "${sn_inst#--}"
                printf '%s\n' "$sn_info" | grep -E '^ *(ID|Ref|Arch|Branch|Version|Origin|Collection|Installation|Installed|Runtime|Sdk|Commit|Date|Subject|Alt-id):' \
                    | sed 's/^ */    /' | capped 20
                sn_fv=$(printf '%s\n' "$sn_info" | sed -n 's/^ *Version: *//p' | head -n 1)
                [ -z "${NATTER_VERSION-}" ] && [ -n "$sn_fv" ] && NATTER_VERSION=$sn_fv
                [ -z "${INSTALL_KIND-}" ] && INSTALL_KIND="the Flatpak"
            else
                printf 'Flatpak (%s): not installed\n' "${sn_inst#--}"
            fi
        done
    else
        printf 'Flatpak: flatpak is not installed\n'
    fi
    if have pacman; then
        sn_pkgs=0
        for sn_pkg in natter-den-git natter-den natter; do
            if sn_q=$(pacman -Q "$sn_pkg" 2>/dev/null); then
                sn_pkgs=1
                # Counted already when its command is on PATH.
                case "$sn_owned" in *" $sn_pkg "*) ;; *) sn_copies=$((sn_copies + 1)) ;; esac
                printf 'Package: %s (installed %s)\n' "$sn_q" "$(LC_ALL=C pacman -Qi "$sn_pkg" 2>/dev/null | sed -n 's/^Install Date *: *//p')"
                [ -z "${INSTALL_KIND-}" ] && INSTALL_KIND="the AUR package"
            fi
        done
        [ "$sn_pkgs" = 1 ] || printf 'Package: no natter-den-git, natter-den or natter package\n'
    fi
    printf 'Copies found: %d%s\n' "$sn_copies" "$([ "$sn_copies" -gt 1 ] && printf ' (more than one: the menu, links and notifications may start a different one)')"

    part "Menu entries (the first of each name is the one used)"
    sn_dirs="$DATA_HOME:${XDG_DATA_DIRS:-/usr/local/share:/usr/share}"
    sn_ids=" "
    sn_found=0
    IFS=:
    for sn_dir in $sn_dirs; do
        IFS=$sn_ifs
        for sn_entry in "$sn_dir"/applications/*[Nn]atter*.desktop; do
            [ -f "$sn_entry" ] || continue
            sn_found=1
            sn_id=${sn_entry##*/}
            case "$sn_ids" in
                *" $sn_id "*) printf '%s (hidden by the one above)\n' "$sn_entry" ;;
                *) printf '%s (used)\n' "$sn_entry"; sn_ids="$sn_ids$sn_id " ;;
            esac
            awk "$ENTRY" "$sn_entry" | capped 12
        done
    done
    IFS=$sn_ifs
    for sn_dir in "$DATA_HOME/flatpak/exports/share" /var/lib/flatpak/exports/share; do
        case ":$sn_dirs:" in *":$sn_dir:"* | *":$sn_dir/:"*) continue ;; esac
        for sn_entry in "$sn_dir"/applications/*[Nn]atter*.desktop; do
            [ -f "$sn_entry" ] && printf '%s (not in XDG_DATA_DIRS: the menu does not see it)\n' "$sn_entry" && sn_found=1
        done
    done
    [ "$sn_found" = 1 ] || printf 'none found\n'
    for sn_entry in "$CONFIG_HOME/autostart/$APP_ID.desktop" "$CONFIG_HOME/autostart/$OLD_APP_ID.desktop"; do
        if [ -f "$sn_entry" ]; then printf 'Autostart: %s\n' "$sn_entry"; awk "$ENTRY" "$sn_entry" | capped 8
        else printf 'Autostart: no %s\n' "${sn_entry##*/}"; fi
    done
    for sn_entry in "$DATA_HOME/dbus-1/services/$APP_ID.service" /usr/share/dbus-1/services/"$APP_ID".service; do
        if [ -f "$sn_entry" ]; then printf 'D-Bus service: %s\n' "$sn_entry"; awk "$ENTRY" "$sn_entry" | capped 4; fi
    done

    part "Running now"
    hz=$(getconf CLK_TCK 2>/dev/null || echo 100)
    NATTER_RUNNING=$(wc -l < "$T/procs" | tr -d ' ')
    if [ "$NATTER_RUNNING" -gt 0 ]; then
        awk -v up="$(uptime_s)" -v hz="$hz" "$PROCLIST" "$T/procs" | capped 40
        awk "$UNIT_OF"'{ print $5 }' "$T/procs" | sort -u | while IFS= read -r sn_cg; do
            [ "$sn_cg" = - ] && continue
            sn_unit=$(printf '%s\n' "$sn_cg" | awk "$UNIT_OF"'{ print unit_of($0) }')
            printf '\nUnit %s:\n' "$sn_unit"
            if have systemctl; then
                t systemctl --user show "$sn_unit" -p Id -p ActiveState -p OOMPolicy -p MemoryMax -p MemoryHigh \
                    -p MemorySwapMax -p MemoryCurrent -p MemorySwapCurrent -p MemoryPeak -p ManagedOOMMemoryPressure \
                    2>&1 | in_mb | sed 's/^/    /' | capped 12
            fi
            if [ -r "$R_CGROOT$sn_cg/memory.events" ]; then
                printf '    memory.events: %s\n' "$(tr '\n' ' ' < "$R_CGROOT$sn_cg/memory.events")"
            fi
        done
        awk '$6 == "natter" { print $1 }' "$T/procs" | while read -r sn_pid; do
            printf '\nLimits of natter %s:\n' "$sn_pid"
            grep -E '^Max (realtime|rt)' "$R_PROC/$sn_pid/limits" 2>/dev/null | sed 's/^/    /'
        done
    else
        printf '%s is not running.\n' "$NAME"
    fi
    if have gdbus; then
        printf "%s on the session bus: %s\n" "$APP_ID" "$(has_owner "$APP_ID" && echo yes || echo no)"
    fi

    part "Natter's folders (sizes and file counts only: nothing in them is read)"
    sizes_only "$DATA_HOME/natter" "$CACHE_HOME/natter" "$CONFIG_HOME/natter" "$HOME/.var/app/$APP_ID"
}

# ---- 2. The system ---------------------------------------------------------------------------

section_system() {
    heading "2. The system"
    for ss_file in /etc/os-release /usr/lib/os-release; do
        if [ -r "$ss_file" ]; then
            grep -E '^(NAME|PRETTY_NAME|VERSION_ID|ID|ID_LIKE|VARIANT_ID|BUILD_ID)=' "$ss_file"
            break
        fi
    done
    printf 'Kernel: %s (%s)\n' "$(uname -r)" "$(uname -m)"
    printf 'Desktop: XDG_CURRENT_DESKTOP=%s XDG_SESSION_TYPE=%s DESKTOP_SESSION=%s\n' \
        "${XDG_CURRENT_DESKTOP-}" "${XDG_SESSION_TYPE-}" "${DESKTOP_SESSION-}"
    printf 'WAYLAND_DISPLAY: %s, DISPLAY: %s\n' "$([ -n "${WAYLAND_DISPLAY-}" ] && echo set || echo unset)" \
        "$([ -n "${DISPLAY-}" ] && echo set || echo unset)"

    part "CPU and memory"
    awk -F': *' '/^model name/ { print "CPU: " $2; exit }' /proc/cpuinfo 2>/dev/null
    printf 'CPUs: %s\n' "$(getconf _NPROCESSORS_ONLN 2>/dev/null)"
    awk '$1 ~ /^(MemTotal|MemAvailable|SwapTotal|SwapFree|Zswap|Zswapped):$/ { printf "%s %d MB\n", $1, $2 / 1024 + 0.5 }' /proc/meminfo 2>/dev/null
    if have swapon; then part "swapon --show"; t swapon --show 2>&1 | capped 12; fi
    if have zramctl; then part "zramctl"; t zramctl 2>&1 | capped 12; fi
    part "Memory settings"
    for ss_file in /sys/module/zswap/parameters/enabled /sys/module/zswap/parameters/compressor \
        /sys/module/zswap/parameters/max_pool_percent /proc/sys/vm/swappiness /proc/sys/vm/page-cluster \
        /proc/sys/vm/vfs_cache_pressure /proc/sys/vm/overcommit_memory /sys/kernel/mm/lru_gen/enabled \
        /sys/kernel/mm/transparent_hugepage/enabled; do
        [ -r "$ss_file" ] && printf '%s: %s\n' "$ss_file" "$(head -n 1 "$ss_file" 2>/dev/null)"
    done
    if have systemctl; then
        for ss_unit in systemd-oomd earlyoom nohang ananicy-cpp rtkit-daemon; do
            printf '%s: %s\n' "$ss_unit" "$(t systemctl is-active "$ss_unit" 2>/dev/null | head -n 1)"
        done
        t systemctl show "user@$(id -u).service" -p ManagedOOMMemoryPressure -p ManagedOOMMemoryPressureLimit \
            -p ManagedOOMSwap 2>/dev/null | sed 's/^/user@.service /'
    fi
    [ -r /etc/default/earlyoom ] && grep -E '^EARLYOOM_ARGS' /etc/default/earlyoom

    part "Graphics"
    if have lspci; then
        t lspci -k 2>/dev/null | awk '/^[^ \t]/ { show = ($0 ~ /VGA|3D|Display/) } show' | capped 20
    else
        printf 'lspci is not installed\n'
    fi
    if have glxinfo; then
        t glxinfo -B 2>&1 | grep -E 'direct rendering|OpenGL (vendor|renderer|version|core profile version)|Accelerated|Video memory' | capped 8
    fi
    if have eglinfo; then
        t eglinfo -B 2>&1 | grep -iE 'platform:|renderer|OpenGL core profile version' | capped 12
    fi
    if have vainfo; then
        t vainfo 2>&1 | grep -E 'Driver version|VAProfileH264|VAProfileVP8|VAProfileVP9|VAProfileAV1' | capped 12
    fi

    part "Packages"
    if have pacman; then
        LC_ALL=C pacman -Q webkitgtk-6.0 gtk4 libadwaita gstreamer gst-plugins-base gst-plugins-base-libs gst-plugins-good \
            gst-plugins-bad gst-plugins-bad-libs gst-libav gst-plugin-va gst-plugin-pipewire libnice pipewire \
            pipewire-pulse wireplumber python python-gobject xdg-desktop-portal xdg-desktop-portal-kde \
            xdg-desktop-portal-gtk xdg-desktop-portal-gnome plasma-workspace kwin mesa python-pyqt6-webengine 2>&1 \
            | sed "s/^error: package '\(.*\)' was not found/\1: not installed/"
    elif have dpkg-query; then
        for ss_pkg in libwebkitgtk-6.0-4 gir1.2-webkit-6.0 libgtk-4-1 libadwaita-1-0 gstreamer1.0-plugins-base \
            gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-nice \
            gstreamer1.0-pipewire gstreamer1.0-vaapi pipewire wireplumber python3-gi xdg-desktop-portal \
            xdg-desktop-portal-kde xdg-desktop-portal-gnome xdg-desktop-portal-gtk; do
            ss_v=$(dpkg-query -W -f='${Version}' "$ss_pkg" 2>/dev/null)
            printf '%s %s\n' "$ss_pkg" "${ss_v:-not installed}"
        done
    elif have rpm; then
        rpm -q webkitgtk6.0 gtk4 libadwaita gstreamer1 gstreamer1-plugins-base gstreamer1-plugins-good \
            gstreamer1-plugins-bad-free gstreamer1-plugins-bad-freeworld gstreamer1-plugin-libav \
            gstreamer1-plugin-openh264 libnice-gstreamer1 pipewire pipewire-gstreamer wireplumber python3-gobject \
            xdg-desktop-portal xdg-desktop-portal-kde xdg-desktop-portal-gnome 2>&1
    else
        printf 'No pacman, dpkg or rpm here.\n'
    fi

    part "GStreamer (the system's, which the Flatpak doesn't use)"
    if have gst-inspect-1.0; then
        t gst-inspect-1.0 --version 2>&1 | head -n 2
        for ss_el in webrtcbin nicesrc opusdec vah264dec avdec_h264 pipewiresrc pulsesink; do
            if t gst-inspect-1.0 "$ss_el" >/dev/null 2>&1; then printf '%s: yes\n' "$ss_el"
            else printf '%s: missing\n' "$ss_el"; fi
        done
    else
        printf 'gst-inspect-1.0 is not installed\n'
    fi
}

# ---- 3. Natter's log -------------------------------------------------------------------------

LOG_LINES=0
WARNINGS=0
TRACEBACKS=0

section_logs() {
    heading "3. $NAME's log, the last $HOURS hours"
    if ! have journalctl; then
        printf 'journalctl is not available here: skipped.\n'
        return
    fi
    natter_units > "$T/units"
    printf 'Units Natter has run in (from the journal): %s\n' "$(wc -l < "$T/units" | tr -d ' ')"
    journal_natter "$T/units" "" "--since=-${HOURS}h" 2>/dev/null | awk "$JFILTER" > "$T/log"
    LOG_LINES=$(wc -l < "$T/log" | tr -d ' ')
    WARNINGS=$(grep -cE 'WARNING|CRITICAL|ERROR|WARN ' "$T/log")
    TRACEBACKS=$(grep -c 'Traceback (most recent call last)' "$T/log")
    printf "Lines kept: %s (Natter's own: %s, warnings and errors: %s, Python tracebacks: %s)\n" \
        "$LOG_LINES" "$(grep -c ' natter: ' "$T/log")" "$WARNINGS" "$TRACEBACKS"
    if [ "$LOG_LINES" = 0 ]; then
        printf '%s\n' "None: Natter Den wasn't started from the menu in that time, or the journal doesn't keep" \
            "user logs here. Started from a terminal, its log goes to that terminal instead."
    fi
    part "Starts and quits"
    grep -E ' systemd(\[[0-9]+\])?: | natter: (quitting|asked to stop|restarting|couldn.t start again|couldn.t open the window)' "$T/log" > "$T/starts"
    newest 200 "$T/starts"
    part "Log lines (the newest 3000, as many as fit)"
    newest 3000 "$T/log" > "$T/s3.log"
}

# ---- 4. Crashes ------------------------------------------------------------------------------

CRASHES=0

# The newest crash of the process called $1: its signal, where and when, and the first 25
# frames of the crashing thread's stack.
CRASH='
/^ *(PID|Signal|Timestamp|Executable|Control Group|User Unit|Unit|Storage|Size on Disk):/ { print; next }
/^ *Stack trace of thread/ { stacks++; inside = stacks == 1; if (inside) print; next }
inside && /^ *#[0-9]+ / { if (frames++ < 25) print; next }
inside && /^ *$/ { inside = 0 }'

section_crashes() {
    heading "4. Crashes, the last $HOURS hours"
    if ! have coredumpctl; then
        printf 'coredumpctl is not available here: skipped.\n'
        return
    fi
    t coredumpctl --no-pager --no-legend list "--since=-${HOURS}h" 2>&1 > "$T/cores.all" | grep -iE 'hint|permission|denied' | head -n 3
    # Natter's crashes show Python as their program: one counts only when its command line or
    # unit names Natter (the natter command, or python -m natter.callwindow), and those aren't
    # shown. Other Python programs' crashes are left out.
    : > "$T/cores"
    sc_checked=0
    while IFS= read -r sc_line; do
        case $sc_line in
            *natter* | *WebKit* | *QtWebEngine*) printf '%s\n' "$sc_line" >> "$T/cores" ;;
            *python*)
                [ "$sc_checked" -lt 30 ] || continue
                sc_checked=$((sc_checked + 1))
                sc_pid=$(printf '%s\n' "$sc_line" | awk '{ for (i = 1; i + 2 <= NF; i++) if ($i ~ /^[0-9]+$/ && $(i + 1) ~ /^[0-9]+$/ && $(i + 2) ~ /^[0-9]+$/) { print $i; exit } }')
                [ -n "$sc_pid" ] || continue
                if t coredumpctl --no-pager info "--since=-${HOURS}h" "COREDUMP_PID=$sc_pid" 2>/dev/null < /dev/null \
                    | grep -qiE '^ *(Command Line|Control Group|Unit|User Unit): .*natter'; then
                    printf '%s\n' "$sc_line" >> "$T/cores"
                fi ;;
        esac
    done < "$T/cores.all"
    CRASHES=$(wc -l < "$T/cores" | tr -d ' ')
    if [ "$CRASHES" = 0 ]; then
        printf 'No crashes of Natter Den or WebKit found.\n'
        return
    fi
    printf 'TIME PID UID GID SIGNAL COREFILE EXECUTABLE SIZE\n'
    newest 60 "$T/cores"
    # The kernel keeps 15 characters of a process's name.
    for sc_comm in natter WebKitWebProces WebKitNetworkPr WebKitGPUProces; do
        t coredumpctl --no-pager info -1 "--since=-${HOURS}h" "COREDUMP_COMM=$sc_comm" 2>/dev/null > "$T/core.info" || continue
        [ -s "$T/core.info" ] || continue
        part "The newest crash of $sc_comm"
        awk "$CRASH" "$T/core.info" | capped 45
    done
}

# ---- 5. Memory pressure and kills ------------------------------------------------------------

KILLED=0

section_memory() {
    heading "5. Out of memory, and other kills, the last $HOURS hours"
    if ! have journalctl; then
        printf 'journalctl is not available here: skipped.\n'
        return
    fi
    part "The kernel"
    : > "$T/kernel"
    if [ -n "$(journalctl -k -n 1 -q --no-pager 2>/dev/null)" ]; then
        journalctl -k -q --no-pager -o short-iso-precise "--since=-${HOURS}h" 2>/dev/null | grep -iE "$KILLS" \
            | sed 's/^\([^ ]*\) [^ ]* /\1 /' > "$T/kernel"
    elif have dmesg && [ -n "$(dmesg 2>/dev/null | head -n 1)" ]; then
        printf '(from dmesg: everything since the computer started)\n'
        dmesg 2>/dev/null | grep -iE "$KILLS" > "$T/kernel"
    else
        printf "The kernel's messages aren't readable without admin rights here (that's fine).\n"
    fi
    KILLED=$(grep -ciE 'killed process' "$T/kernel")
    newest 200 "$T/kernel"
    [ -s "$T/kernel" ] || printf 'No out-of-memory kills.\n'
    part "Memory killers and rtkit (lines naming Natter or WebKit, and rtkit's warnings)"
    journalctl -q --no-pager -o short-iso-precise "--since=-${HOURS}h" -t systemd-oomd -t earlyoom -t nohang 2>/dev/null \
        | grep -iE 'natter|webkit' | sed 's/^\([^ ]*\) [^ ]* /\1 /' | tail -n 100
    journalctl -q --no-pager -o short-iso-precise "--since=-${HOURS}h" -t rtkit-daemon 2>/dev/null \
        | grep -iE 'starv|demot|refus|fail|exceed' | sed 's/^\([^ ]*\) [^ ]* /\1 /' | tail -n 50
    part "Natter's units killed, out of memory or out of real-time time"
    grep -iE 'oom|xcpu|rttime|realtime|ended for its memory|status=9/KILL' "$T/log" 2>/dev/null | tail -n 100
}

# ---- 6. Notifications and the background -----------------------------------------------------

dbus_pid() {
    t gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
        --method org.freedesktop.DBus.GetConnectionUnixProcessID "$1" 2>/dev/null | sed -n 's/^(uint32 \([0-9]*\),)$/\1/p'
}

prop() {  # $1 bus name, $2 object path, $3 interface, $4 property
    t gdbus call --session --dest "$1" --object-path "$2" --method org.freedesktop.DBus.Properties.Get "$3" "$4" 2>&1 | head -n 1
}

has_owner() {
    t gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
        --method org.freedesktop.DBus.NameHasOwner "$1" 2>/dev/null | grep -q true
}

section_notify() {
    heading "6. Notifications and running in the background"
    if ! have gdbus; then
        printf 'gdbus is not available here: skipped.\n'
        return
    fi
    part "The notification server"
    if has_owner org.freedesktop.Notifications; then
        sn_pid=$(dbus_pid org.freedesktop.Notifications)
        printf 'Owned by: %s (pid %s)\n' "$(cat "$R_PROC/$sn_pid/comm" 2>/dev/null)" "$sn_pid"
        printf 'Server: %s\n' "$(t gdbus call --session --dest org.freedesktop.Notifications --object-path /org/freedesktop/Notifications \
            --method org.freedesktop.Notifications.GetServerInformation 2>&1 | head -n 1)"
        printf 'Capabilities: %s\n' "$(t gdbus call --session --dest org.freedesktop.Notifications --object-path /org/freedesktop/Notifications \
            --method org.freedesktop.Notifications.GetCapabilities 2>&1 | head -n 1)"
        printf 'Do not disturb (Inhibited): %s\n' "$(prop org.freedesktop.Notifications /org/freedesktop/Notifications org.freedesktop.Notifications Inhibited)"
    else
        printf 'Nothing owns org.freedesktop.Notifications: notifications cannot show.\n'
    fi
    part "The tray (StatusNotifierWatcher)"
    if has_owner org.kde.StatusNotifierWatcher; then
        printf 'StatusNotifierWatcher: yes, a host registered: %s\n' \
            "$(prop org.kde.StatusNotifierWatcher /StatusNotifierWatcher org.kde.StatusNotifierWatcher IsStatusNotifierHostRegistered)"
        prop org.kde.StatusNotifierWatcher /StatusNotifierWatcher org.kde.StatusNotifierWatcher RegisteredStatusNotifierItems \
            | grep -o "'[^']*'" | tr -d "'" > "$T/items"
        sn_mine=no
        while IFS= read -r sn_item; do
            sn_pid=$(dbus_pid "${sn_item%%/*}")
            [ -n "$sn_pid" ] && grep -qx "$sn_pid" "$T/procs.pids" 2>/dev/null && sn_mine=yes
        done < "$T/items"
        printf "Tray items: %s; one of them Natter's: %s\n" "$(wc -l < "$T/items" | tr -d ' ')" "$sn_mine"
    else
        printf 'StatusNotifierWatcher: none (no tray icons on this desktop)\n'
    fi
    part "The Background portal"
    printf 'org.freedesktop.portal.Desktop on the session bus: %s\n' "$(has_owner org.freedesktop.portal.Desktop && echo yes || echo no)"
    if have systemctl; then
        for sn_unit in xdg-desktop-portal xdg-desktop-portal-kde xdg-desktop-portal-gnome xdg-desktop-portal-gtk; do
            printf 'unit %s.service: %s\n' "$sn_unit" "$(t systemctl --user is-active "$sn_unit" 2>/dev/null | head -n 1)"
        done
    fi
    printf 'Background portal version: %s\n' "$(prop org.freedesktop.portal.Desktop /org/freedesktop/portal/desktop org.freedesktop.portal.Background version)"
    # Read only: the stored answers to "may it run in the background?", Natter's and the one for
    # apps the portal can't name (''). Plasma keeps its answers here too.
    sn_answers=$(t gdbus call --session --dest org.freedesktop.impl.portal.PermissionStore \
        --object-path /org/freedesktop/impl/portal/PermissionStore --method org.freedesktop.impl.portal.PermissionStore.Lookup \
        background background 2>&1 | grep -oE "'([^']*[Nn]atter[^']*)?': \[[^]]*\]|No entry|does not exist|not found|not provided" | tr '\n' ' ')
    printf 'Stored answers (background): %s\n' "${sn_answers:-none for Natter}"
    if have flatpak; then
        part "flatpak permission-show $APP_ID"
        t flatpak permission-show "$APP_ID" 2>&1 | capped 20
    fi
    if [ -r "$CONFIG_HOME/plasmanotifyrc" ]; then
        part "Plasma's notification settings (Natter's, do not disturb, and the general ones)"
        awk '/^\[/ { show = ($0 ~ /[Nn]atter|^\[DoNotDisturb\]|^\[Notifications\]/) } show' "$CONFIG_HOME/plasmanotifyrc" | capped 30
    fi
}

# ---- 7. The watch ----------------------------------------------------------------------------

# Times Natter asked the desktop for a notification, from dbus-monitor's Notify calls on stdin,
# and AddNotification calls to the notification portal (the Flatpak's way, through its proxy).
# Only each call's first line is looked at (who called); its arguments (what the notification
# says) are read past and dropped. A shell loop, as mawk holds a pipe's lines back in blocks.
notify_watch() {
    while IFS= read -r nw_line; do
        case $nw_line in "method call "*" sender=:"*) ;; *) continue ;; esac
        nw_sender=${nw_line#* sender=}
        nw_sender=${nw_sender%% *}
        case $nw_sender in *[!0-9.:]*) continue ;; esac
        nw_pid=$(dbus_pid "$nw_sender")
        [ -n "$nw_pid" ] || continue
        nw_comm=$(cat "$R_PROC/$nw_pid/comm" 2>/dev/null)
        case $nw_comm in
            natter) ;;
            xdg-dbus-proxy) grep -q '[Nn]atter' "$R_PROC/$nw_pid/cgroup" 2>/dev/null || continue ;;  # the Flatpak's
            *) continue ;;
        esac
        case $nw_line in *"member=AddNotification"*) nw_how=" through the notification portal" ;; *) nw_how="" ;; esac
        printf '%s notification: %s asked the desktop to show one%s (%s %s)\n' "$(date +%Y-%m-%dT%H:%M:%S)" "$NAME" "$nw_how" "$nw_comm" "$nw_pid"
    done
}

NOTIFICATIONS=0
SAMPLES=0

watch_run() {
    wr_secs=$((WATCH * 60))
    [ -n "${NATTER_REPORT_TEST_SECONDS-}" ] && wr_secs=$NATTER_REPORT_TEST_SECONDS
    wr_interval=${NATTER_REPORT_TEST_INTERVAL:-5}
    LIMITED=$((wr_secs + 60))  # the followers stop by themselves by then, whatever happens to this script
    hz=$(getconf CLK_TCK 2>/dev/null || echo 100)
    heading "7. Watch: $WATCH minutes"
    say ""
    say "Watching $NAME for $WATCH minutes, until $(date -d "+$WATCH min" +%H:%M 2>/dev/null || echo "$WATCH minutes from now"). While it runs:"
    say "  1. Leave this running (this terminal can go behind other windows)."
    say "  2. Minimise $NAME's window, or close it."
    say "  3. Wait for a message to come in (ask someone to send you one)."
    say "  4. Try a call, and a video."
    say "  5. Note the times when something goes wrong: no notification, the page reloading, a crash."
    say "Press Ctrl+C to stop early: the report is still written."
    say ""
    natter_units > "$T/units"
    : > "$T/follow-user"
    : > "$T/follow-kernel"
    : > "$T/notify"
    wr_pids=""
    if have journalctl; then
        ( journal_natter "$T/units" exec -f -n 0 ) > "$T/follow-user" 2>/dev/null &
        wr_pids="$wr_pids $!"
        ( limited journalctl -k -f -n 0 -q --no-pager -o short-iso-precise ) > "$T/follow-kernel" 2>/dev/null &
        wr_pids="$wr_pids $!"
    fi
    if have dbus-monitor && have gdbus; then
        sh -c 'echo $$ > "$1"; shift; exec "$@"' _ "$T/monitor.pid" ${TIMEOUT:+"$TIMEOUT" "$LIMITED"} dbus-monitor --session \
            "type='method_call',interface='org.freedesktop.Notifications',member='Notify'" \
            "type='method_call',interface='org.freedesktop.portal.Notification',member='AddNotification'" 2>/dev/null \
            | notify_watch > "$T/notify" &
        wr_pids="$wr_pids $!"
    fi
    wr_stop=0
    trap 'wr_stop=1' INT TERM HUP
    wr_start=$(date +%s)
    wr_first=yes
    wr_shown=0
    wr_said=0
    : > "$T/samples"
    : > "$T/prev"
    wr_up=$(uptime_s /proc)
    printf '%s\n' "Every ${wr_interval} s: Natter's processes (memory, swap, CPU since the last sample), its cgroups" \
        "(memory, swap, memory.events) and the system's memory, then which processes started or ended."
    printf '%s\n' "Window state isn't readable from outside the app: the times you noted say what you did." \
        "Lines below are in time order, with Natter's log, the kernel's kills and its notifications." ""
    while [ "$wr_stop" = 0 ]; do
        scan_natter > "$T/procs.now"
        wr_now_up=$(uptime_s /proc)
        wr_dt=$(awk -v a="$wr_now_up" -v b="$wr_up" 'BEGIN { print a - b }')
        awk -v ts="$(date +%Y-%m-%dT%H:%M:%S)" -v dt="$wr_dt" -v hz="$hz" -v first="$wr_first" -v name="$NAME" \
            "$SAMPLE" "$T/prev" "$T/procs.now" > "$T/sample" 2>/dev/null
        cat "$T/sample" >> "$T/samples"
        awk '{ print $1, $3, $6 }' "$T/procs.now" > "$T/prev"
        wr_up=$wr_now_up
        wr_first=no
        SAMPLES=$((SAMPLES + 1))
        # What the person sees: a line a minute, what started or ended, and Natter's new log lines.
        wr_elapsed=$(($(date +%s) - wr_start))
        if [ $((wr_elapsed / 60)) -ge "$wr_said" ]; then
            say "$(date +%H:%M:%S)  $(sed -n 's/^[^ ]* sample: \([^:]*\).*/\1/p' "$T/sample")"
            wr_said=$((wr_elapsed / 60 + 1))
        fi
        sed -n 's/^[^ ]*T\([0-9:]*\) change: \(.*\)/\1  processes \2/p' "$T/sample" >&3
        wr_lines=$(wc -l < "$T/follow-user" | tr -d ' ')
        if [ "$wr_lines" -gt "$wr_shown" ]; then
            tail -n +"$((wr_shown + 1))" "$T/follow-user" | head -n "$((wr_lines - wr_shown))" | awk "$JFILTER" \
                | redact | awk '{ t = substr($1, 12, 8); $1 = ""; print t $0 }' | capped 20 >&3
            wr_shown=$wr_lines
        fi
        [ "$wr_elapsed" -ge "$wr_secs" ] && break
        sleep "$wr_interval" || true
    done
    trap 'exit 130' INT TERM HUP
    for wr_pid in $wr_pids $(cat "$T/monitor.pid" 2>/dev/null); do kill "$wr_pid" 2>/dev/null; done
    wait 2>/dev/null
    say ""
    say "Watch over after $(($(date +%s) - wr_start)) seconds."
    awk "$JFILTER" "$T/follow-user" > "$T/watch-log"
    grep -iE "$KILLS" "$T/follow-kernel" | sed 's/^\([^ ]*\) [^ ]* /\1 /' > "$T/watch-kernel"
    NOTIFICATIONS=$(wc -l < "$T/notify" | tr -d ' ')
    printf 'Samples: %s; Natter log lines: %s; kernel kill lines: %s; notifications Natter asked for: %s\n\n' \
        "$SAMPLES" "$(wc -l < "$T/watch-log" | tr -d ' ')" "$(wc -l < "$T/watch-kernel" | tr -d ' ')" "$NOTIFICATIONS"
    if ! have dbus-monitor; then printf '(dbus-monitor is not installed: notifications not watched)\n\n'; fi
    cat "$T/samples" "$T/watch-log" "$T/watch-kernel" "$T/notify" | sort -s -k1,1 > "$T/timeline"
    newest 6000 "$T/timeline" > "$T/timeline.cut"
    newest_bytes 900000 "$T/timeline.cut"
}

# ---- The report ------------------------------------------------------------------------------

header() {
    printf '%s problem report\n' "$NAME"
    printf 'Made %s by tools/report.sh, run by the person whose computer this is.\n' "$STARTED"
    printf 'Logs: the last %s hours.%s\n' "$HOURS" "$([ "$WATCH" -gt 0 ] && printf ' Watch: %s minutes.' "$WATCH")"
    printf '%s' "$NOTES"
    cat <<EOF

WHAT THIS FILE CONTAINS
- $NAME's version, how and where it is installed, and whether it was running (its processes,
  their memory, and their systemd unit's memory limits)
- the system: distribution, kernel, desktop, CPU, memory, swap and zram, graphics, and the
  versions of the packages $NAME uses
- $NAME's own log lines, WebKit and GStreamer warnings and errors, and Python tracebacks,
  from the system log; the times it started and quit
- crashes (signal and stack) of $NAME and WebKit; out-of-memory kills
- whether notifications, the tray and the Background portal are there for $NAME
- with --watch: $NAME's memory and CPU every few seconds, and the times it asked the desktop
  for a notification

WHAT IT DOES NOT CONTAIN
- no messages, chat names, contacts or phone numbers, and nothing from WhatsApp's storage:
  $NAME's own folders are listed only by size and number of files
- no other app's logs, no passwords, no browsing history
- your home folder, user name, computer name, e-mail, IP and MAC addresses and Wi-Fi names are
  replaced by ~, <user>, <host>, <email>, <ip>, <mac> and <ssid>; numbers of 8 or more digits by
  <digits>

Nothing was sent anywhere: this file stays on this computer until you send it.
EOF
}

summary() {
    printf '%s %s, %s; %s; last %s h: %s log lines (%s warnings, %s tracebacks), %s crashes, %s out-of-memory kills%s.' \
        "$NAME" "${NATTER_VERSION:-(version unknown)}" "${INSTALL_KIND:-installed some other way}" \
        "$([ "${NATTER_RUNNING:-0}" -gt 0 ] && printf 'running (%s processes)' "$NATTER_RUNNING" || printf 'not running')" \
        "$HOURS" "$LOG_LINES" "$WARNINGS" "$TRACEBACKS" "$CRASHES" "$KILLED" \
        "$([ "$WATCH" -gt 0 ] && printf '; watched %s min, %s notifications asked for' "$WATCH" "$NOTIFICATIONS")"
}

main() {
    parse_args "$@"
    umask 077
    exec 3>&1
    TIMEOUT=$(command -v timeout 2>/dev/null)
    unset DEBUGINFOD_URLS  # coredumpctl looks nothing up online
    T=$(mktemp -d "${TMPDIR:-/tmp}/natter-report.XXXXXX") || { echo "report.sh: couldn't make a temporary folder" >&2; exit 1; }
    trap 'rm -rf "$T"' EXIT
    trap 'exit 130' INT TERM HUP
    redact_setup
    STARTED=$(date '+%Y-%m-%d %H:%M %Z')
    STAMP=$(date +%Y%m%d-%H%M)
    say "Collecting what's needed to look into $NAME's problems (it takes a minute)..."
    scan_natter > "$T/procs"
    awk '{ print $1 }' "$T/procs" > "$T/procs.pids"
    section_natter > "$T/s1" 2>&1
    section_system > "$T/s2" 2>&1
    section_notify > "$T/s6" 2>&1
    if [ "$WATCH" -gt 0 ]; then watch_run > "$T/s7" 2>&1; else : > "$T/s7"; fi
    say "Reading the logs..."
    : > "$T/s3.log"
    section_logs > "$T/s3" 2>&1
    section_crashes > "$T/s4" 2>&1
    section_memory > "$T/s5" 2>&1
    # Natter's log gets what room is left, so the report stays under its limit with the watch whole.
    capped 300 < "$T/s1" > "$T/s1.cut"
    capped 300 < "$T/s2" > "$T/s2.cut"
    capped 200 < "$T/s6" > "$T/s6.cut"
    mn_room=$((LIMIT_BYTES - 20000 - $(cat "$T/s1.cut" "$T/s2.cut" "$T/s3" "$T/s4" "$T/s5" "$T/s6.cut" "$T/s7" | wc -c)))
    [ "$mn_room" -gt 50000 ] || mn_room=50000
    {
        header
        cat "$T/s1.cut" "$T/s2.cut" "$T/s3"
        newest_bytes "$mn_room" "$T/s3.log"
        cat "$T/s4" "$T/s5" "$T/s6.cut" "$T/s7"
        printf '\n\n===== End of the report =====\n'
    } | redact | awk '{ if (length($0) > 1000) $0 = substr($0, 1, 1000) " ..."; print }' > "$T/report"
    if [ "$(wc -c < "$T/report")" -gt "$LIMIT_BYTES" ]; then
        head -c "$LIMIT_BYTES" "$T/report" | sed '$d' > "$T/report.cut"
        printf '\n(The report was cut here, at about 2 MB.)\n' >> "$T/report.cut"
        mv "$T/report.cut" "$T/report"
    fi
    OUT="$HOME/natter-report-$STAMP.txt"
    mn_n=2
    while [ -e "$OUT" ]; do OUT="$HOME/natter-report-$STAMP-$mn_n.txt"; mn_n=$((mn_n + 1)); done
    cat "$T/report" > "$OUT" && chmod 600 "$OUT" || { echo "report.sh: couldn't write $OUT" >&2; exit 1; }
    say ""
    say "$OUT"
    say "$(summary | redact)"
    say "Read it over, then send it to whoever asked for it."
}

if [ "${1-}" = --redact ]; then  # test-only: stdin redacted, as the report is
    redact_setup
    redact
    exit
fi
main "$@" < /dev/null
exit
