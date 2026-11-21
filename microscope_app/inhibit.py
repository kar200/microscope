"""Prevent screen blanking / screensaver while the microscope app is open.

Uses the standard Linux session D-Bus interfaces via PySide6.QtDBus:
1. org.freedesktop.ScreenSaver (KDE Plasma, XFCE, Cinnamon, MATE, Wayland/portal)
2. org.gnome.SessionManager (GNOME fallback, flag 8 = inhibit idle)

Because the inhibit cookie is tied to our D-Bus connection, the desktop
automatically releases it if the process exits unexpectedly.
"""
from __future__ import annotations

from PySide6.QtDBus import QDBusConnection, QDBusInterface, QDBusMessage


class ScreenInhibitor:
    """Toggleable Linux screen-blanking / idle inhibitor."""

    def __init__(
        self,
        app_name: str = "Bresun Microscope",
        reason: str = "Live microscope view",
    ) -> None:
        self._app_name = app_name
        self._reason = reason
        self._backend: str | None = None
        self._cookie: int | None = None

    @property
    def active(self) -> bool:
        return self._cookie is not None

    def set_enabled(self, on: bool) -> bool:
        if on:
            return self.acquire()
        self.release()
        return False

    def acquire(self) -> bool:
        if self._cookie is not None:
            return True
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return False

        # 1. FreeDesktop ScreenSaver (KDE, XFCE, Cinnamon, MATE, etc.)
        iface = QDBusInterface(
            "org.freedesktop.ScreenSaver",
            "/org/freedesktop/ScreenSaver",
            "org.freedesktop.ScreenSaver",
            bus,
        )
        if iface.isValid():
            reply = iface.call("Inhibit", self._app_name, self._reason)
            if (
                reply.type() == QDBusMessage.MessageType.ReplyMessage
                and reply.arguments()
            ):
                self._cookie = int(reply.arguments()[0])
                self._backend = "freedesktop"
                return True

        # 2. GNOME SessionManager fallback (flag 8 = inhibit idle)
        iface = QDBusInterface(
            "org.gnome.SessionManager",
            "/org/gnome/SessionManager",
            "org.gnome.SessionManager",
            bus,
        )
        if iface.isValid():
            reply = iface.call("Inhibit", self._app_name, 0, self._reason, 8)
            if (
                reply.type() == QDBusMessage.MessageType.ReplyMessage
                and reply.arguments()
            ):
                self._cookie = int(reply.arguments()[0])
                self._backend = "gnome"
                return True

        return False

    def release(self) -> None:
        if self._cookie is None:
            return
        cookie, backend = self._cookie, self._backend
        self._cookie = None
        self._backend = None

        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return

        if backend == "freedesktop":
            iface = QDBusInterface(
                "org.freedesktop.ScreenSaver",
                "/org/freedesktop/ScreenSaver",
                "org.freedesktop.ScreenSaver",
                bus,
            )
            if iface.isValid():
                iface.call("UnInhibit", cookie)
        elif backend == "gnome":
            iface = QDBusInterface(
                "org.gnome.SessionManager",
                "/org/gnome/SessionManager",
                "org.gnome.SessionManager",
                bus,
            )
            if iface.isValid():
                iface.call("Uninhibit", cookie)
