"""
Bounded, READ-ONLY dump of the visible text in an application's windows via
the macOS Accessibility API — pure Python (ctypes), no build step.

Otto uses this to read Slack.app. It replaces AppleScript's
``entire contents of window 1`` (System Events), which walks Slack's whole
Electron UI tree one Apple Event at a time and routinely takes 10-60 s.
Native AX calls cost microseconds, so the same read finishes in well under a
second, and the walk is capped by node count, character count and a
wall-clock deadline so it can never stall a refresh.

Besides the text, the walk notices Slack's sidebar: every conversation the
sidebar lists (channels, DMs, apps), which section it is in and whether Slack
marks it unread, mention-badged, muted or selected. Slack renders that state
as CSS classes (``p-channel_sidebar__channel--unread`` …) which Electron
exposes as ``AXDOMClassList``; ``--json`` returns it alongside the text of
every window, so Otto can say what is waiting in conversations it cannot see
without opening them.

Why Python and not a compiled helper
------------------------------------
macOS grants Accessibility to a *code identity*. The engine runs under
launchd as the Python interpreter, and children that are Apple platform
binaries (osascript, python) are attributed to that same identity — so the
one grant the user makes for Otto's interpreter covers this reader too. A
separately compiled helper is its own identity: it is silently denied under
launchd even when the interpreter is allowed, and the grant would break on
every rebuild. Running this file with ``sys.executable`` sidesteps all of
that and removes the Xcode toolchain from the setup path.

Run it by *file path* (``python3 ax_dump.py Slack``), not with ``-m``: the
``otto.adapters.browser`` package imports every adapter, which would add
hundreds of milliseconds to each read for nothing. This module therefore
imports only the standard library.

The one thing this reader tells the app
---------------------------------------
Electron apps build their accessibility tree only once an assistive client
has announced itself; until then the window exposes nothing but its title
bar. The documented way for a client to announce itself is to set the
app-level attribute ``AXManualAccessibility`` to true — a flag on the
*application* element meaning "a screen reader is here", nothing else. This
reader sets exactly that flag, on the application element only, and nothing
else, ever: no UI element attribute is written, no action is performed, no
event is posted. (``AXEnhancedUserInterface``, the flag VoiceOver sets, is
deliberately not used: it changes how Chromium windows animate and resize.)
``--no-announce`` turns even that off.

Usage: ax_dump.py <AppName> [--json] [--no-announce] [--max-nodes N] [--max-chars N] [--max-ms N]
  exit 0  text on stdout (window title first, then visible text in document order);
          with --json one object: {"app", "windows": [{"title", "text"}], "conversations": [...],
          "announced", "truncated", "nodes", "ms"}
  exit 1  app not running / no window / nothing readable
  exit 2  Accessibility permission not granted (nothing is prompted)
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
import time
from typing import Any, Dict, Iterable, List, NamedTuple, Optional

EXIT_OK = 0
EXIT_UNAVAILABLE = 1
EXIT_NOT_TRUSTED = 2

NOT_TRUSTED_MESSAGE = "not allowed assistive access"

DEFAULT_MAX_NODES = 6000
DEFAULT_MAX_CHARS = 60_000
DEFAULT_MAX_MS = 3000
# After announcing ourselves to an Electron app, how long to wait for it to
# publish its tree (it takes Slack two or three seconds the first time).
DEFAULT_WARMUP_MS = 5000
# Per-message timeout for the target app; a hung app must not hang the reader.
APP_MESSAGING_TIMEOUT_S = 1.0
# A window whose tree has fewer nodes than this is just a title bar.
_CHROME_ONLY_NODES = 30
# Electron's "an assistive client is present" flag (application element only).
ANNOUNCE_ATTRIBUTE = "AXManualAccessibility"

_LEAF_TEXT_ROLES = {"AXButton", "AXLink", "AXHeading", "AXMenuItem", "AXCheckBox", "AXRadioButton"}
_SKIP_ROLES = {"AXMenuBar", "AXMenu", "AXScrollBar", "AXSplitter", "AXToolbar"}

# Slack's sidebar, as CSS classes (Electron exposes them as AXDOMClassList).
SIDEBAR_ITEM_CLASS = "p-channel_sidebar__channel"
# The link on every message's time; its description is the full date and time.
TIMESTAMP_CLASS = "c-timestamp"
SIDEBAR_SECTION_CLASS = "p-channel_sidebar__static_list__item--section_header_focus"
_SIDEBAR_MODIFIER_PREFIX = SIDEBAR_ITEM_CLASS + "--"
_SIDEBAR_ITEM_MAX_NODES = 40


class DumpResult(NamedTuple):
    code: int
    text: str
    error: str


class WindowDump(NamedTuple):
    title: str
    text: str


class AppDump(NamedTuple):
    code: int
    windows: List[WindowDump]
    conversations: List[Dict[str, Any]]
    error: str
    announced: bool = False
    truncated: bool = False
    nodes: int = 0
    ms: int = 0

    @property
    def text(self) -> str:
        """The main window's text (title first) — what the text mode prints."""
        return self.windows[0].text if self.windows else ""

    def to_json(self, app_name: str) -> str:
        return json.dumps({
            "app": app_name,
            "windows": [{"title": w.title, "text": w.text} for w in self.windows],
            "conversations": self.conversations,
            "announced": self.announced,
            "truncated": self.truncated,
            "nodes": self.nodes,
            "ms": self.ms,
        }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Foreign function bindings (loaded lazily; this module imports on any OS)
# ---------------------------------------------------------------------------

_CFTypeRef = ctypes.c_void_p
_CFIndex = ctypes.c_long
_kCFStringEncodingUTF8 = 0x08000100
_PROC_ALL_PIDS = 1
_PROC_PIDPATHINFO_MAXSIZE = 4 * 1024


class _Frameworks:
    """ctypes handles for CoreFoundation, ApplicationServices and libproc."""

    def __init__(self) -> None:
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        ax = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
        proc = ctypes.CDLL("/usr/lib/libproc.dylib")

        cf.CFStringCreateWithCString.restype = _CFTypeRef
        cf.CFStringCreateWithCString.argtypes = [_CFTypeRef, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFStringGetLength.restype = _CFIndex
        cf.CFStringGetLength.argtypes = [_CFTypeRef]
        cf.CFStringGetMaximumSizeForEncoding.restype = _CFIndex
        cf.CFStringGetMaximumSizeForEncoding.argtypes = [_CFIndex, ctypes.c_uint32]
        cf.CFStringGetCString.restype = ctypes.c_ubyte
        cf.CFStringGetCString.argtypes = [_CFTypeRef, ctypes.c_char_p, _CFIndex, ctypes.c_uint32]
        cf.CFArrayCreate.restype = _CFTypeRef
        cf.CFArrayCreate.argtypes = [_CFTypeRef, ctypes.POINTER(_CFTypeRef), _CFIndex, ctypes.c_void_p]
        cf.CFArrayGetCount.restype = _CFIndex
        cf.CFArrayGetCount.argtypes = [_CFTypeRef]
        cf.CFArrayGetValueAtIndex.restype = _CFTypeRef
        cf.CFArrayGetValueAtIndex.argtypes = [_CFTypeRef, _CFIndex]
        cf.CFGetTypeID.restype = ctypes.c_ulong
        cf.CFGetTypeID.argtypes = [_CFTypeRef]
        cf.CFStringGetTypeID.restype = ctypes.c_ulong
        cf.CFArrayGetTypeID.restype = ctypes.c_ulong
        cf.CFBooleanGetTypeID.restype = ctypes.c_ulong
        cf.CFBooleanGetValue.restype = ctypes.c_ubyte
        cf.CFBooleanGetValue.argtypes = [_CFTypeRef]
        cf.CFEqual.restype = ctypes.c_ubyte
        cf.CFEqual.argtypes = [_CFTypeRef, _CFTypeRef]
        cf.CFRetain.restype = _CFTypeRef
        cf.CFRetain.argtypes = [_CFTypeRef]
        cf.CFRelease.restype = None
        cf.CFRelease.argtypes = [_CFTypeRef]

        ax.AXIsProcessTrusted.restype = ctypes.c_ubyte
        ax.AXUIElementCreateApplication.restype = _CFTypeRef
        ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int]
        ax.AXUIElementSetMessagingTimeout.restype = ctypes.c_int
        ax.AXUIElementSetMessagingTimeout.argtypes = [_CFTypeRef, ctypes.c_float]
        ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int
        ax.AXUIElementCopyAttributeValue.argtypes = [_CFTypeRef, _CFTypeRef, ctypes.POINTER(_CFTypeRef)]
        ax.AXUIElementCopyMultipleAttributeValues.restype = ctypes.c_int
        ax.AXUIElementCopyMultipleAttributeValues.argtypes = [
            _CFTypeRef, _CFTypeRef, ctypes.c_uint32, ctypes.POINTER(_CFTypeRef),
        ]
        ax.AXUIElementGetTypeID.restype = ctypes.c_ulong
        # The single setter, used by announce() for ANNOUNCE_ATTRIBUTE on the
        # application element and nowhere else (a test pins this).
        ax.AXUIElementSetAttributeValue.restype = ctypes.c_int
        ax.AXUIElementSetAttributeValue.argtypes = [_CFTypeRef, _CFTypeRef, _CFTypeRef]

        proc.proc_listpids.restype = ctypes.c_int
        proc.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
        proc.proc_pidpath.restype = ctypes.c_int
        proc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]

        self.cf, self.ax, self.proc = cf, ax, proc
        self.string_tid = cf.CFStringGetTypeID()
        self.array_tid = cf.CFArrayGetTypeID()
        self.boolean_tid = cf.CFBooleanGetTypeID()
        self.element_tid = ax.AXUIElementGetTypeID()
        self.type_array_callbacks = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeArrayCallBacks"))
        self.true_ref = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue").value

    # -- CoreFoundation helpers -------------------------------------------------

    def cfstr(self, value: str) -> int:
        return self.cf.CFStringCreateWithCString(None, value.encode("utf-8"), _kCFStringEncodingUTF8)

    def to_str(self, ref: Optional[int]) -> Optional[str]:
        if not ref or self.cf.CFGetTypeID(ref) != self.string_tid:
            return None
        length = self.cf.CFStringGetLength(ref)
        size = self.cf.CFStringGetMaximumSizeForEncoding(length, _kCFStringEncodingUTF8) + 1
        buf = ctypes.create_string_buffer(size)
        if not self.cf.CFStringGetCString(ref, buf, size, _kCFStringEncodingUTF8):
            return None
        return buf.value.decode("utf-8", "replace")

    def to_bool(self, ref: Optional[int]) -> Optional[bool]:
        if not ref or self.cf.CFGetTypeID(ref) != self.boolean_tid:
            return None
        return bool(self.cf.CFBooleanGetValue(ref))

    def elements(self, array_ref: Optional[int]) -> List[int]:
        """AXUIElement members of a CFArray (borrowed references)."""
        if not array_ref or self.cf.CFGetTypeID(array_ref) != self.array_tid:
            return []
        out: List[int] = []
        for i in range(self.cf.CFArrayGetCount(array_ref)):
            item = self.cf.CFArrayGetValueAtIndex(array_ref, i)
            if item and self.cf.CFGetTypeID(item) == self.element_tid:
                out.append(item)
        return out

    def strings(self, array_ref: Optional[int]) -> List[str]:
        """String members of a CFArray (e.g. AXDOMClassList)."""
        if not array_ref or self.cf.CFGetTypeID(array_ref) != self.array_tid:
            return []
        out: List[str] = []
        for i in range(self.cf.CFArrayGetCount(array_ref)):
            s = self.to_str(self.cf.CFArrayGetValueAtIndex(array_ref, i))
            if s:
                out.append(s)
        return out

    def attribute(self, element: int, name_ref: int) -> Optional[int]:
        """Copy one attribute (caller owns the result) or ``None``."""
        out = _CFTypeRef()
        if self.ax.AXUIElementCopyAttributeValue(element, name_ref, ctypes.byref(out)) != 0:
            return None
        return out.value

    def release(self, ref: Optional[int]) -> None:
        if ref:
            self.cf.CFRelease(ref)

    # -- process lookup ---------------------------------------------------------

    def pids_for_app(self, app_name: str) -> List[int]:
        """PIDs whose executable is ``<app_name>.app`` (or is named *app_name*).

        Helper processes live in nested bundles (``Slack Helper (Renderer).app``)
        and so never match the parent app's name.
        """
        wanted = app_name.strip().lower()
        if not wanted:
            return []
        needed = self.proc.proc_listpids(_PROC_ALL_PIDS, 0, None, 0)
        if needed <= 0:
            return []
        buf = (ctypes.c_int * (needed // ctypes.sizeof(ctypes.c_int) + 64))()
        got = self.proc.proc_listpids(_PROC_ALL_PIDS, 0, buf, ctypes.sizeof(buf))
        path_buf = ctypes.create_string_buffer(_PROC_PIDPATHINFO_MAXSIZE)
        matches: List[int] = []
        for i in range(max(0, got) // ctypes.sizeof(ctypes.c_int)):
            pid = buf[i]
            if pid <= 0:
                continue
            if self.proc.proc_pidpath(pid, path_buf, _PROC_PIDPATHINFO_MAXSIZE) <= 0:
                continue
            path = path_buf.value.decode("utf-8", "replace")
            if _app_name_from_path(path).lower() == wanted:
                matches.append(pid)
        return matches


def _app_name_from_path(path: str) -> str:
    """``/Applications/Slack.app/Contents/MacOS/Slack`` → ``Slack``; plain binaries → basename."""
    parts = path.split("/")
    bundles = [p[:-4] for p in parts if p.endswith(".app")]
    if bundles:
        return bundles[-1]
    return parts[-1] if parts else ""


_FRAMEWORKS: Optional[_Frameworks] = None


def _frameworks() -> _Frameworks:
    global _FRAMEWORKS
    if _FRAMEWORKS is None:
        _FRAMEWORKS = _Frameworks()
    return _FRAMEWORKS


def available() -> bool:
    """True on macOS when the system frameworks load."""
    if sys.platform != "darwin":
        return False
    try:
        _frameworks()
        return True
    except OSError:
        return False


def is_trusted() -> bool:
    """Whether *this process* may use the Accessibility API. Never prompts."""
    return available() and bool(_frameworks().ax.AXIsProcessTrusted())


# ---------------------------------------------------------------------------
# Finding the app and its windows
# ---------------------------------------------------------------------------

_NAMES = ("AXMainWindow", "AXFocusedWindow", "AXWindows", "AXTitle", "AXRole", "AXValue",
          "AXDescription", "AXChildren", "AXDOMClassList", ANNOUNCE_ATTRIBUTE)


def _find_app(fw: _Frameworks, app_name: str, names: dict) -> Optional[int]:
    """Owned reference to the application element that has windows, or ``None``."""
    for pid in fw.pids_for_app(app_name):
        app = fw.ax.AXUIElementCreateApplication(pid)
        if not app:
            continue
        fw.ax.AXUIElementSetMessagingTimeout(app, APP_MESSAGING_TIMEOUT_S)
        windows = fw.attribute(app, names["AXWindows"])
        has_windows = bool(fw.elements(windows))
        fw.release(windows)
        if has_windows:
            return app
        main = fw.attribute(app, names["AXMainWindow"])
        if main:
            fw.release(main)
            return app
        fw.release(app)
    return None


def _windows(fw: _Frameworks, app: int, names: dict) -> List[int]:
    """Owned references to the app's windows: main (or focused) first, titled ones next."""
    picked: List[int] = []

    def add(candidate: Optional[int]) -> None:
        if not candidate:
            return
        for have in picked:
            if fw.cf.CFEqual(have, candidate):
                return
        picked.append(fw.cf.CFRetain(candidate))

    first = fw.attribute(app, names["AXMainWindow"]) or fw.attribute(app, names["AXFocusedWindow"])
    add(first)
    fw.release(first)
    windows = fw.attribute(app, names["AXWindows"])
    untitled: List[int] = []
    for candidate in fw.elements(windows):
        title = fw.attribute(candidate, names["AXTitle"])
        titled = bool(fw.to_str(title))
        fw.release(title)
        if titled:
            add(candidate)
        else:
            untitled.append(candidate)
    if not picked:
        for candidate in untitled[:1]:
            add(candidate)
    fw.release(windows)
    return picked


def _find_window(fw: _Frameworks, app_name: str, names: dict) -> Optional[int]:
    """Owned reference to the app's main/focused/first titled window, or ``None``."""
    app = _find_app(fw, app_name, names)
    if not app:
        return None
    windows = _windows(fw, app, names)
    fw.release(app)
    for extra in windows[1:]:
        fw.release(extra)
    return windows[0] if windows else None


def announce(fw: _Frameworks, app: int, names: dict) -> bool:
    """Tell an Electron app that an assistive client is present.

    Reads ``AXManualAccessibility`` on the *application* element; when the app
    has the attribute (Electron) and it is false, sets it to true. Returns
    True only when the flag was flipped by this call. Apps without the
    attribute (native apps) are left untouched. This is the only attribute
    this module ever writes.
    """
    current = fw.attribute(app, names[ANNOUNCE_ATTRIBUTE])
    value = fw.to_bool(current)
    fw.release(current)
    if value is None or value is True:
        return False
    return fw.ax.AXUIElementSetAttributeValue(app, names[ANNOUNCE_ATTRIBUTE], fw.true_ref) == 0


def _node_count(fw: _Frameworks, root: int, names: dict, limit: int) -> int:
    """How many nodes a shallow walk finds (bounded) — tells a bare title bar from a real tree."""
    count = 0
    stack = [fw.cf.CFRetain(root)]
    try:
        while stack and count < limit:
            element = stack.pop()
            count += 1
            children = fw.attribute(element, names["AXChildren"])
            for child in fw.elements(children):
                stack.append(fw.cf.CFRetain(child))
            fw.release(children)
            fw.release(element)
    finally:
        for leftover in stack:
            fw.release(leftover)
    return count


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------

class _Budget:
    def __init__(self, max_nodes: int, max_chars: int, max_ms: int) -> None:
        self.max_nodes = max_nodes
        self.max_chars = max_chars
        self.deadline = time.monotonic() + max_ms / 1000.0
        self.nodes = 0
        self.chars = 0
        self.truncated = False

    def exhausted(self) -> bool:
        if self.nodes > self.max_nodes or self.chars >= self.max_chars:
            self.truncated = True
        elif self.nodes % 64 == 0 and time.monotonic() > self.deadline:
            self.truncated = True
        return self.truncated


def _sidebar_item(fw: _Frameworks, element: int, names: dict, classes: List[str], section: str) -> Optional[Dict[str, Any]]:
    """One sidebar conversation from its ``p-channel_sidebar__channel`` node (bounded mini-walk)."""
    modifiers = {c[len(_SIDEBAR_MODIFIER_PREFIX):] for c in classes if c.startswith(_SIDEBAR_MODIFIER_PREFIX)}
    texts: List[str] = []
    avatar = False
    stack = [fw.cf.CFRetain(element)]
    visited = 0
    try:
        while stack and visited < _SIDEBAR_ITEM_MAX_NODES:
            node = stack.pop()
            visited += 1
            value = fw.attribute(node, names["AXValue"])
            text = (fw.to_str(value) or "").strip()
            fw.release(value)
            if text:
                texts.append(text)
            if node != element:
                node_classes = fw.attribute(node, names["AXDOMClassList"])
                if any(c.startswith("p-channel_sidebar__user_avatar") for c in fw.strings(node_classes)):
                    avatar = True
                fw.release(node_classes)
            children = fw.attribute(node, names["AXChildren"])
            for child in reversed(fw.elements(children)):
                stack.append(fw.cf.CFRetain(child))
            fw.release(children)
            fw.release(node)
    finally:
        for leftover in stack:
            fw.release(leftover)
    badge = 0
    name = ""
    for text in texts:
        if text.isdigit():
            badge = max(badge, int(text))
        elif not name and text.lower() not in ("you", "(you)"):
            name = text
    if not name:
        return None
    dm = avatar or any(m in ("im", "im-you", "mpim") or m.startswith("im-") for m in modifiers)
    return {
        "name": name,
        "section": section,
        "dm": dm,
        "unread": "unread" in modifiers,
        "badge": badge if badge else ("has-badge" in modifiers),
        "selected": "selected" in modifiers,
        "muted": "muted" in modifiers,
        "self": "im-you" in modifiers,
        "texts": texts,
    }


def _walk_window(fw: _Frameworks, root: int, names: dict, wanted: int, budget: _Budget,
                 conversations: List[Dict[str, Any]]) -> WindowDump:
    """Visible text of one window in document order, noting sidebar conversations on the way."""
    lines: List[str] = []
    state = {"last": ""}

    def emit(raw: str) -> bool:
        text = raw.strip()
        if len(text) < 3 or text == state["last"]:
            return True
        state["last"] = text
        lines.append(text)
        budget.chars += len(text) + 1
        return budget.chars < budget.max_chars

    title_ref = fw.attribute(root, names["AXTitle"])
    title = fw.to_str(title_ref) or ""
    fw.release(title_ref)
    if title:
        emit(title)

    section = ""
    stack: List[int] = [fw.cf.CFRetain(root)]  # owned references
    try:
        while stack:
            element = stack.pop()
            budget.nodes += 1
            if budget.exhausted():
                fw.release(element)
                break
            values = _CFTypeRef()
            ok = fw.ax.AXUIElementCopyMultipleAttributeValues(element, wanted, 0, ctypes.byref(values)) == 0
            arr = values.value
            if not ok or not arr:
                fw.release(element)
                continue
            if fw.cf.CFGetTypeID(arr) != fw.array_tid or fw.cf.CFArrayGetCount(arr) != 6:
                fw.release(arr)
                fw.release(element)
                continue
            item = lambda i: fw.cf.CFArrayGetValueAtIndex(arr, i)  # noqa: E731 — borrowed refs
            role = fw.to_str(item(0)) or ""
            if role in _SKIP_ROLES:
                fw.release(arr)
                fw.release(element)
                continue
            classes = fw.strings(item(5))
            if classes and SIDEBAR_SECTION_CLASS in classes:
                section = fw.to_str(item(3)) or fw.to_str(item(2)) or section
            if classes and SIDEBAR_ITEM_CLASS in classes:
                entry = _sidebar_item(fw, element, names, classes, section)
                fw.release(arr)
                fw.release(element)
                if entry is not None:
                    keep_going = True
                    for text in entry.pop("texts"):
                        keep_going = emit(text) and keep_going
                    conversations.append(entry)
                    if not keep_going:
                        break
                continue
            if role == "AXLink" and classes and TIMESTAMP_CLASS in classes:
                # A message's time is rendered short ("10:47", "5:36 PM") and
                # the day dividers carry no text in the tree (a "Jump to date"
                # pill), so the day a message was posted lives in one place
                # only: the timestamp link's description ("Sep 7th at
                # 10:47:28 AM", "Today at 9:38:16 AM"). Emit that instead of
                # descending to the short form, which would date every
                # message today.
                label = fw.to_str(item(3)) or ""
                fw.release(arr)
                fw.release(element)
                if label and not emit(label):
                    budget.truncated = True
                    break
                continue
            children = fw.elements(item(4))
            value = fw.to_str(item(1))
            keep_going = True
            if value:
                keep_going = emit(value)
            elif not children and role in _LEAF_TEXT_ROLES:
                label = fw.to_str(item(2)) or fw.to_str(item(3)) or ""
                if label:
                    keep_going = emit(label)
            if keep_going:
                # Push in reverse so the traversal keeps document order.
                for child in reversed(children):
                    stack.append(fw.cf.CFRetain(child))
            fw.release(arr)
            fw.release(element)
            if not keep_going:
                budget.truncated = True
                break
    finally:
        for leftover in stack:
            fw.release(leftover)
    return WindowDump(title, "\n".join(lines))


def dump_app(
    app_name: str,
    *,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_ms: int = DEFAULT_MAX_MS,
    announce_client: bool = True,
    warmup_ms: int = DEFAULT_WARMUP_MS,
) -> AppDump:
    """Text of every window of *app_name* (main first) plus the sidebar inventory, bounded.

    Read-only apart from :func:`announce` (see the module docstring); only
    ``Copy*`` accessors are used for everything else.
    """
    started = time.monotonic()
    if not available():
        return AppDump(EXIT_UNAVAILABLE, [], [], "Accessibility API unavailable on this platform")
    fw = _frameworks()
    if not fw.ax.AXIsProcessTrusted():
        return AppDump(EXIT_NOT_TRUSTED, [], [], NOT_TRUSTED_MESSAGE)

    names = {key: fw.cfstr(key) for key in _NAMES}
    keys = (_CFTypeRef * 6)(names["AXRole"], names["AXValue"], names["AXTitle"],
                            names["AXDescription"], names["AXChildren"], names["AXDOMClassList"])
    wanted = fw.cf.CFArrayCreate(None, keys, 6, fw.type_array_callbacks)
    app = None
    windows: List[int] = []
    announced = False
    try:
        app = _find_app(fw, app_name, names)
        if not app:
            running = bool(fw.pids_for_app(app_name))
            return AppDump(EXIT_UNAVAILABLE, [], [], f"{app_name} has no window" if running else f"{app_name} is not running")
        windows = _windows(fw, app, names)
        if not windows:
            return AppDump(EXIT_UNAVAILABLE, [], [], f"{app_name} has no window")

        def published() -> bool:
            return _node_count(fw, windows[0], names, _CHROME_ONLY_NODES + 1) > _CHROME_ONLY_NODES

        if announce_client and not published():
            # A bare title bar: an Electron app that has not heard from an
            # assistive client yet. Say we are here, then wait for the tree
            # this once rather than report an empty window.
            announced = announce(fw, app, names)
            if announced and warmup_ms > 0:
                until = time.monotonic() + warmup_ms / 1000.0
                while time.monotonic() < until and not published():
                    time.sleep(0.25)

        budget = _Budget(max_nodes, max_chars, max_ms)
        conversations: List[Dict[str, Any]] = []
        dumps: List[WindowDump] = []
        for window in windows:
            if budget.exhausted():
                break
            dumps.append(_walk_window(fw, window, names, wanted, budget, conversations))
        ms = int((time.monotonic() - started) * 1000)
        if not any(d.text for d in dumps):
            return AppDump(EXIT_UNAVAILABLE, [], [], f"{app_name} window has no readable text", announced, budget.truncated, budget.nodes, ms)
        return AppDump(EXIT_OK, dumps, conversations, "", announced, budget.truncated, budget.nodes, ms)
    finally:
        for window in windows:
            fw.release(window)
        fw.release(app)
        fw.release(wanted)
        for ref in names.values():
            fw.release(ref)


def dump_window_text(
    app_name: str,
    *,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_chars: int = DEFAULT_MAX_CHARS,
    max_ms: int = DEFAULT_MAX_MS,
    announce_client: bool = True,
) -> DumpResult:
    """Visible text of *app_name*'s main window, in document order, bounded (text mode)."""
    result = dump_app(app_name, max_nodes=max_nodes, max_chars=max_chars, max_ms=max_ms, announce_client=announce_client)
    return DumpResult(result.code, result.text, result.error)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: Iterable[str]) -> dict:
    opts: dict = {"app": "Slack", "max_nodes": DEFAULT_MAX_NODES, "max_chars": DEFAULT_MAX_CHARS,
                  "max_ms": DEFAULT_MAX_MS, "json": False, "announce": True}
    args = list(argv)
    while args:
        arg = args.pop(0)
        if arg in ("--max-nodes", "--max-chars", "--max-ms") and args:
            try:
                opts[arg[2:].replace("-", "_")] = max(1, int(args.pop(0)))
            except ValueError:
                pass
        elif arg == "--json":
            opts["json"] = True
        elif arg == "--no-announce":
            opts["announce"] = False
        elif not arg.startswith("--"):
            opts["app"] = arg
    return opts


def main(argv: Optional[Iterable[str]] = None) -> int:
    opts = _parse_args(sys.argv[1:] if argv is None else argv)
    bounds = {"max_nodes": opts["max_nodes"], "max_chars": opts["max_chars"], "max_ms": opts["max_ms"],
              "announce_client": opts["announce"]}
    if opts["json"]:
        result = dump_app(opts["app"], **bounds)
        if result.code == EXIT_OK:
            sys.stdout.write(result.to_json(opts["app"]) + "\n")
            sys.stdout.flush()
        else:
            sys.stderr.write(result.error + "\n")
            sys.stderr.flush()
        return result.code
    text = dump_window_text(opts["app"], **bounds)
    if text.code == EXIT_OK:
        sys.stdout.write(text.text + "\n")
        sys.stdout.flush()
    else:
        sys.stderr.write(text.error + "\n")
        sys.stderr.flush()
    return text.code


if __name__ == "__main__":  # pragma: no cover — exercised as a subprocess
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
