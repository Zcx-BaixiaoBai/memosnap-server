# recorder —— 工牌静默录音服务端 core（M2 骨架）
#
# 模块边界（对应 design/badge-recorder-daily-digest-design.md）：
#   config        配置加载与校验（密钥只存环境变量名）
#   safe_upstream 出站请求统一校验（复用 gateway.py 的 SSRF 防护语义）
#   providers     能力槽位的 provider 抽象与适配器（asr/diarization/...）
#   ingest        设备侧 HTTP 契约（/health /rec/segment /rec/state）
#   selftest      部署能力报告（哪些槽位可用、是否出网）
__version__ = "0.1.0"
