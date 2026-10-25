# Memory

Most of Natter Den's memory is WhatsApp's own page, in WebKit's web process. This is what Natter
Den does about it. The numbers are constants in `natter/app.py`, `natter/system.py` and
`natter/memguard.py`.

## WebKit's memory limit

Natter Den gives WebKit a 2,048 MB memory limit, checked every 30 s. At every check past 65% of
it (1,331 MB) WebKit drops its caches, Skia's included, and trims malloc; at every check past 85%
(1,741 MB) it also deletes all its compiled code and asks for a garbage collection (a request,
which often doesn't follow). Each runs at every check while the page is above its line, not once
at the crossing. WebKit counts the web process's
resident memory (VmRSS: libraries in, swap out). The strict line sits well above an idle page
(about 1,260-1,280 MB resident on 24 Oct 2026; with the old 1,536 MB and 55%/80% the strict line,
1,229 MB, lay under it, and the full release ran every 30 s all day). The conservative line sits
only just above that idle page: one that creeps past it, about 140 MB an hour, has its caches
dropped at every check again within half an hour or so, which is cheap. `NATTER_MEMORY_LIMIT=<MB>` overrides the limit
(0 = WebKit's own) and `NATTER_MEMORY_THRESHOLDS=0.65,0.85` the two thresholds.

None of WebKit's counting, nor the guard's below, sees the page's GPU buffers: on Intel the
driver backs them with shared memory (542 MB of a page's, one morning), which shows only as the
unit's `shmem`. Natter Den reports it (the log's step lines, `tools/compare-memory.py`) and never
counts it towards ending a page.

## Idle audio

WhatsApp keeps a Web Audio context open, and in WebKitGTK an open context keeps a GStreamer
pipeline running. The bridge suspends it after 30 s of silence.

## Memory back while hidden

90 s after the window closes, and every 15 minutes after that, Natter Den asks the kernel to
reclaim its unit's cold memory in 64 MB steps (`memory.reclaim`), down to 800 MB of the unit's
private memory (`memory.stat` anon: not the file cache or the GPU's shared memory beside it). It
does this only:

- in a systemd unit of its own (see [process grouping](how-it-works.md#process-names-and-grouping)),
- where swapping is cheap: zram, or zswap in front of the swap file, so the memory is compressed
  rather than written to disk,
- where the unit's swap isn't turned off,
- until 1 GB of Natter Den is in swap, 60% of the computer's swap is in use, or memory pressure is
  past 20%,
- and not during a call, while a reply is being sent, or while a page loads or in its first ten
  minutes.

What it puts in swap is never a reason to reload WhatsApp. *Use Less Memory in the Background* in
Preferences → General turns it off (it's shown only where giving memory back is possible, and
greyed, saying why, while swap is full already and a run would do nothing).

For all of this, a call is on from its first sign (WhatsApp making a call's data channel, Natter
Den's call button, or a ringing call's notification) until 30 s after its last channel closes, so
the moment a Wi-Fi roam closes them all doesn't count as the end (`rtc.py`, `GRACE`). Every packet
of a call goes through Natter Den's main loop, so meanwhile it does no more there than it must:
the guard's thread reads the pages' sizes, chats seen are saved once the call is over, and the
dock's recent chats, the Reclaimer and the audio-set check wait. When a call ends the log says how
late the main loop ran: `natter: call: main loop late max N ms, over 50 ms K times, …`.

## Fresh pages

A fresh page (a new web process, back on the chat that was open) reloads WhatsApp, and messages
that arrive meanwhile come without notifications. So a hidden Natter Den gives one only for one of
these reasons:

- a page holds more than the guard's limit for a page (below);
- the computer is short of memory (under a tenth of it available, and swap, if any, more than 60%
  in use) and a page holds more than WebKit's limit;
- a page has grown big and stayed so: past 2 GB, or past 1.5 times its idle size when that's more,
  for half an hour while hidden. The idle size belongs to a page's web process: what it holds,
  resident and swapped, at the first quiet look between ten minutes and an hour after Natter Den
  first saw that process (no call on or just ended, every page up and none loaded in the last ten
  minutes, no reply, voice recording or notification in the last five). It's measured once per
  process: WhatsApp reloading itself stays in the same process and measures nothing again, and a
  page never quiet in its first hour keeps the 2 GB floor alone, so a page that has crept never
  sets its own ceiling. A fresh page or a crash brings a new process, measured afresh; another
  account's fresh page leaves this one's as it was. The log says
  `natter: page: reloaded because it grew to N MB while hidden (idle N MB)`;
- a call left 512 MB or more behind.

And only once the window has been hidden ten minutes; not during a call (ringing or connected,
and 30 s after), a reply or a voice recording, nor while the page is lent to a reply window, nor
within five minutes of a call ending or a notification; not while the page loads; and, except
after a call, not within four hours of the page's last load. With several accounts, one page at a
time, and never two within a minute. Natter Den looks every minute, and again when it runs after a desktop has
paused it while hidden. Each fresh page is logged with the memory it saw, and the chat that was
open opens again.

WebKit's own limit ends a page only at 2.5 times the limit, or higher: half a gigabyte above the
most the guard lets a page hold during a call, as WebKit doesn't know about calls.

## The guard

WebKit's limit only counts what's resident, and only looks when the page's own main thread lets
it, so a page busy growing can get past it (on 20 October 2026 a video being attached took one to
12 GB). Natter Den's guard (`natter/memguard.py`) looks at each page every 2 s from a thread of its
own, with the window shown or hidden, swap included. A page's limit is a quarter of the computer's
memory, between 2.5 and 6 GB and never more than half. A page past it that grows faster than
64 MB/s, or 1.5 GB past it whatever it does (less on small computers), is ended and reloaded,
back on the chat that was open, with a toast if you're using the window. Ended again within ten
minutes, it stays down until you press *Try Again*, and a notification says so if you're not
looking at the window. During a call, and for a minute after it ends (30 s after its last channel
closes), the page may hold 1.5 GB more before growth counts, and half a gigabyte more again
whatever it does.

The last backstop is the kernel's. In a unit whose `OOMPolicy` is `continue`, such as the scope
Natter Den makes for itself, it sets `MemoryMax` (one page's limit, plus 1.5 GB for each account and
half a gigabyte for Natter Den, at most 60% of the computer's memory) and as much swap as one
page's limit, but never more than half the computer's swap (a fixed 1.5 GB sat full all morning
once, with the page idle; the guard counts what's swapped, so a runaway parked in swap is still
ended), and makes its pages the kernel's first
choice: the kernel ending a page there leaves Natter Den running. The units desktops start apps
in have had `OOMPolicy=stop` wherever it was checked, and in those the kernel ending one page
stops all of Natter Den: there Natter Den sets no limit, leaves its pages' standing with the
kernel as it is, and the guard alone ends a page that grows without end (the log says so once).
The log says what each page held, and the files handed to WhatsApp by number, size and type, not
by name. At every half gigabyte a page passes above 1.5 GB, it says what the page's largest
regions look like, and beside them what nothing above counts: the page's GPU buffers (by its DRM
files' `fdinfo`) and the unit's shared memory and zswap pool.

## Closing when asked

A close request to a hidden window, or SIGTERM, quits Natter Den rather than sending it to the
tray, so a desktop closing apps to make room gets the memory back.

## Spare processes

Process swapping and the back/forward cache are off (one site, no history). With calls in the page,
Natter Den keeps one blank web process for as long as it runs: WebKit names every sandbox after the
first one it started, and the desktop's camera portal checks that sandbox is still there.

## Measuring

`tools/compare-memory.py` adds up every process in each running app's systemd scopes: memory as
PSS from `/proc/<pid>/smaps_rollup` plus each process's `Swap:`, and beside them what no process
shows, the scopes' `shmem` and zswap pool from `memory.stat` and each process's GPU buffers from
its `fdinfo`; CPU as user and system time over the window (30 s by default), as a percentage of
one core. It finds Natter Den only in `*.scope` units, so it misses KDE Plasma's
`app-…@….service`. Chat history, open chats and media all change the numbers. The log's own
figure for Natter Den (`holds N MB`) is the unit's private memory, what's swapped and its shared
memory together; not the zswap pool, as what's in it is counted as swapped already (the step
lines give the pool on its own).
