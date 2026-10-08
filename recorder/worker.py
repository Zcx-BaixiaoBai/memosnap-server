# recorder/worker.py —— 分析 worker（M3：段 → 转写 → 逐字稿）。
#
# 流程（design §5.8 前两级的最小闭环）：
#   1. 取 status='stored' 的段（可按日期/设备过滤）；
#   2. 逐段调 ASR provider 转写，原始结果缓存进 segments.asr_json（重跑组装不再调 API，
#      省 quota），段状态 → 'transcribed'；
#   3. 对受影响的 (device, date) 重建当日逐字稿（JSONL）：
#      segment 头记录 + 逐句 utterance（绝对时间戳 = 段起始 + 句内偏移）+ 段间 gap 标注
#      （缺口不填补、显式记录，design §5.8）；
#   4. 结果存 transcripts 表（M2 的 BLOB 决策同款理由：单用户试点期零文件路径面）。
#
# 注意：
# - 段体目前按明文音频处理；设备侧 AES（M1/M2 设备端）接入后在此处解密。
# - fmt 由字节嗅探（OggS/RIFF/ID3...）；设备定型后固定为 opus。
# - ASR 单段失败不中断批次：该段保持 stored，下轮 process 重试。

import json

from . import frec
from .audio import decode_to_pcm16k
from .config import AppConfig
from .ingest import SegmentStore
from .providers import build_providers

GAP_THRESHOLD_MS = 5 * 60 * 1000      # 段间隔超过 5 分钟记 gap（design §5.8 步骤4 的会话切分阈值同源）


def _sniff_fmt(body: bytes) -> str:
    if body[:4] == b"FREC":
        return "frec"
    if body[:4] == b"OggS":
        return "opus"
    if body[:4] == b"RIFF":
        return "wav"
    if body[:4] == b"fLaC":
        return "flac"
    if body[:3] == b"ID3" or (len(body) > 2 and body[0] == 0xFF and (body[1] & 0xE0) == 0xE0):
        return "mp3"
    return "opus"                      # 默认按设备上传格式


def _prepare_audio(body: bytes):
    """返回 (可直接送 ASR 的音频字节, fmt)。设备上传的 FREC v3 段在此解包
    成标准 Ogg/Opus（MiniMax ASR 只收 wav/mp3/aac/opus/ogg，不认私有容器）。"""
    if body[:4] == b"FREC":
        rec = frec.parse_frec(body)
        if not rec["frames"]:
            raise ValueError("空段（无帧，跳过 ASR）")
        return frec.frec_to_ogg(rec), "opus"
    return body, _sniff_fmt(body)


def process(app: AppConfig, date=None, device=None, gap_ms=GAP_THRESHOLD_MS) -> dict:
    """转写所有待处理段并重建当日逐字稿。返回统计。"""
    store = SegmentStore(app.db_path)
    providers = build_providers(app, store=store)
    asr = providers["asr"]
    if not getattr(asr, "available", False):
        raise RuntimeError("asr provider 不可用（selftest 看原因）")
    # 声纹归因（M4，local-only）：有登记库且模型可用时，匿名 S1/S2 → 身份
    vp = providers.get("voiceprint")
    vp_on = vp is not None and getattr(vp, "available", False)

    todo = store.list_stored(date, device)
    print("[worker] 待转写段：%d%s" % (len(todo), "（声纹归因开）" if vp_on else ""))
    done, failed, touched = 0, 0, set()
    for seg in todo:
        body = store.get(seg["device_id"], seg["segment_id"])
        try:
            audio, fmt = _prepare_audio(body)
            t = asr.transcribe(audio, fmt)
            asr_json = json.dumps({
                "text": t.text, "duration": t.duration, "n_speakers": t.n_speakers,
                "utterances": [{"start": u.start, "end": u.end, "speaker": u.speaker,
                                "text": u.text, "emotion": u.emotion,
                                "identity": getattr(u, "identity", "")}
                               for u in t.utterances]}, ensure_ascii=False)
            store.mark_transcribed(seg["device_id"], seg["segment_id"], asr_json)
            touched.add((seg["device_id"], seg["local_date"]))
            done += 1
            note = ""
            if vp_on and t.utterances:
                note = _annotate_with_voiceprint(vp, audio, seg, store, t)
            print("[worker] ✓ %s/%s %dB → %d 句 %ds%s" %
                  (seg["device_id"], seg["segment_id"], seg["bytes"],
                   len(t.utterances), int(t.duration), note))
        except Exception as e:
            failed += 1
            if "空段" in str(e):
                # 历史短读坏段（0 帧）：标记 invalid 退出重试队列
                store.mark_invalid(seg["device_id"], seg["segment_id"])
                print("[worker] ⊘ %s/%s：空段标记 invalid（不再重试）"
                      % (seg["device_id"], seg["segment_id"]))
            else:
                print("[worker] ✗ %s/%s：%s" % (seg["device_id"], seg["segment_id"], e))

    for dev, day in sorted(touched):
        jsonl = _assemble_day(store, dev, day, gap_ms)
        store.upsert_transcript(dev, day, jsonl)
        print("[worker] 逐字稿 %s %s：%d 行" % (dev, day, jsonl.count("\n") + 1))
    return {"processed": done, "failed": failed, "days": len(touched)}


