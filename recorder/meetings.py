# recorder/meetings.py —— 会话切分（M4 规则先行）+ 会话纪要（M5 前半）。
#
# 切分规则（design §5.8 步骤 4，日历对齐在飞书适配器落地后接入）：
#   - 段间隔 > gap（默认 5 分钟）切一个新会话；段间隔 ≤ gap 归同一会话；
#   - 会话满足（时长 ≥ min_sec 且说话人 ≥ min_speakers）标"会议候选"（is_meeting），
#     默认 300 秒 / 2 人，对应设计文档"会话 >5 分钟且 ≥2 说话人"；
#   - 说话人是段内命名空间标签（如 m3-seg-a/S1），跨段不去重——真实身份映射待 M4 声纹。
#
# 纪要（design §5.8 步骤 5 的最小版，MAP→REDUCE 的 MAP 阶段）：
#   - 每个会话拼"时间+说话人+内容"对话记录 → LLM 结构化输出 Markdown：
#     标题/时间与时长/参与人/按时间要点/关键决策/待办与责任人/风险点；
#   - 长会话的分章节 MAP→REDUCE、redact 脱敏在 M5 完整版做（当前 redact 未实现，
#     all-API 路径下逐字稿本就经云端，该开关先只对混合路径有意义）。

import json
import time

from .config import AppConfig
from .ingest import SegmentStore
from .providers import build_providers

GAP_MS = 5 * 60 * 1000
MEETING_MIN_SEC = 300
MEETING_MIN_SPEAKERS = 2
CHUNK_CHARS = 16000       # 对话文本超过此长度走 MAP→REDUCE 分章节（design §5.8 步骤 5）

_MINUTES_SYSTEM = """你是资深会议纪要秘书。根据给定的对话记录（含时间戳与说话人标签或已识别身份）写一份完整、信息密度高的会议纪要。
要求：
- 只依据记录内容，不要编造；记录可能有语音识别错别字，按语义理解整理，但不得补充原文没有的事实；
- 完整覆盖记录中出现的每一个议题，不得遗漏讨论点；重要观点注明说话人并保留关键原话；
- 信息缺失写"未提及"，不要留空或编造。
输出固定 Markdown 结构：

## {标题：一句话概括主题}
- 时间与时长：
- 参与人：{列给定的身份或说话人标签，不要猜真实姓名}
- 讨论议题：
  ### {议题1}
  - {各方观点与讨论过程，注明说话人；保留关键原话}
  ### {议题2}
  - ……（按记录顺序列全所有议题）
- 关键决策：{逐条；没有就写"无"}
- 待办与责任人：{逐条，注明责任人与截止时间；没有就写"无"}
- 风险与悬而未决：{没有就写"无"}"""

# MAP 阶段：长会话按句边界切块，每块先出原子要点（保留时间戳与说话人标签）
_CHAPTER_MAP_SYSTEM = """你是会议记录整理助手。把这段会议片段整理成原子要点：
每条一行，按时间顺序，保留时间戳与说话人标签，保留关键原话与数字/人名/截止时间；
只依据记录内容，不要编造、不要评论、不要合并不同说话人的观点。"""


def _fmt_time(ts_ms: int) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts_ms / 1000))


def conversations_from_records(records, gap_ms=GAP_MS,
                               min_sec=MEETING_MIN_SEC,
                               min_speakers=MEETING_MIN_SPEAKERS):
    """从当日逐字稿记录（dict 列表）切会话。返回会话列表。"""
    convs, cur, prev_end = [], None, None
    for r in records:
        if r.get("type") == "segment":
            if cur is not None and r["start_ts"] - prev_end > gap_ms:
                convs.append(cur)
                cur = None
            if cur is None:
                cur = {"start_ts": r["start_ts"], "end_ts": r["end_ts"],
                       "segments": [], "utterances": []}
            cur["segments"].append(r)
            cur["end_ts"] = max(cur["end_ts"], r["end_ts"])
            prev_end = r["end_ts"]
        elif r.get("type") == "utterance" and cur is not None:
            cur["utterances"].append(r)
    if cur:
        convs.append(cur)
    for c in convs:
        # 参与人优先用声纹归因出的身份（M4），未匹配的用段命名空间匿名标签
        c["speakers"] = sorted({u.get("identity") or u["speaker"] for u in c["utterances"]})
        c["speech_sec"] = round(sum(u["end_ts"] - u["start_ts"]
                                    for u in c["utterances"]) / 1000, 1)
        c["duration_sec"] = round((c["end_ts"] - c["start_ts"]) / 1000, 1)
        c["is_meeting"] = (c["duration_sec"] >= min_sec
                           and len(c["speakers"]) >= min_speakers)
    return convs


def _dialogue_text(conv) -> str:
    lines = []
    for u in conv["utterances"]:
        who = u.get("identity") or u["speaker"]      # 声纹归因优先，匿名标签兜底
        lines.append("[%s] %s：%s" % (_fmt_time(u["start_ts"]), who, u["text"]))
    return "\n".join(lines)


