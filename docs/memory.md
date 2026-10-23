# Memory

Most of Natter Den's memory is WhatsApp's own page, in WebKit's web process. This is what Natter
Den does about it. The numbers are constants in `natter/app.py`, `natter/system.py` and
`natter/memguard.py`.

## WebKit's memory limit

Natter Den gives WebKit a 1,536 MB memory limit, checked every 30 s: WebKit frees caches past 55%
of it and collects garbage hard past 80%. `NATTER_MEMORY_LIMIT=<MB>` overrides the limit (0 =
WebKit's own) and `NATTER_MEMORY_THRESHOLDS=0.55,0.8` the two thresholds.

## Idle audio

WhatsApp keeps a Web Audio context open, and in WebKitGTK an open context keeps a GStreamer
pipeline running. The bridge suspends it after 30 s of silence.

## Memory back while hidden

90 s after the window closes, and every 15 minutes after that, Natter Den asks the kernel to
reclaim its unit's cold memory in 64 MB steps (`memory.reclaim`), down to 800 MB. It does this
only:

- in a systemd unit of its own (see [process grouping](how-it-works.md#process-names-and-grouping)),
- where swapping is cheap: zram, or zswap in front of the swap file, so the memory is compressed
  rather than written to disk,
- where the unit's swap isn't turned off,
- until 1 GB of Natter Den is in swap, 60% of the computer's swap is in use, or memory pressure is
  past 20%,
- and not during a call, while a reply is being sent, or while a page loads or in its first ten
  minutes.

What it puts in swap is never a reason to reload WhatsApp. *Use Less Memory in the Background* in
Preferences → General turns it off (it's shown only where giving memory back is possible).

## Fresh pages

A fresh page (a new web process, back on the chat that was open) reloads WhatsApp, and messages
that arrive meanwhile come without notifications. So a hidden Natter Den gives one only for one of
these reasons:

- a page holds more than the guard's limit for a page (below);
- the computer is short of memory (under a tenth of it available, and swap, if any, all but full)
  and a page holds more than WebKit's limit;
- a call left 512 MB or more behind.

And only once the window has been hidden ten minutes; not during a call or a reply, nor within five
minutes of a call ending or a notification; not while the page loads; and, except after a call,
not within four hours of the page's last load. With several accounts, one page at a time. Natter
Den looks every minute, and again when it runs after being paused (desktops such as Ada may freeze
a hidden app). Each fresh page is logged with the memory it saw.

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
looking at the window. During a call, and for a minute after, the page may hold 1.5 GB more before
growth counts, and half a gigabyte more again whatever it does.

The last backstop is the kernel's. In a unit whose `OOMPolicy` is `continue`, such as the scope
Natter Den makes for itself, it sets `MemoryMax` (one page's limit, plus 1.5 GB for each account and
half a gigabyte for Natter Den, at most 60% of the computer's memory) and at most 1.5 GB of swap,
and makes its pages the kernel's first choice: the kernel ending a page there leaves Natter Den
running. The units desktops start apps in have had `OOMPolicy=stop` wherever it was checked (Ada's),
where the kernel ending one page stops all of Natter Den: there Natter Den sets no limit, leaves its
pages' standing with the kernel as it is, and the guard alone ends a page that grows without end
(the log says so once). The log says what each page held, and the files handed to WhatsApp by
number, size and type, not by name.

## Closing when asked

A close request to a hidden window, or SIGTERM, quits Natter Den rather than sending it to the
tray, so a desktop closing apps to make room gets the memory back.

## Spare processes

Process swapping and the back/forward cache are off (one site, no history). With calls in the page,
Natter Den keeps one blank web process for as long as it runs: WebKit names every sandbox after the
first one it started, and the desktop's camera portal checks that sandbox is still there.

## Measuring

`tools/compare-memory.py` adds up every process in each running app's systemd scopes: memory as
PSS from `/proc/<pid>/smaps_rollup` plus each process's `Swap:`, CPU as user and system time over
the window (30 s by default), as a percentage of one core. It finds Natter Den only in `*.scope`
units, so it misses KDE Plasma's `app-…@….service`. Chat history, open chats and media all change
the numbers.
