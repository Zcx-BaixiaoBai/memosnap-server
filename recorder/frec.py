# recorder/frec.py —— FREC v3 段容器解析 + Ogg/Opus 封装（M1/M3 设备↔服务端格式契约）。
#
# 设备端（firmware/ai-passport/main/frec_store.c）把 Opus 帧写成 FREC v3：
#   48B 头（magic/ver/hdr/rate/ch/frame_ms/payload_size/frame_count/bitrate/seq/
#   start_ts_ms/reserved，小端）+ 每帧 [u16 len][opus]。
# 服务端把段发给 MiniMax ASR 前需要标准 Ogg/Opus 容器（ASR 只收
# wav/mp3/aac/opus/ogg），此模块做 FREC→Ogg 的无依赖封装。
#
# 掉电兼容：payload_size==0（未终结/掉电恢复前）时按帧扫描到 EOF。
# parse_ogg/frec_from_ogg 供测试构造设备同款数据（Ogg 解包按页接续拼接）。

import struct

_HDR = struct.Struct("<4sHHIHHIIIIQQ")   # 48 字节


def parse_frec(data: bytes) -> dict:
    """解析 FREC v3。返回头字段与帧列表；payload_size==0 时扫到 EOF（掉电兼容）。"""
    if len(data) < _HDR.size:
        raise ValueError("段过短，不是 FREC")
    (magic, ver, hdr, rate, ch, frame_ms, payload,
     frames, bitrate, seq, start_ts, _res) = _HDR.unpack_from(data, 0)
    if magic != b"FREC":
        raise ValueError("非 FREC 段（magic=%r）" % magic)
    if ver != 3:
        raise ValueError("FREC v%d 不支持（仅 v3）" % ver)
    pos = hdr
    # 跳过头部后的擦除区填充（0xFF）——设备预留 64B 头区，FREC 头只有 48B，
    # 中间 16B 是擦除残留（v0.4.52 真机 hex dump 实锤：帧区实际从 64 开始）
    while pos < len(data) and data[pos] == 0xFF:
        pos += 1
    # 帧区实际长度 = hdr + Σ(2+len)，与头部 payload_size（纯帧数据）差了前缀字节，
    # 所以一律扫到 EOF；payload_size/frame_count 只作元数据与掉电检测。
    out = []
    while pos + 2 <= len(data):
        (ln,) = struct.unpack_from("<H", data, pos)
        pos += 2
        if ln == 0 or pos + ln > len(data):
            break                      # 尾部损坏/截断/空帧：截到这里
        out.append(data[pos:pos + ln])
        pos += ln
    # 帧长修正：固件 frec_store.c 曾硬编码 frame_ms=60 而实际帧长已改（v0.4.35+
    # 10ms）。用 payload/bitrate/frames 推算真实帧长，头字段只做初始值。
    if bitrate > 0 and frames > 0 and payload > 0:
        inferred_ms = int(payload * 8000 / bitrate / frames)
        if inferred_ms > 0 and abs(inferred_ms - frame_ms) > 1:
            frame_ms = inferred_ms
    return {"sample_rate": rate, "channels": ch, "frame_ms": frame_ms,
            "bitrate": bitrate, "seq": seq, "start_ts_ms": start_ts,
            "frames": out, "declared_frames": frames}


# ---- Ogg 封装（CRC 用 Ogg 专用的 0x04C11DB7 非反射变体） ----

def _crc_table():
    tbl = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if c & 0x80000000 else (c << 1) & 0xFFFFFFFF
        tbl.append(c)
    return tbl


_CRC = _crc_table()