def _split_chunks(conv, chunk_chars: int):
    """按 utterance 边界切块（不切断句子）。返回 [(start_ts, end_ts, 文本), ...]。"""
    chunks = []
    cur_lines, cur_start, cur_end, cur_len = [], None, None, 0
    for u in conv["utterances"]:
        line = "[%s] %s：%s" % (_fmt_time(u["start_ts"]), u["speaker"], u["text"])
        if cur_lines and cur_len + len(line) > chunk_chars:
            chunks.append((cur_start, cur_end, "\n".join(cur_lines)))
            cur_lines, cur_start, cur_len = [], None, 0
        if not cur_lines:
            cur_start = u["start_ts"]
        cur_lines.append(line)
        cur_len += len(line)
        cur_end = u["end_ts"]
    if cur_lines:
        chunks.append((cur_start, cur_end, "\n".join(cur_lines)))
    return chunks


def _minutes_for_conversation(llm, conv, chunk_chars: int) -> str:
    """生成会话纪要。短会话一次出；长会话 MAP→REDUCE：分章节出原子要点再汇总。"""
    text = _dialogue_text(conv)
    if len(text) <= chunk_chars:
        return llm.summarize({"system": _MINUTES_SYSTEM, "user": text})
    # MAP：按句边界切块，每块出原子要点
    chapter_notes = []
    for i, (start, end, ch_text) in enumerate(_split_chunks(conv, chunk_chars)):
        note = llm.summarize({"system": _CHAPTER_MAP_SYSTEM, "user": ch_text})
        chapter_notes.append("### 章节 %d（%s-%s）\n%s" % (i + 1, _fmt_time(start), _fmt_time(end), note))
    # REDUCE：章节要点 → 完整结构化纪要
    reduce_user = ("【材料：该会话的章节原子要点（由分段整理生成，含时间与说话人标签）】\n\n"
                   + "\n\n".join(chapter_notes))
    return llm.summarize({"system": _MINUTES_SYSTEM, "user": reduce_user})


def build(app: AppConfig, date=None, device=None, gap_ms=GAP_MS,
          min_sec=MEETING_MIN_SEC, min_speakers=MEETING_MIN_SPEAKERS,
          skip_llm=False, chunk_chars=CHUNK_CHARS) -> dict:
    """对每个 (device, date) 重建会话并生成纪要。返回统计。"""
    store = SegmentStore(app.db_path)
    llm = build_providers(app)["summarize"]
    use_llm = (not skip_llm) and getattr(llm, "available", False)

    rows = store.list_transcripts(date, device)
    stats = {"days": 0, "conversations": 0, "meetings": 0, "minutes": 0}
    for r in rows:
        dev, day = r["device_id"], r["local_date"]
        records = [json.loads(line) for line in
                   (store.get_transcript(dev, day) or "").splitlines() if line.strip()]
        convs = conversations_from_records(records, gap_ms, min_sec, min_speakers)
        # 不 clear 重建：upsert 保留 title（用户重命名）与旧纪要（COALESCE），
        # 多余旧 idx 最后 prune。
        for idx, c in enumerate(convs):
            minutes = None
            if use_llm:
                try:
                    minutes = _minutes_for_conversation(llm, c, chunk_chars)
                    stats["minutes"] += 1
                except Exception as e:
                    print("[meetings] ✗ %s %s conv%d 纪要失败：%s" % (dev, day, idx, e))
            store.upsert_conversation(dev, day, idx, c["start_ts"], c["end_ts"],
                                      c["is_meeting"], json.dumps(c["speakers"]),
                                      c["speech_sec"], minutes)
        store.prune_conversations(dev, day, len(convs))
        stats["days"] += 1
        stats["conversations"] += len(convs)
        stats["meetings"] += sum(1 for c in convs if c["is_meeting"])
        print("[meetings] %s %s：%d 个会话（会议候选 %d）%s" %
              (dev, day, len(convs), stats["meetings"],
               "" if use_llm else "（LLM 不可用，仅切分）"))
    return stats


def show(app: AppConfig, date=None, device=None):
    """打印会话清单与纪要正文。"""
    store = SegmentStore(app.db_path)
    rows = store.list_conversations(date, device)
    if not rows:
        print("[meetings] 无会话（先跑 process 再跑 meetings）")
        return
    for r in rows:
        print("\n===== %s %s conv%d %s =====" %
              (r["device_id"], r["local_date"], r["conv_idx"],
               "【会议候选】" if r["is_meeting"] else "（普通会话）"))
        print("  %s → %s（%ds，有效语音 %.0fs，%d 人）" %
              (_fmt_time(r["start_ts"]), _fmt_time(r["end_ts"]),
               int((r["end_ts"] - r["start_ts"]) / 1000), r["speech_sec"],
               len(json.loads(r["speakers_json"]))))
        if r["has_minutes"]:
            md = store.get_conversation_minutes(r["device_id"], r["local_date"], r["conv_idx"])
            if md:
                print(md)
