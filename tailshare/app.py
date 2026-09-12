"""Interactive TUI: drop a file, get a tailnet link."""

from __future__ import annotations

import io
from pathlib import Path

from rich.markup import escape
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, OptionList, Static
from textual.widgets.option_list import Option

from . import __version__, core, daemon
from .core import KIND_ICON, Peer, SharedFile, human_size, relative_time

# ----------------------------------------------------------------------------- modals


class Confirm(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "no", "Cancel"), Binding("y", "yes", "Yes"), Binding("n", "no", "No")]

    def __init__(self, title: str, body: str, ok_label: str = "Delete") -> None:
        super().__init__()
        self._title, self._body, self._ok = title, body, ok_label

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self._title, classes="title")
            yield Static(self._body)
            with Vertical(classes="buttons"):
                yield Button("Cancel", id="no")
                yield Button(self._ok, id="yes", variant="error")
            yield Static("y / n · esc", classes="hint")

    @on(Button.Pressed)
    def _btn(self, ev: Button.Pressed) -> None:
        self.dismiss(ev.button.id == "yes")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class PickPeer(ModalScreen[Peer | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, peers: list[Peer], filename: str) -> None:
        super().__init__()
        self._peers = peers
        self._filename = filename

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(f"Send [b]{escape(self._filename)}[/b] via Taildrop to…", classes="title")
            opts = []
            for i, p in enumerate(self._peers):
                dot = "[green]●[/]" if p.online else "[dim]○[/]"
                label = f"{dot} {p.hostname}  [dim]{p.os}{'' if p.online else ' · offline'}[/]"
                opts.append(Option(label, id=str(i), disabled=not p.online))
            yield OptionList(*opts)
            yield Static("↑↓ pick · enter send · esc", classes="hint")

    @on(OptionList.OptionSelected)
    def _pick(self, ev: OptionList.OptionSelected) -> None:
        self.dismiss(self._peers[int(ev.option.id)])

    def action_cancel(self) -> None:
        self.dismiss(None)


class ShowQR(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close"), Binding("q", "close", "Close"), Binding("enter", "close", "Close")]

    def __init__(self, url: str, name: str) -> None:
        super().__init__()
        self._url, self._name = url, name

    def compose(self) -> ComposeResult:
        import qrcode

        qr = qrcode.QRCode(border=1, error_correction=qrcode.constants.ERROR_CORRECT_L)
        qr.add_data(self._url)
        buf = io.StringIO()
        qr.print_ascii(out=buf, invert=True)
        with Vertical(classes="dialog"):
            yield Static(Text(self._name, style="bold"), classes="title")
            yield Static(Text(buf.getvalue().rstrip("\n")), id="qr")
            yield Static(f"[dim]{self._url}[/]\nscan on a device that is on the tailnet · esc", classes="hint")

    def action_close(self) -> None:
        self.dismiss(None)


class Help(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "Close"), Binding("question_mark", "close", "Close")]

    TEXT = """[b]Share something[/b]
  drag a file or folder into this window   shared the moment it lands
  ⌘V a path, then [b]enter[/b]                    shared
  [b]p[/b]  (or enter on the empty prompt)         share the clipboard
                                            (screenshot, image, Finder files)
  folders are zipped before sharing · originals are never touched
  sharing continues after you quit (the server runs under launchd)

[b]On the selected file[/b]
  [b]enter[/b] · [b]c[/b]   copy link
  [b]o[/b]           open in the browser
  [b]k[/b]           show a QR code for your phone
  [b]s[/b]           send with Taildrop to another device
  [b]r[/b]           reveal in Finder
  [b]⌫[/b]           stop sharing (deletes the copy)

[b]Other[/b]
  [b]u[/b] · [b]U[/b]       copy · open the index page (all files)
  [b]L[/b]           switch links between http://<tailnet ip>/ and https://<name>.ts.net/
  [b]S[/b]           start / restart the share server (launchd)
  [b]R[/b]  refresh          [b]/[/b]  type a path          [b]q[/b]  quit"""

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("tailshare " + __version__, classes="title")
            yield Static(self.TEXT)
            yield Static("esc to close", classes="hint")

    def action_close(self) -> None:
        self.dismiss(None)


class DropInput(Input):
    """Path prompt. A paste into the empty prompt is a drop: shared at once, all lines kept."""

    def _on_paste(self, event: events.Paste) -> None:
        text = event.text
        if not self.value.strip() and ("\n" in text.strip() or core.parse_dropped_paths(text)[0]):
            event.stop()
            event.prevent_default()  # Textual also runs Input._on_paste unless told not to
            self.app.share_text(text)  # type: ignore[attr-defined]
            return
        super()._on_paste(event)


# ----------------------------------------------------------------------------- app


class TailshareApp(App[None]):
    TITLE = "tailshare"
    CSS_PATH = "app.tcss"
    BINDINGS = [
        Binding("enter", "copy_link", "copy link", show=False),
        Binding("c", "copy_link", "copy link", show=True),
        Binding("p", "paste", "paste clipboard", show=True),
        Binding("o", "open", "open", show=True),
        Binding("s", "send", "send", show=True),
        Binding("k", "qr", "qr", show=True),
        Binding("r", "reveal", "reveal", show=True),
        Binding("backspace", "delete", "delete", show=True, key_display="⌫"),
        Binding("delete", "delete", "delete", show=False),
        Binding("slash", "focus_input", "path", show=True, key_display="/"),
        Binding("L", "toggle_links", "ip/dns links", show=False),
        Binding("u", "copy_index", "copy index url", show=False),
        Binding("U", "open_index", "open index", show=False),
        Binding("S", "start_server", "server", show=False),
        Binding("R", "refresh", "refresh", show=False),
        Binding("question_mark", "help", "help", show=True, key_display="?"),
        Binding("q", "quit", "quit", show=True),
        Binding("escape", "focus_table", "", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.files: list[SharedFile] = []
        self.ts: core.TailscaleInfo = core.TailscaleInfo(False, error="…")
        self.srv: str = "…"
        self.serve_ok: bool | None = None
        self.link_mode: str = core.LINK_MODE
        self._busy = 0

    # -- layout ---------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(id="topbar"):
            yield Static(f"[b]tailshare[/b] [dim]{__version__}[/]", id="brand")
            yield Static("", id="status")
        yield Static("", id="urlbar")
        yield DataTable(id="files", cursor_type="row", zebra_stripes=False)
        yield Static("Nothing shared yet.\nDrag a file in, or press [b]p[/b] to share the clipboard.", id="empty")
        with Vertical(id="dropzone"):
            yield Static("", id="drophint")
            yield DropInput(placeholder="type a path and press enter · esc to go back", id="dropinput")
        yield Footer()

    def on_mount(self) -> None:
        self.theme = "tokyo-night"
        t = self.query_one(DataTable)
        t.add_column(" ", key="icon", width=2)
        t.add_column("Name", key="name")
        t.add_column("Size", key="size", width=9)
        t.add_column("Shared", key="when", width=11)
        t.focus()
        self._set_hint()
        self.refresh_files()
        self.refresh_status()
        self.set_interval(2.0, self.refresh_files)
        self.set_interval(6.0, self.refresh_status)

    # -- state ---------------------------------------------------------------------
    @property
    def table(self) -> DataTable:
        return self.query_one("#files", DataTable)

    @property
    def selected(self) -> SharedFile | None:
        t = self.table
        if not self.files or t.row_count == 0:
            return None
        i = t.cursor_row
        return self.files[i] if 0 <= i < len(self.files) else None

    def _set_hint(self, text: str | None = None) -> None:
        if text is None:
            text = "[b]⤓[/b]  Drop files here · shared instantly, link copied   [dim]folders become zips · originals untouched[/]"
        self.query_one("#drophint", Static).update(text)

    def refresh_files(self) -> None:
        try:
            files = core.list_shared()
        except OSError as e:
            self.notify(str(e), severity="error")
            return
        if files == self.files and self.table.row_count == len(files):
            for f in files:  # ages tick even when nothing changed
                self.table.update_cell(f.name, "when", Text(relative_time(f.mtime), justify="right", style="dim"))
            return
        keep = self.selected.name if self.selected else None
        self.files = files
        t = self.table
        t.clear()
        for f in files:
            name = f.name if len(f.name) <= 70 else f.name[:67] + "…"
            t.add_row(
                Text(KIND_ICON[f.kind], style="dim"),
                name,
                Text(human_size(f.size), justify="right", style="dim"),
                Text(relative_time(f.mtime), justify="right", style="dim"),
                key=f.name,
            )
        if keep:
            for i, f in enumerate(files):
                if f.name == keep:
                    t.move_cursor(row=i)
                    break
        empty = not files
        self.query_one("#empty").set_class(empty, "show")
        t.set_class(empty, "hide")
        self._update_urlbar()

    @work(thread=True, exclusive=True, group="status")
    def refresh_status(self) -> None:
        srv = core.server_status()
        ts = core.tailscale_info()
        ts.link_mode = self.link_mode
        routes = core.serve_routes() if ts.ok else None
        self.call_from_thread(self._apply_status, srv, ts, routes)

    @property
    def links_work(self) -> bool:
        return self.ts.ok and self.srv != "down" and self.serve_ok is not False

    def _apply_status(self, srv: str, ts: core.TailscaleInfo, routes: dict[str, bool] | None) -> None:
        self.routes = routes
        serve_ok = None if routes is None else routes["dns" if self.link_mode == "dns" else "ip"]
        self.srv, self.ts, self.serve_ok = srv, ts, serve_ok
        if srv == "ours":
            s = "[green]●[/] server"
        elif srv == "other":
            s = f"[yellow]●[/] server [dim](port {core.PORT} held by another process · S)[/]"
        else:
            s = "[red]○[/] server [dim]offline · S to start[/]"
        if not ts.ok:
            s += f"   [red]○[/] tailnet [dim]{ts.error}[/]"
        elif serve_ok is False:
            s += "   [yellow]●[/] tailnet [dim]serve not routed · S[/]"
        else:
            s += "   [green]●[/] tailnet"
        self.query_one("#status", Static).update(s)
        self._update_urlbar()

    def _update_urlbar(self) -> None:
        n = len(self.files)
        url = self.ts.base_url or "tailscale offline"
        mode = "ip · L for dns" if self.link_mode != "dns" else "dns · L for ip"
        self.query_one("#urlbar", Static).update(
            f"{url}   [dim]·   {n} file{'s' if n != 1 else ''}   ·   {mode}   ·   u copy · U open[/]"
        )

    def _url(self, f: SharedFile) -> str:
        return core.url_for(self.ts.base_url, f.name)

    def _need(self) -> SharedFile | None:
        f = self.selected
        if f is None:
            self.notify("Nothing selected", severity="warning", timeout=2)
        return f

    def _need_ts(self) -> bool:
        if not self.ts.ok:
            self.notify("Tailscale is offline; no link available", severity="error")
            return False
        return True

    # -- sharing ----------------------------------------------------------------
    def share_text(self, text: str) -> None:
        found, missing = core.parse_dropped_paths(text)
        for m in missing:
            self.notify(f"Not found: {m}", severity="error")
        if found:
            self.share_paths(found)

    def share_paths(self, paths: list[Path]) -> None:
        self._begin_busy(f"Sharing {paths[0].name if len(paths) == 1 else f'{len(paths)} items'}…")
        self._share_worker(paths)

    @work(thread=True, group="share")
    def _share_worker(self, paths: list[Path]) -> None:
        results: list[SharedFile] = []
        errors: list[str] = []
        for p in paths:
            try:
                results.append(core.add_path(p))
            except Exception as e:  # noqa: BLE001
                errors.append(f"{p.name}: {e}")
        self.call_from_thread(self._shared_done, results, errors)

    def action_paste(self) -> None:
        self._begin_busy("Reading clipboard…")
        self._paste_worker()

    @work(thread=True, group="share")
    def _paste_worker(self) -> None:
        try:
            results = core.paste_clipboard()
            errors: list[str] = []
            if not results:
                kind = core.clipboard_kind()
                errors = ["Clipboard is empty" if kind == "empty" else "Nothing shareable on the clipboard (no image, file or path)"]
        except Exception as e:  # noqa: BLE001
            results, errors = [], [str(e)]
        self.call_from_thread(self._shared_done, results, errors)

    def _shared_done(self, results: list[SharedFile], errors: list[str]) -> None:
        self._end_busy()
        for e in errors:
            self.notify(e, title="Could not share", severity="error")
        if not results:
            return
        self.refresh_files()
        last = results[-1]
        for i, f in enumerate(self.files):
            if f.name == last.name:
                self.table.move_cursor(row=i)
                break
        name = escape(last.name)
        if not self.ts.ok:
            self.notify(f"{name} saved locally · Tailscale is offline, no link", severity="warning", timeout=6)
            return
        copied = core.copy_to_clipboard(self._url(last))
        tail = "link copied" if copied else "could not copy link"
        if len(results) == 1:
            self.notify(f"{name}\n[dim]{tail}[/]", title="Shared", timeout=4)
        else:
            self.notify(f"{len(results)} files shared\n[dim]link to {name} {'copied' if copied else 'not copied'}[/]", title="Shared", timeout=5)
        if self.srv == "down":
            self.notify("Share server is offline, so the link will not open yet. Press S to start it.", severity="warning", timeout=6)
        elif self.serve_ok is False:
            self.notify("tailscale serve is not routed to the server. Press S to fix it.", severity="warning", timeout=6)

    def _begin_busy(self, hint: str) -> None:
        self._busy += 1
        self.query_one("#dropzone").add_class("busy")
        self._set_hint(f"[yellow]⋯[/] {hint}")

    def _end_busy(self) -> None:
        self._busy = max(0, self._busy - 1)
        if self._busy == 0:
            self.query_one("#dropzone").remove_class("busy")
            self._set_hint()

    # -- input / drop handling ---------------------------------------------
    def on_paste(self, event: events.Paste) -> None:
        # Drag-and-drop in iTerm2 / Terminal arrives as a (bracketed) paste.
        if isinstance(self.focused, Input):
            return  # the Input already inserted it; user presses enter
        event.stop()
        self.share_text(event.text)

    def on_key(self, event: events.Key) -> None:
        # A non-bracketed (key-by-key) drop starts with "~": route it into the prompt.
        # "/" is the focus-prompt shortcut, so a raw drop starting with "/" needs the prompt open first.
        if isinstance(self.focused, DataTable) and event.character == "~":
            event.stop()
            inp = self.query_one("#dropinput", Input)
            inp.focus()
            inp.insert_text_at_cursor(event.character)

    @on(Input.Submitted, "#dropinput")
    def _submit(self, ev: Input.Submitted) -> None:
        text = ev.value.strip()
        if not text:
            self.notify("Type or paste a path here, or press p to share the clipboard", timeout=3)
            self.table.focus()
            return
        found, missing = core.parse_dropped_paths(text)
        if not found:
            for m in missing:
                self.notify(f"Not found: {escape(m)}", severity="error")
            return  # keep the text so it can be corrected
        ev.input.value = ""
        self.share_paths(found)
        self.table.focus()

    @on(DataTable.RowSelected)
    def _row_selected(self) -> None:
        self.action_copy_link()

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        self.query_one("#dropzone").set_class(isinstance(event.widget, Input), "armed")

    # -- actions ---------------------------------------------------------------
    def action_focus_input(self) -> None:
        self.query_one("#dropinput", Input).focus()

    def action_focus_table(self) -> None:
        if isinstance(self.focused, Input):
            self.query_one("#dropinput", Input).value = ""
            self.table.focus()

    def action_copy_link(self) -> None:
        f = self._need()
        if f is None or not self._need_ts():
            return
        core.copy_to_clipboard(self._url(f))
        self.notify(f"{escape(f.name)}\n[dim]{escape(self._url(f))}[/]", title="Link copied", timeout=3)

    def action_open(self) -> None:
        f = self._need()
        if f is None or not self._need_ts():
            return
        core.open_url(self._url(f))

    def action_reveal(self) -> None:
        f = self._need()
        if f:
            core.reveal_in_finder(f.path)

    def action_qr(self) -> None:
        f = self._need()
        if f is None or not self._need_ts():
            return
        self.push_screen(ShowQR(self._url(f), f.name))

    def action_toggle_links(self) -> None:
        self.link_mode = "dns" if self.link_mode != "dns" else "ip"
        self.ts.link_mode = self.link_mode
        routes = getattr(self, "routes", None)
        self.serve_ok = None if routes is None else routes[self.link_mode]
        self._update_urlbar()
        self.notify(f"Links now use {escape(self.ts.base_url)}", timeout=2.5)

    def action_copy_index(self) -> None:
        if self._need_ts():
            core.copy_to_clipboard(self.ts.base_url)
            self.notify(escape(self.ts.base_url), title="Index link copied", timeout=3)

    def action_open_index(self) -> None:
        if self._need_ts():
            core.open_url(self.ts.base_url)

    def action_help(self) -> None:
        self.push_screen(Help())

    def action_refresh(self) -> None:
        self.files = []
        self.refresh_files()
        self.refresh_status()
        self.notify("Refreshed", timeout=1.5)

    def action_delete(self) -> None:
        f = self._need()
        if f is None:
            return

        def done(yes: bool | None) -> None:
            if yes:
                core.remove_shared(f.name)
                self.refresh_files()
                self.notify(f"{escape(f.name)} removed", timeout=2)

        self.push_screen(Confirm("Stop sharing?", f"[b]{escape(f.name)}[/b] will be deleted from the share folder. The original file is untouched.", ok_label="Stop sharing"), done)

    def action_send(self) -> None:
        f = self._need()
        if f is None or not self._need_ts():
            return
        peers = [p for p in (self.ts.peers or []) if p.taildrop or p.online]
        if not peers:
            self.notify("No devices on the tailnet", severity="warning")
            return

        def done(peer: Peer | None) -> None:
            if peer:
                self._begin_busy(f"Sending {f.name} to {peer.hostname}…")
                self._send_worker(f, peer)

        self.push_screen(PickPeer(peers, f.name), done)

    @work(thread=True, group="send")
    def _send_worker(self, f: SharedFile, peer: Peer) -> None:
        ok, msg = core.taildrop_send(f.path, peer)
        self.call_from_thread(self._send_done, ok, msg, f)

    def _send_done(self, ok: bool, msg: str, f: SharedFile) -> None:
        self._end_busy()
        if ok:
            self.notify(f"{escape(f.name)} {escape(msg)}", title="Taildrop", timeout=4)
        else:
            self.notify(escape(msg), title="Taildrop failed", severity="error", timeout=8)

    def action_start_server(self) -> None:
        if self.srv == "other":
            pids = ", ".join(map(str, core.pids_on_port())) or "unknown"

            def done(yes: bool | None) -> None:
                if yes:
                    self._begin_busy("Replacing server…")
                    self._start_worker()

            self.push_screen(
                Confirm(
                    f"Port {core.PORT} is in use",
                    f"Another process (pid {pids}) is listening on 127.0.0.1:{core.PORT}. "
                    "Stop it and run the tailshare server there under launchd?",
                    ok_label="Replace",
                ),
                done,
            )
            return
        self._begin_busy("Starting share server…")
        self._start_worker()

    @work(thread=True, group="server")
    def _start_worker(self) -> None:
        ok, msg = daemon.start()
        self.call_from_thread(self._start_done, ok, msg)

    def _start_done(self, ok: bool, msg: str) -> None:
        self._end_busy()
        self.notify(escape(msg), title="Server" if ok else "Server failed", severity="information" if ok else "error", timeout=6)
        self.refresh_status()
