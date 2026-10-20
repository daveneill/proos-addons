"""Samsung IP Control (JSON-RPC) client.

Talks to recent Samsung TVs over the undocumented JSON-RPC interface on
HTTPS port 1516. This is used as a reliable, SmartThings-free power path on
Frame TVs: the WebSocket KEY_POWER command only toggles between normal viewing
and Art Mode, whereas `powerControl` issues an explicit hardware on/off that
works from any state (including Art Mode).

Protocol notes (confirmed on Frame 2024 / 2025):
  * HTTPS POST to https://<ip>:1516/ with a self-signed certificate (no verify).
  * Only Accept + Content-Type headers are sent.
  * Two-step auth: `createAccessToken` returns a token after the user accepts an
    on-screen prompt; the token is then passed in `params.AccessToken` on every
    later call. The token persists across power cycles (pair once).
  * Pairing only works while the TV is OUT of Art Mode; in Art Mode the endpoint
    does not respond and the request times out.
  * The TV must have "IP Remote" enabled
    (Settings -> Connections -> Network -> Expert Settings).
  * Older Samsung TVs (Tizen <= 5.5, ~2020 Frames) negotiate a weak DH group
    that OpenSSL's default security level rejects ("dh key too small"). The
    client detects this and transparently retries with @SECLEVEL=1.

The blocking HTTP work runs in the executor so the event loop is never blocked.
The SSL context is also built lazily inside the executor — `create_default_context`
loads CA certs from disk, which would block the event loop if done at __init__.
"""

from __future__ import annotations

import asyncio
import http.client
import json
import logging
import ssl
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

DEFAULT_IP_CONTROL_PORT = 1516
# Candidate ports probed, in order, when pairing a TV whose port is unknown.
# 2020+ Frames answer on 1516; 2019 QLEDs answer on 1515. A closed port returns
# an immediate "connection refused" (fast), so probing the wrong one first is
# cheap — the first port that actually answers is the TV's real control port.
IP_CONTROL_PORT_CANDIDATES = (1516, 1515)
JSONRPC_VERSION = "2.0"
COLOR_TONE_OPTIONS = ("Cool", "Standard", "Warm1", "Warm2")

CMD_TIMEOUT = 5  # seconds for normal commands
PAIR_TIMEOUT = 30  # seconds: pairing waits for the on-screen acceptance

# JSON-RPC error code returned when the access token is missing/expired.
ERROR_UNAUTHORIZED = -32010
# JSON-RPC "Parse error". Our requests are always well-formed JSON, and the 32"
# and other calls succeed, so in practice this firmware returns -32700 when the
# AccessToken is stale/unrecognized: a fresh pairing always clears it. Treated
# as an auth error (re-pair required) when a token was actually sent.
ERROR_PARSE_STALE_TOKEN = -32700
# Generic "Server error". Observed for expert-picture controls (e.g.
# colorToneControl) when the TV's current picture mode does not allow the
# setting to be changed — Dynamic/HDR-dynamic modes drive color tone
# automatically and reject manual writes, while Standard/Movie/Filmmaker
# accept them. Not a transport or pairing problem: retrying after switching
# picture mode succeeds.
ERROR_SERVER = -32002

# JSON-RPC "Method not found". CONFIRMED ON THIS PANEL (Frame LS03T, Tizen 2020):
# `inputSourceControl` DOES exist -- probed bare it returns the live input. But for
# a short window after power-on the TV answers -32601 for it, because the HTTPS
# endpoint comes up before the input module registers. The two production failures
# we chased both fired within ONE SECOND of the panel powering on.
#
# So -32601 on this API is ambiguous: genuinely-absent method, OR a method that
# exists on a TV that is not ready yet. We treat it as NOT-READY and retry, and
# only surface it as an error once the retries are exhausted. Never assume the
# method is missing -- confirm by probing it bare when the TV is settled.
ERROR_METHOD_NOT_FOUND = -32601

# Retry budget for the not-ready window above. Measured: the panel answers input
# calls reliably within ~2-4s of reporting power-on.
NOT_READY_RETRIES = 6
NOT_READY_DELAY = 1.0  # seconds between retries

# Art-mode desync guard: number of consecutive reads where the artModeControl
# flag claims "on" while getTVStates.pictureMode shows a real (non-art) picture
# mode before we stop trusting the flag and treat the panel as authoritative.
# 1 disagreement is just a transition lag (pictureMode flips ~one sample before
# the flag on exit); a persistent one means the flag has wedged "on".
ART_DESYNC_THRESHOLD = 2


