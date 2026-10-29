"""Digital I/O behind the "gpio:<channel>" addresses stored on the server.

A cabinet usually watches far more bays than a Pi has pins, so a channel number is
mapped by the chosen backend:
  * gpio      - channel -> BCM pin of the Pi itself (small sites), via gpiozero;
  * mcp23017  - channel n -> expander n // 16, pin n % 16 on the I2C bus (16 inputs per chip);
  * simulator - random but realistic traffic, for tests and demos without hardware;
  * network   - car detectors on the cabinet's Ethernet push their state (agent/detectors.py).
Sensors are expected as dry contacts / open-collector outputs: closed = vehicle present.
"""

from __future__ import annotations

import random
import threading
import time


class IOBackend:
    def read(self, channel: int) -> bool | None:
        """True = vehicle present, False = free, None = cannot read (wiring or bus fault)."""
        raise NotImplementedError

    def pulse(self, channel: int, seconds: float) -> None:
        """Close an output relay for a moment (barrier opening)."""
        raise NotImplementedError


class SimulatorIO(IOBackend):
    def __init__(self, seed: int | None = None, fault_rate: float = 0.0):
        self.rng = random.Random(seed)
        self.state: dict[int, bool] = {}
        self.fault_rate = fault_rate
        self.pulses: list[tuple[int, float]] = []

    def read(self, channel: int) -> bool | None:
        if self.fault_rate and self.rng.random() < self.fault_rate:
            return None
        cur = self.state.setdefault(channel, self.rng.random() < 0.7)
        if self.rng.random() < 0.002:  # a car arrives or leaves now and then
            cur = self.state[channel] = not cur
        return cur

    def pulse(self, channel: int, seconds: float) -> None:
        self.pulses.append((channel, seconds))


class GpioIO(IOBackend):
    def __init__(self, pins: dict[int, int]):
        from gpiozero import DigitalInputDevice, OutputDevice  # imported only on a real Pi

        self._in = {}
        self._out = {}
        self._pins = pins
        self._input_cls, self._output_cls = DigitalInputDevice, OutputDevice

    def read(self, channel: int) -> bool | None:
        pin = self._pins.get(channel)
        if pin is None:
            return None
        dev = self._in.get(pin) or self._in.setdefault(pin, self._input_cls(pin, pull_up=True))
        return not dev.value  # pulled up: contact closed -> 0 -> vehicle present

    def pulse(self, channel: int, seconds: float) -> None:
        pin = self._pins[channel]
        dev = self._out.get(pin) or self._out.setdefault(pin, self._output_cls(pin))
        dev.on()
        threading.Timer(seconds, dev.off).start()


class Mcp23017IO(IOBackend):
    """Sensor inputs on MCP23017 expanders (pull-ups enabled); loops and barrier relays on Pi pins."""

    GPPU_A, GPPU_B, GPIO_A, GPIO_B = 0x0C, 0x0D, 0x12, 0x13

    def __init__(self, chips: list[dict], pins: dict[int, int]):
        from smbus2 import SMBus

        self.chips = chips
        self.buses = {c["bus"]: SMBus(c["bus"]) for c in chips}
        for chip in chips:
            bus = self.buses[chip["bus"]]
            bus.write_byte_data(chip["address"], self.GPPU_A, 0xFF)
            bus.write_byte_data(chip["address"], self.GPPU_B, 0xFF)
        self._cache: dict[int, tuple[float, int]] = {}
        self._outputs = GpioIO(pins) if pins else None

    def read(self, channel: int) -> bool | None:
        if self._outputs and channel in self._outputs._pins:  # loop detectors wired on the Pi itself
            return self._outputs.read(channel)
        index, bit = divmod(channel - 1, 16)
        if index >= len(self.chips):
            return None
        stamp, word = self._cache.get(index, (0.0, 0))
        if time.monotonic() - stamp > 0.2:  # one bus read per chip per sample
            chip = self.chips[index]
            try:
                bus = self.buses[chip["bus"]]
                word = bus.read_byte_data(chip["address"], self.GPIO_A) | bus.read_byte_data(chip["address"], self.GPIO_B) << 8
            except OSError:
                return None
            self._cache[index] = (time.monotonic(), word)
        return not (word >> bit) & 1

    def pulse(self, channel: int, seconds: float) -> None:
        if self._outputs:
            self._outputs.pulse(channel, seconds)


def make(cfg) -> IOBackend:
    if cfg.io_backend == "network":
        from agent.detectors import NetworkIO

        outputs = GpioIO(cfg.gpio_pins) if cfg.gpio_pins else None
        return NetworkIO(cfg.detectors.timeout_s, outputs, cfg.detectors.simulate_traffic)
    if cfg.io_backend == "gpio":
        return GpioIO(cfg.gpio_pins)
    if cfg.io_backend == "mcp23017":
        return Mcp23017IO(cfg.mcp23017, cfg.gpio_pins)
    return SimulatorIO()


def channel_of(address: str | None) -> int | None:
    """'gpio:17' -> 17."""
    if address and address.startswith("gpio:"):
        try:
            return int(address.split(":", 1)[1].split(",")[0])
        except ValueError:
            return None
    return None
