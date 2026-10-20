"""Minimal async client for CoolAutomation CoolMasterNet / CoolLink hubs.

This speaks the CoolMasterNet ASCII line protocol over TCP (default :10102) —
the same transport the official Home Assistant ``coolmaster`` integration and
the ``homebridge-cool-automation`` plugin use.

Why this exists
---------------
The official integration relies on ``pycoolmasternet-async``, whose status
parser splits every ``ls2`` line on whitespace and then insists the line has
*exactly* 8 or 9 fields, raising "Unexpected status line format" otherwise.
CoolLinkBridge / CoolLinkHub emit ``ls2`` lines with a different count of
trailing fields, so the official integration refuses to load them.

The working ``homebridge-cool-automation`` plugin instead reads the fields
positionally and tolerates the variation. This client does the same: it maps
the first six fields (unit id, on/off, set-point, room-temp, fan, mode) by
position and treats the error / filter / demand columns as optional. That is
strictly more lenient than the official parser and is what makes CoolLink work.
"""

from __future__ import annotations

import asyncio
import logging

_LOGGER = logging.getLogger(__name__)

# HVAC modes the hub understands (lower-case, as sent on the wire).
MODES = ["auto", "cool", "dry", "fan", "heat"]

# CoolMaster fan-speed name -> single-letter command argument for ``fspeed``.
_FAN_TO_CHAR = {
    "auto": "a",
    "vlow": "v",
    "low": "l",
    "med": "m",
    "high": "h",
    "top": "t",
}


class CoolLinkError(Exception):
    """A command was rejected by the hub."""


def _to_float(value: str) -> float | None:
    """Parse a temperature token like '25.0C', '77F' or '24' into a float."""
    if value is None:
        return None
    value = value.strip()
    if value and value[-1].isalpha():  # drop a trailing unit letter (C/F)
        value = value[:-1]
    value = value.replace(",", ".")
    try:
        return float(value)
    except ValueError:
        return None


class CoolLinkNet:
    """A connection to a CoolMasterNet / CoolLink hub."""

    def __init__(
        self,
        host: str,
        port: int = 10102,
        read_timeout: float = 2.0,
        idle_gap: float = 0.4,
        send_wakeup: bool = False,
    ) -> None:
        self._host = host
        self._port = int(port)
        self._read_timeout = read_timeout  # max wait for the FIRST byte
        self._idle_gap = idle_gap  # gap of silence that marks end of response
        self._send_wakeup = send_wakeup
        # CoolMaster hubs serve a single TCP session; serialise our requests.
        self._lock = asyncio.Lock()

    # ---- transport -------------------------------------------------------
    async def _make_request(self, request: str) -> str:
        """Send one command and return the raw text response."""
        async with self._lock:
            reader, writer = await asyncio.open_connection(self._host, self._port)
            try:
                if self._send_wakeup:
                    # Some hubs need a newline to wake the prompt before they
                    # accept a command; swallow whatever banner comes back.
                    writer.write(b"\n")
                    await writer.drain()
                    try:
                        await asyncio.wait_for(reader.read(4096), 0.3)
                    except asyncio.TimeoutError:
                        pass

                writer.write((request + "\r\n").encode("ascii"))
                await writer.drain()

                buf = bytearray()
                first = True
                loop = asyncio.get_event_loop()
                deadline = loop.time() + 8.0
                while True:
                    timeout = self._read_timeout if first else self._idle_gap
                    try:
                        chunk = await asyncio.wait_for(reader.read(4096), timeout)
                    except asyncio.TimeoutError:
                        break  # first byte never came, or response went quiet
                    if not chunk:
                        break  # remote closed the socket
                    buf += chunk
                    first = False
                    # CoolMaster terminates a response with the '>' prompt.
                    if buf.rstrip().endswith(b">"):
                        break
                    if loop.time() > deadline:
                        break
                return buf.decode("ascii", "ignore")
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:  # noqa: BLE001 - close is best-effort
                    pass

    @staticmethod
    def _clean(raw: str) -> str:
        """Strip the prompt characters and carriage returns from a response."""
        return raw.replace("\r", "").replace(">", "")

    async def _command(self, request: str) -> str:
        """Send a command that should just acknowledge with OK."""
        cleaned = self._clean(await self._make_request(request)).strip()
        if "Unknown command" in cleaned:
            raise ValueError(f"Command not supported by hub: {request!r}")
        lines = [ln.strip() for ln in cleaned.split("\n") if ln.strip()]
        if not lines or lines[-1] != "OK":
            raise CoolLinkError(f"Command {request!r} failed: {cleaned!r}")
        return cleaned

    # ---- high level ------------------------------------------------------
    async def info(self) -> dict[str, str]:
        """Best-effort hub info (firmware version etc.). Never raises."""
        try:
            cleaned = self._clean(await self._make_request("set"))
        except Exception:  # noqa: BLE001
            return {}
        out: dict[str, str] = {}
        for line in cleaned.split("\n"):
            if ":" in line:
                key, val = line.split(":", 1)
                out[key.strip()] = val.strip()
        return out

    async def _status_lines(self, unit_id: str | None = None) -> list[str]:
        cmd = "ls2" if unit_id is None else f"ls2 {unit_id}"
        cleaned = self._clean(await self._make_request(cmd))
        return [
            ln.rstrip()
            for ln in cleaned.split("\n")
            if ln.strip() and ln.strip() != "OK"
        ]

    async def status(self) -> dict[str, "CoolLinkUnit"]:
        """Return a mapping of unit_id -> current snapshot for every unit."""
        units: dict[str, CoolLinkUnit] = {}
        for line in await self._status_lines():
            unit = CoolLinkUnit.parse(self, line)
            if unit is not None:
                units[unit.unit_id] = unit
        return units


