"""Bluetooth Low Energy GATT server of the cabinet (BlueZ over D-Bus).

The Raspberry Pi advertises the Intel Park service; drivers' phones connect (no pairing needed),
read a challenge, write their identity, read the decision. All checks are in agent/identity.py.

Requirements on Raspberry Pi OS (Bookworm):  sudo apt install bluez python3-dbus python3-gi
The service user must be in the "bluetooth" group (install.sh does it) to talk to org.bluez.

Characteristics (UUIDs in agent/identity.py and in the Android app):
  INFO       read          {"v":1,"ctrl":"CTRL-01","site":"Parking 1 · Centre","online":true}
  CHALLENGE  read          {"v":1,"ctrl":"CTRL-01","ch":"<base64url>"}       a new one for each phone
  IDENTIFY   write         framed JSON message (see identity.Reassembler)
  RESULT     read, notify  {"v":1,"ch":"...","ok":true,"status":"authorised","bay":"P1 · EXT · 05"}
                           notify carries only the first 8 characters of the challenge: "your result is ready"
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from agent import identity

log = logging.getLogger("parking-agent.ble")

BLUEZ = "org.bluez"
ADAPTER = "org.bluez.Adapter1"
GATT_MANAGER = "org.bluez.GattManager1"
ADV_MANAGER = "org.bluez.LEAdvertisingManager1"
GATT_SERVICE = "org.bluez.GattService1"
GATT_CHRC = "org.bluez.GattCharacteristic1"
LE_ADV = "org.bluez.LEAdvertisement1"
DBUS_OM = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP = "org.freedesktop.DBus.Properties"
APP_PATH = "/org/intelpark"


def build(dbus, dbus_service, glib):
    """Create the D-Bus classes once the modules are imported (they exist only on the Pi)."""

    class Failed(dbus.exceptions.DBusException):
        _dbus_error_name = "org.bluez.Error.Failed"

    class InvalidArgs(dbus.exceptions.DBusException):
        _dbus_error_name = "org.freedesktop.DBus.Error.InvalidArgs"

    class Application(dbus_service.Object):
        def __init__(self, bus):
            self.path = APP_PATH
            self.services = []
            super().__init__(bus, self.path)

        def get_path(self):
            return dbus.ObjectPath(self.path)

        @dbus_service.method(DBUS_OM, out_signature="a{oa{sa{sv}}}")
        def GetManagedObjects(self):  # noqa: N802 (D-Bus name)
            objects = {}
            for service in self.services:
                objects[service.get_path()] = service.get_properties()
                for chrc in service.characteristics:
                    objects[chrc.get_path()] = chrc.get_properties()
            return objects

    class Service(dbus_service.Object):
        def __init__(self, bus, index, uuid):
            self.path = f"{APP_PATH}/service{index}"
            self.uuid = uuid
            self.characteristics = []
            super().__init__(bus, self.path)

        def get_path(self):
            return dbus.ObjectPath(self.path)

        def get_properties(self):
            return {GATT_SERVICE: {
                "UUID": self.uuid,
                "Primary": dbus.Boolean(True),
                "Characteristics": dbus.Array([c.get_path() for c in self.characteristics], signature="o"),
            }}

        @dbus_service.method(DBUS_PROP, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):  # noqa: N802
            if interface != GATT_SERVICE:
                raise InvalidArgs()
            return self.get_properties()[GATT_SERVICE]

    class Characteristic(dbus_service.Object):
        def __init__(self, bus, index, uuid, flags, service):
            self.path = f"{service.path}/char{index}"
            self.uuid = uuid
            self.flags = flags
            self.service = service
            self.notifying = False
            super().__init__(bus, self.path)

        def get_path(self):
            return dbus.ObjectPath(self.path)

        def get_properties(self):
            return {GATT_CHRC: {"Service": self.service.get_path(), "UUID": self.uuid,
                                "Flags": dbus.Array(self.flags, signature="s")}}

        @dbus_service.method(DBUS_PROP, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):  # noqa: N802
            if interface != GATT_CHRC:
                raise InvalidArgs()
            return self.get_properties()[GATT_CHRC]

        @dbus_service.signal(DBUS_PROP, signature="sa{sv}as")
        def PropertiesChanged(self, interface, changed, invalidated):  # noqa: N802
            pass

        @dbus_service.method(GATT_CHRC, in_signature="a{sv}", out_signature="ay")
        def ReadValue(self, options):  # noqa: N802
            device = str(options.get("device", ""))
            offset = int(options.get("offset", 0))
            try:
                data = self.read(device, offset)
            except Exception as exc:  # never let an exception kill the D-Bus loop
                log.exception("read failed")
                raise Failed(str(exc)) from exc
            return dbus.Array(data[offset:], signature="y")

        @dbus_service.method(GATT_CHRC, in_signature="aya{sv}")
        def WriteValue(self, value, options):  # noqa: N802
            try:
                self.write(str(options.get("device", "")), bytes(value))
            except Exception as exc:
                log.exception("write failed")
                raise Failed(str(exc)) from exc

        @dbus_service.method(GATT_CHRC)
        def StartNotify(self):  # noqa: N802
            self.notifying = True

        @dbus_service.method(GATT_CHRC)
        def StopNotify(self):  # noqa: N802
            self.notifying = False

        def notify(self, data: bytes) -> None:
            if self.notifying:
                self.PropertiesChanged(GATT_CHRC, {"Value": dbus.Array(data, signature="y")}, [])

        def read(self, device: str, offset: int) -> bytes:  # overridden
            raise Failed("not readable")

        def write(self, device: str, data: bytes) -> None:  # overridden
            raise Failed("not writable")

    class CachedRead(Characteristic):
        """Long values are read in several requests (offset > 0): keep the value of the first one."""

        def __init__(self, *args, produce: Callable[[str], bytes]):
            super().__init__(*args)
            self.produce = produce
            self.cache: dict[str, bytes] = {}

        def read(self, device, offset):
            if offset == 0 or device not in self.cache:
                self.cache[device] = self.produce(device)
            return self.cache[device]

    class Identify(Characteristic):
        def __init__(self, *args, on_frame: Callable[[str, bytes], None]):
            super().__init__(*args)
            self.on_frame = on_frame

        def write(self, device, data):
            self.on_frame(device, data)

    class Advertisement(dbus_service.Object):
        PATH = f"{APP_PATH}/advertisement0"

        def __init__(self, bus):
            super().__init__(bus, self.PATH)

        def get_path(self):
            return dbus.ObjectPath(self.PATH)

        @dbus_service.method(DBUS_PROP, in_signature="s", out_signature="a{sv}")
        def GetAll(self, interface):  # noqa: N802
            if interface != LE_ADV:
                raise InvalidArgs()
            # 128-bit service UUID in the advertising data (phones filter on it); the name, set as
            # the adapter alias, goes into the scan response.
            return {"Type": "peripheral",
                    "ServiceUUIDs": dbus.Array([identity.SERVICE_UUID], signature="s"),
                    "Includes": dbus.Array(["local-name"], signature="s")}

        @dbus_service.method(LE_ADV)
        def Release(self):  # noqa: N802
            log.info("advertisement released by BlueZ")

    return {"Application": Application, "Service": Service, "CachedRead": CachedRead, "Identify": Identify,
            "Characteristic": Characteristic, "Advertisement": Advertisement}


class BleServer:
    """Starts the GATT server in its own GLib thread.

    info()                  -> dict shown in INFO
    handle(device, message) -> dict RESULT value; runs in a worker thread (it may call the server)
    """

    def __init__(self, adapter: str, controller_code: str, info: Callable[[], dict],
                 handle: Callable[[str, bytes], dict]):
        self.adapter_name = adapter
        self.controller_code = controller_code
        self.info = info
        self.handle = handle
        self.challenges = identity.Challenges()
        self.frames = identity.Reassembler()
        self.results: dict[str, bytes] = {}
        self.workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ble-identify")
        self.loop = None
        self.result_chrc = None
        self._glib = None

    # -- characteristic callbacks (GLib thread) ---------------------------------------------
    def _info(self, device: str) -> bytes:
        return identity.to_json_bytes({"v": identity.PROTOCOL_VERSION, "ctrl": self.controller_code, **self.info()})

    def _challenge(self, device: str) -> bytes:
        challenge = self.challenges.issue(device)
        self.results.pop(device, None)
        return identity.to_json_bytes({"v": identity.PROTOCOL_VERSION, "ctrl": self.controller_code, "ch": challenge})

    def _result(self, device: str) -> bytes:
        return self.results.get(device) or identity.to_json_bytes(identity.result(None, "waiting", False))

    def _frame(self, device: str, frame: bytes) -> None:
        try:
            message = self.frames.feed(device, frame)
        except identity.IdentifyError as exc:
            self._publish(device, identity.result(self.challenges.peek(device), str(exc), False))
            return
        if message is not None:
            self.workers.submit(self._process, device, message)

    # -- worker thread --------------------------------------------------------------------------
    def _process(self, device: str, message: bytes) -> None:
        try:
            value = self.handle(device, message)
        except Exception:
            log.exception("identification failed")
            value = identity.result(None, "error", False)
        self._glib.idle_add(self._publish, device, value)

    def _publish(self, device: str, value: dict) -> bool:
        self.results[device] = identity.to_json_bytes(value)
        if self.result_chrc is not None:
            self.result_chrc.notify(str(value.get("ch") or "")[:8].encode() or b"-")
        return False  # GLib.idle_add: run once

    # -- start / stop -------------------------------------------------------------------------------
    def start(self) -> None:
        import dbus
        import dbus.mainloop.glib
        import dbus.service
        from gi.repository import GLib

        self._glib = GLib
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        bus = dbus.SystemBus()
        classes = build(dbus, dbus.service, GLib)
        adapter_path = self._find_adapter(bus, dbus)
        props = dbus.Interface(bus.get_object(BLUEZ, adapter_path), DBUS_PROP)
        props.Set(ADAPTER, "Powered", dbus.Boolean(True))
        props.Set(ADAPTER, "Alias", dbus.String(f"IntelPark-{self.controller_code}"))

        app = classes["Application"](bus)
        service = classes["Service"](bus, 0, identity.SERVICE_UUID)
        CachedRead, Identify = classes["CachedRead"], classes["Identify"]
        service.characteristics = [
            CachedRead(bus, 0, identity.INFO_UUID, ["read"], service, produce=self._info),
            CachedRead(bus, 1, identity.CHALLENGE_UUID, ["read"], service, produce=self._challenge),
            Identify(bus, 2, identity.IDENTIFY_UUID, ["write"], service, on_frame=self._frame),
            CachedRead(bus, 3, identity.RESULT_UUID, ["read", "notify"], service, produce=self._result),
        ]
        self.result_chrc = service.characteristics[3]
        app.services.append(service)
        advert = classes["Advertisement"](bus)

        adapter = bus.get_object(BLUEZ, adapter_path)
        dbus.Interface(adapter, GATT_MANAGER).RegisterApplication(
            app.get_path(), {}, reply_handler=lambda: log.info("GATT service registered"),
            error_handler=lambda e: log.error("GATT registration failed: %s", e))
        dbus.Interface(adapter, ADV_MANAGER).RegisterAdvertisement(
            advert.get_path(), {}, reply_handler=lambda: log.info("advertising as IntelPark-%s", self.controller_code),
            error_handler=lambda e: log.error("advertising failed: %s", e))
        self._keep = (app, service, advert)  # D-Bus objects must stay referenced
        self.loop = GLib.MainLoop()
        threading.Thread(target=self.loop.run, name="ble", daemon=True).start()

    def _find_adapter(self, bus, dbus) -> str:
        manager = dbus.Interface(bus.get_object(BLUEZ, "/"), DBUS_OM)
        for path, interfaces in manager.GetManagedObjects().items():
            if GATT_MANAGER in interfaces and ADV_MANAGER in interfaces and str(path).endswith(self.adapter_name):
                return path
        raise RuntimeError(f"Bluetooth adapter {self.adapter_name} not found (is bluetoothd running?)")

    def stop(self) -> None:
        if self.loop is not None:
            self.loop.quit()
        self.workers.shutdown(wait=False)
