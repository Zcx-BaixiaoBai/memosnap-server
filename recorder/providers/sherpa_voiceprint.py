# recorder/providers/sherpa_voiceprint.py —— 声纹登记与 1:N 辨认（M4，local-only）。
#
# 模型：3D-Speaker CAM++（3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx，
# 192 维嵌入，16kHz 单声道输入），经 sherpa-onnx CPU 推理。
# 实测分离度：同一说话人余弦 1.0000，不同说话人 0.3486——阈值 0.5 区分充分。
#
# 硬约束（design §5.1/§5.6）：voiceprint 无任何 API provider，嵌入向量只存
# core 主机的 SQLite（voiceprints 表），比对在本机 numpy 完成，永不外发。
#
# 声纹库重建：嵌入从 DB 加载（load_voiceprints → 余弦比对），不做内存常驻
# 服务——单用户试点登记量个位数，按需加载即可。

import os

import numpy as np

from ..audio import decode_to_pcm16k
from ..config import CapabilityCfg

MIN_ENROLL_SEC = 1.5      # 登记音频最短时长（建议 ≥5s）
MIN_IDENTIFY_SEC = 2.0    # 段内某说话人累计语音低于此值不判身份（样本太少）


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


class SherpaVoiceprint:
    slot = "voiceprint"
    name = "local-sherpa"
    egress = False              # 硬约束：声纹数据不出 core 主机
    module = "local-asr"

    def __init__(self, cfg: CapabilityCfg, store):
        self.cfg = cfg
        self.store = store
        self.model_path = cfg.local.get("model") or "models/voiceprint/campplus_zh.onnx"
        self.threshold = float(cfg.local.get("threshold") or 0.5)
        self.min_margin = float(cfg.local.get("min_margin") or 0.04)
        self._extractor = None   # 惰性初始化：不装 sherpa-onnx/模型时 selftest 仍可跑
        self.reason = ""

    # ---- 基础设施 ----

    def _init_extractor(self):
        if self._extractor is not None:
            return self._extractor
        try:
            import sherpa_onnx
        except ImportError:
            self.reason = "未安装 sherpa-onnx（pip install sherpa-onnx）"
            return None
        if not os.path.exists(self.model_path):
            self.reason = "声纹模型不存在：%s（见 README 下载说明）" % self.model_path
            return None
        self._sherpa = sherpa_onnx
        self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
            sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=self.model_path, num_threads=1, provider="cpu"))
        return self._extractor

    @property
    def available(self) -> bool:
        return self._init_extractor() is not None

    def _embed_one(self, pcm: np.ndarray):
        ex = self._init_extractor()
        if ex is None:
            return None
        s = ex.create_stream()
        s.accept_waveform(16000, np.ascontiguousarray(pcm, dtype=np.float32))
        e = np.array(ex.compute(s), dtype=np.float32)
        n = float(np.linalg.norm(e))
        return e / n if n > 0 else e

    def _embed_pcm(self, pcm: np.ndarray):
        """pcm: float32 单声道 16k。返回嵌入（单位化）或 None（时长不足）。
        长音频（>45s）切 20s 窗 10s 步分块嵌入取平均——整条长波一次性喂入
        会让 ORT 中间特征爆内存（eres2net-large 实测 5GB OOM，2026-09-21）。"""
        if self._init_extractor() is None or len(pcm) < 16000 * MIN_ENROLL_SEC:
            return None
        if len(pcm) <= 16000 * 45:
            return self._embed_one(pcm)
        chunk, step, embs = 16000 * 20, 16000 * 10, []
        for i in range(0, len(pcm) - chunk + 1, step):
            e = self._embed_one(pcm[i:i + chunk])
            if e is not None:
                embs.append(e)
            if len(embs) >= 12:
                break
        if not embs:
            return None
        m = np.mean(embs, axis=0).astype(np.float32)
        n = float(np.linalg.norm(m))
        return m / n if n > 0 else m

    def embed_audio(self, body: bytes, fmt_hint: str = ""):
        """解码任意段字节并取嵌入。"""
        try:
            pcm, _ = decode_to_pcm16k(body, fmt_hint)
        except ValueError:
            return None
        return self._embed_pcm(pcm)

    # ---- 登记库 ----

    def enroll(self, identity: str, body: bytes, fmt_hint: str = "") -> bool:
        """登记/更新一个身份的声纹（音频 ≥1.5s）。"""
        if not identity or len(identity) > 64:
            raise ValueError("identity 需要 1..64 字符")
        emb = self.embed_audio(body, fmt_hint)
        if emb is None:
            return False
        self.store.upsert_voiceprint(identity, int(emb.shape[0]), emb.tobytes())
        return True

    def remove(self, identity: str) -> bool:
        rows = {r["identity"] for r in self.store.list_voiceprints()}
        if identity not in rows:
            return False
        self.store.delete_voiceprint(identity)
        return True

    def enrolled(self):
        return self.store.list_voiceprints()

    # ---- 1:N 辨认 ----

    def _gallery(self):
        gal = []
        for identity, blob in self.store.load_voiceprints():
            emb = np.frombuffer(blob, dtype=np.float32)
            gal.append((identity, emb))
        return gal

    def identify_pcm(self, pcm: np.ndarray, min_sec: float = MIN_IDENTIFY_SEC):
        """返回 (identity|None, score)。低于阈值或样本不足返回 (None, best_score)。
        维度与当前模型不符的旧库条目跳过（换模型后旧声纹自动失效，需重新入档）。"""
        gal = self._gallery()
        if not gal:
            return None, 0.0
        if len(pcm) < 16000 * min_sec:
            return None, 0.0
        emb = self._embed_pcm(pcm)
        if emb is None:
            return None, 0.0
        best_id, best, second = None, -1.0, -1.0
        for identity, ref in gal:
            if ref.shape != emb.shape:
                continue
            s = _cos(emb, ref)
            if s > best:
                best_id, second, best = identity, best, s
            elif s > second:
                second = s
        # 阈值 + 领先裕度双门限：接近平局（两人分数相近）时宁匿名不误判
        if best >= self.threshold and (best - max(second, 0.0)) >= self.min_margin:
            return best_id, best
        return None, best

    # ---- 本地聚类分离：云端 diarization 失效兜底（远场音频 n_speakers 恒 1，
    #      MiniMax 实测 2026-09-21）：滑窗嵌入贪心余弦聚类 → turns ----
    WIN_SEC = 2.0          # v0.5.x：1.6→2.0s 窗——远场短窗嵌入质量是归因差的主因之一
    HOP_SEC = 0.8
    MAX_CLUSTERS = 6
    SILENCE_RMS = 0.012        # float 幅值 RMS 低于此=静音窗（不进聚类）

    def _window_labels(self, pcm):
        """[(start_s, end_s, cid)]；cid=0 静音/无嵌入；cid 从 1 起。"""
        thr = float(self.cfg.local.get("cluster_threshold") or 0.68)
        n = len(pcm)
        win, hop = int(self.WIN_SEC * 16000), int(self.HOP_SEC * 16000)
        cents, counts, out = [], [], []
        pos = 0
        while pos + win <= n:
            seg = pcm[pos:pos + win]
            s0 = pos / 16000
            pos += hop
            rms = float(np.sqrt(np.mean(seg * seg))) if len(seg) else 0.0
            emb = self._embed_pcm(seg) if rms >= self.SILENCE_RMS else None
            if emb is None:
                out.append((s0, pos / 16000, 0))
                continue
            best, best_s = -1, -1.0
            for i, c in enumerate(cents):
                s = _cos(emb, c)
                if s > best_s:
                    best, best_s = i, s
            if best >= 0 and best_s >= thr:
                cid = best + 1
                counts[best] += 1
                w = 1.0 / counts[best]
                cents[best] = cents[best] * (1 - w) + emb * w
            elif len(cents) < self.MAX_CLUSTERS:
                cents.append(emb.copy())
                counts.append(1)
                cid = len(cents)
            else:
                cid = best + 1 if best >= 0 else 0
            out.append((s0, pos / 16000, cid))
        return out

    def diarize_turns(self, pcm, min_turn_sec=1.2):
        """连续同说话人窗合并成 turn（跨 ≤1.7s 静音桥接）。[(start, end, cid)]，
        cid 按首次出现重排为连续 1..N（过滤短 turn 后不留空号）。"""
        turns = []
        for s, e, cid in self._window_labels(pcm):
            if cid == 0:
                continue
            if turns and turns[-1][2] == cid and s - turns[-1][1] <= 1.7:
                turns[-1] = (turns[-1][0], e, cid)
            else:
                turns.append((s, e, cid))
        turns = [t for t in turns if t[1] - t[0] >= min_turn_sec]
        remap = {}
        out = []
        for s, e, cid in turns:
            if cid not in remap:
                remap[cid] = len(remap) + 1
            out.append((s, e, remap[cid]))
        return out

    # ---- 逐字稿匿名说话人 → 身份（worker 调用） ----

    def annotate_speakers(self, utterances: list, pcm: np.ndarray):
        """把 ASR 返回的 utterance（含 speaker 标签与秒级 start/end）标注 identity。
        直接修改 utterance 的 "identity" 字段。返回 {匿名标签: (identity|None, score)}。
        采样按各说话人的句区间拼接（静音段不进声纹）。"""
        by_speaker = {}
        for u in utterances:
            by_speaker.setdefault(u.get("speaker", ""), []).append(
                (float(u.get("start", 0)), float(u.get("end", 0))))
        result = {}
        for spk, ranges in by_speaker.items():
            pieces = []
            for s, e in ranges:
                i0, i1 = int(s * 16000), int(e * 16000)
                if 0 <= i0 < i1 <= len(pcm):
                    pieces.append(pcm[i0:i1])
            if not pieces:
                result[spk] = (None, 0.0)
                continue
            identity, score = self.identify_pcm(np.concatenate(pieces))
            result[spk] = (identity, score)
            if identity:
                for u in utterances:
                    if u.get("speaker", "") == spk:
                        u["identity"] = identity
        return result