class SamsungIPControlError(Exception):
    """Base error for IP Control communication failures."""


class SamsungIPControlPortClosedError(SamsungIPControlError):
    """The TV refused the TCP connection — nothing is listening on this port.

    Raised on ``ConnectionRefusedError`` (and equivalent unreachable-host
    OSErrors). Distinct from a generic transport failure because it is the
    signal the auto-probe uses to move on to the next candidate port: a closed
    port means "wrong port for this model", not "the TV rejected the request".
    """


class SamsungIPControlAuthError(SamsungIPControlError):
    """The access token is missing, invalid or expired — re-pairing is required."""


class SamsungIPControlNotReadyError(SamsungIPControlError):
    """The TV answered -32601 for a method that is known to exist.

    Not "unsupported" -- the JSON-RPC endpoint is up but the module backing this
    method has not registered yet, which happens for a few seconds after power-on.
    Callers should retry rather than reporting a capability failure.
    """


class SamsungIPControlModeLockedError(SamsungIPControlError):
    """The control is rejected by the TV's current picture mode (code -32002).

    Not a transport, pairing, or capability problem — the same request
    succeeds once the TV is switched to a picture mode that allows manual
    writes (e.g. Standard/Movie/Filmmaker rather than Dynamic/HDR-dynamic).
    """


