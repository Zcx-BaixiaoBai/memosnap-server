# recorder/report.py —— 日报合成（M5；design §5.8 步骤 6 的 REDUCE 阶段）。
#
# 输入 = 当日全部会话的纪要（MAP 输出）+ 录音缺口 + 对话原文（不超限时）；
# 输出 = 每设备每天一份 Markdown 日报，存 daily 表：
#   今日概览 / 参与的会议与会话 / 今日工作内容（时间轴） / 工作重点与待办（跨会话去重）
#   / 需要注意的话（粗筛：批评/追责/投诉/承诺/考核类，带时间与说话人标签）
#   / 录音缺口。
#
# "需要注意的话"是 v1 粗筛（LLM 文本判断；MiniMax ASR 路径无音频情绪标签——
# 本地 SenseVoice 路径接入后叠加情绪信号，design §10 风险 7）。
# 日报是"每日定时任务"的落点（Omi daily sweep 模式：先跑 process→meetings→report）。

import json
import time

from .config import AppConfig
from .ingest import SegmentStore
from .providers import build_providers

_MAX_RAW_CHARS = 24000      # 当日对话原文超过此长度就只喂纪要（MAP→REDUCE 的边界）

_REPORT_SYSTEM = """你是个人工作日报助手。根据当日会话纪要与对话记录生成一份完整、信息密度高的日报。
只依据给定材料，不要编造；说话人用给定的身份或标签（如 m3-seg-a/S1），不要猜真实姓名；
材料可能有语音识别错别字，按语义理解整理；信息缺失写"未提及"。
输出固定 Markdown 结构：

# {日期} 工作日报
## 今日概览
（一段话总结当天：主要活动、会议数量与核心议题、整体基调）
## 参与的会议与会话
（每条：时间段、时长、参与人、是否会议候选、主题、核心结论一句话）
## 今日工作内容
（按时间轴列主要活动与讨论，合并同类项，注明关键细节与数字）
## 工作重点与待办
（跨会话去重，逐条：事项/责任人/截止时间/来源会话；按紧急程度排序；没有则写"无"）
## 需要注意的话
（从对话原文找出可能需要佩戴者留意的话语：批评、追责、投诉、对佩戴者的要求或承诺、考核相关。
每条注明时间与说话人并引用原话、简述原因；没有则写"无"）
## 风险与需协调
（资源缺口、冲突、需上级或其他部门协调的事项；没有则写"无"）
## 录音缺口
（列无录音时段；没有则写"无"）"""


def _fmt(ts_ms: int) -> str:
    return time.strftime("%H:%M", time.localtime(ts_ms / 1000))


def _material(store: SegmentStore, device_id: str, local_date: str) -> str:
    parts = ["【当日会话】"]
    raw_lines, raw_chars = [], 0
    for c in store.list_conversations(local_date, device_id):
        head = "### conv%d %s-%s（%ds，有效语音 %.0fs，%d 人%s）" % (
            c["conv_idx"], _fmt(c["start_ts"]), _fmt(c["end_ts"]),
            int((c["end_ts"] - c["start_ts"]) / 1000), c["speech_sec"],
            len(json.loads(c["speakers_json"])),
            "，会议候选" if c["is_meeting"] else "")
        parts.append(head)
        minutes = store.get_conversation_minutes(device_id, local_date, c["conv_idx"])
        parts.append(minutes if minutes else "（无纪要）")

    records = [json.loads(line) for line in
               (store.get_transcript(device_id, local_date) or "").splitlines() if line.strip()]
    gaps = [r for r in records if r.get("type") == "gap"]
    parts.append("【录音缺口】")
    if gaps:
        parts.extend("- %s → %s（%ds）" % (_fmt(g["start_ts"]), _fmt(g["end_ts"]), g["seconds"])
                     for g in gaps)
    else:
        parts.append("无")

    for r in records:
        if r.get("type") == "utterance":
            who = r.get("identity") or r["speaker"]
            line = "[%s] %s：%s" % (time.strftime("%H:%M:%S", time.localtime(r["start_ts"] / 1000)),
                                    who, r["text"])
            raw_lines.append(line)
            raw_chars += len(line)
    parts.append("【当日对话原文（供『需要注意的话』判断）】")
    if raw_chars <= _MAX_RAW_CHARS and raw_lines:
        parts.extend(raw_lines)
    else:
        parts.append("（原文共 %d 字符，超限未附，仅基于纪要判断）" % raw_chars)
    return "\n".join(parts)


def build(app: AppConfig, date=None, device=None) -> dict:
    store = SegmentStore(app.db_path)
    llm = build_providers(app)["report"]
    if not getattr(llm, "available", False):
        raise RuntimeError("report/summarize provider 不可用（selftest 看原因）")

    stats = {"days": 0}
    for r in store.list_transcripts(date, device):
        dev, day = r["device_id"], r["local_date"]
        user = _material(store, dev, day)
        md = llm.summarize({"system": _REPORT_SYSTEM.replace("{日期}", day), "user": user})
        store.upsert_daily(dev, day, md)
        stats["days"] += 1
        print("[report] %s %s 日报 %d 字" % (dev, day, len(md)))
    return stats


def show(app: AppConfig, date=None, device=None):
    store = SegmentStore(app.db_path)
    rows = store.list_daily(date, device)
    if not rows:
        print("[report] 无日报（先 process → meetings → report）")
        return
    for r in rows:
        md = store.get_daily(r["device_id"], r["local_date"])
        print("\n" + (md or ""))
