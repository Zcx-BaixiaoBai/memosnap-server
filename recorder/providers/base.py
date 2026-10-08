# recorder/providers/base.py —— 能力槽位的统一接口（design §5.1）。
#
# 约定：
# - 所有适配器实现同一组接口；主流程只认接口，加厂商 = 加一个适配器文件 + 一条 selftest 用例。
# - 每个实例声明 `egress`（本次调用是否把数据发出本机）与 `module`（依赖哪个本地模块），
#   selftest 据此出"当前部署哪些能力会出网"的报告（design §5.6）。

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class Utterance:
    """一句话：转写 + 归属 + 可选情绪。speaker 保持 ASR/diarization 的原始标签（如 S1）。"""
    start: float                 # 秒（段内相对时间）
    end: float
    text: str
    speaker: str = ""            # 匿名标签（S1/S2…）；经声纹归因后才映射成身份
    emotion: str = ""            # SenseVoice 路径有；MiniMax 路径为空


@dataclass
class Transcript:
    text: str
    duration: float
    n_speakers: int
    utterances: list = field(default_factory=list)


@dataclass
class VoiceIdentity:
    """声纹归因结果：把匿名 speaker 映射成身份。local-only 硬约束在接口层就体现——
    该接口没有任何 'api' 实现，注册表也不会为它构造云端 provider。"""
    speaker: str
    identity: str                # "self"（佩戴者）或登记过的身份名；未匹配留空


class ASRProvider(ABC):
    slot = "asr"
    egress: bool = True          # 是否把音频发出本机
    module: str = ""             # 依赖的本地模块名（core 空串）

    @abstractmethod
    def transcribe(self, audio: bytes, fmt: str, language: str = "") -> Transcript:
        """audio 为原始段字节（如 opus 容器）；fmt 如 'opus'。"""


class DiarizationProvider(ABC):
    slot = "diarization"
    egress: bool = True
    module: str = ""


class VoiceprintProvider(ABC):
    slot = "voiceprint"
    egress = False               # 硬约束：声纹数据不出 core 主机
    module = "local-asr"

    @abstractmethod
    def enroll(self, identity: str, audio: bytes, fmt: str) -> bool:
        ...

    @abstractmethod
    def identify(self, audio: bytes, fmt: str, candidates: list) -> list:
        """返回 [(identity, score), ...] 按分降序；candidates 限候选集。"""


class EmotionProvider(ABC):
    slot = "emotion"
    egress: bool = False
    module: str = ""


class SummarizeProvider(ABC):
    slot = "summarize"
    egress: bool = True
    module: str = ""

    @abstractmethod
    def summarize(self, structured: dict) -> str:
        """structured 为本地抽取好的结构化输入；redact 在构造适配器时决定是否先脱敏。"""


class ReportProvider(SummarizeProvider):
    slot = "report"


class CalendarIntegration(ABC):
    """出站集成（非 AI 能力）：飞书日历读/写（design §5.8 步骤 4/7）。"""
    slot = "calendar"
    egress = True

    @abstractmethod
    def read_events(self, date: str) -> list:
        """返回当天日程 [{title, start, end, id}]，供会议切分对齐。"""

    @abstractmethod
    def write_event(self, title: str, start: str, end: str, note: str = "") -> str:
        """把分析产出的日程回写为飞书日历事件，返回事件 id。"""