class SamsungIPControl:
    """Minimal async client for the Samsung IP Control JSON-RPC interface."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        *,
        port: int = DEFAULT_IP_CONTROL_PORT,
        token: str | None = None,
    ) -> None:
        """Initialize the client."""
        self._hass = hass
        self._host = host
        self._port = port
        self._token = token
        # SSL context is built lazily inside the executor (see _build_ssl_context).
        # `ssl.create_default_context()` loads CA certificates from disk, which
        # blocks the event loop — HA raises a warning if that happens here.
        self._ctx: ssl.SSLContext | None = None
        # Older TVs (Tizen <= 5.5, ~2020 Frames) negotiate a weak DH group and
        # are rejected by OpenSSL's default security level. When that happens
        # we retry once with @SECLEVEL=1 and remember it for subsequent calls.
        self._tls_legacy = False
        # Consecutive (artModeControl says on / pictureMode says not-art)
        # disagreements, for the desync guard in async_get_art_mode.
        self._art_desync_count = 0

    @property
    def token(self) -> str | None:
        """Return the current access token, if any."""
        return self._token

    def set_token(self, token: str | None) -> None:
        """Update the stored access token."""
        self._token = token

    # -- public API ----------------------------------------------------------

    async def async_pair(self) -> str:
        """Create and store an access token. TV must be OUT of Art Mode."""
        result = await self._async_request(
            "createAccessToken", include_token=False, timeout=PAIR_TIMEOUT
        )
        token = result.get("AccessToken")
        if not token or not isinstance(token, str):
            raise SamsungIPControlError(f"no AccessToken in response: {result!r}")
        self._token = token
        return token

    async def async_get_power_state(self) -> str:
        """Return 'powerOn' or 'powerOff'. A TV in Art Mode reports 'powerOn'."""
        result = await self._async_request("powerControl")
        return result.get("power", "unknown")

    async def async_power_on(self) -> str:
        """Power the TV on (returns into its last state, e.g. Art Mode)."""
        result = await self._async_request("powerControl", {"power": "powerOn"})
        return result.get("power", "unknown")

    async def async_power_off(self) -> str:
        """Power the TV off (works from Art Mode)."""
        result = await self._async_request("powerControl", {"power": "powerOff"})
        return result.get("power", "unknown")

    async def async_reboot(self) -> str:
        """Reboot the TV.

        Uses the same ``powerControl`` method as power on/off, with the
        ``reboot`` argument (confirmed empirically on Frame 2024/2025). The
        access token survives the reboot, so no re-pairing is needed afterwards.
        Issued over the JSON-RPC channel (port 1516), which is independent of
        the WebSocket channels — so it still lands when the Art WebSocket has
        gone unresponsive ("zombie"), making it a recovery path for that case.
        """
        result = await self._async_request("powerControl", {"power": "reboot"})
        return result.get("power", "unknown")

    async def async_get_art_mode(self) -> bool | None:
        """Return whether the TV is currently displaying Art Mode.

        Returns ``True`` if art is on the panel, ``False`` for normal viewing
        or a powered-off TV, and ``None`` if the state can't be determined.
        Raises on transport/auth errors.

        ``artModeControl`` (no parameter) is the semantic getter and the
        primary source. It is cross-checked against ``getTVStates.pictureMode``
        — which is ``"Ambient"`` only while art is on the panel — to catch the
        firmware fault we hit once: the artModeControl flag can wedge ``on``
        while a real input is displayed. A single disagreement is just a
        transition lag (pictureMode flips ~one sample before the flag when
        leaving art), so only a disagreement persisting for
        ``ART_DESYNC_THRESHOLD`` consecutive reads makes us treat the panel
        (pictureMode) as authoritative and return ``False``.

        PowerState is checked first and wins: a powered-off TV is never showing
        art (and pictureMode would be a stale ``"Ambient"``), so ``powerOff``
        short-circuits to ``False``. Art Mode itself reports ``powerOn``.
        """
        if await self.async_get_power_state() == "powerOff":
            self._art_desync_count = 0
            return False

        art_result = await self._async_request("artModeControl")
        art_flag = art_result.get("artMode")

        # Independent panel read for the cross-check.
        try:
            states = await self._async_request("getTVStates")
            picture_mode = states.get("pictureMode")
        except SamsungIPControlError:
            picture_mode = None
        panel_art = picture_mode == "Ambient" if picture_mode is not None else None

        if art_flag not in ("artModeOn", "artModeOff"):
            # Unexpected flag value — fall back to the panel signal if we have
            # one, otherwise unknown.
            self._art_desync_count = 0
            return panel_art

        flag_on = art_flag == "artModeOn"

        if flag_on and panel_art is False:
            # Flag says art, panel says a real input. Tolerate a brief lag;
            # escalate to the panel only once it persists.
            self._art_desync_count += 1
            if self._art_desync_count >= ART_DESYNC_THRESHOLD:
                _LOGGER.debug(
                    "IP Control art-mode: artModeControl wedged 'on' but "
                    "pictureMode='%s' (not art) for %d reads — trusting panel, "
                    "art mode off",
                    picture_mode,
                    self._art_desync_count,
                )
                return False
            return True

        self._art_desync_count = 0
        return flag_on

    async def async_set_art_mode_on(self) -> None:
        """Switch the TV to Art Mode."""
        await self._async_request("artModeControl", {"artMode": "artModeOn"})

    async def async_set_art_mode_off(self) -> None:
        """Switch the TV out of Art Mode, back to normal viewing.

        Combined with :meth:`async_power_on`, this gives a deterministic path
        to normal viewing: ``power_on`` lands the TV in Art Mode, then
        ``set_art_mode_off`` exits to live content.
        """
        await self._async_request("artModeControl", {"artMode": "artModeOff"})

    async def async_get_backlight(self) -> int:
        """Return the current picture backlight value."""
        result = await self._async_request("backlightControl")
        value = result.get("backlight")
        if value is None:
            raise SamsungIPControlError(f"no backlight in response: {result!r}")
        try:
            return int(value)
        except (TypeError, ValueError) as ex:
            raise SamsungIPControlError(
                f"invalid backlight response: {result!r}"
            ) from ex

    async def async_set_backlight(self, value: int) -> int:
        """Set and return the picture backlight value."""
        backlight = int(value)
        if backlight < 0 or backlight > 50:
            raise SamsungIPControlError("backlight must be between 0 and 50")
        result = await self._async_request("backlightControl", {"backlight": backlight})
        response_value = result.get("backlight", backlight)
        try:
            return int(response_value)
        except (TypeError, ValueError) as ex:
            raise SamsungIPControlError(
                f"invalid backlight response: {result!r}"
            ) from ex

    async def async_get_color_tone(self) -> str:
        """Return the current picture color tone."""
        result = await self._async_request("colorToneControl")
        value = result.get("colorTone")
        if not isinstance(value, str):
            raise SamsungIPControlError(f"no colorTone in response: {result!r}")
        if value not in COLOR_TONE_OPTIONS:
            raise SamsungIPControlError(f"unexpected colorTone response: {result!r}")
        return value

    async def async_set_color_tone(self, value: str) -> str:
        """Set and return the picture color tone."""
        if value not in COLOR_TONE_OPTIONS:
            raise SamsungIPControlError(
                f"colorTone must be one of {', '.join(COLOR_TONE_OPTIONS)}"
            )
        result = await self._async_request("colorToneControl", {"colorTone": value})
        response_value = result.get("colorTone", value)
        if not isinstance(response_value, str):
            raise SamsungIPControlError(f"invalid colorTone response: {result!r}")
        if response_value not in COLOR_TONE_OPTIONS:
            raise SamsungIPControlError(f"unexpected colorTone response: {result!r}")
        return response_value

    # -- input source (ProOS) ------------------------------------------------

    async def async_get_input_source(self) -> str | None:
        """Return the current input source string (e.g. 'HDMI1'), or None.

        Read from ``getTVStates`` (which reports ``inputSource`` alongside the
        picture/sound state) — already exercised by this client and confirmed
        to carry the live input on Frame and QLED panels, including via the
        certified Control4 control path.
        """
        states = await self._async_request("getTVStates")
        value = states.get("inputSource")
        return None if value is None else str(value)

    async def async_set_input_source(self, source: str, *, confirm: bool = True) -> str:
        """Switch the TV to ``source`` (e.g. 'HDMI1').

        ``confirm=True`` (default): block until ``getTVStates`` reports the panel
        actually landed on ``source``, and return that confirmed value; raise if
        it can't be confirmed. Use this when the caller needs a guaranteed result.

        ``confirm=False`` (ProOS optimistic path): return as soon as the TV
        *accepts* the command (through the not-ready window), WITHOUT the extra
        ``getTVStates`` round-trip. This halves the latency the user sees on an
        input change — the picture appears on the press. The caller is expected
        to confirm in the background (see media_player._async_ip_select_source),
        so verification is not lost, merely moved off the hot path. Returns the
        requested ``source`` string (not a panel-confirmed one).

        Uses the ``inputSourceControl`` JSON-RPC method — the same local call
        certified Control4 drivers use — so discrete input selection works
        WITHOUT SmartThings on panels whose Tizen firmware ignores the discrete
        KEY_HDMIn remote codes.

        TWO THINGS CONFIRMED ON THE PANEL, both of which this method exists to
        handle. Neither was obvious and both caused real failures:

        1. THE ECHO LIES. The reply echoes back whatever string you sent —
           send "HDMI 1" and it replies {"inputSource": "HDMI 1"}; send
           "hdmi1" and it replies {"inputSource": "hdmi1"} — while the panel
           normalises to "HDMI1" internally. So the echo confirms only that the
           request was received, NEVER that the input took, and never what the
           TV actually landed on. We therefore ignore it and read the truth back
           from ``getTVStates``, which reports the real, normalised input.

        2. -32601 HERE MEANS "NOT READY", NOT "NOT SUPPORTED". The method exists
           on this firmware (probing it bare returns the live input), but for a
           few seconds after power-on the TV answers -32601 because the input
           module has not registered yet. Both production failures we chased
           fired within one second of the panel powering on. We retry through
           that window instead of reporting a capability failure.

        Returns the input the TV confirms it is on. Raises if it cannot be
        confirmed — a control that cannot be verified must not report success.
        """
        if not source or not isinstance(source, str):
            raise SamsungIPControlError(f"invalid input source: {source!r}")

        last_error: Exception | None = None
        for attempt in range(NOT_READY_RETRIES):
            try:
                await self._async_request("inputSourceControl", {"inputSource": source})
            except SamsungIPControlNotReadyError as ex:
                # Panel still booting — wait and try again.
                last_error = ex
                _LOGGER.debug(
                    "IP Control: TV not ready for input '%s' (attempt %d/%d): %s",
                    source, attempt + 1, NOT_READY_RETRIES, ex,
                )
                await asyncio.sleep(NOT_READY_DELAY)
                continue

            # Request accepted.
            if not confirm:
                # Optimistic: caller confirms in the background. Return now so the
                # switch feels instant.
                return source
            # Confirm against the panel rather than the (lying) echo.
            confirmed = await self._async_confirm_input(source)
            if confirmed is not None:
                return confirmed

            last_error = SamsungIPControlError(
                f"input '{source}' was accepted but the TV did not report it"
            )
            await asyncio.sleep(NOT_READY_DELAY)

        raise SamsungIPControlError(
            f"could not switch to input '{source}' after {NOT_READY_RETRIES} "
            f"attempts: {last_error}"
        )

    async def async_confirm_input(
        self, source: str, *, retries: int = 5, delay: float = 1.0
    ) -> str | None:
        """Poll ``getTVStates`` a few times until the panel reports ``source``.

        For the optimistic path: run in the background after an unconfirmed set.
        Returns the confirmed panel string, or None if it never matched (caller
        then advises). Never raises for a plain mismatch.
        """
        for _ in range(max(1, retries)):
            try:
                confirmed = await self._async_confirm_input(source)
            except SamsungIPControlError:
                confirmed = None
            if confirmed is not None:
                return confirmed
            await asyncio.sleep(delay)
        return None

    async def _async_confirm_input(self, source: str) -> str | None:
        """Read the live input back from ``getTVStates`` and check it matches.

        Comparison is normalised (case-insensitive, spaces stripped) because the
        TV accepts "HDMI 1"/"hdmi1" but reports "HDMI1". Returns the TV's own
        string on a match, or None if it does not match yet.
        """
        def _norm(value: str) -> str:
            return "".join(str(value).split()).upper()

        try:
            states = await self._async_request("getTVStates")
        except SamsungIPControlNotReadyError:
            return None
        actual = states.get("inputSource")
        if actual is None:
            return None
        return str(actual) if _norm(actual) == _norm(source) else None

    # -- transport -----------------------------------------------------------

    async def _async_request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        include_token: bool = True,
        timeout: int = CMD_TIMEOUT,
    ) -> dict[str, Any]:
        """Run a JSON-RPC request in the executor and return the `result` dict."""
        return await self._hass.async_add_executor_job(
            self._sync_request, method, params, include_token, timeout
        )

    def _build_ssl_context(self, legacy: bool) -> ssl.SSLContext:
        """Build the SSL context for talking to the TV.

        Does file system I/O (CA cert loading) — must only be called from the
        executor, never on the event loop.

        :param legacy: when ``True``, lower OpenSSL's security level so the
            handshake accepts the weak DH group used by older Samsung TVs
            (Tizen <= 5.5, ~2020 Frames).
        """
        ctx = ssl.create_default_context()
        # Panels present a self-signed certificate.
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if legacy:
            try:
                ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
            except ssl.SSLError as ex:
                _LOGGER.warning(
                    "Could not lower TLS security level for %s: %s", self._host, ex
                )
        return ctx

    def _sync_request(
        self,
        method: str,
        params: dict[str, Any] | None,
        include_token: bool,
        timeout: int,
    ) -> dict[str, Any]:
        """Blocking JSON-RPC request — runs in the executor.

        Retries once with @SECLEVEL=1 on a "dh key too small" SSL error so the
        client transparently handles older Samsung TVs.
        """
        body: dict[str, Any] = {
            "jsonrpc": JSONRPC_VERSION,
            "id": 1,
            "method": method,
        }
        if include_token:
            if not self._token:
                raise SamsungIPControlAuthError("no access token — pairing required")
            merged: dict[str, Any] = {"AccessToken": self._token}
            if params:
                merged.update(params)
            body["params"] = merged
        elif params:
            body["params"] = params

        payload = json.dumps(body).encode("utf-8")
        raw = self._sync_post(payload, timeout)

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as ex:
            raise SamsungIPControlError(f"non-JSON response: {raw!r}") from ex

        # The TV reports JSON-RPC errors in TWO shapes: the spec-compliant
        # nested {"error": {"code", "message"}}, AND a flat top-level form
        # {"code": -32700, "message": "Parse error"} with no "result" key
        # (observed e.g. when the AccessToken is stale/invalid). The flat form
        # must be detected explicitly, otherwise a real error slips through as
        # a fake empty success.
        error = data.get("error")
        if error is None and "code" in data and "result" not in data:
            error = {"code": data.get("code"), "message": data.get("message")}
        if error is not None:
            code = error.get("code") if isinstance(error, dict) else None
            message = (
                error.get("message", str(error))
                if isinstance(error, dict)
                else str(error)
            )
            if code == ERROR_UNAUTHORIZED or (
                code == ERROR_PARSE_STALE_TOKEN and include_token
            ):
                raise SamsungIPControlAuthError(
                    f"token rejected (code {code}): {message} — re-pair required"
                )
            if code == ERROR_METHOD_NOT_FOUND:
                raise SamsungIPControlNotReadyError(
                    f"TV returned error {code}: {message} — the method exists on "
                    "this firmware but the TV is not ready yet (this is normal "
                    "for a few seconds after power-on); retry"
                )
            if code == ERROR_SERVER:
                raise SamsungIPControlModeLockedError(
                    f"TV returned error {code}: {message} — the current "
                    "picture mode likely blocks this control (e.g. "
                    "Dynamic/HDR-dynamic); switch to Standard/Movie/"
                    "Filmmaker and retry"
                )
            raise SamsungIPControlError(f"TV returned error {code}: {message}")

        result = data.get("result")
        if not isinstance(result, dict):
            return {}
        return result

    def _sync_post(self, payload: bytes, timeout: int) -> str:
        """Issue the HTTPS POST and return the raw response body.

        Builds the SSL context lazily in the executor; retries once with a
        lowered TLS security level on a "dh key too small" error so the client
        works on older Samsung TVs (Tizen <= 5.5).
        """
        for attempt in (0, 1):
            if self._ctx is None:
                self._ctx = self._build_ssl_context(legacy=self._tls_legacy)
            conn = http.client.HTTPSConnection(
                self._host, self._port, timeout=timeout, context=self._ctx
            )
            try:
                # Keep headers minimal: Host (auto) + Content-Length + Accept +
                # Content-Type. skip_accept_encoding suppresses the default
                # "Accept-Encoding: identity" header.
                conn.putrequest("POST", "/", skip_accept_encoding=True)
                conn.putheader("Accept", "application/json")
                conn.putheader("Content-Type", "application/json")
                conn.putheader("Content-Length", str(len(payload)))
                conn.endheaders()
                conn.send(payload)
                resp = conn.getresponse()
                return resp.read().decode("utf-8")
            except ssl.SSLError as ex:
                if (
                    attempt == 0
                    and not self._tls_legacy
                    and "dh key too small" in str(ex).lower()
                ):
                    _LOGGER.debug(
                        "TLS DH key too small from %s — retrying with legacy "
                        "security level",
                        self._host,
                    )
                    self._tls_legacy = True
                    self._ctx = None
                    continue
                raise SamsungIPControlError(
                    f"TLS error talking to {self._host}:{self._port}: {ex}"
                ) from ex
            except (TimeoutError, OSError) as ex:
                if isinstance(ex, (ConnectionRefusedError, ConnectionResetError)):
                    # Nothing listening on this port (wrong port for this model,
                    # or the TV is off). The auto-probe catches this to try the
                    # next candidate port.
                    raise SamsungIPControlPortClosedError(
                        f"connection refused talking to {self._host}:{self._port}: {ex}"
                    ) from ex
                raise SamsungIPControlError(
                    f"transport failure talking to {self._host}:{self._port}: {ex}"
                ) from ex
            finally:
                conn.close()
        # Loop only retries on the DH error and re-enters; any other path either
        # returns or raises, so reaching here means both attempts somehow fell
        # through without raising — treat as a generic error rather than crash.
        raise SamsungIPControlError("IP Control request failed after retry")


async def async_pair_autoprobe(
    hass: HomeAssistant,
    host: str,
    *,
    ports: tuple[int, ...] = IP_CONTROL_PORT_CANDIDATES,
) -> tuple[int, str]:
    """Pair IP Control, auto-detecting the TV's control port.

    Tries each candidate port in order. A closed port (connection refused)
    means "wrong port for this model" and we move on; the first port that
    answers is the TV's real control port, and we pair on it. The TV must be
    ON in normal viewing (not Art Mode) to accept the on-screen prompt.

    Returns ``(port, token)`` for the port that paired. Raises
    ``SamsungIPControlPortClosedError`` if every candidate port was refused
    (TV off, wrong network, or IP Control unavailable), or another
    ``SamsungIPControlError`` if a port answered but pairing was not completed
    (e.g. the on-screen prompt was not accepted in time).
    """
    last_closed: SamsungIPControlPortClosedError | None = None
    for port in ports:
        client = SamsungIPControl(hass, host, port=port)
        try:
            token = await client.async_pair()
        except SamsungIPControlPortClosedError as ex:
            # Wrong port for this model — try the next candidate.
            _LOGGER.debug(
                "IP Control port %s closed on %s — trying next candidate", port, host
            )
            last_closed = ex
            continue
        # Any other SamsungIPControlError propagates: the port answered, so it
        # is the right one; the failure is a real pairing problem to surface.
        _LOGGER.debug("IP Control paired on %s:%s", host, port)
        return port, token
    # Every candidate port refused the connection.
    raise last_closed or SamsungIPControlPortClosedError(
        f"no IP Control port answered on {host} "
        f"(tried {', '.join(str(p) for p in ports)})"
    )
