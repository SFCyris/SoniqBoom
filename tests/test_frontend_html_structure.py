"""The frontend pages' element structure.

2.3.0 shipped an ``index.html`` whose Settings "Render options" section was
never closed: every element after it — the other Settings tabs, Track Info,
the shortcuts sheet, the queue, the EQ — ended up inside the hidden Settings
overlay, so the I / ? / Q / E keys and the player bar's info key opened
panels nobody could see, and the Settings tabs after Renderers came up empty.
The browser repairs such markup silently, so only a parse of the file shows it.
"""
from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent / "soniqboom" / "frontend"
PAGES = sorted(p.name for p in FRONTEND.glob("*.html"))

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "source", "track", "wbr", "param"}

# Panels a key or a header / player-bar button opens: none may sit inside
# another one (a hidden ancestor hides it however it is opened).
TOP_PANELS = ["admin-overlay", "ti-overlay", "shortcuts-overlay", "queue-panel",
              "eq-overlay", "links-overlay", "welcome-overlay", "player-bar",
              "cast-overlay", "radio-overlay", "playlist-panel"]


class _Tree(HTMLParser):
    """Records each id's ancestors (as ids / classes) and every mis-nesting."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, str, str, int]] = []   # (tag, id, class, line)
        self.problems: list[str] = []
        self.ids: dict[str, int] = {}
        self.ancestors: dict[str, list[tuple[str, str]]] = {}

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        el_id = a.get("id") or ""
        if el_id:
            if el_id in self.ids:
                self.problems.append(f"line {self.getpos()[0]}: duplicate id {el_id!r} "
                                     f"(first at line {self.ids[el_id]})")
            else:
                self.ids[el_id] = self.getpos()[0]
                self.ancestors[el_id] = [(i, c) for _, i, c, _ in self.stack]
        if tag not in VOID:
            self.stack.append((tag, el_id, a.get("class") or "", self.getpos()[0]))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for t, el_id, cls, line in self.stack[i + 1:]:
                    self.problems.append(
                        f"line {self.getpos()[0]}: </{tag}> closes <{t} id={el_id!r} "
                        f"class={cls!r}> from line {line}, which was never closed")
                del self.stack[i:]
                return
        self.problems.append(f"line {self.getpos()[0]}: stray </{tag}>")

    def close(self):
        super().close()
        for t, el_id, cls, line in self.stack:
            self.problems.append(f"<{t} id={el_id!r} class={cls!r}> from line {line} is never closed")


def _parse(text: str) -> _Tree:
    tree = _Tree()
    tree.feed(text)
    tree.close()
    return tree


def _index() -> _Tree:
    return _parse((FRONTEND / "index.html").read_text(encoding="utf-8"))


def test_the_pages_are_found():
    assert {"index.html", "mobile.html", "multiroom.html"} <= set(PAGES), PAGES


@pytest.mark.parametrize("page", PAGES)
def test_every_element_is_closed_where_it_was_opened(page):
    tree = _parse((FRONTEND / page).read_text(encoding="utf-8"))
    assert tree.problems == [], "\n".join(tree.problems[:10])


def test_the_panels_keys_open_sit_outside_each_other():
    tree = _index()
    for panel in TOP_PANELS:
        assert panel in tree.ancestors, f"#{panel} is missing from index.html"
        inside = [i for i, _ in tree.ancestors[panel] if i in TOP_PANELS]
        assert inside == [], f"#{panel} sits inside #{inside[0]}: it can never be seen on its own"


def test_every_settings_pane_sits_directly_in_the_panel_body():
    text = (FRONTEND / "index.html").read_text(encoding="utf-8")
    tree = _parse(text)

    class _Tabs(HTMLParser):
        def __init__(self):
            super().__init__()
            self.tabs: list[str] = []

        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if "admin-tab" in (a.get("class") or "").split() and a.get("data-tab"):
                self.tabs.append(a["data-tab"])

    tabs = _Tabs()
    tabs.feed(text)
    assert len(tabs.tabs) >= 8, tabs.tabs
    for pane in tabs.tabs:
        assert pane in tree.ancestors, f"Settings tab {pane!r} has no pane"
        parent_id, parent_class = tree.ancestors[pane][-1]
        assert "admin-panel-body" in parent_class.split(), (
            f"#{pane} sits inside {parent_id or parent_class!r}, not the Settings panel body: "
            "it shows only when that element does")


def test_the_check_catches_an_unclosed_section():
    """(The check itself: one missing </div> must be reported.)"""
    broken = ('<div id="admin-overlay"><div class="admin-panel-body">'
              '<div id="tab-a" class="admin-tab-pane"><div class="admin-section">x'
              '</div></div></div><div id="ti-overlay"></div>')
    tree = _parse(broken)
    assert tree.problems, "an unclosed section went unnoticed"
    # (three of the four </div> close the section, the pane and the body:
    # the overlay is still open, and the next panel lands in it)
    assert [i for i, _ in tree.ancestors["ti-overlay"]] == ["admin-overlay"]