def _split_sentences(text: str):
    import re as _re
    parts = _re.split(r"(?<=[。！？!?；;])", text or "")
    return [p for p in (x.strip() for x in parts) if p]


def _resplit_by_turns(utts: list, turns: list) -> list:
    """把 ASR 长句按本地分离 turn 重切为多说话人 utterance。
    句级按 turn 时长占比顺序分配（对话句子时序与 turn 时序一致）。"""
    out = []
    for u in utts:
        s0, e0 = float(u["start"]), float(u["end"])
        ov = [(max(s0, ts), min(e0, te), cid)
              for ts, te, cid in turns if ts < e0 and te > s0]
        sents = _split_sentences(u.get("text", ""))
        if not ov:
            # 无 turn 覆盖（整句落在静音/未聚类区间）：归最近 turn（±15s 内）
            mid = (s0 + e0) / 2
            near = min(turns, key=lambda t: abs((t[0] + t[1]) / 2 - mid), default=None)
            nu = dict(u)
            if near and abs((near[0] + near[1]) / 2 - mid) <= 15:
                nu["speaker"] = "C%d" % near[2]
            out.append(nu)
            continue
        if len(ov) == 1 or not sents:
            nu = dict(u)
            nu["speaker"] = "C%d" % ov[0][2]
            out.append(nu)
            continue
        total = sum(e - s for s, e, _ in ov) or 1.0
        idx = 0.0
        placed = []
        for k, (ts, te, cid) in enumerate(ov):
            share = (te - ts) / total
            cnt = len(sents) - int(idx) if k == len(ov) - 1 else int(round(share * len(sents)))
            chunk = sents[int(idx):int(idx) + cnt] if k < len(ov) - 1 else sents[int(idx):]
            idx += cnt
            if not chunk:
                continue
            placed.append({"start": ts, "end": te, "speaker": "C%d" % cid,
                           "text": "".join(chunk),
                           "emotion": u.get("emotion", ""), "identity": ""})
        out.extend(placed or [dict(u)])
    return out


def _annotate_with_voiceprint(vp, audio_bytes, seg, store, t):
    """本地嵌入聚类分离（云端 diarization 远场失效兜底）+ 声纹归因 → 回写 asr_json。
    多簇时把 ASR 长句按 turn 重切为多说话人 utterance（C1/C2…），再按簇归因身份。
    声纹数据只在本地（design §5.6 硬约束）。返回日志附注。"""
    try:
        pcm, _ = decode_to_pcm16k(audio_bytes)
    except ValueError:
        return "（声纹：音频解码失败）"
    turns = vp.diarize_turns(pcm)
    ncl = len({c for _, _, c in turns})
    utterances = [dict(start=u.start, end=u.end, speaker=u.speaker,
                       text=u.text, emotion=u.emotion, identity="")
                  for u in t.utterances]
    if ncl > 1:
        utterances = _resplit_by_turns(utterances, turns)
    mapping = vp.annotate_speakers(utterances, pcm)
    hits = {spk: v for spk, v in mapping.items() if v[0]}
    if ncl > 1 or hits:
        asr_json = json.dumps({
            "text": t.text, "duration": t.duration,
            "n_speakers": max(ncl, t.n_speakers),
            "utterances": utterances,
            "turns": [list(x) for x in turns]}, ensure_ascii=False)
        store.mark_transcribed(seg["device_id"], seg["segment_id"], asr_json)
    names = ", ".join("%s→%s(%.2f)" % (spk, v[0], v[1]) for spk, v in hits.items())
    tag = "（分离：%d人）" % ncl if ncl > 1 else "（分离：单人）"
    return tag + ("（声纹：%s）" % names if names else "")