class CoolLinkUnit:
    """An immutable snapshot of one indoor unit."""

    __slots__ = (
        "_bridge",
        "unit_id",
        "is_on",
        "thermostat",
        "temperature",
        "fan_speed",
        "mode",
        "error_code",
        "clean_filter",
        "temperature_unit",
        "raw",
    )

    def __init__(
        self,
        bridge: CoolLinkNet,
        unit_id: str,
        is_on: bool,
        thermostat: float | None,
        temperature: float | None,
        fan_speed: str,
        mode: str,
        error_code: str | None,
        clean_filter: bool,
        temperature_unit: str,
        raw: str = "",
    ) -> None:
        self._bridge = bridge
        self.unit_id = unit_id
        self.is_on = is_on
        self.thermostat = thermostat
        self.temperature = temperature
        self.fan_speed = fan_speed
        self.mode = mode
        self.error_code = error_code
        self.clean_filter = clean_filter
        self.temperature_unit = temperature_unit
        self.raw = raw

    # ---- parsing ---------------------------------------------------------
    @classmethod
    def parse(cls, bridge: CoolLinkNet, line: str) -> "CoolLinkUnit | None":
        """Parse one ``ls2`` status line, tolerating CoolLink field variation.

        The first six fields are always present and in a fixed order:
            <uid> <ON|OFF> <set-temp> <room-temp> <fan> <mode>
        Anything after that (error code, filter flag, demand) is optional and
        differs between CoolMasterNet and CoolLink firmware, so we treat it as
        best-effort instead of demanding a fixed field count.
        """
        parts = line.split()
        if len(parts) < 6:
            return None
        uid = parts[0].strip()
        onoff = parts[1].strip().upper()
        # Guards out any banner / prompt / echo lines.
        if not uid or onoff not in ("ON", "OFF"):
            return None

        set_tok, room_tok = parts[2], parts[3]
        unit_letter = set_tok[-1:].upper() if set_tok[-1:].isalpha() else ""
        celsius = unit_letter != "F"

        error = parts[6] if len(parts) > 6 else "-"
        filt = parts[7] if len(parts) > 7 else "-"

        return cls(
            bridge,
            unit_id=uid,
            is_on=(onoff == "ON"),
            thermostat=_to_float(set_tok),
            temperature=_to_float(room_tok),
            fan_speed=parts[4].strip().lower(),
            mode=parts[5].strip().lower(),
            error_code=(None if error in ("-", "OK", "") else error),
            clean_filter=(filt in ("#", "1")),
            temperature_unit=("celsius" if celsius else "imperial"),
            raw=line,
        )

    def _with(self, **changes) -> "CoolLinkUnit":
        """Return an optimistic copy with some fields overridden."""
        data = {
            "bridge": self._bridge,
            "unit_id": self.unit_id,
            "is_on": self.is_on,
            "thermostat": self.thermostat,
            "temperature": self.temperature,
            "fan_speed": self.fan_speed,
            "mode": self.mode,
            "error_code": self.error_code,
            "clean_filter": self.clean_filter,
            "temperature_unit": self.temperature_unit,
            "raw": self.raw,
        }
        data.update(changes)
        return CoolLinkUnit(**data)

    # ---- commands --------------------------------------------------------
    async def turn_on(self) -> "CoolLinkUnit":
        await self._bridge._command(f"on {self.unit_id}")
        return self._with(is_on=True)

    async def turn_off(self) -> "CoolLinkUnit":
        await self._bridge._command(f"off {self.unit_id}")
        return self._with(is_on=False)

    async def set_mode(self, mode: str) -> "CoolLinkUnit":
        mode = mode.lower()
        if mode not in MODES:
            raise ValueError(f"Unrecognised mode {mode!r}")
        await self._bridge._command(f"{mode} {self.unit_id}")
        return self._with(mode=mode)

    async def set_thermostat(self, value: float) -> "CoolLinkUnit":
        rounded = round(float(value), 1)
        # Whole degrees send as ints (cleaner for hubs without half-step support).
        arg = int(rounded) if rounded == int(rounded) else rounded
        await self._bridge._command(f"temp {self.unit_id} {arg}")
        return self._with(thermostat=float(rounded))

    async def set_fan_speed(self, speed: str) -> "CoolLinkUnit":
        speed = speed.lower()
        char = _FAN_TO_CHAR.get(speed, speed[:1])
        await self._bridge._command(f"fspeed {self.unit_id} {char}")
        return self._with(fan_speed=speed)
