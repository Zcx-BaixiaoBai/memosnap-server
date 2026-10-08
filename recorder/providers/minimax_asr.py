# recorder/providers/minimax_asr.py —— MiniMax asr-1.0 适配器（已按平台文档核实，2026-09-17）。
#
# 端点：POST {base_url}/speech_to_text   base_url= https://api.minimaxi.com/v1（region=cn 实测）
# 鉴权：Authorization: Bearer <MINIMAX_API_KEY>
# 请求：multipart  model=asr-1.0  response_format=verbose_json  file=<音频>
#       可选请求头 language: zh/yue/en/...（不传=混合语种识别）
# 返回：{"text", "duration", "n_speakers",
#        "segments": [{"id", "start", "end", "speaker": "S1", "text"}], "trace_id"}
# 约束：单次 ≤500 秒 / ≤50MB；**直接吃 opus**（不支持裸 PCM）；错误为 OpenAI 风格
#       {"type":"error","error":{...},"request_id":...}。
# 注意：verbose_json 自带匿名说话人+时间戳 —— ASR 与 diarization 合并为一个云调用，
#       diarization 槽位应配 asr-tags 复用本适配器的输出（design §5.1）。

import json

from .. import safe_upstream
from ..config import CapabilityCfg, api_key
from .base import ASRProvider, Transcript, Utterance

_CONTENT_TYPES = {
    "opus": "audio/ogg", "ogg": "audio/ogg", "wav": "audio/wav",
    "mp3": "audio/mpeg", "m4a": "audio/mp4", "aac": "audio/aac", "flac": "audio/flac",
}


class MiniMaxASR(ASRProvider):
    name = "minimax"
    egress = True            # 原始音频上传到 MiniMax —— selftest 报告里必须显式提示
    module = ""

    def __init__(self, cfg: CapabilityCfg):
        self.cfg = cfg
        self.base_url = (cfg.api.get("base_url") or "https://api.minimaxi.com/v1").rstrip("/")
        self.model = cfg.api.get("model") or "asr-1.0"
        self.response_format = cfg.api.get("response_format") or "verbose_json"
        self._key = api_key(cfg)

    @property
    def available(self) -> bool:
        return bool(self._key)

    def transcribe(self, audio: bytes, fmt: str, language: str = "") -> Transcript:
        if not self._key:
            raise RuntimeError("MINIMAX_API_KEY 未设置（config 里 api_key_env 指向的变量为空）")
        url = self.base_url + "/speech_to_text"
        body, ctype = safe_upstream.multipart_body(
            fields={"model": self.model, "response_format": self.response_format},
            files={"file": ("segment." + fmt, audio, _CONTENT_TYPES.get(fmt, "application/octet-stream"))},
        )
        headers = {"Authorization": "Bearer " + self._key, "Content-Type": ctype}
        if language:
            headers["language"] = language
        status, _, payload = safe_upstream.safe_http_request(
            "POST", url, body=body, headers=headers, timeout=300)
        if status != 200:
            raise safe_upstream.UpstreamError(
                "MiniMax ASR %d：%s" % (status, payload[:300].decode("utf-8", "replace")))
        data = json.loads(payload)
        segs = []
        for s in data.get("segments") or []:
            segs.append(Utterance(
                start=float(s.get("start", 0.0)), end=float(s.get("end", 0.0)),
                text=s.get("text", ""), speaker=s.get("speaker", "")))
        return Transcript(
            text=data.get("text", ""),
            duration=float(data.get("duration", 0.0)),
            n_speakers=int(data.get("n_speakers", 0)),
            utterances=segs,
        )
