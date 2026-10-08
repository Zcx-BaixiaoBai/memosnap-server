# recorder/providers/registry.py —— 从配置构造各槽位的 provider 实例。
#
# 规则：
# - voiceprint 无论配置如何都构造本地实现（stubs 占位，M4 落地）——API provider 在
#   config.VALID_PROVIDERS 层面就不存在，注册表面上也只有这一条路。
# - same-as-summarize / same-as-* 直接复用被引用槽位的实例。
# - diarization=asr-tags 的 egress 跟随 asr 槽位（复用其输出的匿名 speaker 标签）。

from ..config import AppConfig, CapabilityCfg
from ..redact import build_redactor
from .minimax_asr import MiniMaxASR
from .minimax_llm import MiniMaxLLM
from .stubs import AsrTagsDiarization, StubASR, StubCalendar, StubSummarize, StubVoiceprint


def _build_asr(cfg: CapabilityCfg):
    if cfg.provider == "minimax":
        return MiniMaxASR(cfg)
    if cfg.provider == "sherpa":
        from .sherpa_asr import SherpaASR
        return SherpaASR(cfg)
    return StubASR(name=cfg.provider, reason="按需扩展（已实现：minimax 云端 / sherpa 本地）")


def _build_summarize(cfg: CapabilityCfg):
    if cfg.provider == "minimax":
        # 出网前脱敏：redact=true（默认）时按 redact_names 映射在适配器边界替换人名
        return MiniMaxLLM(cfg, redactor=build_redactor(cfg.redact_names))
    return StubSummarize(name=cfg.provider, reason="Ollama 适配器在 M5 交付；openai-compatible 按需")


def build_providers(app: AppConfig, store=None) -> dict:
    """返回 {slot: provider实例}。所有槽位必有一个实例（哪怕是占位），selftest 才能出全量报告。
    store：SegmentStore（声纹登记库落在 index.db；传 None 时声纹退化为占位）。"""
    caps = app.capabilities
    out = {}

    out["asr"] = _build_asr(caps["asr"])

    diar = caps["diarization"]
    if diar.provider == "asr-tags":
        tags = AsrTagsDiarization()
        tags.egress = out["asr"].egress      # 跟随 ASR：音频出网，说话人标签也来自云
        out["diarization"] = tags
    else:
        out["diarization"] = StubASR(name=diar.provider, reason="local-sherpa 在 M3 交付")

    # voiceprint：只有本地实现一条路（硬约束）。store 就绪且模型可初始化时用真实现。
    vp = caps["voiceprint"]
    if store is not None and vp.provider == "local-only":
        from .sherpa_voiceprint import SherpaVoiceprint
        out["voiceprint"] = SherpaVoiceprint(vp, store)
    else:
        out["voiceprint"] = StubVoiceprint(name="local-only",
                                           reason="需 SegmentStore（worker/CLI 路径才有）")

    emo = caps["emotion"]
    out["emotion"] = StubSummarize(name=emo.provider, reason="llm-infer 在 M5 / asr-tags 在 M3 交付")

    sm = caps["summarize"]
    out["summarize"] = _build_summarize(sm)

    rep = caps["report"]
    if rep.provider.startswith("same-as-"):
        out["report"] = out["summarize"]
    else:
        # 配置页可能给 report 独立选云端 provider，而该槽 yaml 无 api 块
        # （原 same-as-summarize 继承态）：继承 summarize 的 api 配置（密钥/base_url），
        # 否则独立选择后 key 缺失 → "provider 不可用"。
        if not rep.api.get("api_key_env") and sm.api.get("api_key_env"):
            rep.api = {**sm.api, **rep.api}
        out["report"] = _build_summarize(rep)

    if app.calendar.provider == "feishu":
        out["calendar"] = StubCalendar(name="feishu", reason="飞书 OpenAPI 适配器在 M4 交付")
    return out