def _ogg_crc(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = ((crc << 8) & 0xFFFFFFFF) ^ _CRC[((crc >> 24) ^ b) & 0xFF]
    return crc


def _page(serial: int, seq: int, granule: int, packets: list,
          first=False, last=False) -> bytes:
    lacing = b""
    body = b""
    for p in packets:
        n = len(p)
        lacing += b"\xff" * (n // 255) + bytes([n % 255])
        body += p
    if len(lacing) > 255:
        raise ValueError("单页段数超 255，应分批")
    htype = (0x02 if first else 0) | (0x04 if last else 0)
    header = (b"OggS" + bytes([0, htype]) + struct.pack("<QII", granule & 0xFFFFFFFFFFFFFFFF,
                                                        serial, seq)
              + b"\0\0\0\0" + bytes([len(lacing)]) + lacing)
    crc = _ogg_crc(header + body)
    header = header[:22] + struct.pack("<I", crc) + header[26:]
    return header + body


def frec_to_ogg(rec: dict) -> bytes:
    """FREC（parse_frec 的返回）→ Ogg/Opus 字节流。可直接喂 ASR / 浏览器 <audio>。"""
    rate = rec["sample_rate"] or 16000
    ch = rec["channels"] or 1
    frame_ms = rec["frame_ms"] or 60
    # Ogg Opus 的 granule position 规范固定 48kHz 时钟（与流内 rate 无关）；
    # 用 16k 步进会被 libsndfile 判 malformed（MiniMax 容忍，soundfile 不容忍）。
    per_frame = 48000 * frame_ms // 1000
    serial = (rec["seq"] or 1) & 0x7FFFFFFF or 1

    opus_head = (b"OpusHead" + bytes([1, ch]) + struct.pack("<H", 0)
                 + struct.pack("<I", rate) + struct.pack("<h", 0) + bytes([0]))
    vendor = b"badge-recorder"
    tags = b"OpusTags" + struct.pack("<I", len(vendor)) + vendor + struct.pack("<I", 0)

    out = [_page(serial, 0, 0, [opus_head], first=True),
           _page(serial, 1, 0, [tags])]
    seq, granule = 2, 0
    batch, batch_g = [], 0
    frames = rec["frames"]

    def flush(last):
        nonlocal seq, batch, batch_g
        if batch:
            out.append(_page(serial, seq, granule, batch, last=last))
            seq += 1
            batch, batch_g = [], 0

    for i, f in enumerate(frames):
        batch.append(f)
        granule += per_frame
        batch_g += len(f)
        if batch_g >= 4096 or len(batch) >= 200:   # 页大小/段数上限，任一触发即出页
            flush(last=(i == len(frames) - 1))
    flush(last=True)
    if not frames:                                  # 空段也封合法流
        flush(last=True)
    return b"".join(out)


# ---- Ogg 解析 / FREC 构造（测试用：把现成 ogg 转成设备同款 FREC） ----

def parse_ogg(ogg: bytes) -> dict:
    """最小 Ogg 解析：跨页接续拼包。返回 {packets, sample_rate, channels,
    total_samples}（total_samples 取末页 granule，用于推算帧时长）。"""
    packets, cur = [], b""
    pos, rate, ch, total = 0, 0, 0, 0
    while pos + 27 <= len(ogg) and ogg[pos:pos + 4] == b"OggS":
        granule = struct.unpack_from("<Q", ogg, pos + 6)[0]
        nsegs = ogg[pos + 26]
        lacing = ogg[pos + 27:pos + 27 + nsegs]
        body_len = sum(lacing)
        body = ogg[pos + 27 + nsegs: pos + 27 + nsegs + body_len]
        p = 0
        for l in lacing:
            cur += body[p:p + l]
            p += l
            if l < 255:                             # 值 <255 = 包结束
                packets.append(cur)
                cur = b""
        pos += 27 + nsegs + body_len
        total = granule
    if packets and packets[0][:8] == b"OpusHead":
        ch = packets[0][9]
        rate = struct.unpack_from("<I", packets[0], 12)[0]
    return {"packets": packets, "sample_rate": rate, "channels": ch,
            "total_samples": total}


def frec_from_frames(frames: list, sample_rate: int, channels: int, frame_ms: int,
                     seq: int, start_ts_ms: int, bitrate: int,
                     finalized: bool = True) -> bytes:
    """由 Opus 帧列表构造 FREC v3（测试用）。finalized=False 模拟掉电（头部统计为 0）。"""
    payload = sum(len(f) for f in frames)
    hdr = _HDR.pack(b"FREC", 3, 48, sample_rate, channels, frame_ms,
                    payload if finalized else 0,
                    len(frames) if finalized else 0,
                    bitrate, seq, start_ts_ms, 0)
    body = b"".join(struct.pack("<H", len(f)) + f for f in frames)
    return hdr + body
