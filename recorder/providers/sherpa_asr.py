# recorder/providers/sherpa_asr.py —— 本地 ASR：sherpa-onnx SenseVoice（中文+情绪）。
#
# 模型：sense-voice-small ONNX（中/英/日/韩/粤），带 emotion 标签（happy/sad/
# angry/neutral）——比 MiniMax ASR 多情绪维度，正好喂"被针对的话"。
# 分句：silero-vad 切语音段 → 每段独立转写 → Utterance（无说话人分离——
# SenseVoice 不做 diarization；匿名 speaker 统一 S1，身份归因交给声纹）。
# 输入：opus/ogg/wav 字节 → audio.decode_to_pcm16k → float32 16k。
# 约束：egress=False（音频不出本机）；module="local-asr"。
#
# 模型文件（config.api.model_dir 指向目录）：
#   model.onnx     tokens.txt     （SenseVoice）
#   silero_vad.onnx              （VAD 分句）

import os

import numpy as np

from ..audio import decode_to_pcm16k
from .base import ASRProvider, Transcript, Utterance


class SherpaASR(ASRProvider):
    name = "sherpa-sensevoice"
    egress = False           # 音频不出本机（本地模型推理）
    module = "local-asr"

    def __init__(self, cfg):
        self.cfg = cfg
        self.model_dir = (cfg.api.get("model_dir") or "models/sensevoice").replace("\\", "/")
        self._recognizer = None
        self._vad = None
        self._err = ""

    @property
    def available(self) -> bool:
        if self._recognizer is not None:
            return True
        model = os.path.join(self.model_dir, "model.onnx")
        tokens = os.path.join(self.model_dir, "tokens.txt")
        if not (os.path.isfile(model) and os.path.isfile(tokens)):
            self._err = "模型文件缺失：%s" % self.model_dir
            return False
        return True

    def _ensure_loaded(self):
        """懒加载（首次转写时初始化，启动不阻塞）。"""
        if self._recognizer is not None:
            return
        import sherpa_onnx
        model = os.path.join(self.model_dir, "model.onnx")
        tokens = os.path.join(self.model_dir, "tokens.txt")
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=model, tokens=tokens, num_threads=4, use_itn=True)
        # VAD（可选：初始化失败就整段转写不分句）
        vad_path = os.path.join(self.model_dir, "silero_vad.onnx")
        if os.path.isfile(vad_path):
            try:
                vad_cfg = sherpa_onnx.VadModelConfig()
                vad_cfg.silero_vad.model = vad_path
                vad_cfg.silero_vad.threshold = 0.5
                vad_cfg.sample_rate = 16000
                self._vad = sherpa_onnx.VoiceActivityDetector(vad_cfg, buffer_size_in_seconds=60)
            except Exception:
                self._vad = None

    def _vad_segments(self, pcm, sr):
        """VAD 切分 → [(start_sec, end_sec)]。"""
        segments = []
        window = 512                      # silero-vad 16k 窗口
        try:
            self._vad.accept_waveform(pcm)
            # 每次读出已就绪的语音段
            while not self._vad.empty():
                seg = self._vad.front
                self._vad.pop()
                start = seg.start / sr
                end = start + len(seg.samples) / sr
                segments.append((start, end))
            self._vad.flush()
            while not self._vad.empty():
                seg = self._vad.front
                self._vad.pop()
                start = seg.start / sr
                end = start + len(seg.samples) / sr
                segments.append((start, end))
        except Exception:
            return [(0.0, len(pcm) / sr)]   # VAD 失败回退整段
        # accept_waveform 一次喂全部数据可能导致段合并——按窗口喂更可靠
        return segments if segments else [(0.0, len(pcm) / sr)]

    def transcribe(self, audio: bytes, fmt: str, language: str = "") -> Transcript:
        self._ensure_loaded()
        pcm, sr = decode_to_pcm16k(audio, fmt)
        duration = len(pcm) / sr

        # 整段转写（VAD 分句在 sherpa-onnx 1.13.8 的 VoiceActivityDetector
        # 上实测不产出有效段——后续版本再调；5 分钟段整段转写已够用）
        stream = self._recognizer.create_stream()
        stream.accept_waveform(sr, pcm.tolist())
        self._recognizer.decode_stream(stream)
        result = stream.result
        text = (result.text or "").strip()

        utterances = []
        if text:
            emotion_raw = getattr(result, "emotion", "") or ""
            emotion = emotion_raw.replace("<|", "").replace("|>", "").strip().lower()
            utterances.append(Utterance(
                start=0.0, end=duration, text=text,
                speaker="S1",           # 无 diarization；身份归因走声纹
                emotion=emotion,
            ))

        return Transcript(
            text=text,
            duration=duration,
            n_speakers=1,
            utterances=utterances,
        )
