"""
Screen Recording, asked quietly.

Thumbnails of the Slack window need macOS Screen Recording for the process
that takes them — under launchd that is the engine's Python interpreter, not
the terminal. ``CGPreflightScreenCaptureAccess`` answers "is it granted?"
without showing a prompt; ``CGRequestScreenCaptureAccess`` shows the system
prompt once (only ``otto permissions`` uses it, while the user is watching).
"""
from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger("otto.utils.screen")

_CORE_GRAPHICS = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"


def _core_graphics() -> ctypes.CDLL | None:
    if sys.platform != "darwin":
        return None
    try:
        return ctypes.CDLL(_CORE_GRAPHICS)
    except OSError as e:
        logger.debug("CoreGraphics unavailable: %s", e)
        return None


def screen_recording_granted() -> bool | None:
    """
    ``True``/``False`` when macOS can say whether this process may record the
    screen; ``None`` when it cannot be determined (not macOS, old macOS).
    Never prompts.
    """
    cg = _core_graphics()
    if cg is None:
        return None
    try:
        fn = cg.CGPreflightScreenCaptureAccess
    except AttributeError:          # before macOS 10.15 there was nothing to grant
        return None
    fn.restype = ctypes.c_bool
    fn.argtypes = []
    try:
        return bool(fn())
    except Exception as e:          # pragma: no cover - defensive
        logger.debug("CGPreflightScreenCaptureAccess failed: %s", e)
        return None


def request_screen_recording() -> bool | None:
    """Show the system's Screen Recording prompt for this process (once). Returns the resulting grant."""
    cg = _core_graphics()
    if cg is None:
        return None
    try:
        fn = cg.CGRequestScreenCaptureAccess
    except AttributeError:
        return None
    fn.restype = ctypes.c_bool
    fn.argtypes = []
    try:
        return bool(fn())
    except Exception as e:          # pragma: no cover - defensive
        logger.debug("CGRequestScreenCaptureAccess failed: %s", e)
        return None
