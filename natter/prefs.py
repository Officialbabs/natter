"""Preferences: everything is applied the moment it's changed."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from . import APP_ID, NAME, media, themes
from . import lock as applock
from .launcher import menu_supported
from .settings import parse_clock
from .system import in_sandbox

SYNC = GObject.BindingFlags.SYNC_CREATE
APPEARANCES = [("system", "Follow System"), ("light", "Light"), ("dark", "Dark")]
# Privacy → Microphone and Camera: each switch's setting and title, in the order they're listed.
DEVICES = (("microphone", "Microphone"), ("camera", "Camera"))
# Under Microphone: other apps' players pause while a voice message is recorded (mediapause.py).
PAUSE_OTHER_AUDIO = ("Pause Other Audio While Recording",
                     "Music and videos in other apps pause while you record a voice message, then play again")
# Desktops that start what's in ~/.config/autostart themselves, as XDG_CURRENT_DESKTOP names them.
AUTOSTART_DESKTOPS = {"ada", "budgie", "budgie-desktop", "cinnamon", "x-cinnamon", "cosmic", "dde", "deepin",
                      "enlightenment", "gnome", "kde", "lxde", "lxqt", "mate", "pantheon", "ukui", "unity", "xfce"}


def runs_autostart(desktop: str) -> bool:
    """Whether the desktop (XDG_CURRENT_DESKTOP, e.g. "ubuntu:GNOME") starts apps at login by
    itself. Sway, Hyprland, i3 and most window managers don't: they run what their config says."""
    return any(name.strip().lower() in AUTOSTART_DESKTOPS for name in (desktop or "").split(":"))


def shown_folder(path, home=None) -> str:
    """A folder the way people write it, with their home folder as ~."""
    path, home = Path(path), Path(home or Path.home())
    try:
        inside = path.relative_to(home)
    except ValueError:
        return str(path)
    return "~" if inside == Path() else f"~/{inside}"


def account_status(account) -> str:
    """How an account is doing, in a few words under its name on the Accounts page."""
    if account.needs_link:
        return "Needs linking"
    if account.crashing:
        return "Used too much memory" if account.memory_stopped else "Keeps stopping"
    if account.failed:
        return "Can't reach WhatsApp"
    return "Linked" if account.ready else "Loading…"


def passcode_ready(current: str, first: str, again: str, needs_current: bool, remove: bool) -> bool:
    """Whether the passcode dialog can go ahead: the current passcode typed where it's asked for,
    and, unless the lock is going, a new one long enough and the same both times."""
    if needs_current and not current:
        return False
    return remove or (not applock.too_short(first) and first == again)


def mismatched(first: str, again: str) -> bool:
    """Whether the passcode typed again can no longer match the first. One that's only half typed
    still can, so it isn't marked wrong while someone is typing it."""
    return bool(again) and not first.startswith(again)


def camera_help(on: bool, desktop: Optional[str], lockdown: bool, sandboxed: bool) -> str:
    """The row under the Camera switch that gets past the desktop's answer: "ask" for Ask Again…,
    natively, once the desktop has said no (NAME can have it ask again); "how" for How to Allow…
    in the Flatpak, which can neither read nor undo the desktop's answer, until it's known to be
    yes; "" for neither. Neither while the switch is off, as NAME's own no comes first, nor while
    the desktop has the camera off for every app, which no answer for NAME changes."""
    if not on or lockdown:
        return ""
    if sandboxed:
        return "" if desktop == "yes" else "how"
    return "ask" if desktop == "no" else ""


def camera_blocked(on: bool, desktop: Optional[str], lockdown: bool) -> bool:
    """Whether the Camera row wears a warning: on in NAME, but the desktop keeps it from WhatsApp."""
    return bool(on) and (bool(lockdown) or desktop == "no")


def device_line(kind: str, state: dict) -> str:
    """The line under a device's switch, from app.media_state(kind)."""
    return media.device_subtitle(kind, state["on"], state["desktop"], state["present"], state["lockdown"],
                                 state["sandboxed"], restart=bool(state.get("restart")))


def icon_button(icon: str, tooltip: str) -> Gtk.Button:
    """A flat icon button for the end of a row. The tooltip is also its name for screen readers,
    as an icon has no text of its own to read out."""
    button = Gtk.Button(icon_name=icon, tooltip_text=tooltip, valign=Gtk.Align.CENTER, css_classes=["flat"])
    button.update_property([Gtk.AccessibleProperty.LABEL], [tooltip])
    return button


def plain_row(title: str, subtitle: str = "", **props) -> Adw.ActionRow:
    """A row that shows its text as it is, not as markup: a folder, a command or an account name
    may have an & in it. The text goes in once markup is off, or GTK warns as it parses it."""
    row = Adw.ActionRow(use_markup=False, **props)
    row.set_title(title)
    row.set_subtitle(subtitle)
    return row


def button_row(title: str, icon: str = "") -> Gtk.Widget:
    """A row that works as a button. Adw.ButtonRow needs libadwaita 1.6; before that, an
    activatable row does the same job."""
    if hasattr(Adw, "ButtonRow"):
        row = Adw.ButtonRow(title=title)
        if icon:
            row.set_start_icon_name(icon)
        return row
    row = Adw.ActionRow(title=title, activatable=True)
    if icon:
        row.add_prefix(Gtk.Image(icon_name=icon))
    return row


