# recorder/ingest.py —— 设备侧 HTTP 契约（design §6）。
#
# 路由：
#   GET  /health                 {ok, server_time_ms} —— 探活 + 设备校时（不鉴权，无敏感数据）
#   POST /rec/segment            收一个加密段，幂等入库 + 时间轴索引
#   GET  /rec/state              {pending, backlog}    —— 设备侧可查询补传队列
#   GET  /rec/days               有数据的日子列表（UI 骨架用）
#   GET  /rec/day?date=YYYY-MM-DD 某天的段列表（UI 骨架用）
#
# 存储形态（M2，单用户试点）：段体直接存 SQLite BLOB（index.db）。
# - 去重+落盘是同一个事务，崩溃不会出现"有文件无记录"的孤儿段；
# - 服务端不构造任何来自请求输入的文件路径，从根上消除路径穿越面；
# - 规模化（多用户、数据量大）时在 SegmentStore 同一接口后换成
#   "服务端生成文件名的文件存储"（物理名=uuid，客户端 ID 只进库，不进路径）。
#
# 安全：
# - 除 /health 外一律校验 X-Gateway-Token（与工牌现有网关同一种鉴权）。
# - X-Device-Id / X-Segment-Id 只允许 [A-Za-z0-9_-]{1,64}，且只作为数据库逻辑键。
# - 段体上限 20MB（5 分钟 Opus 段约 600KB，留足余量；MiniMax ASR 上限 50MB，我们更紧）。
# - 幂等：同 (Device-Id, Segment-Id) 再传返回 dedup=true，不重写不重记。

import json
import os
import re
import sqlite3

# 声纹身份名白名单（中英文/数字/连字符/空格，1-32字）
_SAFE_NAME = re.compile(r"^[\w\u4e00-\u9fff\- ]{1,32}$")
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .config import AppConfig

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# 会议/会话重命名标题（中英文/数字/空格/常见标点，1-60 字）
_SAFE_TITLE = re.compile(r"^[\w\u4e00-\u9fff·\- ()（）]{1,60}$")
_MAX_SEGMENT_BYTES = 20 * 1024 * 1024


