"""Edge 官方 TTS（纯标准库）。

为什么自己写 WebSocket：本插件零第三方依赖，而 Python 标准库没有 WS 客户端。
这里只实现用到的那一小块：客户端握手 + 掩码文本帧 + 读二进制音频帧。

它一定会坏：这是逆向出来的接口，不是稳定公开 API。所以：
  - 任何异常都返回空 bytes，绝不让它把回复弄丢；
  - 上层拿到空就退回文字/原有 TTS；
  - 音色列表放在 VOICES，换接口时只动这一个文件。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import time
import urllib.parse
import uuid

HOST = "speech.platform.bing.com"
PATH = "/consumer/speech/synthesize/readaloud/edge/v1"
#: 公开在 Edge 客户端里的固定 token（社区通用，不是谁的私钥）
TRUSTED_TOKEN = "6A5AA1D4EAFF4E9FB37E23D68491D6F4"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
)
ORIGIN = "chrome-extension://jdiccldimpdaibmpdkjnbmckianbfold"
FORMAT = "audio-24khz-48kbitrate-mono-mp3"

#: 常用中文音色（名字就是微软那边的 voice name）
VOICES: dict[str, str] = {
    "xiaoyi": "zh-CN-XiaoyiNeural",      # 活泼女声
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",  # 温柔女声
    "yunxi": "zh-CN-YunxiNeural",        # 清朗男声
    "yunjian": "zh-CN-YunjianNeural",    # 沉稳男声
    "xiaobei": "zh-CN-liaoning-XiaobeiNeural",   # 东北口音女声
}
DEFAULT_VOICE = "zh-CN-XiaoyiNeural"

#: 2024 年后 Edge 要求带上 Sec-MS-GEC：把「5 分钟对齐的 Windows 时间戳」和
#: token 拼起来取 SHA256。不带这个，握手能过但服务端会直接不理你。
_WIN_EPOCH = 11644473600
_TICKS_PER_SEC = 10 ** 7
_GEC_VERSION = "1-130.0.2849.68"


def _gec() -> tuple[str, str]:
    ticks = int((time.time() + _WIN_EPOCH) // 300) * 300
    raw = str(ticks * _TICKS_PER_SEC) + TRUSTED_TOKEN
    return hashlib.sha256(raw.encode("utf-8")).hexdigest().upper(), _GEC_VERSION


def _frame(payload: bytes, opcode: int = 1) -> bytes:
    """客户端发出的帧必须掩码。"""
    mask = os.urandom(4)
    size = len(payload)
    head = bytes([0x80 | opcode])
    if size < 126:
        head += bytes([0x80 | size])
    elif size < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", size)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", size)
    body = bytes(one ^ mask[i % 4] for i, one in enumerate(payload))
    return head + mask + body


def _read_exact(sock: socket.socket, size: int) -> bytes:
    buf = b""
    while len(buf) < size:
        chunk = sock.recv(size - len(buf))
        if not chunk:
            raise ConnectionError("连接断了")
        buf += chunk
    return buf


def _read_frame(sock: socket.socket) -> tuple[int, bytes]:
    first, second = _read_exact(sock, 2)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    size = second & 0x7F
    if size == 126:
        size = struct.unpack(">H", _read_exact(sock, 2))[0]
    elif size == 127:
        size = struct.unpack(">Q", _read_exact(sock, 8))[0]
    mask = _read_exact(sock, 4) if masked else b""
    data = _read_exact(sock, size)
    if masked:
        data = bytes(one ^ mask[i % 4] for i, one in enumerate(data))
    return opcode, data


def _connect(timeout: float) -> socket.socket:
    gec, gec_version = _gec()
    query = urllib.parse.urlencode({
        "TrustedClientToken": TRUSTED_TOKEN,
        "ConnectionId": uuid.uuid4().hex,
        "Sec-MS-GEC": gec,
        "Sec-MS-GEC-Version": gec_version,
    })
    raw = socket.create_connection((HOST, 443), timeout=timeout)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=HOST)
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        "GET " + PATH + "?" + query + " HTTP/1.1" + chr(13) + chr(10)
        + "Host: " + HOST + chr(13) + chr(10)
        + "Upgrade: websocket" + chr(13) + chr(10)
        + "Connection: Upgrade" + chr(13) + chr(10)
        + "Sec-WebSocket-Key: " + key + chr(13) + chr(10)
        + "Sec-WebSocket-Version: 13" + chr(13) + chr(10)
        + "Origin: " + ORIGIN + chr(13) + chr(10)
        + "User-Agent: " + UA + chr(13) + chr(10)
        + "Pragma: no-cache" + chr(13) + chr(10)
        + "Cache-Control: no-cache" + chr(13) + chr(10) + chr(13) + chr(10)
    )
    sock.sendall(request.encode("utf-8"))
    head = b""
    while b"\r\n\r\n" not in head:
        head += sock.recv(4096)
    if b" 101 " not in head.split(b"\r\n")[0]:
        raise ConnectionError("握手失败：" + head.split(b"\r\n")[0].decode("latin-1"))
    return sock


def _escape(text: str) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace(chr(39), "&apos;").replace(chr(34), "&quot;"))


def synthesize(text: str, voice: str = DEFAULT_VOICE, rate: str = "+0%",
               pitch: str = "+0Hz", timeout: float = 15.0) -> bytes:
    """把一段话合成 MP3 字节。失败一律返回空 bytes。"""
    body = str(text or "").strip()
    if not body:
        return b""
    if voice in VOICES:
        voice = VOICES[voice]
    sock = None
    try:
        sock = _connect(timeout)
        sock.settimeout(timeout)
        stamp = time.strftime("%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)",
                              time.gmtime())
        config = json.dumps({
            "context": {"synthesis": {"audio": {
                "metadataoptions": {"sentenceBoundaryEnabled": "false",
                                    "wordBoundaryEnabled": "false"},
                "outputFormat": FORMAT,
            }}}
        }, separators=(",", ":"))
        sock.sendall(_frame(("X-Timestamp:" + stamp + chr(13) + chr(10)
                             + "Content-Type:application/json; charset=utf-8" + chr(13) + chr(10)
                             + "Path:speech.config" + chr(13) + chr(10) + chr(13) + chr(10)
                             + config).encode("utf-8")))
        ssml = (
            "<speak version='1.0' xmlns='http://www.w3.org/2001/10/synthesis' xml:lang='zh-CN'>"
            + "<voice name='" + voice + "'>"
            + "<prosody pitch='" + pitch + "' rate='" + rate + "' volume='+0%'>"
            + _escape(body) + "</prosody></voice></speak>"
        )
        sock.sendall(_frame(("X-RequestId:" + uuid.uuid4().hex + chr(13) + chr(10)
                             + "Content-Type:application/ssml+xml" + chr(13) + chr(10)
                             + "X-Timestamp:" + stamp + "Z" + chr(13) + chr(10)
                             + "Path:ssml" + chr(13) + chr(10) + chr(13) + chr(10)
                             + ssml).encode("utf-8")))
        audio = bytearray()
        deadline = time.time() + timeout
        while time.time() < deadline:
            opcode, data = _read_frame(sock)
            if opcode == 0x8:
                break
            if opcode == 0x2:
                # 二进制帧：前两个字节是头长，后面才是音频
                if len(data) > 2:
                    skip = struct.unpack(">H", data[:2])[0]
                    audio += data[2 + skip:]
            elif opcode == 0x1:
                if b"Path:turn.end" in data:
                    break
        return bytes(audio)
    except Exception:  # noqa: BLE001 - 逆向接口，任何失败都只是「这次没有语音」
        return b""
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:  # noqa: BLE001
                pass


__all__ = ["synthesize", "VOICES", "DEFAULT_VOICE"]

