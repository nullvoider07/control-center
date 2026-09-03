#!/usr/bin/env python3
"""A coordinate picker for a human operator on Wayland.

Wayland does not let a client observe the pointer outside its own surfaces, and
every route that gets round that is privileged in the compositor's eyes: inside
it (a Shell extension), consented (the ScreenCast portal), or below it
(/dev/input). None of those is acceptable here, and no amount of searching
changes a deliberate security property.

But the constraint is "outside its own surfaces". A window of our own, mapped and
focused, is a surface the pointer can be observed over - measured: with a
full-screen XWayland window up, every position read was exact. A transient window
is not enough (tried: the compositor never gives it pointer focus), so this is a
real window the operator interacts with: it appears, they move to the target and
click, it reports the coordinate and closes.

That is only acceptable because the caller is a person doing discovery. The agent
path never needs it - it locates targets from an image.
"""
import ctypes
import ctypes.util
import os
import sys

_X = ctypes.CDLL(ctypes.util.find_library("X11"))

_X.XOpenDisplay.argtypes = [ctypes.c_char_p]
_X.XOpenDisplay.restype = ctypes.c_void_p
_X.XCloseDisplay.argtypes = [ctypes.c_void_p]
_X.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
_X.XDefaultRootWindow.restype = ctypes.c_ulong
_X.XDefaultScreen.argtypes = [ctypes.c_void_p]
_X.XDefaultScreen.restype = ctypes.c_int
_X.XDisplayWidth.argtypes = [ctypes.c_void_p, ctypes.c_int]
_X.XDisplayWidth.restype = ctypes.c_int
_X.XDisplayHeight.argtypes = [ctypes.c_void_p, ctypes.c_int]
_X.XDisplayHeight.restype = ctypes.c_int
_X.XBlackPixel.argtypes = [ctypes.c_void_p, ctypes.c_int]
_X.XBlackPixel.restype = ctypes.c_ulong
_X.XCreateSimpleWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
                                   ctypes.c_uint, ctypes.c_ulong, ctypes.c_ulong]
_X.XCreateSimpleWindow.restype = ctypes.c_ulong
_X.XSelectInput.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_long]
_X.XMapRaised.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
_X.XDestroyWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
_X.XFlush.argtypes = [ctypes.c_void_p]
_X.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
_X.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
_X.XStoreName.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_char_p]
_X.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
_X.XInternAtom.restype = ctypes.c_ulong
_X.XChangeProperty.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong,
                               ctypes.c_ulong, ctypes.c_int, ctypes.c_int,
                               ctypes.c_void_p, ctypes.c_int]

ButtonPressMask = 1 << 2
KeyPressMask = 1 << 0
ButtonPress, KeyPress = 4, 2
XA_CARDINAL = 6
PropModeReplace = 0
# Mutter honours _NET_WM_WINDOW_OPACITY, so the overlay can be see-through while
# still receiving the click: the operator aims at what is underneath it.
OPACITY = 0.18


class _XEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("pad", ctypes.c_long * 30)]


class _XButtonEvent(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong),
                ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                ("window", ctypes.c_ulong), ("root", ctypes.c_ulong),
                ("subwindow", ctypes.c_ulong), ("time", ctypes.c_ulong),
                ("x", ctypes.c_int), ("y", ctypes.c_int),
                ("x_root", ctypes.c_int), ("y_root", ctypes.c_int),
                ("state", ctypes.c_uint), ("button", ctypes.c_uint),
                ("same_screen", ctypes.c_int)]


def pick(display=None):
    """Show the overlay and return (x, y) of the click, or None if cancelled."""
    name = (display or os.environ.get("DISPLAY") or ":0").encode()
    dpy = _X.XOpenDisplay(name)
    if not dpy:
        raise RuntimeError(f"cannot open X display {name.decode()!r}")
    try:
        scr = _X.XDefaultScreen(dpy)
        root = _X.XDefaultRootWindow(dpy)
        w = _X.XDisplayWidth(dpy, scr)
        h = _X.XDisplayHeight(dpy, scr)
        win = _X.XCreateSimpleWindow(dpy, root, 0, 0, w, h, 0,
                                     _X.XBlackPixel(dpy, scr),
                                     _X.XBlackPixel(dpy, scr))
        _X.XStoreName(dpy, win, b"Pick a point - left-click the target, right-click cancels")
        opacity = _X.XInternAtom(dpy, b"_NET_WM_WINDOW_OPACITY", 0)
        val = ctypes.c_ulong(int(0xFFFFFFFF * OPACITY))
        _X.XChangeProperty(dpy, win, opacity, XA_CARDINAL, 32, PropModeReplace,
                           ctypes.byref(val), 1)
        _X.XSelectInput(dpy, win, ButtonPressMask | KeyPressMask)
        _X.XMapRaised(dpy, win)
        _X.XSync(dpy, 0)

        ev = _XEvent()
        while True:
            _X.XNextEvent(dpy, ctypes.byref(ev))
            if ev.type == ButtonPress:
                be = ctypes.cast(ctypes.byref(ev), ctypes.POINTER(_XButtonEvent)).contents
                if be.button == 3:      # right-click cancels
                    return None
                if be.button == 1:      # left-click picks
                    return (be.x_root, be.y_root)
                continue                # ignore middle click and scroll
            # Key presses are ignored on purpose. Cancelling on "any key" made a
            # stray keystroke close the picker with no result, which is
            # indistinguishable from a failure to read the pointer - measured
            # twice while testing. Cancelling is a right-click: it cannot arrive
            # by accident from another window's key handling.
    finally:
        try:
            _X.XDestroyWindow(dpy, win)
            _X.XSync(dpy, 0)
        except Exception:
            pass
        _X.XCloseDisplay(dpy)


if __name__ == "__main__":
    got = pick()
    if got is None:
        print("cancelled", file=sys.stderr)
        sys.exit(1)
    print(f"X={got[0]}\nY={got[1]}")
