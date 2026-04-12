"""Volcengine BigModel ASR — WebSocket binary protocol client.

Transcribes Telegram voice messages (OGG Opus) to text using
wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream
"""

import json
import os
import struct
import uuid

import websockets


ASR_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"

# Binary protocol constants
_FULL_CLIENT_REQUEST = 0x10   # msg_type=0001, flags=0000
_AUDIO_ONLY          = 0x20   # msg_type=0010, flags=0000
_AUDIO_ONLY_LAST     = 0x22   # msg_type=0010, flags=0010 (NEG_SEQUENCE)
_JSON_NO_COMPRESS    = 0x10   # serial=JSON(0001), compress=NONE(0000)
_NO_SERIAL           = 0x00   # serial=NONE, compress=NONE
_HEADER_BYTE0        = 0x11   # version=0001, header_size=0001 (4 bytes)

_FULL_SERVER_RESPONSE = 0b1001


def _build_config() -> bytes:
    """Build the JSON config payload for the initial handshake."""
    config = {
        "audio": {
            "format": "ogg",
            "codec": "opus",
            "rate": 16000,
            "bits": 16,
            "channel": 1,
        },
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": False,
            "result_type": "single",
        },
    }
    return json.dumps(config).encode("utf-8")


def _make_frame(msg_type_flags: int, serial_compress: int, payload: bytes) -> bytes:
    """Build a binary WebSocket frame: 4-byte header + 4-byte size + payload."""
    header = struct.pack(">BBBB", _HEADER_BYTE0, msg_type_flags, serial_compress, 0x00)
    size = struct.pack(">I", len(payload))
    return header + size + payload


def _parse_response(data: bytes) -> str | None:
    """Parse a server response frame, return transcribed text or None."""
    if len(data) < 4:
        return None
    msg_type = data[1] >> 4
    compression = data[2] & 0x0F
    header_size = data[0] & 0x0F
    payload = data[header_size * 4:]

    # Check for sequence number flag
    flags = data[1] & 0x0F
    if flags & 0x01:  # POS_SEQUENCE — skip 4-byte seq
        payload = payload[4:]

    if msg_type != _FULL_SERVER_RESPONSE:
        return None

    if len(payload) < 4:
        return None
    payload_size = int.from_bytes(payload[:4], "big")
    payload_msg = payload[4:4 + payload_size]

    if compression == 0x01:  # gzip
        import gzip
        payload_msg = gzip.decompress(payload_msg)

    try:
        result = json.loads(payload_msg.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None

    # result field can be a dict {"text": "..."} or a list [{"text": "..."}]
    r = result.get("result")
    if isinstance(r, dict):
        text = r.get("text", "").strip()
        return text if text else None
    if isinstance(r, list) and r and isinstance(r[0], dict):
        text = r[0].get("text", "").strip()
        return text if text else None
    return None


async def transcribe_voice(ogg_bytes: bytes) -> str | None:
    """Transcribe OGG Opus audio bytes to text via Volcengine BigModel ASR.

    Returns transcribed text on success, None on failure.
    """
    app_id = os.environ.get("VOLCENGINE_ASR_APP_ID", "")
    token = os.environ.get("VOLCENGINE_ASR_TOKEN", "")
    if not app_id or not token:
        print("[bot] ASR credentials not configured")
        return None

    headers = {
        "X-Api-App-Key": app_id,
        "X-Api-Access-Key": token,
        "X-Api-Resource-Id": "volc.bigasr.sauc.duration",
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }

    try:
        async with websockets.connect(
            ASR_URL,
            additional_headers=headers,
            close_timeout=5,
            open_timeout=10,
        ) as ws:
            # 1. Send config frame (FULL_CLIENT_REQUEST, JSON, no compression)
            config_bytes = _build_config()
            frame = _make_frame(_FULL_CLIENT_REQUEST, _JSON_NO_COMPRESS, config_bytes)
            await ws.send(frame)

            # 2. Read ACK
            await ws.recv()

            # 3. Send audio data (AUDIO_ONLY, no serialization, no compression)
            frame = _make_frame(_AUDIO_ONLY, _NO_SERIAL, ogg_bytes)
            await ws.send(frame)

            # 4. Send final empty frame (AUDIO_ONLY + NEG_SEQUENCE)
            frame = _make_frame(_AUDIO_ONLY_LAST, _NO_SERIAL, b"")
            await ws.send(frame)

            # 5. Read responses until final result
            text = None
            for _ in range(20):  # safety limit
                resp = await ws.recv()
                if len(resp) < 4:
                    continue
                flags = resp[1] & 0x0F
                parsed = _parse_response(resp)
                if parsed:
                    text = parsed
                # flags & 0x02 = last response (NEG_SEQUENCE)
                if flags & 0x02:
                    break

            if text:
                print(f"[bot] ASR result: {text[:50]}...")
                return text

            print(f"[bot] ASR: no text in response")
            return None

    except Exception as exc:
        print(f"[bot] ASR error: {type(exc).__name__}: {exc}")
        return None
