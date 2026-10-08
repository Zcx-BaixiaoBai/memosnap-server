# recorder/redact.py —— 出网前脱敏（M5；design §5.6 "混合脱敏"边界的落地）。
#
# 机制：字典替换。config 的 summarize 段配
#   redact: true                    # 开关（默认开：设计文档默认"混合脱敏"）
#   redact_names: {张三: 工程部主管, 李四: 业主代表}
# LLM 适配器在把 system/user 文本发往云端前逐个替换（长名优先，避免子串误伤）。
# 位置收敛在适配器边界（MiniMaxLLM.summarize/chat），meetings/report 的调用
# 自动被覆盖；本地库里的逐字稿/纪要原文不受影响——"逐字稿永不外发"指的就是
# 原文只留在 core 主机，出网的是角色化文本。
#
# 局限（如实声明）：v1 只替换已登记的人名，未登记的名字识别不出来——
# 自动 NER 需要本地模型，属 local-asr 模块配套的后续工作。当前 all-API 路径下
# 逐字稿本就经云端（ASR 在云上），此开关主要保护两件事：
#   1) 混合路径（本地 ASR + 云端 LLM）下逐字稿不出网；
#   2) 已登记的核心人名不落在云端生成的纪要/日报里。


def build_redactor(names: dict):
    """返回 callable(text)->str。names 为 {人名: 角色标签}；空映射/None 返回恒等函数。"""
    if not names:
        return lambda text: text
    items = sorted(names.items(), key=lambda kv: -len(kv[0]))   # 长名优先
    def _redact(text: str) -> str:
        for name, label in items:
            if name and name in text:
                text = text.replace(name, label)
        return text
    return _redact
