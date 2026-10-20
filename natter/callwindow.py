"""Natter's call window: WhatsApp Web in Chromium's engine (Qt WebEngine), for calls.

WebKitGTK, which shows WhatsApp in Natter's main window, is built without WebRTC on every
distro, so WhatsApp can't call from there. This separate process runs WhatsApp Web in Qt
WebEngine (Chromium), linked once as its own device, and stays hidden except while linking,
ringing or in a call. Natter starts it and talks to it over a Unix socket, one JSON object
per line:

  Natter → window   {"cmd": "call", "chat": NAME, "video": BOOL} | {"cmd": "answer"} |
                    {"cmd": "decline"} | {"cmd": "show"} | {"cmd": "hide"} | {"cmd": "quit"} |
                    {"cmd": "media", "microphone": BOOL, "camera": BOOL} |
                    {"cmd": "eval", "js": BODY} (only with --debug)
  window → Natter   {"event": "state", "value": "loading" | "needs-link" | "ready"} |
                    {"event": "incoming", "name": NAME, "video": BOOL} | {"event": "incoming-ended"} |
                    {"event": "in-call", "value": BOOL} | {"event": "call-failed", "reason": TEXT} |
                    {"event": "eval", "result": ...}

Run by Natter as: python3 -m natter.callwindow --socket PATH --data DIR --app-id ID
                  [--no-microphone] [--no-camera]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from PyQt6.QtCore import QTimer, QUrl, Qt
from PyQt6.QtGui import QDesktopServices, QIcon
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript, QWebEngineSettings
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QLabel, QMainWindow, QVBoxLayout, QWidget

from . import NAME

WHATSAPP = "https://web.whatsapp.com/"
PREFIX = "__natter_calls__"
SCRIPT = (Path(__file__).resolve().parent / "calls.js").read_text()
LINK_TEXT = ("<b>Link the call window.</b> On your phone, open WhatsApp → Settings → Linked devices → "
             "Link a device, and scan this code. It's linked once, as its own device, and stays "
             "hidden except while you're in a call.")


def whatsapp(url: QUrl) -> bool:
    return url.scheme() == "https" and url.host() == "web.whatsapp.com"


# Qt's permissions (QWebEnginePermission.PermissionType and the older QWebEnginePage.Feature
# have the same names) that use the microphone and the camera, and which of them each needs.
MEDIA = {"MediaAudioCapture": ("microphone",), "MediaVideoCapture": ("camera",),
         "MediaAudioVideoCapture": ("microphone", "camera")}


def allowed(kind: str, microphone: bool, camera: bool) -> bool:
    """Whether WhatsApp gets a permission (by its Qt name): the microphone and the camera as
    Natter's switches say (and the desktop's answer for the camera: Chromium opens it itself,
    never asking), a request for both only when both may be used. The rest as before: the
    screen in a call, notifications."""
    switches = {"microphone": microphone, "camera": camera}
    return all(switches[device] for device in MEDIA.get(kind, ()))


class Page(QWebEnginePage):
    """WhatsApp only: other sites open in the browser; calls get the screen without asking, and
    the microphone and camera as Natter's switches say (allowed)."""

    def __init__(self, profile: QWebEngineProfile, window: "CallWindow") -> None:
        super().__init__(profile, window)
        self.window = window
        if hasattr(self, "permissionRequested"):  # Qt 6.8+
            self.permissionRequested.connect(self._permission)
        else:
            self.featurePermissionRequested.connect(self._feature)
        if hasattr(self, "desktopMediaRequested"):  # Qt 6.7+: sharing a screen in a call
            self.desktopMediaRequested.connect(self._desktop_media)

    def _permission(self, permission) -> None:
        if whatsapp(permission.origin()) and self.window.allows(permission.permissionType().name):
            permission.grant()
        else:
            permission.deny()

    def _feature(self, url: QUrl, feature) -> None:
        policy = (QWebEnginePage.PermissionPolicy.PermissionGrantedByUser
                  if whatsapp(url) and self.window.allows(feature.name)
                  else QWebEnginePage.PermissionPolicy.PermissionDeniedByUser)
        self.setFeaturePermission(url, feature, policy)

    def _desktop_media(self, request) -> None:
        # On Wayland Chromium goes through the screen-cast portal, which asks the user itself.
        screens = request.screensModel()
        if screens.rowCount() > 0:
            request.selectScreen(screens.index(0, 0))
        else:
            request.cancel()

    def acceptNavigationRequest(self, url: QUrl, kind, main_frame: bool) -> bool:
        if not main_frame or whatsapp(url) or url.scheme() in ("about", "blob", "data"):
            return True
        if kind == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
            QDesktopServices.openUrl(url)
        return False

    def createWindow(self, kind):
        # WhatsApp may put a call in a window of its own; anything else goes to the browser.
        print(f"natter-calls: WhatsApp opened a window ({kind.name})", flush=True)
        popup = self.window.popup()
        popup.page().urlChanged.connect(lambda url: print(f"natter-calls: its address: {url.path()[:80]}", flush=True))
        return popup.page()

    def javaScriptConsoleMessage(self, level, message: str, line: int, source: str) -> None:
        if message.startswith(PREFIX):
            try:
                self.window.from_page(json.loads(message[len(PREFIX):]))
            except ValueError:
                pass
        elif self.window.debug:
            print(f"console: {message[:300]}", flush=True)


