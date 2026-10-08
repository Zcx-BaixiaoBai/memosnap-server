# recorder/providers/stubs.py —— 占位 provider。
#
# M2 只落地契约与 MiniMax ASR；local-sherpa（M3）、Ollama（M5）、飞书日历（M4）
# 的适配器在后续里程碑填。占位对象可被构造、可被 selftest 报告（available=False，
# reason 写明是哪个里程碑交付），被真正调用时抛 NotImplementedError —— 宁可
# 运行时报"未实现"，也不要静默给出空结果。

from .base import (ASRProvider, CalendarIntegration, SummarizeProvider,
                   VoiceprintProvider)


class _Stub:
    name = "stub"
    available = False
    reason = "适配器在后续里程碑交付"

    def __init__(self, name=None, reason=None):
        if name:
            self.name = name
        if reason:
            self.reason = reason

    def _raise(self):
        raise NotImplementedError("provider %r（槽位 %s）：%s" %
                                  (self.name, getattr(self, "slot", "?"), self.reason))


class StubASR(_Stub, ASRProvider):
    def transcribe(self, audio, fmt, language=""):
        self._raise()


class StubSummarize(_Stub, SummarizeProvider):
    def summarize(self, structured):
        self._raise()


class StubVoiceprint(_Stub, VoiceprintProvider):
    def enroll(self, identity, audio, fmt):
        self._raise()

    def identify(self, audio, fmt, candidates):
        self._raise()


class StubCalendar(_Stub, CalendarIntegration):
    def read_events(self, date):
        self._raise()

    def write_event(self, title, start, end, note=""):
        self._raise()


class AsrTagsDiarization:
    """diarization=asr-tags：复用 ASR 自带的匿名 speaker 标签，不做任何独立计算。
    这是 MiniMax 路径的推荐配置（design §5.1），egress 跟随 ASR。"""
    slot = "diarization"
    name = "asr-tags"
    egress = None          # 跟随 asr 槽位（由 registry 填）
    module = ""
    available = True
