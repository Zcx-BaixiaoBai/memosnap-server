# 秒忆卡 MemoSnap · 服务端（memosnap-server）

工牌静默录音 + 每日自动分析的服务端 core：接收 ESP32 工牌上传的 Opus 录音段，
自动转写、说话人分离/声纹归因、切会话、生成会议纪要与每日工作日报，并提供
一套深色品牌风格的 Web 回看/管理界面。

配套固件仓库（ESP32-C3 工牌）：
<https://github.com/Zcx-BaixiaoBai/badge-recorder-firmware>

设计文档：[docs/design.md](docs/design.md)

## 功能

- **接入**：HTTP  ingest（`POST /rec/segment`），X-Gateway-Token 鉴权、幂等去重、
  短读拒收（防静默丢数据）、设备补传队列查询。
- **分析流水线**：段 → ASR 转写 → 当日逐字稿（含录音缺口标注）→ 会话切分与
  会议候选判定 → LLM 会议纪要（长会话自动 MAP→REDUCE 分章节）→ 每日日报
  （概览/会议/时间轴/待办/需要注意的话/风险与需协调/录音缺口）。
- **说话人**：云端 ASR 的匿名标签 + 本地声纹嵌入（CAM++/ERes2Net）1:N 归因；
  云端分离失效时自动启用**本地嵌入聚类分离**兜底；WebUI 支持说话人入档、
  手动改归因（优先级高于声纹）。
- **WebUI**（`/rec/ui`）：录音日历、当日回看（说话人标注/录音时段/逐字稿/
  会话纪要/日报）、全文检索（FTS5 中文 bigram）、声纹管理、配置控制台
  （各能力槽位 provider/base_url/model/密钥/阈值前端可配）、底部 Dock 导航
  与整页横移动效、pjax 平滑切换。
- **自动分析调度**：serve 进程内常驻调度器，每 10 分钟扫待转写段；有新内容
  自动重建纪要/日报；声纹库变化自动对历史段重归因。

## 快速上手

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # 本地模型能力另需 sherpa-onnx（见下）

cp config.example.yaml config.yaml       # 必填 deployment.token（自生成随机值）
cp .env.example .env                     # 密钥只进环境变量，不进配置文件

python -m recorder selftest --config config.yaml   # 部署能力报告（哪些槽位可用/出网）
python -m recorder serve   --config config.yaml    # 起 ingest + WebUI + 自动分析
```

浏览器打开 `http://<host>:8787/`（自动跳 `/rec/ui`），首次输入 config 里的 token 登录。

无工牌时模拟设备上传：

```bash
python -m recorder ingest-file --config config.yaml --device testbadge --file some.opus
```

手动跑分析三步（serve 内调度器会自动跑，CLI 用于补跑/重跑）：

```bash
python -m recorder process  --config config.yaml   # 段→转写→逐字稿
python -m recorder meetings --config config.yaml   # 切会话+纪要
python -m recorder report   --config config.yaml   # 当日日报
```

## 能力槽位与部署形态

每个 AI 能力是一个可插拔槽位，部署时独立选择本地模型或云端 API：

| 槽位 | 可选 provider | 说明 |
|---|---|---|
| asr | `minimax` / `sherpa`(本地 SenseVoice) / 预留 | 转写+匿名说话人标签 |
| diarization | `asr-tags` / `none` / 预留 | 复用 ASR 标签或本地分离 |
| voiceprint | `local-only`（硬约束） | 声纹永不出本机，无云端选项 |
| emotion | `asr-tags` / `llm-infer` | 情绪标签 |
| summarize / report | `minimax` / 预留 ollama、openai-compatible | 纪要 / 日报 LLM |

两个正交部署选择：core 装在哪台主机（局域网 PC = 隐私最强 / 公网主机 = 工牌
随处可传），以及每个槽位指向本地模型还是自配 API。WebUI 配置控制台可在线切换
并保存（覆盖存本机数据库；密钥存本机 DB 或环境变量，环境变量优先）。

### 本地模型下载（可选）

- SenseVoice（本地 ASR，int8）：sherpa-onnx 模型库 `csukuangfj/sherpa-onnx-asr-models`
  释放包中的 `sense-voice-zh-en-ja-ko-yue-int8` （model.onnx + tokens.txt），
  放入 `models/sensevoice/`。
- 声纹（CAM++ / ERes2Net）：k2-fsa/sherpa-onnx 释放 tag `speaker-recongition-models`
  中的 `3dspeaker_speech_campplus_sv_zh-cn_16k-common.onnx` 或
  `3dspeaker_speech_eres2net_large_sv_zh-cn_3dspeaker_16k.onnx`，
  放入 `models/voiceprint/` 并在配置/WebUI 指向。

## 安全与隐私

- 设备上传与 WebUI 均凭 `deployment.token` 鉴权；token 为空拒绝启动。
- **密钥治理**：配置文件只允许 `*_env` 环境变量名；WebUI 填入的密钥存本机
  数据库（与录音数据同权限类），环境变量优先；源码/示例/测试中不含可用凭据。
- **声纹硬约束**：嵌入向量只存 core 主机 SQLite，比对在本机完成，代码层不接受
  任何云端 voiceprint provider。
- 出网脱敏：summarize/report 走云端时默认 `redact: true`，人名→角色标签在
  适配器边界替换。
- 出站校验：服务端主动请求仅 http/https 且 host 白名单校验；`local_endpoints`
  仅接受环回地址。
- 存储：段体/逐字稿/纪要/声纹全部落 SQLite（单用户试点期零文件路径面）。
- 合规提示：静默录制他人对话在部分司法辖区受隐私法约束；产品侧默认无录音
  指示的设计需配合上岗告知与背面印字使用，服务端保留录音时段可查记录。

## 目录

```
recorder/            core 包（ingest/worker/meetings/report/webui/providers/…）
recorder/providers/  能力适配器（minimax asr/llm、sherpa asr/voiceprint、stubs）
tests/fixtures/      合成测试音频（TTS，无隐私）
config.example.yaml  配置模板（密钥只填环境变量名）
docs/design.md       设计文档
```

## License

MIT © 2026 MemoSnap contributors
