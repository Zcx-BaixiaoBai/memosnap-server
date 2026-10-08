# recorder/providers/minimax_llm.py —— MiniMax LLM 适配器（2026-09-17 实测核实）。
#
# 端点：POST {base_url}/anthropic/v1/messages
#   base_url = https://api.minimaxi.com（region=cn，与 mmx CLI 同一条路）
#   注意：这是 MiniMax 的 **Anthropic 兼容 Messages 端点**（mmx text chat 实测走的正是它），
#   与 ASR 的 /v1/speech_to_text 不同级，base_url 不带 /v1（构造时会剥掉误配的尾缀）。
# 认证：Authorization: Bearer <MINIMAX_API_KEY>（x-api-key 亦接受，实测均 200）。
# 请求体（Anthropic Messages 形态）：{"model", "max_tokens", "system"?, "messages":[{role,content}]}
# 返回：content: [{"type":"text","text"}]，拼接全部 text 块；usage 里有 token 数。
# 模型：MiniMax-M3（套餐配额桶名叫 general，模型 ID 是 MiniMax-M3；api.model 可换）。
#
# summarize(structured) 的入参约定：{"system": str, "user": str}——纪要/日报的提示词
# 由 worker（M5）构造，适配器只做纯 chat 通道，不含业务提示词。

import json

from .. import safe_upstream
from ..config import CapabilityCfg, api_key
from .base import SummarizeProvider


class MiniMaxLLM(SummarizeProvider):
    name = "minimax"
    egress = True            # 文本出网到 MiniMax —— selftest 报告可见
    module = ""

    def __init__(self, cfg: CapabilityCfg, redactor=None):
        self.cfg = cfg
        base = (cfg.api.get("base_url") or "https://api.minimaxi.com").rstrip("/")
        if base.endswith("/v1"):           # 容错：误配成 ASR 那条 /v1 时剥掉
            base = base[:-3]
        self.base_url = base
        self.model = cfg.api.get("model") or "MiniMax-M3"
        self._key = api_key(cfg)
        # 出网前脱敏（design §5.6 / redact.py）：redact=true 时对 system/user
        # 文本做人名→角色标签替换；redactor=None 或恒等函数时为直通。
        self._redact = redactor if cfg.redact else (lambda text: text)

    @property
    def available(self) -> bool:
        return bool(self._key)

    def chat(self, system: str, user: str, max_tokens: int = 4096) -> str:
        if not self._key:
            raise RuntimeError("MINIMAX_API_KEY 未设置（config 里 api_key_env 指向的变量为空）")
        payload = {"model": self.model, "max_tokens": max_tokens,
                   "messages": [{"role": "user", "content": self._redact(user)}]}
        system = self._redact(system)
        if system:
            payload["system"] = system
        status, _, body = safe_upstream.safe_http_request(
            "POST", self.base_url + "/anthropic/v1/messages",
            body=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": "Bearer " + self._key,
                     "Content-Type": "application/json"},
            timeout=300)
        if status != 200:
            raise safe_upstream.UpstreamError(
                "MiniMax LLM %d：%s" % (status, body[:300].decode("utf-8", "replace")))
        data = json.loads(body)
        return "".join(b.get("text", "") for b in data.get("content") or []
                       if b.get("type") == "text")

    def summarize(self, structured: dict) -> str:
        return self.chat(structured.get("system", ""), structured.get("user", ""))
