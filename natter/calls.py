"""Natter's side of its call window (callwindow.py): starts it, sends it commands, and passes
on what it reports. The window is a separate process because it runs Qt WebEngine (Chromium),
which has WebRTC, next to Natter's GTK and WebKitGTK, which doesn't."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Optional

from gi.repository import Gio, GLib, GObject

from . import APP_ID
from .system import give_system_bus

PACKAGE_ROOT = Path(__file__).resolve().parent.parent
CONNECT_TRIES = 75  # every 200 ms: 15 s for Qt WebEngine to start
RESTART_DELAYS = (2, 10, 60)  # seconds, when it stops on its own


def available() -> bool:
    """Whether Qt WebEngine for Python is installed (python3-pyqt6.qtwebengine and the like)."""
    try:
        return importlib.util.find_spec("PyQt6.QtWebEngineWidgets") is not None
    except (ImportError, ValueError):
        return False


class CallWindow(GObject.Object):
    __gsignals__ = {
        "state-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),  # loading, needs-link, ready, stopped
        "incoming": (GObject.SignalFlags.RUN_FIRST, None, (str, bool)),  # caller, video
        "incoming-ended": (GObject.SignalFlags.RUN_FIRST, None, ()),
        "in-call": (GObject.SignalFlags.RUN_FIRST, None, (bool,)),
        "call-failed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
    }

    def __init__(self, data: Path, debug: bool = False, microphone: bool = True, camera: bool = True) -> None:
        super().__init__()
        self.data = data
        self.debug = debug
        # What WhatsApp there may use (the app's switches, and the desktop's answer about the
        # camera): told at start, and again whenever it changes (set_media).
        self.microphone = microphone
        self.camera = camera
        self.socket_path = Path(GLib.get_user_runtime_dir()) / f"natter-calls-{APP_ID}.sock"
        self.process: Optional[Gio.Subprocess] = None
        self.connection: Optional[Gio.SocketConnection] = None
        self.input: Optional[Gio.DataInputStream] = None
        self.queue: list[dict] = []
        self.state = "stopped"
        self.in_call = False
        self.keep_running = False
        self._stopping = False
        self._restarts = 0
        self._tries = 0

    @property
    def running(self) -> bool:
        return self.process is not None

    # ---- The process -----------------------------------------------------------------------

    def start(self) -> None:
        if self.process is not None:
            return
        argv = [sys.executable, "-m", "natter.callwindow", "--socket", str(self.socket_path),
                "--data", str(self.data), "--app-id", APP_ID] + (["--debug"] if self.debug else []) + \
            (["--no-microphone"] if not self.microphone else []) + (["--no-camera"] if not self.camera else [])
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.NONE)
        path = os.environ.get("PYTHONPATH")
        launcher.setenv("PYTHONPATH", str(PACKAGE_ROOT) + (os.pathsep + path if path else ""), True)
        give_system_bus(launcher)  # Natter hides it from itself and WebKit, not from this window
        try:
            self.process = launcher.spawnv(argv)
        except GLib.Error as error:
            print(f"natter: couldn't start the call window: {error.message}", file=sys.stderr)
            self.emit("call-failed", "start")
            return
        self._stopping = False
        self.process.wait_async(None, self._exited, self.process)
        self._set_state("loading")
        self._tries = 0
        GLib.timeout_add(200, self._connect)

    def stop(self) -> None:
        """Close the window for good (quit, or ringing turned off with no call on)."""
        if self.process is None:
            return
        self._stopping = True
        self._send_now({"cmd": "quit"})
        process = self.process
        GLib.timeout_add_seconds(3, lambda: process.get_if_exited() or process.force_exit() or False)

    def _exited(self, process: Gio.Subprocess, result, which: Gio.Subprocess) -> None:
        try:
            process.wait_finish(result)
        except GLib.Error:
            pass
        if which is not self.process:
            return
        self.process = self.connection = self.input = None
        self.in_call = False
        self._set_state("stopped")
        if not self._stopping and self.keep_running and self._restarts < len(RESTART_DELAYS):
            delay = RESTART_DELAYS[self._restarts]
            self._restarts += 1
            GLib.timeout_add_seconds(delay, lambda: self.keep_running and self.start() and False)

    # ---- The socket ------------------------------------------------------------------------

    def _connect(self) -> bool:
        if self.process is None:
            return GLib.SOURCE_REMOVE
        self._tries += 1
        if not self.socket_path.exists():
            return self._tries < CONNECT_TRIES
        client = Gio.SocketClient.new()
        client.connect_async(Gio.UnixSocketAddress.new(str(self.socket_path)), None, self._connected)
        return GLib.SOURCE_REMOVE

    def _connected(self, client: Gio.SocketClient, result) -> None:
        try:
            self.connection = client.connect_finish(result)
        except GLib.Error:
            if self.process is not None and self._tries < CONNECT_TRIES:
                GLib.timeout_add(200, self._connect)
            return
        self._restarts = 0
        self.input = Gio.DataInputStream.new(self.connection.get_input_stream())
        self._read()
        queued, self.queue = self.queue, []
        for message in queued:
            self._send_now(message)

    def _read(self) -> None:
        if self.input is not None:
            self.input.read_line_async(GLib.PRIORITY_DEFAULT, None, self._line)

    def _line(self, stream: Gio.DataInputStream, result) -> None:
        try:
            line, _length = stream.read_line_finish_utf8(result)
        except GLib.Error:
            line = None
        if line is None:
            return  # the window closed its end; _exited follows
        try:
            message = json.loads(line)
        except ValueError:
            message = None
        if isinstance(message, dict):
            self._event(message)
        self._read()

    def _send_now(self, message: dict) -> None:
        if self.connection is None:
            return
        data = (json.dumps(message) + "\n").encode()
        try:
            self.connection.get_output_stream().write_all(data, None)
        except GLib.Error:
            pass

    def send(self, message: dict) -> None:
        """Send a command, starting the window first if it isn't running."""
        if self.connection is None:
            self.queue.append(message)
            self.start()
        else:
            self._send_now(message)

    # ---- What it reports -------------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.emit("state-changed", state)

    def _event(self, message: dict) -> None:
        event = message.get("event")
        if event == "state":
            self._set_state(str(message.get("value", "")))
        elif event == "incoming":
            self.emit("incoming", str(message.get("name", ""))[:200], bool(message.get("video")))
        elif event == "incoming-ended":
            self.emit("incoming-ended")
        elif event == "in-call":
            self.in_call = bool(message.get("value"))
            self.emit("in-call", self.in_call)
        elif event == "call-failed":
            self.emit("call-failed", str(message.get("reason", ""))[:100])
        elif event == "eval" and self.debug:
            print("natter: call window eval →", message.get("result"), flush=True)

    # ---- Commands ----------------------------------------------------------------------------

    def call(self, chat: str, video: bool) -> None:
        self.send({"cmd": "call", "chat": chat, "video": video})

    def answer(self) -> None:
        self.send({"cmd": "answer"})

    def decline(self) -> None:
        self._send_now({"cmd": "decline"})

    def show(self) -> None:
        self.send({"cmd": "show"})

    def set_media(self, microphone: bool, camera: bool) -> None:
        """What WhatsApp in the call window may use now. A window that isn't running gets it
        when it starts; one starting up, once it's listening."""
        if (microphone, camera) == (self.microphone, self.camera):
            return
        self.microphone, self.camera = microphone, camera
        message = {"cmd": "media", "microphone": microphone, "camera": camera}
        if self.connection is not None:
            self._send_now(message)
        elif self.process is not None:
            self.queue = [queued for queued in self.queue if queued.get("cmd") != "media"] + [message]