class SegmentStore:
    """段入库（BLOB）+ 时间轴索引。M2 只做 stored 状态；分析 worker（M3+）消费后改状态。"""

    def __init__(self, db_path: str):
        self._db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        with self._lock, self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS segments(
                device_id TEXT NOT NULL, segment_id TEXT NOT NULL,
                start_ts INTEGER, end_ts INTEGER, seq INTEGER, bitrate INTEGER,
                local_date TEXT, received_at INTEGER NOT NULL,
                body BLOB NOT NULL, status TEXT NOT NULL DEFAULT 'stored',
                PRIMARY KEY(device_id, segment_id))""")
            # M4：声纹登记库（local-only；嵌入向量存本机 DB，永不发第三方）
            db.execute("""CREATE TABLE IF NOT EXISTS voiceprints(
                identity TEXT PRIMARY KEY, dim INTEGER NOT NULL,
                embedding BLOB NOT NULL, created_at INTEGER NOT NULL)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_segments_date ON segments(local_date)")
            # M3 迁移：逐字稿缓存（存原始 ASR 结果，重跑 day 组装不再调 API）与逐字稿表
            try:
                db.execute("ALTER TABLE segments ADD COLUMN asr_json TEXT")
            except sqlite3.OperationalError:
                pass  # 列已存在
            db.execute("""CREATE TABLE IF NOT EXISTS transcripts(
                device_id TEXT NOT NULL, local_date TEXT NOT NULL,
                jsonl TEXT NOT NULL, updated_at INTEGER NOT NULL,
                PRIMARY KEY(device_id, local_date))""")
            # M4/M5：会话（含纪要）
            db.execute("""CREATE TABLE IF NOT EXISTS conversations(
                device_id TEXT NOT NULL, local_date TEXT NOT NULL, conv_idx INTEGER NOT NULL,
                start_ts INTEGER NOT NULL, end_ts INTEGER NOT NULL,
                is_meeting INTEGER NOT NULL DEFAULT 0, speakers_json TEXT NOT NULL DEFAULT '[]',
                speech_sec REAL NOT NULL DEFAULT 0, minutes_md TEXT,
                created_at INTEGER NOT NULL,
                PRIMARY KEY(device_id, local_date, conv_idx))""")
            # WebUI：会议/会话可重命名
            try:
                db.execute("ALTER TABLE conversations ADD COLUMN title TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass  # 列已存在
            # WebUI 配置页：能力槽位 provider 覆盖（优先级高于 yaml；各进程启动时读）
            db.execute("""CREATE TABLE IF NOT EXISTS provider_overrides(
                slot TEXT PRIMARY KEY, provider TEXT NOT NULL,
                updated_at INTEGER NOT NULL)""")
            # v3 配置控制台：整槽覆盖（provider+api+local+redact；api_key 内联存这里）
            db.execute("""CREATE TABLE IF NOT EXISTS slot_overrides(
                slot TEXT PRIMARY KEY, provider TEXT NOT NULL,
                api_json TEXT NOT NULL DEFAULT '{}', local_json TEXT NOT NULL DEFAULT '{}',
                redact INTEGER, updated_at INTEGER NOT NULL)""")
            # WebUI 说话人面板：手动归因纠正（优先级高于声纹归因）
            db.execute("""CREATE TABLE IF NOT EXISTS speaker_overrides(
                device_id TEXT NOT NULL, local_date TEXT NOT NULL, speaker TEXT NOT NULL,
                identity TEXT NOT NULL, updated_at INTEGER NOT NULL,
                PRIMARY KEY(device_id, local_date, speaker))""")
            # M5：日报
            db.execute("""CREATE TABLE IF NOT EXISTS daily(
                device_id TEXT NOT NULL, local_date TEXT NOT NULL,
                report_md TEXT NOT NULL, created_at INTEGER NOT NULL,
                PRIMARY KEY(device_id, local_date))""")
            # M6：全文检索（FTS5）。中文无空格，unicode61 分词器无法切词——
            # 入库时把文本转成空格分隔的二字 bigram（经典做法，无外部依赖，
            # 支持 ≥2 字查询与子串命中；查询侧做同样变换后按短语匹配）。
            db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS fts_utterances USING fts5(
                bigram, device_id UNINDEXED, local_date UNINDEXED,
                start_ts UNINDEXED, speaker UNINDEXED, text UNINDEXED)""")

    def _connect(self):
        db = sqlite3.connect(self._db_path)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _local_date(start_ms: int) -> str:
        return time.strftime("%Y-%m-%d", time.localtime((start_ms or time.time() * 1000) / 1000))

    def put(self, device_id, segment_id, start_ts, end_ts, seq, bitrate, body: bytes):
        """入库。返回 (dedup)。段体作为 BLOB 与索引同事务写入。"""
        if not (_SAFE_ID.match(device_id) and _SAFE_ID.match(segment_id)):
            raise ValueError("device_id/segment_id 含非法字符")
        date = self._local_date(start_ts)
        if not _SAFE_DATE.match(date):
            raise ValueError("local_date 非法")
        with self._lock, self._connect() as db:
            row = db.execute("SELECT 1 FROM segments WHERE device_id=? AND segment_id=?",
                             (device_id, segment_id)).fetchone()
            if row:
                return True
            db.execute("INSERT INTO segments(device_id,segment_id,start_ts,end_ts,seq,bitrate,"
                       "local_date,received_at,body,status) VALUES(?,?,?,?,?,?,?,?,?,'stored')",
                       (device_id, segment_id, start_ts, end_ts, seq, bitrate,
                        date, int(time.time() * 1000), sqlite3.Binary(body)))
        return False

    def get(self, device_id, segment_id):
        """取段体（分析 worker 用）。"""
        with self._lock, self._connect() as db:
            row = db.execute("SELECT body FROM segments WHERE device_id=? AND segment_id=?",
                             (device_id, segment_id)).fetchone()
        return bytes(row["body"]) if row else None

    def get_segment_ts(self, device_id, segment_id):
        """取段起始时间戳（声纹按说话人切音频用）。"""
        with self._lock, self._connect() as db:
            row = db.execute("SELECT start_ts FROM segments WHERE device_id=? AND segment_id=?",
                             (device_id, segment_id)).fetchone()
        return row["start_ts"] if row else None

    def count_stored(self):
        with self._lock, self._connect() as db:
            n = db.execute("SELECT COUNT(*) FROM segments WHERE status='stored'").fetchone()[0]
        return n

    def days(self):
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT local_date, COUNT(*) AS n FROM segments "
                              "GROUP BY local_date ORDER BY local_date").fetchall()
        return [dict(r) for r in rows]

    def day(self, date):
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT device_id, segment_id, start_ts, end_ts, seq, bitrate,"
                              "status, length(body) AS bytes FROM segments"
                              " WHERE local_date=? ORDER BY start_ts", (date,)).fetchall()
        return [dict(r) for r in rows]

    # ---- worker（M3）所需。全部为固定语句 + 占位符参数，不做任何 SQL 字符串拼接 ----

    def list_stored(self, date=None, device=None):
        """待转写段（status='stored'）。date/device 为 None 时不过滤（用可空参数表达）。"""
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT device_id, segment_id, start_ts, end_ts, seq, bitrate, local_date,"
                " length(body) AS bytes FROM segments"
                " WHERE status='stored' AND (? IS NULL OR local_date=?)"
                " AND (? IS NULL OR device_id=?) ORDER BY start_ts",
                (date, date, device, device)).fetchall()
        return [dict(r) for r in rows]

    def mark_transcribed(self, device_id, segment_id, asr_json: str):
        """缓存该段的 ASR 原始结果并置 status='transcribed'。"""
        with self._lock, self._connect() as db:
            db.execute("UPDATE segments SET asr_json=?, status='transcribed' "
                       "WHERE device_id=? AND segment_id=?", (asr_json, device_id, segment_id))

    def mark_invalid(self, device_id, segment_id):
        """历史坏段（空帧等）标记 invalid，不再每轮重试。"""
        with self._lock, self._connect() as db:
            db.execute("UPDATE segments SET status='invalid' "
                       "WHERE device_id=? AND segment_id=?", (device_id, segment_id))

    def day_segments(self, device_id, local_date):
        """某天某设备的全部已转写段（含 asr_json），按 start_ts 排序——组装逐字稿的输入。"""
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT segment_id, start_ts, end_ts, seq, asr_json FROM segments"
                              " WHERE device_id=? AND local_date=? AND status='transcribed'"
                              " ORDER BY start_ts", (device_id, local_date)).fetchall()
        return [dict(r) for r in rows]

    def upsert_transcript(self, device_id, local_date, jsonl: str):
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO transcripts(device_id,local_date,jsonl,updated_at)"
                       " VALUES(?,?,?,?) ON CONFLICT(device_id,local_date)"
                       " DO UPDATE SET jsonl=excluded.jsonl, updated_at=excluded.updated_at",
                       (device_id, local_date, jsonl, int(time.time() * 1000)))

    def list_transcripts(self, date=None, device=None):
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT device_id, local_date, updated_at, length(jsonl) AS bytes FROM transcripts"
                " WHERE (? IS NULL OR local_date=?) AND (? IS NULL OR device_id=?)"
                " ORDER BY local_date, device_id",
                (date, date, device, device)).fetchall()
        return [dict(r) for r in rows]

    def get_transcript(self, device_id, local_date):
        with self._lock, self._connect() as db:
            row = db.execute("SELECT jsonl FROM transcripts WHERE device_id=? AND local_date=?",
                             (device_id, local_date)).fetchone()
        return row["jsonl"] if row else None

    # ---- 会话/纪要（M4 切分 + M5 纪要）。固定语句 + 占位符参数 ----

    def upsert_conversation(self, device_id, local_date, conv_idx, start_ts, end_ts,
                            is_meeting, speakers_json, speech_sec, minutes_md):
        with self._lock, self._connect() as db:
            # minutes_md 用 COALESCE：LLM 不可用时传 None 不覆盖已有纪要；
            # title 不在 UPDATE 列表：重建会话保留用户重命名。
            db.execute("INSERT INTO conversations(device_id,local_date,conv_idx,start_ts,end_ts,"
                       "is_meeting,speakers_json,speech_sec,minutes_md,created_at)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?)"
                       " ON CONFLICT(device_id,local_date,conv_idx) DO UPDATE SET"
                       " start_ts=excluded.start_ts, end_ts=excluded.end_ts,"
                       " is_meeting=excluded.is_meeting, speakers_json=excluded.speakers_json,"
                       " speech_sec=excluded.speech_sec,"
                       " minutes_md=COALESCE(excluded.minutes_md, minutes_md)",
                       (device_id, local_date, conv_idx, start_ts, end_ts,
                        int(is_meeting), speakers_json, speech_sec, minutes_md,
                        int(time.time() * 1000)))

    def prune_conversations(self, device_id, local_date, keep_count: int):
        """重建当日会话后删除多余旧 idx（替代 clear：保留 title/minutes）。"""
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM conversations WHERE device_id=? AND local_date=?"
                       " AND conv_idx>=?", (device_id, local_date, keep_count))

    def clear_conversations(self, device_id, local_date):
        """重建当日会话前清空（对话级别的幂等）。"""
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM conversations WHERE device_id=? AND local_date=?",
                       (device_id, local_date))

    def list_conversations(self, date=None, device=None):
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT device_id, local_date, conv_idx, start_ts, end_ts, is_meeting,"
                " speakers_json, speech_sec, created_at, title,"
                " minutes_md IS NOT NULL AS has_minutes"
                " FROM conversations WHERE (? IS NULL OR local_date=?)"
                " AND (? IS NULL OR device_id=?) ORDER BY local_date, device_id, conv_idx",
                (date, date, device, device)).fetchall()
        return [dict(r) for r in rows]

    def set_conversation_title(self, device_id, local_date, conv_idx, title: str):
        """会议/会话重命名（WebUI）。固定语句+参数化。"""
        with self._lock, self._connect() as db:
            db.execute("UPDATE conversations SET title=? WHERE device_id=? AND local_date=?"
                       " AND conv_idx=?", (title, device_id, local_date, conv_idx))

    def get_provider_overrides(self) -> dict:
        """WebUI 配置页写入的槽位覆盖（表不存在=首跑，返回空）。"""
        with self._lock, self._connect() as db:
            try:
                rows = db.execute(
                    "SELECT slot, provider FROM provider_overrides").fetchall()
            except sqlite3.OperationalError:
                return {}
        return {r["slot"]: r["provider"] for r in rows}

    def set_provider_override(self, slot: str, provider: str):
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO provider_overrides(slot,provider,updated_at)"
                       " VALUES(?,?,?) ON CONFLICT(slot) DO UPDATE SET"
                       " provider=excluded.provider, updated_at=excluded.updated_at",
                       (slot, provider, int(time.time() * 1000)))

    def set_slot_override(self, slot: str, provider: str, api: dict,
                          local: dict, redact: bool):
        """v3 配置控制台整槽覆盖。api 里可含 api_key（WebUI 填入的密钥，本机 DB）。"""
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO slot_overrides"
                       "(slot,provider,api_json,local_json,redact,updated_at)"
                       " VALUES(?,?,?,?,?,?) ON CONFLICT(slot) DO UPDATE SET"
                       " provider=excluded.provider, api_json=excluded.api_json,"
                       " local_json=excluded.local_json, redact=excluded.redact,"
                       " updated_at=excluded.updated_at",
                       (slot, provider, json.dumps(api, ensure_ascii=False),
                        json.dumps(local, ensure_ascii=False), int(redact),
                        int(time.time() * 1000)))

    def get_conversation_minutes(self, device_id, local_date, conv_idx):
        with self._lock, self._connect() as db:
            row = db.execute("SELECT minutes_md FROM conversations"
                             " WHERE device_id=? AND local_date=? AND conv_idx=?",
                             (device_id, local_date, conv_idx)).fetchone()
        return row["minutes_md"] if row else None

    # ---- 声纹登记库（M4；local-only：嵌入只在 core 主机 DB） ----

    def upsert_voiceprint(self, identity: str, dim: int, embedding: bytes):
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO voiceprints(identity,dim,embedding,created_at)"
                       " VALUES(?,?,?,?) ON CONFLICT(identity) DO UPDATE SET"
                       " dim=excluded.dim, embedding=excluded.embedding,"
                       " created_at=excluded.created_at",
                       (identity, dim, embedding, int(time.time() * 1000)))

    def delete_voiceprint(self, identity: str):
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM voiceprints WHERE identity=?", (identity,))

    def list_voiceprints(self):
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT identity, dim, created_at FROM voiceprints"
                              " ORDER BY identity").fetchall()
        return [dict(r) for r in rows]

    def load_voiceprints(self):
        """返回 [(identity, np-ready bytes)] —— 供比对侧重建声纹库。"""
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT identity, embedding FROM voiceprints").fetchall()
        return [(r["identity"], r["embedding"]) for r in rows]

    # ---- 说话人手动纠正（优先级高于声纹归因；WebUI 说话人面板维护）----

    def set_speaker_override(self, device_id, local_date, speaker, identity: str):
        """identity 空串=清除该标签的手动纠正。speaker 为全标签或当日簇后缀。"""
        with self._lock, self._connect() as db:
            if identity:
                db.execute("INSERT INTO speaker_overrides"
                           "(device_id,local_date,speaker,identity,updated_at)"
                           " VALUES(?,?,?,?,?) ON CONFLICT(device_id,local_date,speaker)"
                           " DO UPDATE SET identity=excluded.identity,"
                           " updated_at=excluded.updated_at",
                           (device_id, local_date, speaker, identity,
                            int(time.time() * 1000)))
            else:
                db.execute("DELETE FROM speaker_overrides WHERE device_id=? AND"
                           " local_date=? AND speaker=?",
                           (device_id, local_date, speaker))

    def get_speaker_overrides(self, device_id, local_date) -> dict:
        with self._lock, self._connect() as db:
            try:
                rows = db.execute("SELECT speaker, identity FROM speaker_overrides"
                                  " WHERE device_id=? AND local_date=?",
                                  (device_id, local_date)).fetchall()
            except sqlite3.OperationalError:
                return {}
        return {r["speaker"]: r["identity"] for r in rows}

    def voiceprint_signature(self):
        """声纹库版本签名 (条数, 最新 created_at)——调度器据此检测入档/改名/删除。"""
        with self._lock, self._connect() as db:
            row = db.execute("SELECT COUNT(*), COALESCE(MAX(created_at), 0)"
                             " FROM voiceprints").fetchone()
        return (row[0], row[1])

    def list_transcribed(self, date=None):
        """已转写段（含 asr_json）——声纹重归因用。"""
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT device_id, segment_id, local_date, asr_json FROM segments"
                " WHERE status='transcribed' AND asr_json IS NOT NULL"
                " AND (? IS NULL OR local_date=?) ORDER BY start_ts",
                (date, date)).fetchall()
        return [dict(r) for r in rows]

    # ---- 录音时段可查记录（M6 合规；design §1"何时录了多久"的举证数据） ----
    # 依据 segments 表本身（不依赖转写是否完成）——没分析也能回答"那天录了多久"。

    def recording_timeline(self, device_id, local_date, max_gap_ms=6 * 60 * 1000):
        """按设备/日期输出录音时段与缺口。相邻段间隔 ≤ max_gap_ms（默认 6 分钟，
        匹配设备 5 分钟分段节奏）合并为同一录音时段；更大间隔记为缺口。"""
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT start_ts, end_ts FROM segments"
                              " WHERE device_id=? AND local_date=? ORDER BY start_ts",
                              (device_id, local_date)).fetchall()
        periods, gaps, cur = [], [], None
        for r in rows:
            if cur and r["start_ts"] - cur["end_ts"] > max_gap_ms:
                periods.append(cur)
                gaps.append({"start_ts": cur["end_ts"], "end_ts": r["start_ts"]})
                cur = None
            if cur is None:
                cur = {"start_ts": r["start_ts"], "end_ts": r["end_ts"], "segments": 1}
            else:
                cur["end_ts"] = max(cur["end_ts"], r["end_ts"])
                cur["segments"] += 1
        if cur:
            periods.append(cur)
        return {"device_id": device_id, "local_date": local_date,
                "periods": periods, "gaps": gaps,
                "segment_count": len(rows),
                "recorded_sec": round(sum(p["end_ts"] - p["start_ts"]
                                          for p in periods) / 1000, 1)}

    def day_devices(self, local_date):
        """某天有哪些设备有段。"""
        with self._lock, self._connect() as db:
            rows = db.execute("SELECT device_id, COUNT(*) AS n FROM segments"
                              " WHERE local_date=? GROUP BY device_id ORDER BY device_id",
                              (local_date,)).fetchall()
        return [dict(r) for r in rows]

    # ---- 日报（M5）。固定语句 + 占位符参数 ----

    def upsert_daily(self, device_id, local_date, report_md):
        with self._lock, self._connect() as db:
            db.execute("INSERT INTO daily(device_id,local_date,report_md,created_at)"
                       " VALUES(?,?,?,?) ON CONFLICT(device_id,local_date)"
                       " DO UPDATE SET report_md=excluded.report_md,"
                       " created_at=excluded.created_at",
                       (device_id, local_date, report_md, int(time.time() * 1000)))

    def list_daily(self, date=None, device=None):
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT device_id, local_date, created_at, length(report_md) AS bytes"
                " FROM daily WHERE (? IS NULL OR local_date=?) AND (? IS NULL OR device_id=?)"
                " ORDER BY local_date, device_id",
                (date, date, device, device)).fetchall()
        return [dict(r) for r in rows]

    def get_daily(self, device_id, local_date):
        with self._lock, self._connect() as db:
            row = db.execute("SELECT report_md FROM daily WHERE device_id=? AND local_date=?",
                             (device_id, local_date)).fetchone()
        return row["report_md"] if row else None

    # ---- 全文检索（M6）。固定语句 + 占位符参数 ----

    @staticmethod
    def _bigram(text: str) -> str:
        """中文二元分词：'外立面维修' → '外立 立面 面维 维修'；
        ASCII 字母数字连续段整词保留（'OK80' → 'OK80'）。文档与查询用同一变换，
        查询词整体包成一个 FTS5 短语即可实现子串命中。"""
        tokens, i, n = [], 0, len(text)
        while i < n:
            if text[i].isascii() and text[i].isalnum():
                j = i
                while j < n and text[j].isascii() and text[j].isalnum():
                    j += 1
                tokens.append(text[i:j])
                i = j
                continue
            if i + 1 < n and not (text[i + 1].isascii() and text[i + 1].isalnum()):
                tokens.append(text[i:i + 2])
            i += 1
        return " ".join(t for t in tokens if t)

    def rebuild_fts(self):
        """从全部逐字稿重建 FTS 索引（单用户数据量小，重建毫秒级，正确性优先）。"""
        with self._lock, self._connect() as db:
            db.execute("DELETE FROM fts_utterances")
            rows = db.execute("SELECT device_id, local_date, jsonl FROM transcripts").fetchall()
            for r in rows:
                for line in (r["jsonl"] or "").splitlines():
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if rec.get("type") != "utterance":
                        continue
                    db.execute("INSERT INTO fts_utterances(bigram,device_id,local_date,"
                               "start_ts,speaker,text) VALUES(?,?,?,?,?,?)",
                               (self._bigram(rec.get("text", "")), r["device_id"], r["local_date"],
                                rec.get("start_ts", 0), rec.get("speaker", ""),
                                rec.get("text", "")))
        return len(rows)

    def search_utterances(self, query: str, date=None, device=None, limit=50):
        """查询按空白拆词：每词做同样 bigram 变换后包成一个短语（子串命中），
        多词之间为隐式 AND。返回命中 utterance 行。"""
        parts = []
        for term in query.split():
            bg = self._bigram(term)
            if bg:
                parts.append('"%s"' % bg)
        if not parts:
            return []
        q = " ".join(parts)
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT device_id, local_date, start_ts, speaker, text FROM fts_utterances"
                " WHERE fts_utterances MATCH ? AND (? IS NULL OR local_date=?)"
                " AND (? IS NULL OR device_id=?) ORDER BY start_ts LIMIT ?",
                (q, date, date, device, device, limit)).fetchall()
        return [dict(r) for r in rows]


class IngestServer:
    def __init__(self, app: AppConfig):
        self.app = app
        self.store = SegmentStore(app.db_path)
        self._vp = None            # 声纹 provider（懒构建，enroll 时才加载模型）
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                print("[ingest] " + fmt % args, flush=True)

            def _json(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authed(self):
                tok = self.headers.get("X-Gateway-Token", "")
                if tok and tok == outer.app.token:
                    return True
                # Web UI 浏览器路径：HttpOnly cookie。首次访问 /rec/ui?token= 下发并 302，
                # 之后浏览器自动带 cookie；无参数 token 落在地址栏/历史里。
                for pair in self.headers.get("Cookie", "").split(";"):
                    k, _, v = pair.strip().partition("=")
                    if k == "gw_token" and v and v == outer.app.token:
                        return True
                return False

            def _html(self, code: int, payload: bytes):
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                # UI 永远拿最新 HTML（pjax/样式随页面下发，缓存旧页=旧交互）
                self.send_header("Cache-Control", "no-store, max-age=0")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                from urllib.parse import parse_qs, urlparse
                path = self.path.split("?")[0]
                qs = parse_qs(urlparse(self.path).query)
                if path == "/health":
                    self._json(200, {"ok": True, "server_time_ms": int(time.time() * 1000)})
                    return
                # 根路径 → 跳转 WebUI（浏览器直接打开域名时不再看到 JSON 401）
                if path == "/":
                    self.send_response(302)
                    self.send_header("Location", "/rec/ui")
                    self.end_headers()
                    return
                # 所有 /rec/ui* 路径：?token= 首次访问时种 cookie（后续浏览器自动带）
                if path.startswith("/rec/ui"):
                    tok = (qs.get("token") or [""])[0]
                    if tok and tok == outer.app.token:
                        self.send_response(302)
                        self.send_header("Set-Cookie", "gw_token=%s; HttpOnly; Path=/" % tok)
                        self.send_header("Location", path)   # 同路径去掉 token
                        self.end_headers()
                        return
                    if not self._authed():
                        self._html(401, ("<!doctype html><meta charset=utf-8>"
                                         "<meta name=viewport content='width=device-width,initial-scale=1'>"
                                         "<body style='background:#0b0b0c;color:#e8eaea;"
                                         "font-family:system-ui;padding:40px;text-align:center'>"
                                         "<h2 style='color:#e96a2e'>秒忆卡 MemoSnap</h2>"
                                         "<p>需要登录才能查看</p>"
                                         "<form action='" + path + "' method=get style='max-width:320px;margin:20px auto'>"
                                         "<input type=password name=token placeholder='访问令牌' "
                                         "style='width:100%;padding:10px;border:1px solid #2a2c2e;"
                                         "border-radius:8px;background:#17181a;color:#e8eaea;font-size:16px'>"
                                         "<button style='width:100%;margin-top:12px;padding:12px;border:0;"
                                         "border-radius:8px;background:#e96a2e;color:#fff;"
                                         "font-size:16px;cursor:pointer'>进入</button></form>"
                                         "<p style='color:#6e7c8c;font-size:13px'>"
                                         "令牌在 recorder-server 的 config.yaml 里</p></body>").encode("utf-8"))
                        return
                    from . import webui
                    webui.set_nav_ctx(self.headers.get("Referer") or "")
                    if path == "/rec/ui":
                        self._html(200, webui.days_page(outer.store))
                    elif path == "/rec/ui/voiceprints":
                        self._html(200, webui.voiceprints_page(
                            outer.store,
                            msg=(qs.get("msg") or [""])[0],
                            msg_err=(qs.get("err") or [""])[0]))
                    elif path == "/rec/ui/day":
                        self._html(200, webui.day_page(
                            outer.store, (qs.get("date") or [""])[0],
                            msg=(qs.get("msg") or [""])[0],
                            msg_err=(qs.get("err") or [""])[0]))
                    elif path == "/rec/ui/settings":
                        self._html(200, webui.settings_page(
                            outer.app,
                            msg=(qs.get("msg") or [""])[0],
                            msg_err=(qs.get("err") or [""])[0]))
                    elif path == "/rec/ui/search":
                        self._html(200, webui.search_page(
                            outer.store, (qs.get("q") or [""])[0],
                            (qs.get("device") or [""])[0], ""))
                    elif path == "/rec/audio":
                        self._serve_audio(qs)
                    else:
                        self._json(404, {"error": "not found"})
                    return
                if not self._authed():
                    self._json(401, {"error": "invalid or missing X-Gateway-Token"})
                    return
                if path == "/rec/state":
                    self._json(200, {"pending": outer.store.count_stored(), "backlog": 0})
                elif path == "/rec/days":
                    self._json(200, {"days": outer.store.days()})
                elif path == "/rec/day":
                    date = (qs.get("date") or [""])[0]
                    if not _SAFE_DATE.match(date):
                        self._json(400, {"error": "date=YYYY-MM-DD"})
                        return
                    self._json(200, {"date": date, "segments": outer.store.day(date)})
                else:
                    self._json(404, {"error": "not found"})

            def _serve_audio(self, qs):
                device = (qs.get("device") or [""])[0]
                seg = (qs.get("segment") or [""])[0]
                if not (_SAFE_ID.match(device) and _SAFE_ID.match(seg)):
                    self._json(400, {"error": "device/segment 仅允许 [A-Za-z0-9_-]"})
                    return
                body = outer.store.get(device, seg)
                if body is None:
                    self._json(404, {"error": "segment not found"})
                    return
                ctype = "audio/ogg" if body[:4] == b"OggS" else (
                    "audio/wav" if body[:4] == b"RIFF" else "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                path = self.path.split("?")[0]
                # ---- 声纹管理（WebUI 表单）----
                if path in ("/rec/ui/voiceprints/enroll",
                            "/rec/ui/voiceprints/auto-enroll",
                            "/rec/ui/voiceprints/rename",
                            "/rec/ui/voiceprints/delete"):
                    self._vp_action(path)
                    return
                if path in ("/rec/ui/conversations/rename", "/rec/ui/settings",
                            "/rec/ui/speakers/override"):
                    self._ui_action(path)
                    return
                if path != "/rec/segment":
                    self._json(404, {"error": "not found"})
                    return
                if not self._authed():
                    self._json(401, {"error": "invalid or missing X-Gateway-Token"})
                    return
                device = self.headers.get("X-Device-Id", "")
                seg = self.headers.get("X-Segment-Id", "")
                if not (_SAFE_ID.match(device) and _SAFE_ID.match(seg)):
                    self._json(400, {"error": "X-Device-Id/X-Segment-Id 仅允许 [A-Za-z0-9_-]"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if not 0 < length <= _MAX_SEGMENT_BYTES:
                    self._json(413, {"error": "段体大小非法（上限 %d B）" % _MAX_SEGMENT_BYTES})
                    return

                def _int(name):
                    try:
                        return int(self.headers.get(name, "0") or 0)
                    except ValueError:
                        return 0

                body = self.rfile.read(length)
                if len(body) != length:
                    # 断流短读（真机实测踩过）：连接中断时 read 返回部分/0 字节，
                    # 若照常入库会存下 0 字节行并 200；工牌重试时被 dedup 拦住
                    # （同 (device, segment) 已存在），真数据永久丢失。
                    # 短读一律拒收，让设备侧重传完整段。
                    self._json(400, {"error": "short body: %d/%d" % (len(body), length)})
                    return
                try:
                    dedup = outer.store.put(
                        device, seg, _int("X-Start-Ts"), _int("X-End-Ts"),
                        _int("X-Seq"), _int("X-Bitrate"), body)
                except ValueError as e:
                    self._json(400, {"error": str(e)})
                    return
                self._json(200, {"ok": True, "dedup": dedup,
                                 "server_time_ms": int(time.time() * 1000)})

            def _vp_action(self, path: str):
                """声纹管理 POST 处理（enroll/rename/delete → 302 回管理页）。"""
                if not self._authed():
                    self._html(401, "401：未授权".encode("utf-8"))
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = 0
                if not 0 < length <= 20 * 1024 * 1024:
                    self._redirect_vp(err="请求体大小非法")
                    return
                body = self.rfile.read(length)
                ctype = self.headers.get("Content-Type", "")

                if path == "/rec/ui/voiceprints/enroll":
                    # multipart：identity（文本）+ audio（文件）
                    fields = IngestServer._parse_multipart(body, ctype)
                    identity = fields.get("identity", "")
                    audio = fields.get("audio")
                    if not isinstance(identity, str) or not _SAFE_NAME.match(identity):
                        self._redirect_vp(err="身份名非法（1-32字符，中英文/数字）")
                        return
                    if not isinstance(audio, tuple) or len(audio[1]) < 100:
                        self._redirect_vp(err="音频文件缺失或过小（至少3秒）")
                        return
                    audio_bytes = audio[1]
                    if len(audio_bytes) > 20 * 1024 * 1024:
                        self._redirect_vp(err="音频文件过大（上限20MB）")
                        return
                    try:
                        vp = outer._get_vp()
                        ok = vp.enroll(identity, audio_bytes)
                        if ok:
                            self._redirect_vp(msg="声纹已入档：%s" % identity)
                        else:
                            self._redirect_vp(err="声纹提取失败（音频太短或无人声）")
                    except Exception as e:
                        self._redirect_vp(err="入档失败：%s" % str(e)[:100])

                elif path == "/rec/ui/voiceprints/auto-enroll":
                    # 按说话人自动入档：从当天录音中提取该说话人的音频 → 声纹入档
                    from urllib.parse import parse_qs
                    params = parse_qs(body.decode("utf-8", "replace"))
                    identity = (params.get("identity") or [""])[0]
                    speaker = (params.get("speaker") or [""])[0]
                    device = (params.get("device") or [""])[0]
                    date = (params.get("date") or [""])[0]
                    if not (_SAFE_NAME.match(identity) and _SAFE_ID.match(device)
                            and _SAFE_ID.match(speaker) and _SAFE_DATE.match(date)):
                        self._redirect_vp(err="参数非法")
                        return
                    try:
                        audio = IngestServer._extract_speaker_audio(
                            outer.store, device, date, speaker)
                        if audio is None or len(audio) < 1000:
                            self._redirect_vp(
                                err="该说话人无有效音频（至少需要1秒语音）")
                            return
                        vp = outer._get_vp()
                        ok = vp.enroll(identity, audio)
                        if ok:
                            self._redirect_vp(
                                msg="已从 %s 的录音提取 %s 的声纹并入档" % (speaker, identity))
                        else:
                            self._redirect_vp(err="声纹提取失败")
                    except Exception as e:
                        self._redirect_vp(err="自动入档失败：%s" % str(e)[:100])

                elif path == "/rec/ui/voiceprints/rename":
                    from urllib.parse import parse_qs
                    params = parse_qs(body.decode("utf-8", "replace"))
                    old = (params.get("old") or [""])[0]
                    new = (params.get("new") or [""])[0]
                    if not (_SAFE_NAME.match(old) and _SAFE_NAME.match(new)):
                        self._redirect_vp(err="身份名非法")
                        return
                    if old == new:
                        self._redirect_vp(err="新旧名字相同")
                        return
                    try:
                        rows = {r["identity"] for r in outer.store.list_voiceprints()}
                        if old not in rows:
                            self._redirect_vp(err="身份不存在：%s" % old)
                            return
                        # 复制嵌入到新名 → 删旧名（参数化 SQL，无拼接）
                        db = sqlite3.connect(outer.store._db_path)
                        row = db.execute(
                            "SELECT dim, embedding FROM voiceprints WHERE identity=?",
                            (old,)).fetchone()
                        if row:
                            db.execute(
                                "INSERT OR REPLACE INTO voiceprints"
                                "(identity, dim, embedding, created_at) VALUES(?,?,?,?)",
                                (new, row[0], row[1], int(time.time())))
                            db.execute(
                                "DELETE FROM voiceprints WHERE identity=?", (old,))
                            db.commit()
                        db.close()
                        self._redirect_vp(msg="已改名：%s → %s" % (old, new))
                    except Exception as e:
                        self._redirect_vp(err="改名失败：%s" % str(e)[:100])

                elif path == "/rec/ui/voiceprints/delete":
                    from urllib.parse import parse_qs
                    params = parse_qs(body.decode("utf-8", "replace"))
                    identity = (params.get("identity") or [""])[0]
                    if not _SAFE_NAME.match(identity):
                        self._redirect_vp(err="身份名非法")
                        return
                    try:
                        outer.store.delete_voiceprint(identity)
                        self._redirect_vp(msg="已删除：%s" % identity)
                    except Exception as e:
                        self._redirect_vp(err="删除失败：%s" % str(e)[:100])

            def _redirect_vp(self, msg: str = "", err: str = ""):
                """302 回声纹管理页（带 msg/err 参数）。"""
                from urllib.parse import urlencode
                params = {}
                if msg:
                    params["msg"] = msg
                if err:
                    params["err"] = err
                qs = urlencode(params) if params else ""
                self.send_response(302)
                self.send_header("Location",
                                 "/rec/ui/voiceprints" + ("?" + qs if qs else ""))
                self.end_headers()

            def _redirect_ui(self, url: str, msg: str = "", err: str = ""):
                """302 回指定 UI 页（带 msg/err 参数；url 已带 query 时用 & 接）。"""
                from urllib.parse import urlencode
                params = {}
                if msg:
                    params["msg"] = msg
                if err:
                    params["err"] = err
                qs = urlencode(params) if params else ""
                sep = "&" if "?" in url else "?"
                self.send_response(302)
                self.send_header("Location", url + (sep + qs if qs else ""))
                self.end_headers()

            def _ui_action(self, path: str):
                """WebUI 表单：会议重命名 / 模型来源配置保存（值一律过白名单）。"""
                from urllib.parse import parse_qs
                if not self._authed():
                    self._json(401, {"error": "invalid or missing X-Gateway-Token"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                params = parse_qs(body.decode("utf-8", "replace"))
                if path == "/rec/ui/conversations/rename":
                    dev = (params.get("device") or [""])[0]
                    date = (params.get("date") or [""])[0]
                    conv = (params.get("conv") or [""])[0]
                    title = (params.get("title") or [""])[0].strip()
                    back = "/rec/ui/day?date=%s" % date if _SAFE_DATE.match(date) else "/rec/ui"
                    if not (_SAFE_ID.match(dev) and _SAFE_DATE.match(date)
                            and conv.isdigit()):
                        self._redirect_ui("/rec/ui", err="参数非法")
                        return
                    if not _SAFE_TITLE.match(title):
                        self._redirect_ui(back, err="名称非法（1-60 字）")
                        return
                    try:
                        outer.store.set_conversation_title(dev, date, int(conv), title)
                        self._redirect_ui(back, msg="已重命名")
                    except Exception as e:
                        self._redirect_ui(back, err="重命名失败：%s" % str(e)[:80])
                elif path == "/rec/ui/speakers/override":
                    dev = (params.get("device") or [""])[0]
                    date = (params.get("date") or [""])[0]
                    speaker = (params.get("speaker") or [""])[0]
                    identity = (params.get("identity") or [""])[0].strip()
                    back = "/rec/ui/day?date=%s" % date if _SAFE_DATE.match(date) else "/rec/ui"
                    if not (_SAFE_ID.match(dev) and _SAFE_DATE.match(date)
                            and re.match(r"^[A-Za-z0-9_/-]{1,80}$", speaker)):
                        self._redirect_ui(back, err="参数非法")
                        return
                    if identity and not _SAFE_NAME.match(identity):
                        self._redirect_ui(back, err="身份名非法（1-32 字）")
                        return
                    outer.store.set_speaker_override(dev, date, speaker, identity)
                    # 逐字稿同步重建（快）；纪要/日报后台重建（LLM 慢）
                    from . import meetings, report, worker
                    try:
                        jsonl = worker._assemble_day(
                            outer.store, dev, date, worker.GAP_THRESHOLD_MS)
                        outer.store.upsert_transcript(dev, date, jsonl)
                    except Exception as e:
                        print("[ui] 纠正后逐字稿重建失败：%s" % e, flush=True)

                    def _rebuild(a=outer.app, d=date):
                        try:
                            meetings.build(a, date=d)
                            report.build(a, date=d)
                        except Exception as e:
                            print("[ui] 纠正后纪要/日报重建失败：%s" % e, flush=True)
                    threading.Thread(target=_rebuild, daemon=True).start()
                    self._redirect_ui(back, msg="归因已更新（%s%s），纪要/日报后台刷新中"
                                      % (speaker, ("→" + identity) if identity else " 已清除手动"))
                    return
                elif path == "/rec/ui/settings":
                    from .config import VALID_PROVIDERS
                    changed = []
                    for slot in ("asr", "diarization", "voiceprint", "emotion",
                                 "summarize", "report"):
                        cap = outer.app.capabilities[slot]
                        dirty = False
                        val = (params.get(slot) or [""])[0]
                        if val:
                            if val not in VALID_PROVIDERS.get(slot, set()):
                                self._redirect_ui("/rec/ui/settings",
                                                  err="%s 取值非法：%s" % (slot, val))
                                return
                            if cap.provider != val:
                                cap.provider = val
                                dirty = True
                        api_up, loc_up = {}, {}
                        b = (params.get(slot + "__base_url") or [""])[0].strip()
                        if b:
                            if not b.startswith(("http://", "https://")):
                                self._redirect_ui("/rec/ui/settings",
                                                  err="%s base_url 仅允许 http(s)" % slot)
                                return
                            api_up["base_url"] = b.rstrip("/")
                        m = (params.get(slot + "__model") or [""])[0].strip()
                        if m and len(m) <= 128:
                            api_up["model"] = m
                        md = (params.get(slot + "__model_dir") or [""])[0].strip()
                        if md:
                            if os.pardir in md.replace("\\", "/").split("/"):
                                self._redirect_ui("/rec/ui/settings",
                                                  err="%s 模型目录非法" % slot)
                                return
                            api_up["model_dir"] = md
                        key = (params.get(slot + "__key") or [""])[0].strip()
                        if key and len(key) <= 512 and key.isprintable():
                            api_up["api_key"] = key      # 本机 DB；env 变量优先
                        if (params.get(slot + "__keyclear") or [""])[0] == "1":
                            cap.api.pop("api_key", None)   # 清除库内密钥（回退 env）
                            dirty = True
                        if slot == "voiceprint":
                            for fld, nm in (("threshold", "voiceprint__threshold"),
                                            ("min_margin", "voiceprint__min_margin")):
                                v = (params.get(nm) or [""])[0].strip()
                                if v:
                                    try:
                                        fv = float(v)
                                    except ValueError:
                                        fv = -1.0
                                    if not 0.0 <= fv <= 1.0:
                                        self._redirect_ui("/rec/ui/settings",
                                                          err="%s 需 0-1 之间" % nm)
                                        return
                                    loc_up[fld] = fv
                            vm = (params.get("voiceprint__model") or [""])[0].strip()
                            if vm:
                                if os.pardir in vm.replace("\\", "/").split("/"):
                                    self._redirect_ui("/rec/ui/settings",
                                                      err="声纹模型路径非法")
                                    return
                                loc_up["model"] = vm
                        if slot in ("summarize", "report"):
                            cap.redact = bool(
                                (params.get(slot + "__redact") or [""])[0] == "1")
                            dirty = True
                        if api_up:
                            cap.api.update(api_up)
                            dirty = True
                        if loc_up:
                            cap.local.update(loc_up)
                            dirty = True
                        if dirty:
                            outer.store.set_slot_override(
                                slot, cap.provider, cap.api, cap.local, cap.redact)
                            changed.append(slot)
                    if not changed:
                        self._redirect_ui("/rec/ui/settings", msg="无变更")
                        return
                    self._redirect_ui(
                        "/rec/ui/settings",
                        msg="已保存：%s。本服务立即生效；CLI/分析进程下次运行自动读取"
                            % "、".join(changed))

        self._httpd = ThreadingHTTPServer((app.listen_host, app.listen_port), Handler)

    def _get_vp(self):
        """懒构建声纹 provider（SherpaVoiceprint：模型 ~30MB，首次 enroll 时加载）。"""
        if self._vp is None:
            from .providers.sherpa_voiceprint import SherpaVoiceprint
            self._vp = SherpaVoiceprint(self.app.capabilities["voiceprint"], self.store)
        return self._vp

    @staticmethod
    def _extract_speaker_audio(store, device: str, date: str, speaker: str):
        """从当天录音中提取指定说话人的音频（按逐字稿时间戳切割，拼接送声纹）。
        返回 WAV bytes，不足 1 秒返回 None。"""
        import io
        import json as _json

        import numpy as np
        import soundfile as sf

        from .audio import decode_to_pcm16k

        transcript = store.get_transcript(device, date)
        if not transcript:
            return None
        # 解析逐字稿：追踪当前段 → 收集该说话人的 utterance
        #（本地 ASR 的标签带段前缀如 "m3-seg-a/S1"——按后缀匹配）
        cur_seg = None
        utts = []       # [(segment_id, start_ts, end_ts)]
        for line in transcript.splitlines():
            try:
                rec = _json.loads(line)
            except ValueError:
                continue
            if rec.get("type") == "segment":
                cur_seg = rec.get("segment_id", "")
            elif rec.get("type") == "utterance" and cur_seg:
                sp = rec.get("speaker", "")
                if sp == speaker or sp.endswith("/" + speaker):
                    utts.append((cur_seg, rec.get("start_ts", 0),
                                 rec.get("end_ts", 0)))
        if not utts:
            return None
        # 按段分组提取（段体 → 解码 PCM → 切 utterance 时间窗 → 拼接）
        pieces = []
        for seg_id, utt_start, utt_end in utts[:50]:     # 上限 50 条防超大拼接
            seg_body = store.get(device, seg_id)
            if seg_body is None:
                continue
            seg_ts = store.get_segment_ts(device, seg_id)
            if seg_ts is None:
                continue
            try:
                pcm, sr = decode_to_pcm16k(seg_body)
            except (ValueError, Exception):
                continue
            start_off = max(0, int((utt_start - seg_ts) / 1000 * sr))
            end_off = min(len(pcm), int((utt_end - seg_ts) / 1000 * sr))
            if end_off - start_off >= sr:                # ≥1 秒的片段才收
                pieces.append(pcm[start_off:end_off])
        if not pieces:
            return None
        combined = np.concatenate(pieces)
        if len(combined) < 16000:                        # 至少 1 秒 @16k
            return None
        # 编码为 WAV bytes（声纹 provider 的 embed_audio 接口）
        buf = io.BytesIO()
        sf.write(buf, combined, 16000, format="WAV", subtype="PCM_16")
        return buf.getvalue()

    @staticmethod
    def _parse_multipart(body: bytes, content_type: str) -> dict:
        """简单 multipart/form-data 解析 → {name: str 或 (filename, bytes)}。"""
        m = re.search(r"boundary=([^;]+)", content_type)
        if not m:
            return {}
        boundary = m.group(1).strip('"').encode()
        fields = {}
        for part in body.split(b"--" + boundary):
            part = part.strip(b"\r\n")
            if not part or part == b"--":
                continue
            if b"\r\n\r\n" not in part:
                continue
            hdr, data = part.split(b"\r\n\r\n", 1)
            name_m = re.search(rb'name="([^"]+)"', hdr)
            if not name_m:
                continue
            name = name_m.group(1).decode("utf-8", "replace")
            file_m = re.search(rb'filename="([^"]+)"', hdr)
            if file_m:
                fields[name] = (file_m.group(1).decode("utf-8", "replace"), data)
            else:
                fields[name] = data.decode("utf-8", "replace")
        return fields

    def serve_forever(self):
        print("[ingest] listening on %s:%d, data_dir=%s" %
              (self.app.listen_host, self.app.listen_port, self.app.data_dir), flush=True)
        from . import sched
        sched.start(self.app)      # 服务内自动分析（转写扫描+新内容触发纪要/日报）
        self._httpd.serve_forever()
