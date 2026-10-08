# recorder/audio.py —— 服务端音频解码：任意段字节 → float32/16kHz/单声道 PCM。
#
# 用 soundfile（libsndfile 带 opus 支持）替代 ffmpeg：纯 pip 依赖、无外部二进制，
# Windows 裸机部署不用再装东西。FREC 容器先经 frec_to_ogg 转成标准 Ogg/Opus。
# 重采样：线性插值到 16k（CAM++ 声纹模型要求 16k；ASR 走云端不经此路径）。

import io

import numpy as np
import soundfile as sf

from . import frec

TARGET_RATE = 16000


def decode_to_pcm16k(body: bytes, fmt_hint: str = ""):
    """解码为 (pcm: np.float32 单声道 16k, 采样率 16000)。
    支持 Ogg/Opus、WAV、FLAC、mp3（libsndfile 能读的都行）与 FREC 容器。
    失败抛 ValueError。"""
    if body[:4] == b"FREC":
        rec = frec.parse_frec(body)
        body = frec.frec_to_ogg(rec)
    try:
        data, sr = sf.read(io.BytesIO(body), dtype="float32", always_2d=False)
    except Exception as e:
        raise ValueError("音频解码失败：%s" % e)
    if data.ndim > 1:
        data = data.mean(axis=1)
    data = np.asarray(data, dtype=np.float32)
    if sr != TARGET_RATE:
        n = int(len(data) * TARGET_RATE / sr)
        t = np.arange(n) * (sr / TARGET_RATE)
        data = np.interp(t, np.arange(len(data)), data).astype(np.float32)
    return data, TARGET_RATE
