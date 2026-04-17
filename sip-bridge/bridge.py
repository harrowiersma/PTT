"""openPTT SIP bridge — Phase 2b-initial.

Responsibilities in this initial slice:
  1. Read trunk + DID config from the admin API on startup.
  2. Register to the first enabled trunk using pjsua2.
  3. Accept inbound INVITEs. Play a TTS greeting, then hang up.
  4. Expose minimal status via logs; no bidirectional audio bridge yet.

The audio bridge into Mumble (Phase 2b-audio) is a separate build — pjsua2
doesn't expose raw-frame callbacks cleanly from Python, so that phase will
either swap to Asterisk+ARI or layer in a Baresip sidecar with ALSA
loopback. For now this container proves the network path: credentials
register, calls reach us, we accept, the caller gets audio back.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import time

import httpx

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("sip-bridge")


ADMIN_BASE_URL = os.environ.get("ADMIN_INTERNAL_URL", "http://admin:8000")
INTERNAL_SECRET = os.environ.get("PTT_INTERNAL_API_SECRET", "").strip()
LOCAL_SIP_PORT = int(os.environ.get("LOCAL_SIP_PORT", "5060"))
PUBLIC_ADDR = os.environ.get("PUBLIC_ADDR", "").strip() or None
GREETING_WAV = os.environ.get("GREETING_WAV", "/app/greeting.wav")
GREETING_TEXT = os.environ.get(
    "GREETING_TEXT",
    "You are now being connected to the openPTT radio trunk system. "
    "Please note that there may be small delays between transmissions.",
)


def fetch_config() -> tuple[list[dict], list[dict]]:
    """Pull trunk + DID config from the admin API's internal endpoint."""
    if not INTERNAL_SECRET:
        logger.error("PTT_INTERNAL_API_SECRET not set; cannot call admin")
        return [], []
    headers = {"X-Internal-Auth": INTERNAL_SECRET}
    for attempt in range(30):
        try:
            with httpx.Client(timeout=5, headers=headers) as client:
                trunks = client.get(f"{ADMIN_BASE_URL}/api/sip/internal/config/trunks")
                trunks.raise_for_status()
                numbers = client.get(f"{ADMIN_BASE_URL}/api/sip/internal/config/numbers")
                numbers.raise_for_status()
                trunks_j = trunks.json()
                numbers_j = numbers.json()
                logger.info(
                    "Loaded config: %d trunk(s), %d DID(s)",
                    len(trunks_j), len(numbers_j),
                )
                return trunks_j, numbers_j
        except Exception as e:
            logger.warning("Admin API not ready (attempt %d): %s", attempt + 1, e)
            time.sleep(2)
    logger.error("Gave up waiting for admin API after 30 attempts")
    return [], []


def ensure_greeting_wav(path: str) -> None:
    """Fetch a TTS greeting WAV from the admin, or fall back to tones.

    First tries admin's /api/sip/internal/tts with GREETING_TEXT.
    On any failure (admin unreachable, Piper not loaded yet, etc.)
    falls back to a three-tone pattern so the caller still hears
    something and knows the bridge answered.
    """
    if os.path.exists(path):
        return

    # Attempt TTS via admin first.
    if INTERNAL_SECRET:
        try:
            with httpx.Client(timeout=60) as client:
                resp = client.post(
                    f"{ADMIN_BASE_URL}/api/sip/internal/tts",
                    headers={"X-Internal-Auth": INTERNAL_SECRET},
                    json={"text": GREETING_TEXT},
                )
                if resp.status_code == 200 and resp.content:
                    with open(path, "wb") as f:
                        f.write(resp.content)
                    logger.info(
                        "Greeting TTS cached at %s (%d bytes, text=%r)",
                        path, len(resp.content),
                        GREETING_TEXT[:60] + ("..." if len(GREETING_TEXT) > 60 else ""),
                    )
                    return
                logger.warning("admin TTS returned %d; falling back to tones", resp.status_code)
        except Exception as e:
            logger.warning("admin TTS unreachable (%s); falling back to tones", e)

    # Fallback: synthesize a three-tone pattern with numpy+wave.
    try:
        import numpy as np
        import wave

        sr = 8000
        def tone(freq_hz: float, ms: int, amp: float = 0.25) -> np.ndarray:
            n = int(sr * ms / 1000)
            t = np.arange(n, dtype=np.float32) / sr
            return (np.sin(2 * np.pi * freq_hz * t) * amp * 32767).astype(np.int16)
        def silence(ms: int) -> np.ndarray:
            return np.zeros(int(sr * ms / 1000), dtype=np.int16)

        data = np.concatenate([
            tone(800, 300), silence(150),
            tone(600, 300), silence(150),
            tone(400, 400), silence(500),
        ])
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(data.tobytes())
        logger.info("Fallback tone WAV written to %s (%d frames @ %dHz)", path, len(data), sr)
    except Exception as e:
        logger.warning("Could not synthesize fallback greeting: %s", e)


