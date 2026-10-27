"""ProOS certified additions to the Apple TV driver (register 1034; Dave 27 Oct: "Please build it").

Everything here talks to the Apple TV through pyatv's OWN protocol objects (pinned pyatv==0.18.0) — the same messages
Apple's Remote app sends. The platform's integration code is untouched; these are extra readings and actions the pages,
Core and Assist use. Each one answers "I don't know" (None / an error naming why) when the Apple TV or the paired
protocol does not offer it — never a guess.

  display_state   asleep · screensaver · idle · awake — the Apple TV's own pushed system status (Companion)
  accounts        the Apple TV's user profiles; switch_account
  key             any Siri Remote key as tap · double · hold · down · up (down/up = a real press held until released)
  touch           swipe (start → end, ms), press/hold/release at a point, click (tap/double/hold)
  media           skip_by seconds, scrub (fast-forward / rewind begin · end), next/previous chapter
  captions        the caption settings (get / set) the Apple TV offers through Companion
  now_playing     the play queue (up next), language options (audio / subtitles) and chapter count, when the app shares
                  them (MRP over AirPlay); play a queue item; choose a language option
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Optional

_LOGGER = logging.getLogger(__name__)

# pyatv's own companion system-status numbers (pyatv.protocols.companion.api.SystemStatus)
DISPLAY_STATES = {1: "asleep", 2: "screensaver", 3: "awake", 4: "idle"}
# PLUMBING: how many queue items to ask for (up next), and the longest a key may stay held without its release
# (Chowmain's guide: "Max Hold Time", default 5 s) — a lost release must never leave a key held on the Apple TV.
QUEUE_LENGTH = 12
MAX_HOLD_S = 5.0
KEYS = ("up", "down", "left", "right", "select", "menu", "home", "play_pause", "volume_up", "volume_down",
        "screensaver", "channel_up", "channel_down", "guide", "page_up", "page_down")
_HID = {"up": "Up", "down": "Down", "left": "Left", "right": "Right", "select": "Select", "menu": "Menu",
        "home": "Home", "play_pause": "PlayPause", "volume_up": "VolumeUp", "volume_down": "VolumeDown",
        "screensaver": "Screensaver", "channel_up": "ChannelIncrement", "channel_down": "ChannelDecrement",
        "guide": "Guide", "page_up": "PageUp", "page_down": "PageDown"}
# taps / doubles / 1-second holds go through pyatv's public remote (it chooses the protocol); these are its names
_REMOTE_NAMES = {"play_pause": "play_pause", "home": "home", "menu": "menu", "select": "select", "up": "up",
                 "down": "down", "left": "left", "right": "right", "volume_up": "volume_up",
                 "volume_down": "volume_down", "screensaver": "screensaver", "channel_up": "channel_up",
                 "channel_down": "channel_down", "guide": "guide",
                 "top_menu": "top_menu", "control_center": "control_center"}


# the keys pyatv's remote lets be tapped, double-tapped or held (interface.RemoteControl: up/down/left/right/select/menu/home)
_ACTION_KEYS = ("up", "down", "left", "right", "select", "menu", "home")


class NotOffered(Exception):
    """The Apple TV (or the paired protocol) does not offer this; the message says which."""


def _protocol_enum():
    from pyatv.const import Protocol
    return Protocol


class ProOSExtras:
    """One per connected Apple TV. `on_change` is called (no arguments) when a pushed reading changes."""

    def __init__(self, atv, on_change: Optional[Callable[[], None]] = None):
        self.atv = atv
        self.on_change = on_change or (lambda: None)
        self.display_state: Optional[str] = None
        self._held: dict = {}

    # ── the protocol objects ────────────────────────────────────────────────────────────────────────────────────
    def _companion(self):
        try:
            rc = self.atv.remote_control.get(_protocol_enum().Companion)
            return getattr(rc, "api", None)
        except Exception:                                   # noqa: BLE001
            return None

    def _mrp(self):
        try:
            return self.atv.remote_control.get(_protocol_enum().MRP)
        except Exception:                                   # noqa: BLE001
            return None

    # ── display state (pushed) ──────────────────────────────────────────────────────────────────────────────────
    async def start(self) -> None:
        api = self._companion()
        if api is None:
            return
        for ev in ("SystemStatus", "TVSystemStatus"):
            try:
                api.listen_to(ev, self._system_status)
            except Exception as err:                        # noqa: BLE001
                _LOGGER.debug("ProOS: cannot listen to %s (%s)", ev, err)
        try:
            st = await api.fetch_attention_state()
            self._set_display(getattr(st, "value", st))
        except Exception as err:                            # noqa: BLE001 — tvOS 26.5+: no handler; pushes still come
            _LOGGER.debug("ProOS: initial attention state unavailable (%s)", err)

    async def _system_status(self, data) -> None:
        try:
            self._set_display(int((data or {}).get("state")))
        except Exception:                                   # noqa: BLE001
            pass

    def _set_display(self, value) -> None:
        new = DISPLAY_STATES.get(value) if isinstance(value, int) else None
        if new != self.display_state:
            self.display_state = new
            try:
                self.on_change()
            except Exception:                               # noqa: BLE001
                pass

    # ── profiles ────────────────────────────────────────────────────────────────────────────────────────────────
    async def accounts(self) -> list:
        try:
            lst = await self.atv.user_accounts.account_list()
        except Exception as err:                            # noqa: BLE001
            raise NotOffered("This Apple TV does not list its user profiles (%s)" % type(err).__name__) from err
        return [{"id": a.identifier, "name": a.name} for a in lst]

    async def switch_account(self, account_id: str) -> None:
        try:
            await self.atv.user_accounts.switch_account(account_id)
        except Exception as err:                            # noqa: BLE001
            raise NotOffered("This Apple TV did not switch profile (%s)" % type(err).__name__) from err

    # ── keys ────────────────────────────────────────────────────────────────────────────────────────────────────
    async def key(self, name: str, action: str = "tap") -> None:
        from pyatv.const import InputAction
        if action in ("down", "up"):
            api = self._companion()
            if api is None:
                raise NotOffered("A held key needs the remote (Companion) pairing")
            if name not in _HID:
                raise NotOffered("%s cannot be held" % name)
            from pyatv.protocols.companion.api import HidCommand
            cmd = HidCommand[_HID[name]]
            if action == "down":
                await api.hid_command(True, cmd)
                self._arm_release(name, api, cmd)
            else:
                self._disarm(name)
                await api.hid_command(False, cmd)
            return
        fn = getattr(self.atv.remote_control, _REMOTE_NAMES.get(name, ""), None)
        if fn is None and name in _HID and action == "tap":
            # a key pyatv's remote does not name (Page Up / Page Down): a press and release on the remote itself
            api = self._companion()
            if api is None:
                raise NotOffered("%s needs the remote (Companion) pairing" % name)
            from pyatv.protocols.companion.api import HidCommand
            await api.hid_command(True, HidCommand[_HID[name]])
            await api.hid_command(False, HidCommand[_HID[name]])
            return
        if fn is None:
            raise NotOffered("%s is not a key this Apple TV offers" % name)
        if name not in _ACTION_KEYS:
            # register 1038: pyatv's play_pause, volume, top menu… take no press style — passing one made the Apple TV
            # refuse every Play/Pause from the page ("got an unexpected keyword argument 'action'", measured 27 Oct)
            if action not in ("tap", None):
                raise NotOffered("%s has no %s" % (name, action))
            await fn()
            return
        act = {"tap": InputAction.SingleTap, "double": InputAction.DoubleTap, "hold": InputAction.Hold}.get(action)
        if act is None:
            raise NotOffered("Unknown key action %s" % action)
        await fn(action=act)

    def _arm_release(self, name, api, cmd) -> None:
        self._disarm(name)

        async def _auto():
            await asyncio.sleep(MAX_HOLD_S)
            self._held.pop(name, None)
            try:
                await api.hid_command(False, cmd)
                _LOGGER.info("ProOS: %s released after %.0f s with no release (max hold)", name, MAX_HOLD_S)
            except Exception:                               # noqa: BLE001
                pass
        try:
            self._held[name] = asyncio.get_event_loop().create_task(_auto())
        except Exception:                                   # noqa: BLE001
            pass

    def _disarm(self, name) -> None:
        t = self._held.pop(name, None)
        if t is not None:
            t.cancel()

    async def release_all(self) -> None:
        """Called when the connection closes: nothing stays held."""
        for name in list(self._held):
            self._disarm(name)

    # ── touch surface ───────────────────────────────────────────────────────────────────────────────────────────
    async def touch(self, kind: str, x: int = 500, y: int = 500, x2: int = 500, y2: int = 500,
                    duration_ms: int = 200, action: str = "tap") -> None:
        api = self._companion()
        if api is None:
            raise NotOffered("The touch surface needs the remote (Companion) pairing")
        from pyatv.const import InputAction, TouchAction
        clamp = lambda v: max(0, min(1000, int(v)))              # noqa: E731
        if kind == "swipe":
            await api.swipe(clamp(x), clamp(y), clamp(x2), clamp(y2), max(50, min(2000, int(duration_ms))))
        elif kind in ("press", "hold", "release"):
            await api.action(clamp(x), clamp(y), {"press": TouchAction.Press, "hold": TouchAction.Hold,
                                                  "release": TouchAction.Release}[kind])
        elif kind == "click":
            await api.click({"tap": InputAction.SingleTap, "double": InputAction.DoubleTap,
                             "hold": InputAction.Hold}.get(action, InputAction.SingleTap))
        else:
            raise NotOffered("Unknown touch %s" % kind)

    # ── media ───────────────────────────────────────────────────────────────────────────────────────────────────
    async def media(self, command: str, seconds: float = 0.0) -> None:
        if command in ("next_chapter", "previous_chapter"):
            await self._mrp_command("NextChapter" if command == "next_chapter" else "PreviousChapter")
            return
        api = self._companion()
        if api is None:
            raise NotOffered("This needs the remote (Companion) pairing")
        from pyatv.protocols.companion.api import MediaControlCommand as M
        if command == "skip_by":
            await api.mediacontrol_command(M.SkipBy, {"_skpS": float(max(-600.0, min(600.0, seconds)))})
        elif command in ("scrub_forward_begin", "scrub_forward_end", "scrub_back_begin", "scrub_back_end"):
            await api.mediacontrol_command({"scrub_forward_begin": M.FastForwardBegin, "scrub_forward_end": M.FastForwardEnd,
                                            "scrub_back_begin": M.RewindBegin, "scrub_back_end": M.RewindEnd}[command])
        else:
            raise NotOffered("Unknown media command %s" % command)

    # ── captions ────────────────────────────────────────────────────────────────────────────────────────────────
    async def captions(self, settings: Optional[dict] = None) -> dict:
        """Read (no settings) or write the caption settings exactly as the Apple TV gives them."""
        api = self._companion()
        if api is None:
            raise NotOffered("Captions need the remote (Companion) pairing")
        from pyatv.protocols.companion.api import MediaControlCommand as M
        if settings is None:
            resp = await api.mediacontrol_command(M.GetCaptionSettings)
            return dict((resp or {}).get("_c") or {})
        resp = await api.mediacontrol_command(M.SetCaptionSettings, dict(settings))
        return dict((resp or {}).get("_c") or {})

    # ── now playing: queue, language options, chapters (MRP) ────────────────────────────────────────────────────
    async def now_playing(self) -> dict:
        mrp = self._mrp()
        if mrp is None:
            raise NotOffered("What's playing needs the AirPlay pairing")
        from pyatv.protocols.mrp import messages, protobuf
        psm = mrp.psm
        playing = psm.playing
        msg = messages.create(protobuf.PLAYBACK_QUEUE_REQUEST_MESSAGE)
        req = protobuf.extract_inner(msg)
        req.location = int(getattr(playing, "location", 0) or 0)
        req.length = QUEUE_LENGTH
        req.includeMetadata = True
        req.includeLanguageOptions = True
        req.includeSections = True
        req.artworkWidth = -1
        req.artworkHeight = 0
        resp = await mrp.protocol.send_and_receive(msg)
        if not resp.HasField("type"):
            return {"items": [], "location": req.location}
        queue = protobuf.extract_inner(resp).playbackQueue
        return parse_queue(queue, req.location)

    async def play_queue_item(self, item_id: str) -> None:
        await self._mrp_command("PlayItemInPlaybackQueue", contentItemID=str(item_id))

    async def language_option(self, option: dict, enable: bool = True) -> None:
        from pyatv.protocols.mrp.protobuf import LanguageOption
        lo = LanguageOption()
        for k in ("type", "languageTag", "displayName", "identifier"):
            if option.get(k) is not None:
                setattr(lo, k, option[k])
        for c in option.get("characteristics") or []:
            lo.characteristics.append(str(c))
        await self._mrp_command("EnableLanguageOption" if enable else "DisableLanguageOption",
                                languageOption=lo.SerializeToString())

    async def _mrp_command(self, name: str, **opts) -> None:
        mrp = self._mrp()
        if mrp is None:
            raise NotOffered("This needs the AirPlay pairing")
        from pyatv.protocols.mrp.protobuf import CommandInfo_pb2
        try:
            await mrp._send_command(getattr(CommandInfo_pb2, name), **opts)   # pyatv's own sender; raises on refusal
        except Exception as err:                            # noqa: BLE001
            raise NotOffered("The app on the Apple TV refused %s (%s)" % (name, err)) from err


def parse_queue(queue, location: int) -> dict:
    """Pure: an MRP PlaybackQueue → plain data. Language options are returned in the GROUPS the app sends (it names
    them; ProOS does not decide which group is audio and which subtitles until that is measured per app)."""
    items = []
    for it in getattr(queue, "contentItems", []) or []:
        md = it.metadata if it.HasField("metadata") else None
        f = lambda n: (getattr(md, n) if md is not None and md.HasField(n) else None)   # noqa: E731
        items.append({
            "id": it.identifier,
            "title": f("title"),
            "artist": f("trackArtistName"),
            "series": f("seriesName"),
            "season": f("seasonNumber"),
            "episode": f("episodeNumber"),
            "duration": f("duration"),
            "chapter_count": f("chapterCount"),
            "language_groups": [{"allow_empty": g.allowEmptySelection if g.HasField("allowEmptySelection") else None,
                                 "default": lang(g.defaultLanguageOption) if g.HasField("defaultLanguageOption") else None,
                                 "options": [lang(o) for o in g.languageOptions]}
                                for g in it.availableLanguageOptions],
            "current_language": [lang(o) for o in it.currentLanguageOptions],
        })
    return {"location": location, "items": items}


def lang(o) -> dict:
    return {"type": o.type if o.HasField("type") else None,
            "languageTag": o.languageTag if o.HasField("languageTag") else None,
            "displayName": o.displayName if o.HasField("displayName") else None,
            "identifier": o.identifier if o.HasField("identifier") else None,
            "characteristics": list(o.characteristics)}