def show_percent(row: Adw.SpinRow) -> None:
    """Show a spin row's value as "120%". GTK reads typed text back through the "input" signal,
    which Python can't answer (its value is an out pointer), so while the field has the focus it
    holds the bare number, which GTK reads by itself. Text GTK can't read leaves the value as it was."""
    row.set_numeric(False)  # numeric rows refuse the "%"
    row.set_update_policy(Gtk.SpinButtonUpdatePolicy.IF_VALID)
    typing = Gtk.EventControllerFocus()
    row.add_controller(typing)

    def output(spin) -> bool:
        if typing.contains_focus():
            return False  # GTK writes the bare number
        spin.set_text(f"{round(spin.get_value())}%")
        return True

    def redraw() -> bool:
        if not typing.contains_focus():
            output(row)
        return GLib.SOURCE_REMOVE

    row.connect("output", output)
    typing.connect("enter", lambda _c: row.set_text(str(round(row.get_value()))))
    # GTK reads what was typed as the focus leaves, still counting it as inside: the "%" comes back after.
    typing.connect("leave", lambda _c: GLib.idle_add(redraw))


# Between swatches, across and down. With 36 px swatches, seven fit across the picker at the
# dialog's narrowest, 360 px, and eleven on the widest.
SWATCH_GAP = 8
# With room to spare, a group's swatches take this many a line: WhatsApp's 22 make two even lines.
NATURAL_COLUMNS = 11
# The arrow keys among the swatches (ThemeRow): Left and Right go along the themes in order, Up and
# Down from line to line.
ACROSS = {Gdk.KEY_Left: -1, Gdk.KEY_KP_Left: -1, Gdk.KEY_Right: 1, Gdk.KEY_KP_Right: 1}
UP_DOWN = {Gdk.KEY_Up: False, Gdk.KEY_KP_Up: False, Gdk.KEY_Down: True, Gdk.KEY_KP_Down: True}