class Popup(QMainWindow):
    """A window WhatsApp opened itself (a call, say). External links still go to the browser."""

    def __init__(self, profile: QWebEngineProfile, owner: "CallWindow") -> None:
        super().__init__()
        self.setWindowTitle(f"WhatsApp Call — {NAME}")
        self.view = QWebEngineView(self)
        self.view.setPage(Page(profile, owner))
        self.setCentralWidget(self.view)
        self.resize(720, 560)

    def page(self):
        return self.view.page()


class CallWindow(QMainWindow):
    def __init__(self, socket_path: str, data: Path, app_id: str, debug: bool,
                 microphone: bool = True, camera: bool = True) -> None:
        super().__init__()
        self.data = data
        self.debug = debug
        self.microphone, self.camera = microphone, camera
        self.clients: list[QLocalSocket] = []
        self.popups: list[Popup] = []
        self.state = "loading"
        self.ringing = False
        self.in_call = False
        self.quitting = False
        self.setWindowTitle(f"Calls — {NAME}")
        self.setWindowIcon(QIcon.fromTheme(app_id))
        self.resize(900, 680)

        profile = QWebEngineProfile("natter-calls", self)
        profile.setPersistentStoragePath(str(data / "profile"))
        profile.setCachePath(str(data / "cache"))
        profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        # WhatsApp checks the browser: present plain Chrome rather than "QtWebEngine/x Chrome/y".
        profile.setHttpUserAgent(" ".join(p for p in profile.httpUserAgent().split() if not p.startswith("QtWebEngine/")))
        # Message notifications are Natter's job; this window only cares about calls.
        profile.setNotificationPresenter(self._notification)
        script = QWebEngineScript()
        script.setName("natter-calls")
        script.setSourceCode(SCRIPT)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(False)
        profile.scripts().insert(script)
        self.profile = profile
        self._forget_grants()

        self.view = QWebEngineView(self)
        self.view.setPage(Page(profile, self))
        settings = self.view.page().settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.ScreenCaptureEnabled, True)
        self.banner = QLabel(LINK_TEXT, self)
        self.banner.setWordWrap(True)
        self.banner.setContentsMargins(16, 12, 16, 12)
        self.banner.setVisible(False)
        body = QWidget(self)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.banner)
        layout.addWidget(self.view)
        self.setCentralWidget(body)
        # Quiet unless ringing or in a call: WhatsApp's message sounds come from Natter's window.
        self.view.page().setAudioMuted(True)
        self.view.load(QUrl(WHATSAPP))

        QLocalServer.removeServer(socket_path)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not self.server.listen(socket_path):
            print(f"natter-calls: can't listen on {socket_path}: {self.server.errorString()}", file=sys.stderr)
            sys.exit(1)
        self.server.newConnection.connect(self._connected)

    # ---- Natter's side ---------------------------------------------------------------------

    def _connected(self) -> None:
        while self.server.hasPendingConnections():
            client = self.server.nextPendingConnection()
            self.clients.append(client)
            client.readyRead.connect(lambda c=client: self._read(c))
            client.disconnected.connect(lambda c=client: self._gone(c))
            self._send({"event": "state", "value": self.state}, [client])
            self._send({"event": "in-call", "value": self.in_call}, [client])

    def _gone(self, client: QLocalSocket) -> None:
        if client in self.clients:
            self.clients.remove(client)
        client.deleteLater()

    def _send(self, message: dict, to=None) -> None:
        line = (json.dumps(message) + "\n").encode()
        for client in to or self.clients:
            client.write(line)
            client.flush()

    def _read(self, client: QLocalSocket) -> None:
        while client.canReadLine():
            try:
                message = json.loads(bytes(client.readLine()).decode())
            except ValueError:
                continue
            if isinstance(message, dict):
                self._command(message)

    def _command(self, message: dict) -> None:
        cmd = message.get("cmd")
        if cmd == "call":
            name, video = str(message.get("chat", ""))[:200], bool(message.get("video"))
            self.show_window()
            self._js(f"return window.__natterCalls ? await window.__natterCalls.startCall({json.dumps(name)}, {json.dumps(video)}) : {{ok: false, reason: 'loading'}}",
                     lambda result: result and result.get("ok") or self._send(
                         {"event": "call-failed", "reason": (result or {}).get("reason", "unknown")}))
        elif cmd == "answer":
            self.show_window()
            self._js("return window.__natterCalls ? window.__natterCalls.answer() : false")
        elif cmd == "decline":
            self._js("return window.__natterCalls ? window.__natterCalls.decline() : false")
        elif cmd == "show":
            self.show_window()
        elif cmd == "hide":
            if not (self.in_call or self.ringing or self.state == "needs-link"):
                self.hide()
        elif cmd == "media":
            self.microphone, self.camera = bool(message.get("microphone")), bool(message.get("camera"))
            self._forget_grants()
        elif cmd == "quit":
            self.quitting = True
            QApplication.quit()
        elif cmd == "eval" and self.debug:
            self._js(str(message.get("js", "")), lambda result: self._send({"event": "eval", "result": result}))

    def allows(self, kind: str) -> bool:
        return allowed(kind, self.microphone, self.camera)

    def _forget_grants(self) -> None:
        """Qt 6.8 and later keep what was granted in the profile, and don't ask again: forget
        what was granted to WhatsApp for a device that's now off, so its next request is asked
        (and refused) here. Only the profile's own answers for WhatsApp; nothing of the page's."""
        query = getattr(self.profile, "queryPermission", None)
        if query is None:
            return  # before Qt 6.8 there's no reading or forgetting them
        from PyQt6.QtWebEngineCore import QWebEnginePermission

        for kind in MEDIA:
            if self.allows(kind):
                continue
            permission = query(QUrl(WHATSAPP), getattr(QWebEnginePermission.PermissionType, kind))
            if permission.isValid() and permission.state() == QWebEnginePermission.State.Granted:
                permission.reset()

    def _js(self, body: str, then=None) -> None:
        # runJavaScript has no await: run an async function and come back through the console.
        token = os.urandom(6).hex()
        if then:
            self._waiting = getattr(self, "_waiting", {})
            self._waiting[token] = then
        source = (f"(async () => {{ let r; try {{ r = await (async () => {{ {body} }})(); }} "
                  f"catch (e) {{ r = {{ok: false, reason: String(e)}}; }} "
                  f"console.debug({json.dumps(PREFIX)} + JSON.stringify({{type: 'result', token: {json.dumps(token)}, value: r === undefined ? null : r}})); }})();")
        self.view.page().runJavaScript(source)

    # ---- The page's side -------------------------------------------------------------------

    def from_page(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "result":
            then = getattr(self, "_waiting", {}).pop(message.get("token"), None)
            if then:
                then(message.get("value"))
        elif kind == "state":
            self.state = str(message.get("value"))
            self.banner.setVisible(self.state == "needs-link")
            if self.state == "needs-link":
                self.show_window()
            elif self.state == "ready" and not (self.in_call or self.ringing):
                self.hide()
            self._send({"event": "state", "value": self.state})
        elif kind == "dom":
            # What WhatsApp's call screen is made of, for building Natter's own call window.
            reason = "ringing" if message.get("reason") == "ringing" else "in-call"
            target = self.data / f"call-screen-{reason}.json"
            target.write_text(json.dumps(message, indent=1))
            os.chmod(target, 0o600)
            print(f"natter-calls: saved {target.name}: {len(message.get('layers', []))} layers, "
                  f"{len(message.get('controls', []))} controls, {len(message.get('media', []))} media", flush=True)
        elif kind == "incoming":
            self.ringing = True
            self.view.page().setAudioMuted(False)
            self._send({"event": "incoming", "name": str(message.get("name", ""))[:200],
                        "video": bool(message.get("video"))})
        elif kind == "incoming-ended":
            self.ringing = False
            self._quiet_if_idle()
            self._send({"event": "incoming-ended"})
        elif kind == "in-call":
            self.in_call = bool(message.get("value"))
            if self.in_call:
                self.view.page().setAudioMuted(False)
                self.show_window()
            else:
                self._quiet_if_idle()
                QTimer.singleShot(1500, lambda: self.in_call or self.ringing or self.state == "needs-link" or self.hide())
            self._send({"event": "in-call", "value": self.in_call})

    def _quiet_if_idle(self) -> None:
        if not (self.in_call or self.ringing):
            self.view.page().setAudioMuted(True)

    def _notification(self, notification) -> None:
        # WhatsApp also tells the browser about a call with a notification; the page script
        # watches for the call screen itself, so this is only a hint for it.
        if self.debug:
            print(f"notification: {notification.title()[:80]!r} / {notification.message()[:80]!r}", flush=True)
        self._js("if (window.__natterCalls) window.__natterCalls.check(); return null")

    # ---- The window --------------------------------------------------------------------------

    def popup(self) -> Popup:
        popup = Popup(self.profile, self)
        self.popups.append(popup)
        popup.show()
        return popup

    def show_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:
        if self.quitting:
            event.accept()
            return
        event.ignore()  # stays running, to ring for calls; Natter decides when it goes
        self.hide()


def main() -> int:
    parser = argparse.ArgumentParser(description=f"{NAME}'s call window")
    parser.add_argument("--socket", required=True)
    parser.add_argument("--data", required=True)
    parser.add_argument("--app-id", default="io.github.Officialbabs.Natter")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--no-microphone", action="store_true", help="WhatsApp can't use the microphone")
    parser.add_argument("--no-camera", action="store_true", help="WhatsApp can't use the camera")
    args = parser.parse_args()
    data = Path(args.data)
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Screen sharing on Wayland goes through PipeWire and the portal.
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    if "WebRTCPipeWireCapturer" not in flags:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (flags + " --enable-features=WebRTCPipeWireCapturer").strip()
    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication([sys.argv[0]])
    app.setApplicationName(NAME)
    app.setDesktopFileName(args.app_id)  # the window belongs to Natter on the dock
    app.setQuitOnLastWindowClosed(False)
    window = CallWindow(args.socket, data, args.app_id, args.debug,
                        microphone=not args.no_microphone, camera=not args.no_camera)
    window.hide()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