def run_bridge(trunks: list[dict], numbers: list[dict]) -> None:
    """Start pjsua2, register, and handle inbound calls."""
    import pjsua2 as pj

    enabled_trunks = [t for t in trunks if t.get("enabled")]
    if not enabled_trunks:
        logger.error("No enabled SIP trunks configured; nothing to do")
        return

    # Phase 2b-initial: single-trunk support.
    trunk = enabled_trunks[0]
    logger.info(
        "Using trunk id=%s label=%r host=%s:%s transport=%s auth=%s",
        trunk.get("id"), trunk.get("label"),
        trunk.get("sip_host"), trunk.get("sip_port"), trunk.get("transport"),
        "user" if trunk.get("sip_user") else "ip",
    )

    enabled_dids = [n.get("did") for n in numbers if n.get("enabled")]
    logger.info("Active DIDs: %s", ", ".join(enabled_dids) or "(none)")

    ep = pj.Endpoint()
    ep.libCreate()

    ep_cfg = pj.EpConfig()
    ep_cfg.logConfig.level = 4
    ep_cfg.logConfig.consoleLevel = 4
    ep_cfg.uaConfig.userAgent = "openPTT-SIP-Bridge/0.1"
    ep.libInit(ep_cfg)

    tcfg = pj.TransportConfig()
    tcfg.port = LOCAL_SIP_PORT
    if PUBLIC_ADDR:
        tcfg.publicAddress = PUBLIC_ADDR
    transport_map = {
        "udp": pj.PJSIP_TRANSPORT_UDP,
        "tcp": pj.PJSIP_TRANSPORT_TCP,
        "tls": pj.PJSIP_TRANSPORT_TLS,
    }
    tp_type = transport_map.get(trunk.get("transport", "udp"), pj.PJSIP_TRANSPORT_UDP)
    ep.transportCreate(tp_type, tcfg)

    ep.libStart()
    logger.info("pjsua2 started on port %d", LOCAL_SIP_PORT)

    # Ensure pjsua2 sees silence as the "sound device" — the container has
    # no ALSA hardware and we're not bridging audio in this phase.
    ep.audDevManager().setNullDev()

    # Build and register the account.
    acfg = pj.AccountConfig()
    sip_user = trunk.get("sip_user") or ""
    sip_host = trunk.get("sip_host")
    sip_port = trunk.get("sip_port") or 5060
    if sip_user:
        acfg.idUri = f"sip:{sip_user}@{sip_host}"
        acfg.regConfig.registrarUri = f"sip:{sip_host}:{sip_port}"
        cred = pj.AuthCredInfo("digest", "*", sip_user, 0, trunk.get("sip_password") or "")
        acfg.sipConfig.authCreds.append(cred)
    else:
        # IP-auth: no registration, accept inbound INVITEs on our port.
        acfg.idUri = f"sip:bridge@{sip_host}"
        acfg.regConfig.registrarUri = ""

    ensure_greeting_wav(GREETING_WAV)

    # Custom Account to handle inbound calls.
    class BridgeAccount(pj.Account):
        def __init__(self):
            super().__init__()
            self._calls = []

        def onRegState(self, prm):
            ai = self.getInfo()
            logger.info(
                "Registration state: code=%d reason=%s reg_active=%s expires=%s",
                prm.code, prm.reason, ai.regIsActive, ai.regExpiresSec,
            )

        def onIncomingCall(self, prm):
            call = BridgeCall(self, prm.callId)
            call_info = call.getInfo()
            logger.info(
                "Incoming call from %s to %s (id=%s)",
                call_info.remoteUri, call_info.localUri, call_info.callIdString,
            )
            cop = pj.CallOpParam()
            cop.statusCode = pj.PJSIP_SC_OK
            try:
                call.answer(cop)
                self._calls.append(call)
            except pj.Error as e:
                logger.error("answer() failed: %s", e)

    class BridgeCall(pj.Call):
        def __init__(self, acc: "BridgeAccount", call_id):
            super().__init__(acc, call_id)
            self._player = None

        def onCallState(self, prm):
            ci = self.getInfo()
            logger.info(
                "Call state: %s (reason=%s)",
                ci.stateText, prm.e.body.tsxState.tsxStatusText
                if prm.e.body.tsxState.tsxStatusText else "",
            )
            if ci.state == pj.PJSIP_INV_STATE_DISCONNECTED:
                if self._player:
                    try:
                        self._player = None
                    except Exception:
                        pass

        def onCallMediaState(self, prm):
            ci = self.getInfo()
            for i, m in enumerate(ci.media):
                if m.status != pj.PJSUA_CALL_MEDIA_ACTIVE:
                    continue
                try:
                    call_audio = self.getAudioMedia(i)
                except Exception as e:
                    logger.error("getAudioMedia(%d) failed: %s", i, e)
                    continue
                # Phase 2b-initial: play the TTS greeting, then hang up
                # when it finishes (we use a simple timer since pjsua2's
                # Python AudioMediaPlayer doesn't expose an end callback).
                if os.path.exists(GREETING_WAV):
                    try:
                        self._player = pj.AudioMediaPlayer()
                        self._player.createPlayer(GREETING_WAV, pj.PJMEDIA_FILE_NO_LOOP)
                        self._player.startTransmit(call_audio)
                        logger.info("Playing greeting to caller")
                        # Schedule hangup once the greeting has had time
                        # to finish. TTS for the full welcome line is
                        # ~10s on Piper lessac-medium plus a 2s pad.
                        import threading
                        def _hangup():
                            try:
                                time.sleep(12)
                                cop = pj.CallOpParam()
                                cop.statusCode = pj.PJSIP_SC_OK
                                self.hangup(cop)
                                logger.info("Hung up after greeting")
                            except Exception as e:
                                logger.warning("Hangup failed: %s", e)
                        threading.Thread(target=_hangup, daemon=True).start()
                    except Exception as e:
                        logger.error("Greeting playback failed: %s", e)
                else:
                    logger.info("No greeting file — hanging up immediately")
                    cop = pj.CallOpParam()
                    cop.statusCode = pj.PJSIP_SC_OK
                    self.hangup(cop)

    acc = BridgeAccount()
    acc.create(acfg)
    logger.info("Account created; awaiting registration...")

    # Main loop — pjsua2 runs callbacks on its own threads, so we just park here.
    running = True
    def _stop(signum, frame):
        nonlocal running
        logger.info("Received signal %d; shutting down", signum)
        running = False
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while running:
        time.sleep(1)

    logger.info("Destroying pjsua2 endpoint")
    try:
        ep.libDestroy()
    except Exception as e:
        logger.warning("libDestroy error: %s", e)


def main() -> None:
    trunks, numbers = fetch_config()
    if not trunks:
        logger.error("No trunks returned from admin API; exiting")
        sys.exit(1)
    run_bridge(trunks, numbers)


if __name__ == "__main__":
    main()