def wrap_columns(count: int, width: float, cell: float, gap: float = SWATCH_GAP) -> int:
    """How many swatches go on each line of a group `width` wide: as many as fit, then evened out
    so the last line isn't left with one or two (WhatsApp's 22 at the dialog's narrowest: 6, 6, 6
    and 4 rather than 7, 7, 7 and 1)."""
    if count < 1:
        return 1
    fits = max(1, int((width + gap) // (cell + gap)))
    lines = -(-count // fits)
    return -(-count // lines)


def swatch_across(count: int, index: int, step: int) -> Optional[int]:
    """Left or Right: the theme before or after, in the order they're laid out, on to the next
    line and the next group. None at either end, where the choice stays."""
    target = index + step
    return target if 0 <= target < count else None


def swatch_above_below(centres: list[tuple[float, float]], index: int, down: bool) -> Optional[int]:
    """Up or Down: the swatch on the nearest line above or below (in the other group, from a
    group's first or last line), the one nearest across. None from the top or bottom line."""
    x, y = centres[index]
    beyond = [(abs(cy - y), abs(cx - x), i) for i, (cx, cy) in enumerate(centres)
              if (cy > y + 0.5 if down else cy < y - 0.5)]
    if not beyond:
        return None
    line = min(dy for dy, _dx, _i in beyond)
    return min((dx, i) for dy, dx, i in beyond if dy < line + 0.5)[1]


def hex_colour(rgba) -> str:
    """A colour from GTK as settings.json keeps it: "#rrggbb"."""
    return "#" + "".join(f"{round(max(0.0, min(1.0, c)) * 255):02x}" for c in (rgba.red, rgba.green, rgba.blue))


class SwatchGrid(Gtk.Widget):
    """A group's swatches, in lines that wrap to the width there is (wrap_columns). Only a
    layout: Gtk.FlowBox has keys, focus and selection of its own that fight the swatches' radio
    group, and Adw.WrapBox needs libadwaita 1.7."""

    def append(self, child: Gtk.Widget) -> None:
        child.set_parent(self)

    def _swatches(self) -> list[Gtk.Widget]:
        swatches, child = [], self.get_first_child()
        while child is not None:
            swatches.append(child)
            child = child.get_next_sibling()
        return swatches

    @staticmethod
    def _cell(swatches: list[Gtk.Widget]) -> int:
        """Each swatch's square: 36 px, as their CSS asks (themes.swatch_css), or more if one asks for more."""
        return max((max(swatch.measure(Gtk.Orientation.HORIZONTAL, -1)[0],
                        swatch.measure(Gtk.Orientation.VERTICAL, -1)[0]) for swatch in swatches), default=0)

    def do_get_request_mode(self) -> Gtk.SizeRequestMode:
        return Gtk.SizeRequestMode.HEIGHT_FOR_WIDTH

    def do_measure(self, orientation: Gtk.Orientation, for_size: int) -> tuple[int, int, int, int]:
        swatches = self._swatches()
        if not swatches:
            return 0, 0, -1, -1
        cell = self._cell(swatches)
        if orientation == Gtk.Orientation.HORIZONTAL:
            columns = min(len(swatches), NATURAL_COLUMNS)
            return cell, columns * cell + (columns - 1) * SWATCH_GAP, -1, -1
        columns = wrap_columns(len(swatches), for_size, cell) if for_size >= 0 else \
            min(len(swatches), NATURAL_COLUMNS)
        lines = -(-len(swatches) // columns)
        height = lines * cell + (lines - 1) * SWATCH_GAP
        return height, height, -1, -1

    def do_size_allocate(self, width: int, _height: int, _baseline: int) -> None:
        swatches = self._swatches()
        cell = self._cell(swatches)
        columns = wrap_columns(len(swatches), width, cell)
        rtl = self.get_direction() == Gtk.TextDirection.RTL
        for index, swatch in enumerate(swatches):
            line, column = divmod(index, columns)
            place = Gdk.Rectangle()
            place.x = column * (cell + SWATCH_GAP)
            if rtl:
                place.x = width - place.x - cell
            place.y, place.width, place.height = line * (cell + SWATCH_GAP), cell, cell
            swatch.size_allocate(place, -1)

    def do_dispose(self) -> None:
        # GTK leaves a widget's children to it to let go of.
        for swatch in self._swatches():
            swatch.unparent()
        super().do_dispose()


class ThemeRow(Adw.PreferencesRow):
    """The Theme row: groups of swatches rather than a row to stop at. Like libadwaita's spin
    and entry rows, it hands the focus on to its control, so the list's Up and Down keys land on
    the chosen swatch. A row that can't take the focus would swallow one press and be skipped by
    the next, and the swatches could only be reached with Tab.

    Its arrow keys go before anything under it (the list's own Up and Down would leave the row
    from any line): Left and Right go along the themes in order, Up and Down to the nearest swatch
    on the line above or below, choosing it, as a radio group does. From the top or bottom line,
    Up and Down move on to the next row; at either end of the themes, Left and Right stay put."""

    def __init__(self, **props) -> None:
        super().__init__(**props)
        self.swatches: list[Gtk.CheckButton] = []
        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key_pressed)
        self.add_controller(keys)

    def do_grab_focus(self) -> bool:
        # Focus that's in the row already is on its way out (Shift+Tab from a swatch): taking it
        # back to the swatch would trap it there.
        if self.get_focus_child() is not None:
            return False
        for swatch in self.swatches:
            if swatch.get_active():
                return swatch.grab_focus()
        return False

    def _key_pressed(self, _keys, keyval: int, _keycode: int, state: Gdk.ModifierType) -> bool:
        if state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK | Gdk.ModifierType.SHIFT_MASK):
            return False
        current = next((index for index, swatch in enumerate(self.swatches) if swatch.has_focus()), None)
        if current is None:
            return False
        if keyval in ACROSS:
            step = -ACROSS[keyval] if self.get_direction() == Gtk.TextDirection.RTL else ACROSS[keyval]
            target = swatch_across(len(self.swatches), current, step)
            if target is None:
                return True
        elif keyval in UP_DOWN:
            target = swatch_above_below(self._centres(), current, UP_DOWN[keyval])
            if target is None:
                return self._leave(UP_DOWN[keyval])
        else:
            return False
        self.swatches[target].grab_focus()
        self.swatches[target].set_active(True)
        return True

    def _leave(self, down: bool) -> bool:
        """Up from the top line or Down from the bottom one: on to the row above or below, as the
        list's own Up and Down go from any row. Not left to the list: it goes from the row it last
        moved to itself, which isn't this one when the focus came by Tab or a click."""
        row = self.get_next_sibling() if down else self.get_prev_sibling()
        while row is not None:
            if row.get_mapped() and row.is_sensitive() and row.grab_focus():
                return True
            row = row.get_next_sibling() if down else row.get_prev_sibling()
        return False  # the list's first or last row: what's beyond is the list's to say

    def _centres(self) -> list[tuple[float, float]]:
        """Where each swatch's centre is, in the row."""
        centres = []
        for swatch in self.swatches:
            ok, bounds = swatch.compute_bounds(self)
            centre = bounds.get_center() if ok else None
            centres.append((centre.x, centre.y) if centre is not None else (0.0, 0.0))
        return centres


class Preferences(Adw.PreferencesDialog):
    def __init__(self, app) -> None:
        super().__init__(search_enabled=False)
        self.app = app
        self.settings = app.settings
        self._rows: dict[str, Adw.SwitchRow] = {}
        self._account_handlers: list[tuple[object, int]] = []
        # Switches also flipped elsewhere (the tray menu, Ctrl+Shift+B) follow while it's open.
        self.settings.connect(self._on_setting)
        self.connect("closed", lambda *_: self._closed())
        self.add(self._general())
        self.add(self._notifications())
        self.add(self._privacy())
        self.add(self._accounts())
        # The lines under Microphone and Camera keep up with the desktop and the hardware while
        # it's open: a camera plugged in, the desktop's answer changed in its own settings.
        self._media_watch = app.watch_media(self._show_devices)
        self._open = True

    def _switch(self, key: str, title: str, subtitle: str = "") -> Adw.SwitchRow:
        row = Adw.SwitchRow(title=title, subtitle=subtitle, active=bool(self.settings[key]))
        row.connect("notify::active", lambda r, _p: self.settings.__setitem__(key, r.get_active()))
        self._rows[key] = row
        return row

    def _on_setting(self, key: str, _value) -> None:
        # The value now, not the one announced: an earlier listener may have changed it back
        # (Start at Login refused), and acting on the stale one would flip it again, and again.
        row = self._rows.get(key)
        if row is not None and row.get_active() != bool(self.settings[key]):
            row.set_active(bool(self.settings[key]))
        elif key == "zoom":
            # Ctrl+Plus and Ctrl+Minus still reach the window while this is open.
            zoom = round(self.settings["zoom"] * 100)
            if round(self.zoom.get_value()) != zoom:
                self.zoom.set_value(zoom)
        elif key == "theme":
            self._show_theme()
        elif key == "theme_custom":
            self._show_custom()
        if key in dict(DEVICES):
            self._show_devices()  # what the switch now means: "can't use it", or the desktop's say

    def _closed(self) -> None:
        self.settings.disconnect(self._on_setting)
        self._forget_accounts()
        if self._open:
            self.app.unwatch_media(self._media_watch)
        self._open = False

    # ---- Pages -----------------------------------------------------------------------------

    def _general(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(name="general", title="General", icon_name="preferences-system-symbolic")
        self.media = Adw.PreferencesGroup(visible=False)
        page.add(self.media)
        self.app.media(self._show_media)

        tray_host = self.app.has_tray_host()
        running = Adw.PreferencesGroup(title="Running")
        running.add(self._switch("run_in_background", "Keep Running When Closed",
                                 "Closing the window leaves it in the tray, still notifying you" if tray_host else
                                 "Closing the window keeps it running, still notifying you. "
                                 "Open it again from your apps"))
        login = self._switch("start_at_login", "Start at Login", "Starts hidden, ready to notify you")
        if not runs_autostart(GLib.getenv("XDG_CURRENT_DESKTOP") or ""):
            command = f"flatpak run {APP_ID} --background" if in_sandbox() else "natter --background"
            login.set_subtitle(f"Your desktop may not start apps by itself: add “exec {command}” to its config")
        running.add(login)
        if self.app.reclaimer.available:
            running.add(self._switch("light_background", "Use Less Memory in the Background",
                                     "While the window is closed, memory that isn't in use goes back to the system"))
        tray = self._switch("tray_icon", "Tray Icon", "Shows the unread count; click it to open the window")
        if not tray_host:
            tray.set_sensitive(False)
            tray.set_subtitle("Your desktop doesn't show tray icons")
        running.add(tray)
        if menu_supported():
            running.add(self._switch("dock_recent_chats", "Recent Chats in Dock Menu",
                                     "Right-click the dock icon to jump straight to a chat"))
        running.add(self._switch("search_provider", "Chats in Desktop Search",
                                 "Lets the desktop's search find chats by name, on desktops that ask apps for results"))
        page.add(running)

        look = Adw.PreferencesGroup(title="Appearance")
        keys = [key for key, _ in APPEARANCES]
        # Markup off: the line app.py may put under it (show_style_hint) is shown as it is.
        self.style = Adw.ComboRow(title="Style", use_markup=False,
                                  model=Gtk.StringList.new([label for _, label in APPEARANCES]))
        self.style.set_selected(keys.index(self.settings["appearance"]) if self.settings["appearance"] in keys else 0)
        self.style.connect("notify::selected",
                           lambda r, _p: self.settings.__setitem__("appearance", keys[r.get_selected()]))
        look.add(self.style)
        self.custom_row = self._custom_row()
        look.add(self._theme_row())
        look.add(self.custom_row)
        self.zoom = Adw.SpinRow.new_with_range(50, 200, 10)
        self.zoom.set_title("Zoom")
        self.zoom.set_subtitle("Ctrl+Plus and Ctrl+Minus also change it")
        show_percent(self.zoom)
        self.zoom.set_value(round(self.settings["zoom"] * 100))
        self.zoom.connect("notify::value",
                          lambda r, _p: self.settings.__setitem__("zoom", round(r.get_value() / 100, 2)))
        look.add(self.zoom)
        page.add(look)

        spelling = Adw.PreferencesGroup(title="Spelling",
                                        description="Language codes separated by commas, such as en_US or pt_BR.")
        spell = self._switch("spellcheck", "Spell Check")
        spelling.add(spell)
        languages = Adw.EntryRow(title="Languages", text=", ".join(self.settings["spell_languages"]),
                                 show_apply_button=True)
        languages.connect("apply", lambda r: self.settings.__setitem__(
            "spell_languages", [p.strip() for p in r.get_text().replace(";", ",").split(",") if p.strip()]))
        spell.bind_property("active", languages, "sensitive", SYNC)
        spelling.add(languages)
        page.add(spelling)

        files = Adw.PreferencesGroup(title="Downloads")
        self.folder = plain_row("Save Files To", shown_folder(self.settings.download_dir))
        choose = icon_button("folder-open-symbolic", "Choose Folder")
        choose.connect("clicked", self._choose_folder)
        self.folder.add_suffix(choose)
        self.folder.set_activatable_widget(choose)
        files.add(self.folder)
        page.add(files)

        trouble = Adw.PreferencesGroup(title="Troubleshooting")
        trouble.add(self._switch("hardware_acceleration", "Hardware Acceleration",
                                 "Turn off if WhatsApp's page is blank or flickers"))
        page.add(trouble)
        return page

    def _theme_row(self) -> ThemeRow:
        """Theme: a round swatch per theme, in labelled groups, as themes.GROUPS has them (NAME's
        own, then WhatsApp's chat themes in WhatsApp's order), under a line with the chosen theme's
        name. The swatches are check buttons in one group across all of them, so Tab stops only
        at the chosen one and the arrow keys move the choice along (ThemeRow). Their colours come
        from themes.swatch_css(), which app.py loads with the theme's while Preferences is open:
        here they're only classes."""
        # Not a row to activate or focus: it's the group the swatches are in, named for screen readers.
        row = ThemeRow(title="Theme", activatable=False, focusable=False, accessible_role=Gtk.AccessibleRole.GROUP)
        row.update_property([Gtk.AccessibleProperty.LABEL], ["Theme"])
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14,
                      margin_top=10, margin_bottom=14, margin_start=12, margin_end=12)
        top = Gtk.Box(spacing=6)
        top.append(Gtk.Label(label="Theme", xalign=0, hexpand=True))
        self.theme_name = Gtk.Label(xalign=1, css_classes=["dim-label"])
        top.append(self.theme_name)
        box.append(top)
        words = {theme_id: (name, describe) for theme_id, name, describe in themes.CHOICES}
        self.theme_buttons: dict[str, Gtk.CheckButton] = {}
        first = None
        for title, theme_ids in themes.GROUPS:
            # Each group is named for screen readers by its heading, which is what it shows.
            group = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, accessible_role=Gtk.AccessibleRole.GROUP)
            group.update_property([Gtk.AccessibleProperty.LABEL], [title])
            group.append(Gtk.Label(label=title, xalign=0, css_classes=["caption-heading", "dim-label"]))
            swatches = SwatchGrid()
            for theme_id in theme_ids:
                name, describe = words[theme_id]
                # The radio role is set by hand: GTK 4.14 reads a check button in a group out as a check box.
                button = Gtk.CheckButton(css_classes=["natter-swatch", f"theme-{theme_id}"], tooltip_text=name,
                                         accessible_role=Gtk.AccessibleRole.RADIO,
                                         halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
                # The tick, a small badge in the corner; swatch_css shows it on the chosen swatch only.
                button.set_child(Gtk.Image(icon_name="object-select-symbolic", hexpand=True, vexpand=True,
                                           halign=Gtk.Align.END, valign=Gtk.Align.END))
                # A swatch has no words of its own: its name and what it looks like are given here.
                button.update_property([Gtk.AccessibleProperty.LABEL, Gtk.AccessibleProperty.DESCRIPTION],
                                       [name, describe])
                if first is None:
                    first = button
                else:
                    button.set_group(first)
                self.theme_buttons[theme_id] = button
                swatches.append(button)
            group.append(swatches)
            box.append(group)
        row.set_child(box)
        row.swatches = list(self.theme_buttons.values())
        self._show_theme()
        # Connected only now, so showing the theme in use doesn't count as choosing it.
        for theme_id, button in self.theme_buttons.items():
            button.connect("toggled", self._theme_toggled, theme_id)
        return row

    def _show_theme(self) -> None:
        """The theme in use, chosen and named, and Custom's colour row while it's Custom. A value
        in settings.json that isn't a theme shows as what themes.chosen() makes of it, which is
        what Natter uses: a theme from before WhatsApp's own as the nearest of those, anything
        else as the default, Den."""
        current = themes.chosen(self.settings["theme"])
        if not self.theme_buttons[current].get_active():
            self.theme_buttons[current].set_active(True)
        self.theme_name.set_label(next(name for theme_id, name, _ in themes.CHOICES if theme_id == current))
        self.custom_row.set_visible(current == themes.CUSTOM)

    def _theme_toggled(self, button: Gtk.CheckButton, theme_id: str) -> None:
        # Choosing one turns the last one off, which toggles too: only the one now on counts.
        if not button.get_active():
            return
        # Shown because it's in use already (set elsewhere while this is open): nothing to write back.
        if themes.chosen(self.settings["theme"]) != theme_id:
            self.settings["theme"] = theme_id
        self._show_theme()

    def _custom_row(self) -> Adw.ActionRow:
        """Custom's colour, under the swatches while Custom is the theme (_show_theme). The theme
        is made from it as WhatsApp makes its own (themes.py), and the Custom swatch shows that."""
        row = Adw.ActionRow(title="Custom Colour", subtitle="Accents, bubbles and the chat background are made from it")
        self.custom_button = Gtk.ColorDialogButton(dialog=Gtk.ColorDialog(title="Custom Colour", with_alpha=False),
                                                   tooltip_text="Choose Colour", valign=Gtk.Align.CENTER)
        self.custom_button.update_property([Gtk.AccessibleProperty.LABEL], ["Custom Colour"])
        row.add_suffix(self.custom_button)
        row.set_activatable_widget(self.custom_button)
        self._show_custom()
        # Connected only now, so showing the colour in use doesn't count as choosing it.
        self.custom_button.connect("notify::rgba", self._custom_chosen)
        return row

    def _show_custom(self) -> None:
        """Custom's colour as Natter uses it, themes.custom_colour() of the setting: one that
        isn't a colour shows as Den's orange."""
        colour = themes.custom_colour(self.settings["theme_custom"])
        shown = self.custom_button.get_rgba()
        if shown is None or hex_colour(shown) != colour:
            rgba = Gdk.RGBA()
            rgba.parse(colour)
            self.custom_button.set_rgba(rgba)

    def _custom_chosen(self, button: Gtk.ColorDialogButton, _param) -> None:
        colour = hex_colour(button.get_rgba())
        # Shown because it's in use already: nothing to write back.
        if colour != themes.custom_colour(self.settings["theme_custom"]):
            self.settings["theme_custom"] = colour

    def show_style_hint(self, hint: str) -> None:
        """A line under Style, for app.py to say that WhatsApp has a light or dark look of its
        own, which Natter can't change. An empty hint takes the line away."""
        self.style.set_subtitle(hint)

    def _show_media(self, missing: list[str], hint: str) -> None:
        """What's missing to play WhatsApp's videos and voice messages, and the command that adds it.
        A hint with nothing missing says why Natter couldn't check."""
        if missing:
            about = f"Some videos and voice messages can't play. Missing: {', '.join(missing)}."
            subtitle = f"{about}\nInstall them with: {hint}" if hint else about
        elif hint:
            subtitle = hint
        else:
            return
        row = plain_row("Media", subtitle, subtitle_selectable=True)
        row.add_prefix(Gtk.Image(icon_name="dialog-warning-symbolic"))
        if missing and hint:
            copy = icon_button("edit-copy-symbolic", "Copy Command")
            copy.connect("clicked", lambda button: button.get_clipboard().set(hint))
            row.add_suffix(copy)
        self.media.add(row)
        self.media.set_visible(True)

    def _notifications(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(name="notifications", title="Notifications",
                                   icon_name="preferences-system-notifications-symbolic")
        group = Adw.PreferencesGroup(
            description="One notification per chat, replaced as new messages come in. Clicking it opens that chat; "
                        "Reply answers it in a small window. Sounds follow WhatsApp's own setting, under Settings → "
                        "Notifications in WhatsApp.")
        shown = self._switch("notifications", "Show Notifications")
        group.add(shown)
        sender = self._switch("notify_sender", "Show Who It's From", "Their name and picture")
        preview = self._switch("notify_preview", "Show Message Text")
        reply = self._switch("notify_reply", "Show Reply Button", f"Answer a message without opening {NAME}")
        for row in (sender, preview, reply):
            shown.bind_property("active", row, "sensitive", SYNC)
            group.add(row)
        page.add(group)

        quiet = Adw.PreferencesGroup()
        expander = Adw.ExpanderRow(title="Quiet Hours",
                                   subtitle="No notifications between these times. The unread count still updates.",
                                   show_enable_switch=True, enable_expansion=bool(self.settings["quiet_hours"]),
                                   expanded=True)
        expander.connect("notify::enable-expansion",
                         lambda r, _p: self.settings.__setitem__("quiet_hours", r.get_enable_expansion()))
        for key, title in (("quiet_start", "From"), ("quiet_end", "Until")):
            row = Adw.EntryRow(title=title, text=self.settings[key], show_apply_button=True)
            row.connect("apply", self._set_clock, key)
            row.connect("changed", lambda r: r.remove_css_class("error"))
            expander.add_row(row)
        quiet.add(expander)
        page.add(quiet)
        return page

    def _privacy(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(name="privacy", title="Privacy", icon_name="view-conceal-symbolic")
        group = Adw.PreferencesGroup(description="For working in public or sharing your screen.")
        group.add(self._switch("privacy_blur", "Privacy Blur",
                               "Blurs names, pictures and messages until you point at them. Ctrl+Shift+B"))
        group.add(self._switch("blur_when_away", "Blur When in the Background",
                               "Blurs everything while another window is active"))
        page.add(group)
        for device_group in self._device_groups():
            page.add(device_group)
        for lock_group in self._lock_groups():
            page.add(lock_group)
        return page

    def _device_groups(self) -> list[Adw.PreferencesGroup]:
        """Microphone and Camera: NAME's own say in what WhatsApp's page may use, as a browser's
        site permissions are. Each switch is NAME's choice; the line under it says what the desktop
        or the hardware makes of it (media.device_subtitle), in words, so a screen reader hears it
        as the row's description. Under them, while the desktop keeps the camera from NAME, a way
        past that (camera_help)."""
        group = Adw.PreferencesGroup(title="Microphone and Camera",
                                     description="What WhatsApp can use for calls and voice messages.")
        self.device_rows: dict[str, Adw.SwitchRow] = {}
        self._device_lines: dict[str, str] = {}
        for key, title in DEVICES:
            self.device_rows[key] = self._switch(key, title)
            group.add(self.device_rows[key])
            if key == "microphone":
                group.add(self._switch("pause_other_audio", *PAUSE_OTHER_AUDIO))
        # Only to catch the eye: the line under the title says it in words, so screen readers skip
        # it. It's put in and taken out rather than hidden: a hidden prefix still moves the title.
        self.camera_warning = Gtk.Image(icon_name="dialog-warning-symbolic",
                                        accessible_role=Gtk.AccessibleRole.PRESENTATION)
        self._warning_shown = False
        # Something to do, not a setting: a group of its own under the switches, as Change
        # Passcode… has, shown only while there's something it can do.
        self.camera_help = Adw.PreferencesGroup(visible=False)
        self.ask_again = button_row("Ask Again…")
        self.ask_again.connect("activated", lambda *_: self._ask_camera_again())
        self.how_to_allow = button_row("How to Allow…")
        self.how_to_allow.connect("activated", lambda *_: self._how_to_allow())
        self.camera_help.add(self.ask_again)
        self.camera_help.add(self.how_to_allow)
        self._show_devices()
        return [group, self.camera_help]

    def _show_devices(self) -> None:
        """Each device's line, and the Camera's way past the desktop, as things are now: when it
        opens, when a switch changes, and whenever app.watch_media says something else did."""
        for key, row in self.device_rows.items():
            line = device_line(key, self.app.media_state(key))
            # Set only when it changes: a line set again is a description changed, for screen readers.
            if self._device_lines.get(key) != line:
                self._device_lines[key] = line
                row.set_subtitle(line)
        camera = self.app.media_state("camera")
        shown = camera_help(camera["on"], camera["desktop"], camera["lockdown"], camera["sandboxed"])
        self.ask_again.set_visible(shown == "ask")
        self.how_to_allow.set_visible(shown == "how")
        self.camera_help.set_visible(bool(shown))
        blocked = camera_blocked(camera["on"], camera["desktop"], camera["lockdown"])
        if blocked != self._warning_shown:
            if blocked:
                self.device_rows["camera"].add_prefix(self.camera_warning)
            else:
                self.device_rows["camera"].remove(self.camera_warning)
            self._warning_shown = blocked

    def _ask_camera_again(self) -> None:
        """Have the desktop ask again for the camera, once it has said no. Only after a yes here:
        NAME forgets the desktop's answer for itself (no other app's) and asks it straight away, so
        its own dialog comes up now, not stacked up behind WhatsApp's next try."""
        alert = Adw.AlertDialog(heading="Ask Again for the Camera?", body="Your desktop will ask you now.")
        alert.add_response("cancel", "_Cancel")
        alert.add_response("ask", "_Ask Again")
        alert.set_response_appearance("ask", Adw.ResponseAppearance.SUGGESTED)
        alert.set_default_response("ask")
        alert.set_close_response("cancel")

        def answered(dlg, result) -> None:
            if dlg.choose_finish(result) != "ask":
                return
            self.ask_again.set_sensitive(False)  # one question at a time
            self.app.ask_camera_again(asked)

        def asked(answer: Optional[str] = None) -> None:
            self.ask_again.set_sensitive(True)
            if not self._open:
                return  # closed while the desktop asked: there's nothing left to show it in
            self._show_devices()
            if answer is None:
                self.add_toast(Adw.Toast(title="Your desktop couldn't ask about the camera"))
                return
            # What the desktop's answer makes of the camera, said once. It's worked out from the
            # answer: the line may follow a moment later, once the desktop's store is read again.
            # The lines change by themselves too (a camera plugged in), but nobody asked to hear those.
            line = device_line("camera", dict(self.app.media_state("camera"), desktop=answer))
            self.device_rows["camera"].announce(line, Gtk.AccessibleAnnouncementPriority.MEDIUM)

        alert.choose(self, None, answered)

    def _how_to_allow(self) -> None:
        """In the Flatpak, the desktop's answer can be neither read nor undone from inside: only
        its settings can, or a command outside the sandbox. Only NAME's own answer: without the
        app id the command would forget every app's."""
        command = f"flatpak permission-remove devices camera {APP_ID}"
        alert = Adw.AlertDialog(
            heading="How to Allow the Camera",
            body=f"Your desktop may have blocked the camera for {NAME}. Allow it in your desktop's privacy "
                 "settings, or run this command to have your desktop ask again the next time WhatsApp uses it:")
        # A label of its own, so the command can be selected. It wraps between words only: broken
        # inside the app id, the command reads wrong, so the dialog grows to fit the id instead.
        label = Gtk.Label(label=command, selectable=True, wrap=True, wrap_mode=Pango.WrapMode.WORD,
                          css_classes=["monospace"])
        # Out of the focus chain (selectable made it focusable): as the dialog's first focus it
        # would open with all of it selected. Co_py Command is the way in from the keyboard.
        label.set_focusable(False)
        alert.set_extra_child(label)
        alert.add_response("close", "_Close")
        alert.add_response("copy", "Co_py Command")  # "_Close" has C
        alert.set_response_appearance("copy", Adw.ResponseAppearance.SUGGESTED)
        alert.set_default_response("copy")
        alert.set_close_response("close")
        alert.connect("response", lambda _alert, response: response == "copy" and self.get_clipboard().set(command))
        alert.present(self)

    def _lock_groups(self) -> list[Adw.PreferencesGroup]:
        group = Adw.PreferencesGroup(
            title="Lock", description="Hides WhatsApp behind a passcode while you're away. Ctrl+L locks now.")
        self.lock_switch = Adw.SwitchRow(title="Lock With a Passcode",
                                         subtitle="While locked, notifications and recent chats don't show names, "
                                                  "and Reply asks for the passcode first",
                                         active=bool(self.settings["lock_hash"]))
        self.lock_switch.connect("notify::active", self._lock_switched)
        group.add(self.lock_switch)
        on_start = self._switch("lock_on_start", f"Lock When {NAME} Starts")
        on_hide = self._switch("lock_on_hide", "Lock When the Window Is Closed")
        idle = Adw.ComboRow(title="Lock When Idle",
                            model=Gtk.StringList.new([applock.idle_label(m) for m in applock.IDLE_CHOICES]))
        minutes = self.settings["lock_idle_minutes"]
        idle.set_selected(applock.IDLE_CHOICES.index(minutes) if minutes in applock.IDLE_CHOICES else 0)
        idle.connect("notify::selected", lambda r, _p: self.settings.__setitem__(
            "lock_idle_minutes", applock.IDLE_CHOICES[r.get_selected()]))
        for row in (on_start, on_hide, idle):
            self.lock_switch.bind_property("active", row, "sensitive", SYNC)
            group.add(row)
        # Changing the passcode is something to do, not a setting: it gets a group of its own
        # under the switches rather than a place among them.
        passcode = Adw.PreferencesGroup()
        change = button_row("Change Passcode…")
        change.connect("activated", lambda *_: self._ask_passcode(change=True))
        self.lock_switch.bind_property("active", change, "sensitive", SYNC)
        passcode.add(change)
        return [group, passcode]

    def _lock_switched(self, row: Adw.SwitchRow, _param) -> None:
        if row.get_active() == bool(self.settings["lock_hash"]):
            return  # set back after a cancelled dialog
        if row.get_active():
            self._ask_passcode()
        else:
            self._ask_passcode(remove=True)

    def _ask_passcode(self, change: bool = False, remove: bool = False) -> None:
        """New passcode (twice), or the current one first to change or remove it. The answer can
        be given only once what's typed will do, so all that's left to refuse afterwards is a
        wrong current passcode, which only the check can tell."""
        needs_current = (change or remove) and bool(self.settings["lock_hash"])
        heading = "Remove Lock?" if remove else "Change Passcode" if change else "Set Passcode"
        alert = Adw.AlertDialog(heading=heading,
                                body="" if remove else f"Use at least {applock.SHORTEST} characters.")
        rows = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        current = Adw.PasswordEntryRow(title="Current Passcode", visible=needs_current, activates_default=True)
        first = Adw.PasswordEntryRow(title="New Passcode", visible=not remove, activates_default=True)
        again = Adw.PasswordEntryRow(title="New Passcode Again", visible=not remove, activates_default=True)
        for entry in (current, first, again):
            rows.append(entry)
        alert.set_extra_child(rows)
        alert.add_response("cancel", "_Cancel")
        alert.add_response("ok", "_Remove" if remove else "_Set Passcode")
        alert.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if remove
                                      else Adw.ResponseAppearance.SUGGESTED)
        alert.set_default_response("ok")
        alert.set_close_response("cancel")

        def ready() -> bool:
            return passcode_ready(current.get_text(), first.get_text(), again.get_text(), needs_current, remove)

        def check(*_args) -> None:
            alert.set_response_enabled("ok", ready())
            if mismatched(first.get_text(), again.get_text()):
                again.add_css_class("error")
            else:
                again.remove_css_class("error")

        for entry in (current, first, again):
            entry.connect("changed", check)
        check()

        def answered(dlg, result) -> None:
            # Checked again here: a passcode set by mistake is one nobody knows.
            if dlg.choose_finish(result) != "ok" or not ready():
                self.lock_switch.set_active(bool(self.settings["lock_hash"]))
                return
            if not needs_current:
                apply()
                return

            # The current passcode is checked the way the lock screen checks it: off the main
            # thread, and wrong tries count towards the same waits.
            def checked(ok: bool, message: str) -> None:
                if ok:
                    apply()
                    return
                self.add_toast(Adw.Toast(title=message or "That isn't the current passcode"))
                self.lock_switch.set_active(bool(self.settings["lock_hash"]))
            self.app.check_passcode(current.get_text(), checked)

        def apply() -> None:
            if remove:
                self.settings["lock_hash"] = ""
                self.lock_switch.set_active(False)
                return
            self.settings["lock_hash"] = applock.make_hash(first.get_text())
            self.lock_switch.set_active(True)
            self.add_toast(Adw.Toast(title="Passcode set" if not change else "Passcode changed"))

        alert.choose(self, None, answered)
        (current if needs_current else first).grab_focus()  # ready to type, as on the lock screen

    def _accounts(self) -> Adw.PreferencesPage:
        page = Adw.PreferencesPage(name="accounts", title="Accounts", icon_name="system-users-symbolic")
        self.account_group = Adw.PreferencesGroup(
            description="Each account has its own WhatsApp session. Link one by scanning its code with your phone. "
                        "Ctrl+1, Ctrl+2… switch between them.")
        self.account_rows: list[Gtk.Widget] = []
        self._fill_accounts()
        page.add(self.account_group)
        more = Adw.PreferencesGroup()
        add = button_row("Add Account", "list-add-symbolic")
        add.connect("activated", lambda *_: (self.app.activate_action("add-account", None), self._fill_accounts()))
        more.add(add)
        page.add(more)
        return page

    def _fill_accounts(self) -> None:
        for row in self.account_rows:
            self.account_group.remove(row)
        self.account_rows = []
        self._forget_accounts()
        alone = len(self.app.accounts) < 2
        for account in self.app.accounts:
            row = plain_row(account.name, account_status(account))
            row.add_prefix(Adw.Avatar(size=32, text=account.name, show_initials=True))
            rename = icon_button("document-edit-symbolic", "Rename")
            rename.connect("clicked", lambda _b, a=account: self._rename(a))
            row.add_suffix(rename)
            remove = icon_button("user-trash-symbolic", "Remove Account")
            if alone:
                remove.set_sensitive(False)
                remove.set_tooltip_text("The only account can't be removed")
            remove.connect("clicked", lambda _b, a=account: self._confirm_remove(a))
            row.add_suffix(remove)
            self.account_group.add(row)
            self.account_rows.append(row)
            # The line under the name keeps up while this is open: a new account goes from
            # loading to needing linking, and to linked once the phone has scanned the code.
            handler = account.connect("state-changed", lambda a, r=row: r.set_subtitle(account_status(a)))
            self._account_handlers.append((account, handler))

    def _forget_accounts(self) -> None:
        for account, handler in self._account_handlers:
            account.disconnect(handler)
        self._account_handlers = []

    # ---- Handlers --------------------------------------------------------------------------

    def _set_clock(self, row: Adw.EntryRow, key: str) -> None:
        parsed = parse_clock(row.get_text())
        if parsed is None:
            row.add_css_class("error")
            return
        text = parsed.strftime("%H:%M")
        row.set_text(text)
        self.settings[key] = text

    def _choose_folder(self, _button) -> None:
        dialog = Gtk.FileDialog(title="Save Files To", initial_folder=Gio.File.new_for_path(str(self.settings.download_dir)))

        def chosen(dlg, result) -> None:
            try:
                folder = dlg.select_folder_finish(result)
            except GLib.Error:
                return
            if folder and folder.get_path():
                self.settings["download_dir"] = folder.get_path()
                self.folder.set_subtitle(shown_folder(folder.get_path()))

        dialog.select_folder(self.get_root(), None, chosen)

    def _rename(self, account) -> None:
        name = Adw.EntryRow(title="Name", text=account.name, activates_default=True)
        rows = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE)
        rows.append(name)
        alert = Adw.AlertDialog(heading="Rename Account")
        alert.set_extra_child(rows)
        alert.add_response("cancel", "_Cancel")
        alert.add_response("rename", "_Rename")
        alert.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        alert.set_default_response("rename")
        alert.set_close_response("cancel")
        name.connect("changed", lambda r: alert.set_response_enabled("rename", bool(r.get_text().strip())))

        def answered(dlg, result) -> None:
            if dlg.choose_finish(result) == "rename" and name.get_text().strip():
                self.app.rename_account(account, name.get_text().strip())
                self._fill_accounts()

        alert.choose(self, None, answered)
        name.grab_focus()  # with the name selected, so typing replaces it

    def _confirm_remove(self, account) -> None:
        alert = Adw.AlertDialog(
            heading=f"Remove “{account.name}”?",
            # A linked account is also a linked device on the phone; one never linked isn't.
            body="The account is removed from this computer." if account.needs_link else
                 "The account is removed from this computer. "
                 "To unlink it fully, also remove it under Linked devices on your phone.")
        alert.add_response("cancel", "_Cancel")
        alert.add_response("remove", "_Remove")
        alert.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("cancel")

        def answered(dlg, result) -> None:
            if dlg.choose_finish(result) == "remove":
                self.app.remove_account(account)
                self._fill_accounts()

        alert.choose(self, None, answered)

