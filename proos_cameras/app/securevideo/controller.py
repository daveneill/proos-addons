"""ProOS Cameras — Apple's new Secure Video camera (iOS 27, "HKSV3") on a HAP-python accessory. Register 970u, Route 2.

Ported from HAP-NodeJS PR #1132 src/lib/controller/SecureVideoController.ts (Apache-2.0). It puts on the accessory,
as the original does: Camera Capabilities, Global Operating Mode, Motion Zones, Multi-Tier RTP Stream Management,
WebRTC Stream Management, Camera Recording Management + Camera Operating Mode + Data Stream Transport Management,
Microphone, Speaker (two-way talk), Motion Sensor (with Contributing Sensors and Motion Enabled) — linked the way the
hub expects (WebRTC ↔ RTP, both → Data Stream, Recording → Data Stream and Motion Sensor). No classic camera service:
the original's author found the two cannot share an accessory.

Differences, each stated:
  - HAP-python answers writes on the spot, so the live-view delegate is called without waiting: prepare (ports, keys)
    is immediate, and starting ffmpeg happens in the background after "started" goes back.
  - WebRTC (viewing away from home) is not built yet: an offer request is answered with the original's own error
    status (privacy status when the camera is off in the Home app); other WebRTC writes answer "unknown session".
  - CMAF direct upload is not included (the original's author: every upload refused by Apple so far).
  - our own address for a live view is the address of the very connection the Home app used (HAP-NodeJS takes the
    first address of that connection's network interface).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os

from . import datastream as D
from . import hapdefs as H
from . import recording as R
from . import snapshot as S
from . import svtypes as T

log = logging.getLogger("proos_cameras.securevideo")

PRIMARY, MAIN = 1, 1


def hap_uuid_generate(data: str) -> str:
    """HAP-NodeJS util/uuid.generate: a name-based id from SHA-1 of the text."""
    s = hashlib.sha1(data.encode()).hexdigest()
    out, i = [], -1
    for c in "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx":
        if c in "xy":
            i += 1
            out.append(format((int(s[i], 16) & 0x3) | 0x8, "x") if c == "y" else s[i])
        else:
            out.append(c)
    return "".join(out)


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


class SecureVideoCamera:
    def __init__(self, accessory, options: dict, server: D.Server, rtp_delegate, recording_delegate,
                 snapshot=None, state_path=None, local_address=None):
        """options:
             sensor: {uuid, width, height, type?, intent?}
             video: {codec, payload_type?, tiers: [{identifier, quality, targetAverageBitrate, width, height,
                     frameRate, peakBitrate}]}
             audio: {payload_type?, tier: {...}, two_way?}  (optional)
             recording: RecordingManagement options; event_triggers: bitmask (MOTION always included)
           rtp_delegate: prepare_stream(req) -> resp, start_stream(req), stop_stream(session_id)   (all quick)
           snapshot: async () -> JPEG bytes
           local_address: (client_addr, version) -> our address on that HAP connection"""
        self.acc, self.o, self.server = accessory, options, server
        self.rtp_delegate, self.snapshot = rtp_delegate, snapshot
        self.state_path, self.local_address = state_path, local_address
        self.last = {}            # characteristic uuid -> last response (base64)
        self.zones = None
        self._save_handle = None

        self.capabilities = H.make_service("CameraCapabilities")
        self.global_mode = H.make_service("CameraGlobalOperatingMode", ["ThirdPartyCameraActive"])
        H.char(self.global_mode, "HomeKitCameraActive").set_value(1)
        H.char(self.global_mode, "StreamingEnabled").set_value(True)
        H.char(self.global_mode, "CameraOperatingModeIndicator").set_value(True)
        H.char(self.global_mode, "ThirdPartyCameraActive").set_value(1)
        self.motion_zones = H.make_service("CameraMotionZones")
        self.rtp = H.make_service("CameraMultiTierRTPStreamManagement")
        H.char(self.rtp, "StreamingEnabled").set_value(True)
        self.webrtc = H.make_service("CameraWebRTCStreamManagement")
        H.char(self.webrtc, "StreamingEnabled").set_value(True)

        self.dsm = H.make_service("DataStreamTransportManagement")
        self.management = D.Management(server)
        self.motion = H.make_service("MotionSensor", ["ContributingSensors", "MotionEnabled", "StatusActive"])
        H.char(self.motion, "MotionEnabled").set_value(True)
        H.char(self.motion, "StatusActive").set_value(True)

        audio = options.get("audio")
        self.microphone = self.speaker = None
        if audio:
            self.microphone = H.make_service("Microphone", ["Volume"])
            H.char(self.microphone, "Volume").set_value(100)
            if audio.get("two_way"):
                self.speaker = H.make_service("Speaker", ["Volume"])
                H.char(self.speaker, "Volume").set_value(100)

        self.recording = R.RecordingManagement(options["recording"], recording_delegate,
                                               R.MOTION | int(options.get("event_triggers") or 0), server,
                                               sensor_services=[self.motion], on_state_change=self.state_changed)
        self.snapshots = S.SnapshotTransport(server, snapshot, lambda: self.camera_active) if snapshot else None

        for svc in self.services():          # HAP-python links only services already on the accessory
            accessory.add_service(svc)
        self._configure()

    def services(self):
        out = [self.capabilities, self.global_mode, self.motion_zones, self.rtp, self.webrtc, self.recording.svc,
               self.recording.op, self.dsm]
        out += [s for s in (self.microphone, self.speaker) if s]
        out.append(self.motion)
        return out

    # ---------- wiring (configureServices) ----------
    def _configure(self):
        o = self.o
        sensor, video, audio = o["sensor"], o["video"], o.get("audio")
        sensor_uuid = _b64(T.encode_sensor_uuid(sensor["uuid"]))
        video_tiers = _b64(T.encode_video_tiers(video["codec"], video.get("payload_type", 99), video["tiers"]))
        audio_tiers = _b64(T.encode_audio_tiers(T.OPUS, audio.get("payload_type", 110), [audio["tier"]])) if audio else ""

        H.char(self.capabilities, "Version").set_value(T.CAMERA_CAPABILITIES_VERSION)
        self._read(H.char(self.capabilities, "CameraCapabilitiesConfiguration"), lambda: _b64(T.encode_capabilities(1, [{
            "width": sensor["width"], "height": sensor["height"], "sensorUUID": sensor["uuid"],
            "type": sensor.get("type", PRIMARY), "intent": sensor.get("intent", MAIN),
            "videoStreams": [{"identifier": hap_uuid_generate("%s:tier:%s" % (sensor["uuid"], t["identifier"])),
                              "quality": t["quality"], "width": t["width"], "height": t["height"],
                              "frameRate": t["frameRate"], "averageBitrate": t["targetAverageBitrate"],
                              "peakBitrate": t["peakBitrate"]} for t in video["tiers"]]}])))

        for name in ("HomeKitCameraActive", "StreamingEnabled", "CameraOperatingModeIndicator"):
            H.char(self.global_mode, name).setter_callback = lambda v: (self._update_status_active(), self.state_changed())

        H.char(self.motion_zones, "Version").set_value(T.CAMERA_CAPABILITIES_VERSION)
        H.char(self.motion_zones, "Active").setter_callback = lambda v: self.state_changed()
        zones_char = H.char(self.motion_zones, "CameraZones")
        self._read(zones_char, lambda: _b64(T.encode_zones(self.zones["version"], self.zones["zones"])) if self.zones else "")
        self._write(zones_char, self._zones_write)

        H.char(self.rtp, "SensorUUID").set_value(sensor_uuid)
        H.char(self.rtp, "SupportedVideoStreamTiers").set_value(video_tiers)
        H.char(self.rtp, "SupportedAudioStreamTiers").set_value(audio_tiers)
        H.char(self.rtp, "SupportedRTPConfiguration").set_value(_b64(bytes([0x02, 0x01, 0x00])))   # AES_CM_128_HMAC_SHA1_80
        H.char(self.rtp, "StreamingEnabled").setter_callback = lambda v: (self._update_status_active(), self.state_changed())
        self._write(H.char(self.rtp, "SetupEndpoints"), self._setup_endpoints)
        self._write(H.char(self.rtp, "RTPStreamingControl"), self._rtp_control)

        self.webrtc.add_linked_service(self.rtp)
        self.rtp.add_linked_service(self.webrtc)
        H.char(self.webrtc, "SensorUUID").set_value(sensor_uuid)
        H.char(self.webrtc, "WebRTCSupportedVideoStreamTiers").set_value(video_tiers)
        H.char(self.webrtc, "WebRTCSupportedAudioStreamTiers").set_value(audio_tiers)
        H.char(self.webrtc, "WebRTCNumberOfActiveSessions").set_value(0)
        H.char(self.webrtc, "StreamingEnabled").setter_callback = lambda v: self.state_changed()
        self._write(H.char(self.webrtc, "WebRTCSolicitOffer"), self._solicit_offer)
        for name in ("WebRTCProvideAnswer", "WebRTCUpdateSession", "WebRTCStreamingControl"):
            self._write(H.char(self.webrtc, name), self._webrtc_unknown_session)
        self._write(H.char(self.webrtc, "WebRTCReoffer"), self._reoffer_unknown_session)

        # the data stream (recording, snapshots) and how the hub finds the stream services through it
        H.char(self.dsm, "SupportedDataStreamTransportConfiguration").set_value(_b64(D.SUPPORTED_TRANSPORT_CONFIGURATION))
        H.char(self.dsm, "Version").set_value(D.VERSION)
        setup = H.char(self.dsm, "SetupDataStreamTransport")
        setup.getter_callback = lambda: _b64(self.management.last_response)
        setup.write_handler = self._dsm_setup
        self.recording.svc.add_linked_service(self.dsm)
        self.rtp.add_linked_service(self.dsm)
        self.webrtc.add_linked_service(self.dsm)

        self.recording.svc.add_linked_service(self.motion)
        H.char(self.motion, "MotionEnabled").setter_callback = lambda v: self.state_changed()
        self._update_status_active()

    def _read(self, ch, read):
        ch.getter_callback = lambda: self.last.get(ch.type_id) or read()

    def _write(self, ch, handler):
        """HAP-NodeJS handleTLVWrite: decode base64, run, remember the answer for reads; return it only when the
        characteristic has write-response (Setup Endpoints has not — the Home app reads it back)."""
        if ch.getter_callback is None:
            ch.getter_callback = lambda: self.last.get(ch.type_id, "")
        wr = "wr" in ch.properties["Permissions"]

        def on_write(value, client_addr):
            resp = handler(base64.b64decode(str(value)), client_addr)
            if resp is None:
                return None
            enc = _b64(resp)
            self.last[ch.type_id] = enc
            return enc if wr else None
        ch.write_handler = on_write

    # ---------- state ----------
    @property
    def camera_active(self) -> bool:
        return bool(H.char(self.global_mode, "HomeKitCameraActive").value)

    def streaming_allowed(self, svc=None) -> bool:
        if not self.camera_active:
            return False
        if not H.char(self.global_mode, "StreamingEnabled").value:
            return False
        if H.has(self.global_mode, "ManuallyDisabled") and H.char(self.global_mode, "ManuallyDisabled").value:
            return False
        return svc is None or bool(H.char(svc, "StreamingEnabled").value)

    def _update_status_active(self):
        H.char(self.rtp, "StatusActive").set_value(self.streaming_allowed(self.rtp))

    def set_motion(self, active: bool):
        """Motion from Protect: the motion sensor, plus which sensor saw it (what the hub's recording keys on)."""
        ch = H.char(self.motion, "MotionDetected")
        if bool(ch.value) == bool(active):
            return
        ch.set_value(bool(active))
        H.char(self.motion, "ContributingSensors").set_value(
            _b64(T.encode_contributing_sensors([self.o["sensor"]["uuid"]] if active else [])))

    # ---------- handlers ----------
    def _zones_write(self, value, _addr):
        self.zones = T.decode_zones(value) if value else None
        log.info("Home app motion zones %s", "set" if self.zones else "cleared")
        self.state_changed()
        return None

    def _dsm_setup(self, value, client_addr):
        secret = D.shared_key_for(self.acc.driver, client_addr)
        resp = self.management.setup_write(base64.b64decode(str(value)), secret, client_addr)
        log.info("hub %s asked for a data stream — port %d", client_addr, self.server.port)
        return _b64(resp)

    def _setup_endpoints(self, value, client_addr):
        req = T.decode_rtp_setup_endpoints(value)
        sid = req["sessionIdentifier"]
        version = req["controllerAddress"]["version"]
        try:
            source = self.local_address(client_addr, version)
            resp = self.rtp_delegate.prepare_stream({
                "sessionIdentifier": sid, "addressVersion": version,
                "targetAddress": req["controllerAddress"]["address"], "sourceAddress": source,
                "controllerVideoPort": req["controllerAddress"]["videoRTPPort"],
                "controllerAudioPort": req["controllerAddress"]["audioRTPPort"],
                "video": req["video"], "audio": req["audio"]})
            return T.encode_rtp_setup_endpoints_response({
                "sessionIdentifier": sid, "status": 0,
                "accessoryAddress": {"version": version, "address": resp.get("addressOverride") or source,
                                     "videoRTPPort": resp["videoPort"], "audioRTPPort": resp["audioPort"]},
                "video": resp["video"], "audio": resp["audio"],
                "videoSSRC": resp["videoSSRC"], "audioSSRC": resp["audioSSRC"]})
        except Exception:
            log.exception("live view: setup failed")
            empty = {"cryptoSuite": 0, "masterKey": b"", "masterSalt": b""}
            return T.encode_rtp_setup_endpoints_response({
                "sessionIdentifier": sid, "status": 2,
                "accessoryAddress": {"version": "ipv4", "address": "0.0.0.0", "videoRTPPort": 0, "audioRTPPort": 0},
                "video": empty, "audio": empty, "videoSSRC": 0, "audioSSRC": 0})

    def _rtp_control(self, value, _addr):
        req = T.decode_rtp_streaming_control(value)
        sid = req["sessionIdentifier"]
        try:
            if req["command"] == T.RTP_START:
                if not self.streaming_allowed(self.rtp):
                    return T.encode_session_status(sid, T.RTP_ERROR)
                self.rtp_delegate.start_stream({"sessionIdentifier": sid, "videoTier": req.get("videoTier") or 0,
                                                "videoSSRC": req.get("videoSSRC") or 0,
                                                "audioTier": req.get("audioTier"), "audioSSRC": req.get("audioSSRC")})
            else:
                self.rtp_delegate.stop_stream(sid)
            return T.encode_session_status(sid, T.RTP_SUCCESS)
        except Exception:
            log.exception("live view: streaming control failed")
            return T.encode_session_status(sid, T.RTP_ERROR)

    def _solicit_offer(self, value, _addr):
        if not self.streaming_allowed(self.webrtc):
            return T.encode_webrtc_solicit_offer_status(
                T.WEBRTC_OFFER_PRIVACY_MODE_ACTIVE if not self.camera_active else T.WEBRTC_OFFER_ERROR)
        log.info("away-from-home viewing (WebRTC) asked for — not built yet, answered with an error")
        return T.encode_webrtc_solicit_offer_status(T.WEBRTC_OFFER_ERROR)

    def _webrtc_unknown_session(self, value, _addr):
        return T.encode_session_status(T.decode_session_identifier(value), T.WEBRTC_UNKNOWN_SESSION)

    def _reoffer_unknown_session(self, value, _addr):
        # encodeWebRTCReofferResponse({sessionIdentifier, status}): no answer text, the status at tag 3
        sid = T.decode_session_identifier(value)
        return (T.item(1, T.uuid_bytes(sid)) if sid else b"") + T.item(3, T.u8(T.WEBRTC_UNKNOWN_SESSION))

    # ---------- snapshots through the Home app's picture request ----------
    def snapshot_allowed(self, reason=None) -> bool:
        op = self.recording.op
        if not self.camera_active or not H.char(op, "HomeKitCameraActive").value:
            return False
        if not H.char(op, "EventSnapshotsActive").value and reason in (None, "event"):
            return False
        if not H.char(op, "PeriodicSnapshotsActive").value and reason in (None, "periodic"):
            return False
        return True

    # ---------- persistence ----------
    def state_changed(self):
        if not self.state_path:
            return
        loop = self.server.loop
        if self._save_handle:
            self._save_handle.cancel()
        self._save_handle = loop.call_later(0.5, self.save)

    def serialize(self) -> dict:
        g = self.global_mode
        return {"homeKitCameraActive": int(H.char(g, "HomeKitCameraActive").value or 0),
                "operatingModeIndicator": int(bool(H.char(g, "CameraOperatingModeIndicator").value)),
                "globalStreamingEnabled": bool(H.char(g, "StreamingEnabled").value),
                "webrtcStreamingEnabled": bool(H.char(self.webrtc, "StreamingEnabled").value),
                "rtpStreamingEnabled": bool(H.char(self.rtp, "StreamingEnabled").value),
                "motionEnabled": bool(H.char(self.motion, "MotionEnabled").value),
                "motionZonesActive": int(H.char(self.motion_zones, "Active").value or 0),
                "zones": _b64(T.encode_zones(self.zones["version"], self.zones["zones"])) if self.zones else None,
                "recordingManagement": self.recording.serialize()}

    def save(self):
        self._save_handle = None
        try:
            tmp = self.state_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.serialize(), f)
            os.replace(tmp, self.state_path)
        except Exception:
            log.exception("could not save camera state")

    def load(self):
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            s = json.load(open(self.state_path))
        except Exception:
            log.exception("could not read saved camera state")
            return
        g = self.global_mode
        H.char(g, "HomeKitCameraActive").set_value(int(s.get("homeKitCameraActive", 1)))
        H.char(g, "CameraOperatingModeIndicator").set_value(bool(s.get("operatingModeIndicator", 1)))
        H.char(g, "StreamingEnabled").set_value(bool(s.get("globalStreamingEnabled", True)))
        H.char(self.webrtc, "StreamingEnabled").set_value(bool(s.get("webrtcStreamingEnabled", True)))
        H.char(self.rtp, "StreamingEnabled").set_value(bool(s.get("rtpStreamingEnabled", True)))
        H.char(self.motion, "MotionEnabled").set_value(bool(s.get("motionEnabled", True)))
        H.char(self.motion_zones, "Active").set_value(int(s.get("motionZonesActive", 0)))
        if s.get("recordingManagement"):
            self.recording.deserialize(s["recordingManagement"])
        self.zones = T.decode_zones(base64.b64decode(s["zones"])) if s.get("zones") else None
        self._update_status_active()
