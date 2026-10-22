"""ProOS Cameras — snapshots over the HomeKit Data Stream (iOS 27 asks Secure Video cameras for pictures this way).
Register 970u, Route 2.

Ported from HAP-NodeJS PR #1132 src/lib/camera/HDSSnapshotTransport.ts (Apache-2.0): the hub sends dataSend/open with
type "ipcamera.snapshot"; we answer, take one picture (one capture shared by requests that overlap), send it as
256 KB "image" chunks, and finish on the hub's end-of-stream ack or close; 25 s without either closes it as timed out.
"""
from __future__ import annotations

import asyncio
import logging

from . import datastream as D

log = logging.getLogger("proos_cameras.snapshot")

CHUNK_SIZE = 0x40000
TIMEOUT = 25.0


class _Transfer:
    __slots__ = ("conn", "stream_id", "timer", "sent", "on_close")

    def __init__(self, conn, stream_id):
        self.conn, self.stream_id, self.timer, self.sent, self.on_close = conn, stream_id, None, False, None


class SnapshotTransport:
    def __init__(self, server: D.Server, snapshot, is_active):
        """snapshot: async () -> JPEG bytes; is_active: () -> bool (the camera is on in the Home app)."""
        self.server, self.snapshot, self.is_active = server, snapshot, is_active
        self.transfers = []
        self._capture = None
        server.on("dataSend", "request", "open", self._on_open)
        server.on("dataSend", "event", "ack", self._on_ack)
        server.on("dataSend", "event", "close", self._on_close_event)

    def destroy(self):
        self.server.off("dataSend", "request", "open", self._on_open)
        self.server.off("dataSend", "event", "ack", self._on_ack)
        self.server.off("dataSend", "event", "close", self._on_close_event)
        self.close_all()

    def close_all(self):
        for t in list(self.transfers):
            self._finish(t, D.R_CANCELLED)

    def _on_open(self, conn, rid, message):
        if isinstance(message, dict) and message.get("type") == "ipcamera.snapshot":
            self._open(conn, rid, message)

    def _on_ack(self, conn, message):
        t = self._find(conn, (message or {}).get("streamId"))
        if t and t.sent and (message or {}).get("endOfStream") is True:
            self._finish(t)

    def _on_close_event(self, conn, message):
        t = self._find(conn, (message or {}).get("streamId"))
        if t:
            self._finish(t)

    def _find(self, conn, stream_id=None):
        for t in self.transfers:
            if t.conn is conn and (stream_id is None or t.stream_id == stream_id):
                return t
        return None

    def _reject(self, conn, rid, reason):
        conn.send_response("dataSend", "open", rid, D.PROTOCOL_SPECIFIC_ERROR, {"status": reason})

    def _open(self, conn, rid, message):
        sid = message.get("streamId")
        if message.get("target") != "controller" or not isinstance(sid, int) or isinstance(sid, bool) or sid < 0:
            self._reject(conn, rid, D.R_UNEXPECTED_FAILURE)
            return
        if not self.is_active():
            self._reject(conn, rid, D.R_NOT_ALLOWED)
            return
        if self._find(conn, sid):
            self._reject(conn, rid, D.R_BUSY)
            return
        t = _Transfer(conn, sid)
        t.on_close = lambda _c, t=t: self._finish(t)
        self.transfers.append(t)
        conn.on_close.append(t.on_close)
        self._arm(t)
        try:
            conn.send_response("dataSend", "open", rid, D.SUCCESS, {"status": 0})
        except Exception:
            self._finish(t)
            return
        asyncio.ensure_future(self._send_image(t), loop=conn.loop)

    def _capture_once(self, loop):
        if self._capture is None or self._capture.done():
            self._capture = asyncio.ensure_future(self.snapshot(), loop=loop)
        return self._capture

    async def _send_image(self, t):
        try:
            jpeg = await asyncio.shield(self._capture_once(t.conn.loop))
            if t not in self.transfers:
                return
            if not self.is_active():
                self._finish(t, D.R_NOT_ALLOWED)
                return
            if not jpeg or len(jpeg) < 4 or jpeg[0] != 0xFF or jpeg[1] != 0xD8:
                raise ValueError("snapshot is empty or not JPEG")
            seq, off = 1, 0
            while off < len(jpeg):
                if t not in self.transfers:
                    return
                data = jpeg[off:off + CHUNK_SIZE]
                last = off + len(data) == len(jpeg)
                t.sent = last
                meta = {"dataType": "image", "dataSequenceNumber": 1, "dataChunkSequenceNumber": seq, "isLastDataChunk": last}
                if seq == 1:
                    meta["dataTotalSize"] = len(jpeg)
                t.conn.send_event("dataSend", "data", {"streamId": t.stream_id, "packets": [{"data": data, "metadata": meta}],
                                                        "endOfStream": last})
                if not last:
                    await asyncio.sleep(0)
                off += len(data)
                seq += 1
            log.info("snapshot: sent %d bytes (stream %d)", len(jpeg), t.stream_id)
            if t in self.transfers:
                self._arm(t)
        except Exception as e:
            if t not in self.transfers:
                return
            log.info("snapshot failed: %s", e)
            self._finish(t, D.R_UNEXPECTED_FAILURE)

    def _arm(self, t):
        if t.timer:
            t.timer.cancel()
        t.timer = t.conn.loop.call_later(TIMEOUT, self._finish, t, D.R_TIMEOUT)

    def _finish(self, t, reason=None):
        if t not in self.transfers:
            return
        self.transfers.remove(t)
        if t.timer:
            t.timer.cancel()
        if t.on_close in t.conn.on_close:
            t.conn.on_close.remove(t.on_close)
        if reason is not None:
            try:
                t.conn.send_event("dataSend", "close", {"streamId": t.stream_id, "reason": reason})
            except Exception:
                pass
