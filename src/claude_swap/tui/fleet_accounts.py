"""Account settings: add a login, sign in in the browser (add or renew, as
bare ``cc-swap login``), a token or API key, re-login the selected account,
alias, delete, and inspect every login (``cc-swap doctor`` in a modal).

codex-swap's ``manage`` screen shape: an account table, then the items,
then the key hints. Row keys act on the highlighted account; choosing an
item from the list first asks which account (pick mode).
"""

from __future__ import annotations

from functools import partial
from typing import TYPE_CHECKING

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, ListItem, ListView, Static

from claude_swap.maximize import fleet as fx
from claude_swap.models import AccountsSnapshot
from claude_swap.tui import menus
from claude_swap.tui.fleet_modals import TextInputModal
from claude_swap.tui.theme import Palette

if TYPE_CHECKING:
    from claude_swap.tui.app import CswapApp

_NEEDS_ACCOUNT = {"relogin", "alias", "delete"}
_PICK_VERBS = {"relogin": "re-login", "alias": "name", "delete": "delete"}


class AccountItem(ListItem):
    def __init__(self, key: str, title: str, action: str) -> None:
        super().__init__(Static("", markup=False))
        self.key, self.title, self.action_id = key, title, action


class AccountsScreen(Screen):
    CSS_PATH = "fleet.tcss"
    BINDINGS = [
        Binding("a", "item('add')", show=False),
        Binding("s", "item('new')", show=False),
        Binding("t", "item('token')", show=False),
        Binding("r", "row('relogin')", show=False),
        Binding("n", "row('alias')", show=False),
        Binding("d", "row('delete')", show=False),
        Binding("i", "item('verify')", show=False),
        Binding("b,escape,left", "back", show=False),
        Binding("q", "quit", show=False),
    ]

    app: "CswapApp"

    def __init__(self) -> None:
        super().__init__()
        self._numbers: list[str] = []
        self._picking: str | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="fx-ac-head", markup=False)
        yield DataTable(id="fx-ac-table", cursor_type="row", zebra_stripes=False)
        yield Static("", id="fx-ac-prompt", markup=False)
        yield ListView(
            *(AccountItem(k, t, a) for k, t, a in menus.ACCOUNT_ITEMS),
            id="fx-ac-menu",
        )
        yield Static("", id="fx-ac-keys", markup=False)

    def on_mount(self) -> None:
        table = self.query_one("#fx-ac-table", DataTable)
        for label in ("account", "login", "tier"):
            table.add_column(label, key=label)
        self.watch(self.app, "snapshot", self._on_snapshot)
        self._render_static()
        table.focus()

    # -- rendering -------------------------------------------------------------------

    def _palette(self) -> Palette:
        return Palette.from_theme(self.app.current_theme)

    def _render_static(self) -> None:
        from claude_swap.tui.fleet import menu_text

        palette = self._palette()
        self.query_one("#fx-ac-head", Static).update(
            Text("cc-swap · account settings", style=f"bold {palette.foreground}")
        )
        for item in self.query(AccountItem):
            item.query_one(Static).update(menu_text(item.title, item.key, palette))
        self.query_one("#fx-ac-keys", Static).update(
            Text(menus.ACCOUNT_KEYS, style=palette.muted)
        )
        self._render_prompt()

    def _render_prompt(self) -> None:
        palette = self._palette()
        text = ""
        if self._picking:
            text = (
                f"Which account to {_PICK_VERBS[self._picking]}? "
                "↑↓ then enter · esc cancels"
            )
        self.query_one("#fx-ac-prompt", Static).update(Text(text, style=palette.accent))

    def _on_snapshot(self, snap: AccountsSnapshot | None) -> None:
        if snap is None or not self.is_attached:
            return
        from claude_swap.tui.fleet import fleet_rows_now, tone_style

        palette = self._palette()
        rows = fleet_rows_now(self.app, snap)
        table = self.query_one("#fx-ac-table", DataTable)
        keep = self.current_number()
        table.clear()
        for row in rows:
            login, tone = fx.login_text(row)
            table.add_row(
                Text(f"{row.name}  [{row.org}]" + ("  ● active" if row.active else ""),
                     style=tone_style(
                         "crit" if row.login == "relogin"
                         else "bold" if row.active else "plain", palette)),
                Text(login, style=tone_style(tone, palette)),
                Text(fx.TIER_CELLS.get(row.tier, row.tier),
                     style=tone_style("plain" if row.tier == "normal" else "dim", palette)),
                key=row.number,
            )
        self._numbers = [r.number for r in rows]
        if keep in self._numbers:
            table.move_cursor(row=self._numbers.index(keep))

    def current_number(self) -> str | None:
        table = self.query_one("#fx-ac-table", DataTable)
        if not self._numbers or not (0 <= table.cursor_row < len(self._numbers)):
            return None
        return self._numbers[table.cursor_row]

    # -- actions -----------------------------------------------------------------------

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item = event.item
        if isinstance(item, AccountItem):
            self.action_item(item.action_id)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self._picking:
            action, self._picking = self._picking, None
            self._render_prompt()
            self._run(action, str(event.row_key.value))

    def action_item(self, action: str) -> None:
        """A list item: account-less items run now; the rest ask which."""
        if action not in _NEEDS_ACCOUNT:
            self._run(action, None)
            return
        self._picking = action
        self._render_prompt()
        self.query_one("#fx-ac-table", DataTable).focus()

    def action_row(self, action: str) -> None:
        """A row key: act on the highlighted account."""
        number = self.current_number()
        if number is None:
            self.notify("No account selected", severity="warning")
            return
        self._run(action, number)

    def _run(self, action: str, number: str | None) -> None:
        app = self.app
        if action == "add":
            app.action_add_current()
        elif action == "new":
            from claude_swap.tui.fleet import open_sign_in

            open_sign_in(app)
        elif action == "token":
            app.action_add_token()
        elif action == "verify":
            from claude_swap.tui.fleet_doctor import DoctorModal

            app.push_screen(DoctorModal())
        elif action == "relogin" and number is not None:
            from claude_swap.tui.fleet import open_relogin

            open_relogin(app, number)
        elif action == "alias" and number is not None:
            snap = app.snapshot
            acc = next((a for a in (snap.accounts if snap else ()) if a.number == number), None)
            app.push_screen(
                TextInputModal(
                    f"Name {self._account_name(number)}",
                    "Alias (letters, digits, - _ .); empty removes it",
                    acc.alias if acc else "",
                ),
                partial(self._on_alias, number),
            )
        elif action == "delete" and number is not None:
            snap = app.snapshot
            email = next(
                (a.email for a in (snap.accounts if snap else ()) if a.number == number), "?"
            )
            app.confirm_remove(number, email)

    def _on_alias(self, number: str, alias: str | None) -> None:
        if alias is None:
            return
        switcher = self.app.switcher
        if alias:
            self.app._start_action(
                f"Name {self._account_name(number)}", partial(switcher.set_alias, number, alias)
            )
        else:
            self.app._start_action(
                f"Unname {self._account_name(number)}", partial(switcher.unset_alias, number)
            )

    def _account_name(self, number: str) -> str:
        """Slot ``number``'s display name (maximize/names.py)."""
        from claude_swap.tui.fleet import _who

        return _who(self.app, number)

    def action_back(self) -> None:
        if self._picking:
            self._picking = None
            self._render_prompt()
            return
        self.app.pop_screen()

    def action_quit(self) -> None:
        from claude_swap.tui.fleet import request_quit

        request_quit(self.app)