def reannotate(app: AppConfig, date=None) -> set:
    """声纹库变化后对已转写段重新归因身份（不重跑 ASR/云端分离）：
    asr_json 的 utterances 直接做 1:N 归因（turns 有缓存不重聚类），回写后
    重建受影响日逐字稿。返回 touched (dev, day) 集合。"""
    store = SegmentStore(app.db_path)
    providers = build_providers(app, store=store)
    vp = providers.get("voiceprint")
    if vp is None or not getattr(vp, "available", False):
        return set()
    touched = set()
    for seg in store.list_transcribed(date):
        try:
            rec = json.loads(seg["asr_json"])
        except ValueError:
            continue
        utts = rec.get("utterances") or []
        if not utts:
            continue
        try:
            pcm, _ = decode_to_pcm16k(store.get(seg["device_id"], seg["segment_id"]))
        except ValueError:
            continue
        turns = rec.get("turns")
        if turns is None:
            turns = [list(x) for x in vp.diarize_turns(pcm)]
        mapping = vp.annotate_speakers(utts, pcm)
        asr_json = json.dumps({
            "text": rec.get("text", ""), "duration": rec.get("duration", 0),
            "n_speakers": rec.get("n_speakers", 0),
            "utterances": utts, "turns": turns}, ensure_ascii=False)
        store.mark_transcribed(seg["device_id"], seg["segment_id"], asr_json)
        touched.add((seg["device_id"], seg["local_date"]))
        names = ", ".join("%s→%s" % (k, v[0]) for k, v in mapping.items() if v[0])
        print("[worker] 重归因 %s：%s" % (seg["segment_id"], names or "无匹配"), flush=True)
    for dev, day in sorted(touched):
        jsonl = _assemble_day(store, dev, day, GAP_THRESHOLD_MS)
        store.upsert_transcript(dev, day, jsonl)
        print("[worker] 逐字稿重建 %s %s：%d 行" % (dev, day, jsonl.count("\n") + 1), flush=True)
    return touched


def _assemble_day(store: SegmentStore, device_id: str, local_date: str,
                  gap_ms: int) -> str:
    """把一天的已转写段组装成 JSONL：segment 头 + utterance（绝对时间戳）+ gap 标注。
    identity 优先级：手动纠正（speaker_overrides）> 声纹归因（asr_json）。"""
    overrides = store.get_speaker_overrides(device_id, local_date)
    records = []
    prev_end = None
    for seg in store.day_segments(device_id, local_date):
        start_ts, end_ts = seg["start_ts"], seg["end_ts"]
        if prev_end is not None and start_ts - prev_end > gap_ms:
            records.append({"type": "gap", "start_ts": prev_end, "end_ts": start_ts,
                            "seconds": round((start_ts - prev_end) / 1000)})
        asr = json.loads(seg["asr_json"] or "{}")
        records.append({"type": "segment", "segment_id": seg["segment_id"],
                        "start_ts": start_ts, "end_ts": end_ts, "seq": seg["seq"],
                        "n_speakers": asr.get("n_speakers", 0)})
        for u in asr.get("utterances") or []:
            spk = u.get("speaker", "")
            full = "%s/%s" % (seg["segment_id"], spk)
            # 手动纠正优先：先查全标签（段/簇），再查当日簇后缀（C2 跨段统一改）
            identity = overrides.get(full) or overrides.get(spk) or u.get("identity", "")
            records.append({
                "type": "utterance",
                "start_ts": start_ts + round(float(u["start"]) * 1000),
                "end_ts": start_ts + round(float(u["end"]) * 1000),
                "speaker": full,
                "identity": identity,                   # 手动纠正 > 声纹归因（空=未匹配）
                "text": u.get("text", ""),
                "emotion": u.get("emotion", "")})
        prev_end = max(prev_end or end_ts, end_ts)
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in records)


def dump(app: AppConfig, date=None, device=None):
    """打印逐字稿索引与内容（CLI 调试用）。"""
    store = SegmentStore(app.db_path)
    rows = store.list_transcripts(date, device)
    if not rows:
        print("[worker] 无逐字稿")
        return
    for r in rows:
        print("=== %s %s（%dB，更新于 %s）" %
              (r["device_id"], r["local_date"], r["bytes"], r["updated_at"]))
        print(store.get_transcript(r["device_id"], r["local_date"]))
