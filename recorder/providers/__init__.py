# recorder/providers —— 能力槽位 provider 集合
from .base import (ASRProvider, CalendarIntegration, DiarizationProvider,
                   EmotionProvider, ReportProvider, SummarizeProvider,
                   Transcript, Utterance, VoiceIdentity, VoiceprintProvider)
from .registry import build_providers

__all__ = [
    "ASRProvider", "DiarizationProvider", "VoiceprintProvider", "EmotionProvider",
    "SummarizeProvider", "ReportProvider", "CalendarIntegration",
    "Transcript", "Utterance", "VoiceIdentity", "build_providers",
]
