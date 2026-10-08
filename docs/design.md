# 工牌静默录音 + 每日分析 — 设计方案

> 状态：待确认（确认后进入开发）
> 日期：2026-09-17
> 关联：本方案是 AI Passport 工牌（ESP32-C3）的第二条产品线，与现有"按键进 ZCode 会话"远控用途并行；复用现有 `firmware/ai-passport` 与 `gateway/` 骨架。

## 1. 产品定义

工牌静默录音，边录边传（分钟级批量），每日根据上传录音自动分析：智能识别会议、生成妙记式纪要、产出每日工作记录（参与会议 / 工作内容 / 需要注意的话 / 工作重点）。参考飞书妙记。

**核心约束（来自用户决策）**：

- 录音模式无任何指示、无交互直接熄屏；合规告知放到不耗电的物理/制度层（工牌背面印字 + 上岗告知 + 服务端保留"何时录了多久"可查记录）。
- 数据本地优先是产品能力（自己的电脑当网关、数据不脱离本地），但**部署时可选**：core 装在局域网 PC（隐私最强）或公网 IP 主机如 HK（免局域网限制、工牌随处可传）；HK 不再只是哑收件箱，可直接跑 core + API 分析。
- 部署支持 Docker 与纯本地两种；**部署时**即可按能力槽位选"本地模型"或"自配 API"，接 API 的能力对应的本地模型模块不安装。
- 默认值可配：隐私敏感用户选全本地，便捷/多用户优先选 API（用户本人无所谓出网，但产品开发出来不止自己用，故两种都支持、部署时定）；跨隐私边界降级不默认；声纹硬约束只允许 core 主机本地、绝不发第三方 API。

## 2. 硬件基线（已核实，附证据）

| 项 | 值 | 证据 |
| --- | --- | --- |
| 主控 | ESP32-C3，单核 160MHz，无 FPU，无 PSRAM | `firmware/ai-passport/sdkconfig.defaults:2` `CONFIG_IDF_TARGET="esp32c3"`；同文件:3 注释"ESP32-C3 无 PSRAM" |
| 动态堆 | 约 178KB（55+113+10 KB + 7KB RTCRAM） | `firmware/boot_log.txt` heap_init 行 |
| Flash | 8MB | `sdkconfig.defaults:7` `CONFIG_ESPTOOLPY_FLASHSIZE_8MB=y` |
| 现有分区 | nvs 0x9000/0x6000、phy 0xf000/0x1000、factory 0x10000/**0x300000(3MB)**、cardid 0x356000/0x4000 | `firmware/ai-passport/partitions.csv` |
| 可用空闲 | 0x35A000–0x800000 = 4,874,240 B；新增 `recordings` 分区 0x360000/0x4A0000 = 4,849,664 B（4.625 MiB），保留 cardid | `docs/development/engineering/protected-flash-layout.zh_CN.md`："应用可以新增资源分区，但不得覆盖 cardid"；`tools/verify_firmware.py` 强制校验 |
| 音频 codec | ES8311，I2S，采集必须 24kHz（BCLK 512kHz 不足致 ADC 录几块就停） | `firmware/ai-passport/main/audio_pipe.c:25`；`bsp_pins.h`（MCLK=6/BCLK=5/WS=3/DOUT=2/DIN=4） |
| 显示 | ST7789P3 240×320，4-line SPI，背光 GPIO21 LEDC 5kHz/10bit，**无硬件复位脚**（rst=-1 走 SWRESET） | `components/bsp/include/bsp_pins.h`；`bsp_display.c` |
| 电池 | 520mAh 锂电（优特利），CW2017 电量计（I2C 0x63，与 ES8311 共总线） | `docs/hardware-design/specifications.zh_CN.md:23`；`CHANGELOG.zh_CN.md:9` |
| 功放 | 使能脚未接 MCU（`BSP_I2S_PA_CTRL=-1`，常通），固件无法关断 | `bsp_pins.h` 末行 |
| 唤醒 | 无板级唤醒电路，仅定时器唤醒 | `main/demo_low_power.c` 注释"仓库尚无板级唤醒电路证据" |
| 指示灯 | 仅一颗绿色充电状态灯，由充电 IC 状态脚驱动，**固件不可控**，且只在充电时亮 | `bsp_pins.h` 无 LED 引脚；硬件指南引脚表仅 GPIO21 背光 |
| WiFi 省电 | 刻意 `WIFI_PS_NONE` | `main/wifi_sta.c:59`，根因：MIN_MODEM→CPU 在 beacon 间隔睡眠→I2S DMA 停转 |
| 时钟 | 固件与网关均无 SNTP/校时；官方录音笔备份中文件 FAT 时间为 1980-01-01 | `tools/badgeflash/backups/录音笔/` FAT 解析；全仓 grep 无 sntp |
| 现有录音链路 | `main/audio_pipe.c`（采集任务 + 32KB StreamBuffer + 24k）+ `main/gw_client.c`（esp_http_client 手工 chunked 边录边传） | 见 `audio_pipe.c`、`gw_client.c` |

**官方录音笔固件参考（已逆向）**：`tools/badgeflash/backups/录音笔/image.bin` = `folo_recorder_c3` v0.6.2，ESP-IDF v5.5.2，构建 2026-08-26。

- 分区：app 1.5MB + `recordings` 6.38MiB（FAT16，4096B 扇区/簇，`esp_vfs_fat_spiflash_mount_rw_wl` 磨损均衡），**砍掉 cardid**。
- Opus 在 C3 已验证：`esp_opus_enc`/`esp_opus_dec`（esp_audio_codec），实测 `R0000001.FRC` = 358 帧 / 7.16s / 22607B → **16kHz 单声道、20ms 帧、约 24kbps**。FREC 容器 32B 头：`ver=2, hdr=32, rate=16000, samples, ch=1, payload_size, ?=2, capture_rate=24000`，之后每帧 `[u16 len][opus]`。采集 24kHz、设备侧重采样到 16kHz（头部两个采样率并存为证）。
- 取回：SoftAP `Folo-Recorder-%02X%02X` + 内置 HTTP 服务（HTML 索引、`206 Partial Content`、在线 Opus→PCM WAV 转码）；掉电恢复 `Recovered interrupted recording R%07lu`。
- 没有 VAD、没有上行、没有 ASR（`vad` 字样全是 `invalid`/`vaddr` 子串）。

## 3. 系统总览

```
工牌(ESP32-C3) ──HTTP 分段上传──> core（一套代码，部署在任选主机）
                                    │
        ┌───────────────────────────┴───────────────────────────┐
   部署位置(选择一)                                          能力后端(选择二，逐槽位)
   ├─ 局域网 PC：仅同局域网可传，外出缓冲到 flash 回传        ├─ 本地模型模块(local-asr/local-llm)
   └─ 公网 IP 主机(HK/VPS)：任何地方直传，免局域网限制        └─ API 适配器(MiniMax/OpenAI 兼容…)
                                    │
                            core 主机本地完成：
                            ingest→存储(SQLite+FTS5)→时间轴→分析流水线→纪要/日报→Web UI
                            声纹归因(无 API，永远 core 主机本地模型，不出主机)
```

设备侧与部署解耦：工牌只认一个 HTTP endpoint 契约（见 §7）。**没有独立的 HK/relay 开发路径**——HK 只是"core 部署在公网 IP 主机 + 能力指向 API"这一组合，价值在那个固定公网 IP（免局域网限制）。同一套 core，装在局域网 PC 就是隐私最强形态，装在 HK 就是免局域网限制形态，能力槽位再逐位选本地模型或 API。MiniMax 套餐新上 ASR 后，"公网 IP 主机 + 全 API"成为多用户默认：HK 零算力、免模型、工牌随处可传。

## 4. 设备侧设计

### 4.1 会话制录音（v2 架构，2026-09-17 实测驱动）与熄屏

**RAM 铁账（v0.4.x 13 版实测）**：C3 无 PSRAM 可分配堆 ~86KB；WiFi 驱动 init 一次性吃 ~34KB 且 `esp_wifi_stop()` **不释放**（只有 `esp_wifi_deinit` 释放）；Opus 编码器状态 ~25KB 必须开机预开（WiFi 之后堆碎片化，运行期 open 必败）。"边录边传"（WiFi+录音常驻）装不下——v0.4.26 实测 rec_task 6KB 栈在 WiFi 常驻的 10.9KB 碎片堆上创建失败。

**方案：会话制，录音与传物时分复用堆**（用户 2026-09-17 定）：

- 每节录音上限 **30 分钟**（30000 帧 @60ms = 3.66MB@16kbps，分区 4.625MiB 的 78%）；到点自动收尾。
- **录音阶段**：`wifi_sta_deinit()` 整体卸载 WiFi 驱动（netif/凭据/事件回调保留——v5.5 文档与 esp_netif/esp_wifi 源码核实，无 netif 前置约束，`esp_netif_destroy_default_wifi` 已移除），堆独占给采集/编码/写盘；顺带省电（无线电全关）。
- **同步窗口**（每节结束）：WiFi 重建（`wifi_sta_start(NULL,NULL)` 沿用凭据）→ 等 IP → `/health` 校时 → 上传排空 → 验证 `pending==0`。网络全部在 upload/worker 已有任务里做，会话宿主任务（4KB）只编排。
- **双模式**：手动（长按上键/菜单"录音"）录满 30 分钟同步后提示"可再次录音"；续录（长按下键/菜单"连续录音"）同步排空成功后自动开始下一节。**排空失败即停续录**——Flash 环保护：不清空不续录，绝不写穿 4.625MiB 环；遗留段由闲时心跳（60s）连网后自动补传。
- 开新节守卫：遗留待传 >900KB 拒绝（一节 3.66MB + 0.9MB < 环容量）。
- 音损评估：同步窗口 ~40–60s/节（WiFi 关联+6 段上传），占 30 分钟的 ~3%，用户接受。

熄屏时序（ST7789 无硬件复位脚，只能走命令）：
- 进入：`bsp_display_backlight(0)` → DISPOFF(0x28) → SLPIN(0x10) → `lvgl_port_stop()`（停 tick，任务驻留）；续录循环期间保持熄屏。
- 会话彻底结束：宿主任务 SLPOUT(0x11) → **等 120ms**（ST7789 规格）→ DISPON(0x29) → `lvgl_port_resume()` → 背光亮；worker 250ms 监视 phase 回 IDLE 后刷收尾 UI（同步完成/异常/遗留待传三态文案）。
- SLPIN 期间不发绘制命令；UI 只归 worker 任务。
- **无录音指示**（用户决策）；合规告知走物理/制度层。

### 4.2 音频链路

ES8311 采集固定 24kHz → 设备侧 3:2 重采样到 16kHz → 定点 Opus 编码，单声道 **16kbps、60ms 帧**（esp_audio_codec 的 `esp_opus_enc`；码率可配 6000–510000）。60ms 帧比官方的 20ms 省三分之二包头开销，适合上行。

### 4.3 本地存储与分区

- 分区表（保留 cardid，过 `tools/verify_firmware.py` 门禁）：

```
nvs,        data, nvs,     0x9000,   0x6000,
phy_init,   data, phy,     0xf000,   0x1000,
factory,    app,  factory, 0x10000,  0x300000,
cardid,     data, nvs,     0x356000, 0x4000,
recordings, data, fat,     0x360000, 0x4a0000,
```

- 文件系统：FAT16 + `esp_vfs_fat_spiflash_mount_rw_wl`（磨损均衡，官方已验证）。簇 4096B → 不要写大量小文件。
- 分段：**5 分钟一段**（16kbps 下约 600KB/段），文件名带起始时间戳。
- 容器：沿用 FREC 思路，头部补三字段使其自描述、可拼接、可标注缺口：`start_ts_ms`、`seq_base`、`bitrate`（外加官方原有的 ver/hdr/rate/samples/ch/payload_size/capture_rate）。
- 掉电恢复：照抄官方 `Recovered interrupted recording` 逻辑。

容量（按 16kHz 单声道）：

| 编码 | 码率 | 4.625MiB 缓冲时长 |
| --- | --- | --- |
| 原始 PCM | 32 KB/s | 2.5 分钟 |
| Opus 16kbps | 2 KB/s | 40 分钟 |
| Opus 12kbps | 1.5 KB/s | 54 分钟 |

叠加设备侧 VAD 粗门限（只编有话音段，工作时间话音占比约 20%），Opus 16kbps 断网兜底约 3.4 小时墙钟。Flash 寿命：Opus 2KB/s 下 4.625MiB 环约 41 分钟绕一圈，8 小时/天约 4400 次扇区擦除/年，NOR 10 万次寿命可用数十年；写原始 PCM 会一年多写秃，故只缓压缩流。

### 4.4 时间同步（M1 必修，优先级最高）

设备没有时钟（证据见 §2），没有时间轴则"按天汇总/跨重启排序/会议对齐日历"全失效。

- `/health` 返回 `server_time_ms`；设备连上网关后先校时，设定墙钟基准，之后靠 C3 单调时钟推算。
- 每次重启/断网重连都重校。
- 每段文件头同时写"墙钟基准 + 相对偏移"两个值；服务端收段时用自己的时间兜底校正并记录 `arrived_at`。
- 出门无 LAN 网关时，用 HK 服务器或公共 NTP 兜底。

### 4.5 上行：会话同步窗口（v2，替代无线电占空比）

- v1 的"无线电占空比"（录音中每 5 分钟开一次无线电传段）已被会话制取代：录音期 WiFi **整体 deinit**，节末同步窗口集中上传。upload 任务为**被动模型**——kick 驱动、不碰无线电；同步窗口/闲时心跳都先把网弄好才 kick。
- 协议：**HTTP 分段上传**（不用 WebSocket）。每段一个请求、段 ID 幂等、可重传；Content-Length 定长流式（1KB 读块）。走 `http://` 明文 + `X-Gateway-Token`，不上 TLS（C3 无 PSRAM，TLS 要几十 KB 连续堆，160MHz 单核握手贵）。
- 突发成本：一节 3.66MB 集中在一节末尾 ~30–60s TX，@130mA ≈ 1.5–2mAh/节；2 节/小时折算 ~3–4mA 平均；常连 WiFi 是 55–80mA。
- 上行地址：设备存 core 的上行 URL。**若 core 在公网 IP 主机（HK/VPS），一个地址即可，工牌随处可传、无局域网限制**——这是公网部署的核心价值。若同一台 core 同时有局域网和公网两个可达地址，设备可优先走局域网省流量、不可达再走公网（同一 core 的两个网络路径，数据不分裂）；这是可选优化，不是必须。配网页面（`setup-gateway.py` 那套表单）填 core 地址即可。

### 4.6 设备侧加密

ESP32-C3 有 AES 硬件加速器。分段在落盘和上传前用 AES 加密，密钥与网关 token 走同一套治理（用户此前已规划密钥治理与轮换）。设备侧加密保证传输与静态存储都是密文；**解密只在 core 主机**（core 在 PC 则明文只在 PC，core 在公网主机则明文只在那台主机）。若 core 在公网主机，密文段经公网传输、到 core 主机才解密分析。

### 4.7 兜底取回

借官方录音笔那套：SoftAP `Folo-Recorder-XXXX` + 内置 HTTP 索引页 + `206 Partial Content` 断点续传 + 在线 Opus→WAV 转码。服务端挂了或工牌没网时，笔记本连热点即可捞录音。

### 4.8 VAD

设备侧只做能量 + 过零率 + 自适应噪声底的粗门限（官方录音笔完全没有 VAD，必须自建）。精细切分放服务端 silero-vad。esp-sr 在 C3 上只有 WakeNet9s 唤醒词可用，AFE/VADNet 需 S3+，不可用。

### 4.9 电源预算（估算，M0 实测）

520mAh 电芯，按典型器件值估：

| 方案 | 平均电流 | 续航 |
| --- | --- | --- |
| A 现状：屏亮 + WiFi 常连 + 持续上行 | 100–140mA | 3.7–5.2h |
| B 只熄屏，WiFi 仍常连 | 70–100mA | 5.2–7.4h |
| C 会话制 v2：录音期 WiFi 整体 deinit，节末同步 | 待测（估 30–50mA） | **10.5–17h** |

方案 C 是 v0.4.27 落地形态：录音 30 分钟全程无无线电（I2S DMA + Opus cpx0 + 写盘），同步窗口 ~1 分钟无线电满载。M0 用换段日志的 CW2017 电量采样实测填表。估算外的固定开销：功放常通（`PA_CTRL=-1`，约 5–10mA，固件关不掉，下一版硬件应接 GPIO）。

## 5. 服务端设计

### 5.1 能力槽位与 provider 抽象

每个能力是一个可插拔槽位，部署时选"本地模型"或"自配 API"：

| 槽位 | 本地实现 | 自配 API | 备注 |
| --- | --- | --- | --- |
| `asr` 转写+时间戳 | sherpa-onnx SenseVoice/Paraformer | **MiniMax `asr-1.0`**（`/v1/speech_to_text`，直吃 opus）、OpenAI 兼容、阿里/腾讯/讯飞 | API 路径=**原始音频出网** |
| `diarization` 说话人分离 | sherpa-onnx / pyannote / 3D-Speaker | MiniMax `verbose_json` 自带 `n_speakers`+`segments[]{speaker,start,end}` | `asr.provider=minimax` 时 diarization=`asr-tags`（零成本复用） |
| `voiceprint` 声纹登记与 1:N | 3D-Speaker CAM++ / sherpa-onnx | **不提供，硬约束 local-only** | 生物识别信息；永远在 core 主机本地跑，匿名 S1/S2→身份映射不出主机 |
| `emotion` 情绪/事件 | SenseVoice 自带标签 | **MiniMax ASR 不提供**；只能 LLM 从文本推断 | 走云时"需要注意的话"精度下降 |
| `summarize` 纪要 | Ollama | **MiniMax `general`**、任意 OpenAI 兼容端点 | 配 `redact` 脱敏 |
| `report` 日报/"需要注意的话" | Ollama | 同上，可独立配 | 最吃模型能力 |
| `embed` 语义检索（可选） | ONNX embedding | API | FTS5 够用时先不做 |
| `calendar` 日历集成（非 AI 能力，出站集成） | — | **飞书 OpenAPI**（读日程做会议对齐 + 回写检测到的日程/待办） | 出站走 §11 校验；凭据 app secret 入 env |

所有适配器实现同一接口：`transcribe(wav) -> [{start,end,text,speaker?,emotion?}]`、`summarize(structured) -> markdown`。`openai-compatible` 一个适配器覆盖 DeepSeek/通义/Kimi/智谱/OpenAI/Groq/vLLM/Ollama 兼容端点；**MiniMax ASR** 端点是 `/v1/speech_to_text`（非 OpenAI 的 `/audio/transcriptions`），单独写一个适配器，但仍在 core 内、零模型依赖；讯飞/腾讯/阿里协议特殊的同样放 `providers/` 按需启用。

**MiniMax 一站式（asr-1.0）要点**：单次 ≤500 秒 / ≤50MB（我们 5 分钟 Opus 段约 600KB，远低于上限，若延长分段须 cap 在 500 秒内）；不支持裸 PCM 流式输入（我们批量上传不受影响）；`verbose_json` 一次给文本+说话人+时间戳，**ASR 与 diarization 合并成一个云调用**；不提供情绪/事件标签。

### 5.2 部署模型：两个正交选择，一套 core

**没有独立的 "hk-cloud / relay" 开发路径。** 只有一套 core + 一组能力适配器；所谓不同形态，只是下面两个正交选择的组合，部署时定，不改代码。

**选择一：core 跑在哪台主机（部署位置）**

- **局域网主机（你的 PC）**：工牌只能在同局域网时上传；外出缓冲到 flash（4.6MB≈40 分钟），回局域网补传。隐私最强（数据不出局域网），但受局域网限制。
- **公网 IP 主机（HK 服务器 / 任意 VPS）**：工牌从任何地方直传，**不受局域网限制——这是 HK 的核心价值**。代价是音频经公网到这台主机。
- 位置只决定"工牌往哪传、数据落谁的机器"，不改变 core 代码。

**选择二：每个能力槽位指向本地模型还是 API**（即 §5.1 的 provider）

- 本地模型：装 `local-asr` / `local-llm` 模块（sherpa-onnx / Ollama + 模型），需 GPU/磁盘，数据不出主机。
- API：用 core 内置适配器（MiniMax / OpenAI 兼容等），零模型依赖，数据出网到该 API。
- 逐槽位独立选。

**组合即形态**（两选择的笛卡尔积，不是独立 profile）：

| 部署位置 | 能力后端 | 体积 | GPU | 数据出网 | 适用 |
| --- | --- | --- | --- | --- | --- |
| 公网 IP 主机 | 全 API（MiniMax） | 几十 MB | 无 | 音频→MiniMax | **多用户默认**：免局域网限制、免模型、HK 零算力 |
| 公网 IP 主机 | 全本地 | ~5–6GB | 需要 | 无 | 既要公网可达又要隐私（需带 GPU 的 VPS，少见） |
| 局域网 PC | 全本地 | ~5–6GB | 需要 | 无 | 隐私最强，接受"仅局域网上传" |
| 局域网 PC | 全 API | 几十 MB | 无 | 音频→MiniMax | PC 当网关、分析走云 |
| 任意 | 混合 | 视组合 | 视组合 | 视组合 | 按槽位细调 |

**模块**（安装单位，按"选择二"装）：

- `core`（必装）：ingest HTTP、SQLite+FTS5、时间轴、worker 框架、Web UI、统一出站校验、**所有 API 适配器**。零模型依赖，几十 MB。API 适配器是 HTTP 调用，走 core 现成 `safe_upstream_fetch()`——"接 API"在安装意义上等于"什么都没多装"。
- `local-asr`（可选）：sherpa-onnx + onnxruntime + 模型，一个模块覆盖 VAD/ASR/分离/情绪/声纹。三变体 `local-asr-cpu` / `-directml`（Windows 免 CUDA） / `-cuda`（Linux/Docker）。
- `local-llm`（可选）：Ollama + LLM 模型（Q4 量化 4GB 量级）。

**声纹归因（简化）**：voiceprint 无 API provider（MiniMax 不做声纹比对），永远是 **core 主机上的本地模型**（sherpa-onnx speaker embedding，CPU 可跑）。"local-only" = 声纹数据只留在 core 主机、绝不发往第三方 API——任何部署位置都自动成立。core 在 HK，声纹就在 HK；core 在 PC，声纹就在 PC。匿名 S1/S2 → 真实身份的映射在 core 主机本地完成，**不需要"拉回 PC"**（之前那套是多此一举）。隐私敏感的用户把 core 装在自己局域网 PC 上，声纹自然不出局域网。

### 5.3 部署时选择机制

**裸机（Windows）**：安装选择器 `install.ps1`（交互式或 flag），对"音频侧"和"LLM 侧"各问"本地 / API / 跳过"：只装选中模块的 pip 包；拉选中模型到 `MODEL_DIR`，写 `models.lock.json` 钉校验和；写 `config.yaml` 把槽位指向已装 provider。可重复运行：`--enable local-asr-directml` 后续把 ASR 从 API 切本地；`--disable local-llm` 切回 API（只改配置）。沿用 v0.3.7"启动脚本内置依赖自检 + 防火墙规则"风格，启动时按 `modules.lock.json` 只检查已装模块。

**Docker**：compose profile 控制装哪些**本地模型 sidecar**（core 镜像恒定不含模型/GPU 运行时）；部署在哪台主机（PC 局域网 / HK 公网）只是"在哪跑 compose"，不是 profile：

```bash
docker compose --profile asr-local --profile llm-local up   # 全本地（core + 两个模型 sidecar）
docker compose --profile asr-local up                       # ASR 本地、LLM 走 API
docker compose up                                            # 全 API（只 core，几十 MB，无 GPU）——公网主机默认
```

不 `--profile asr-local`，那个镜像不拉、模型不下、GPU 不沾。同一份 compose 在 PC 和 HK 上跑，区别只是 `config.yaml` 里 capabilities 指向本地还是 API、以及工牌配的上行地址是局域网还是公网。Windows 上 Docker 跑 GPU 需 WSL2 后端 + NVIDIA 驱动 WSL 支持；不想装 WSL2 则 Docker 退化 CPU（仍可跑，慢 5–10 倍）。裸机用 DirectML 吃 GPU，建议自己那台走裸机。

### 5.4 统一出站校验与环回例外

复用 `gateway/gateway.py` 现成的出站校验为所有自配 API 的统一出口：仅 http/https、解析 host、拒绝 localhost/环回/私有/保留地址、http 钉已校验 IP 防 DNS rebinding、不跟随重定向。

**冲突处理**：本地模型服务（Ollama `127.0.0.1:11434`、本机 FunASR runtime `127.0.0.1:8000`）是环回地址，会被挡。解法区分两类：

- `local-asr` 走**进程内库**（sherpa-onnx 直接 import），不走 HTTP，不碰出站校验。
- `local-llm` 用 Ollama 是本地 HTTP 服务，需要**受控环回例外**：配置显式声明 `local_endpoints`，**只接受 `127.0.0.1`/`::1`**，其他私网段一律拒绝；环回端点同样钉 IP、不跟随重定向；启动时把允许的环回端点打进日志。**只在装了 `local-llm` 模块时启用**，安全面最小。

不要为图省事把 loopback 加进通用白名单。

### 5.5 推理运行时与模型来源

统一 **sherpa-onnx（ONNX Runtime）** 为默认推理层，不装 torch：镜像小、CPU/GPU 同一份代码、GPU 后端可切 CUDA/DirectML/CPU。FunASR 原生 torch 路径保留为"高精度可选后端"，仅 Docker GPU 模式启用，默认不开。

模型来源统一走 **ModelScope**（国内直连，无 HF 门控、无需 token/在线同意条款）：silero-vad（ONNX）、SenseVoiceSmall（逐字稿+情绪/事件）、Paraformer-zh（可选更高逐字稿质量）、3D-Speaker CAM++（分离+声纹）。M3 第一件事验证中文声纹模型在 ONNX 路径精度，不够则接 3D-Speaker torch 后端（仅 Docker GPU）。

### 5.6 隐私边界

隐私由"选择一（core 在哪台主机）× 选择二（哪些槽位走 API）"共同决定，不是独立形态：

| 出网程度 | 组合 | 出网点 | 适用 |
| --- | --- | --- | --- |
| 无 | core 在局域网 PC + 全本地模型 | 无 | 音频/转写/声纹/纪要全不出局域网，需 GPU+模型 |
| 仅文本 | core 任意 + ASR 本地、LLM 走 API（脱敏） | 脱敏要点→LLM API | 逐字稿/声纹不出主机，只送脱敏要点 |
| 含音频 | core 任意 + ASR 走 API（MiniMax） | **原始音频→MiniMax** | 落地最快、免模型；隐私代价最高 |

- `asr` 选 API = **原始音频出网到 MiniMax**，启动日志与 UI 必须红字告警"原始音频将上传到 MiniMax"。这是隐私代价最高的一次选择，必须可见。`summarize` 选 API 且 `redact:true` 时，出网的是脱敏要点而非逐字稿。
- **声纹永不出 core 主机**：voiceprint 无 API provider，永远是 core 主机上的本地模型；匿名 S1/S2→真实身份的映射在 core 主机本地完成。core 在 HK 则声纹在 HK，core 在 PC 则声纹在 PC——隐私敏感用户把 core 装在自己局域网 PC 即可，无需"拉回 PC"那套。
- `emotion` 走云时缺失：MiniMax ASR 不提供情绪/事件标签（SenseVoice 有）。ASR 走 API 时"需要注意的话"只能靠 LLM 从文本推断语气，精度低于本地 SenseVoice 路径——这是用云端省事的代价。
- 现有网关 TTS 走 edge-tts（微软云），是出站但只涉及播报文本；绝对不出网可换本地 piper/kokoro。

### 5.7 存储与目录布局

SQLite + FTS5（Python 自带 sqlite3，零额外依赖；10 人规模够用，不引入 Meilisearch/Elastic）。时间统一存 UTC + 本地时区偏移（校时延伸，否则日报按天切错）。数据放 `DATA_DIR`（建议 E: 或 F: 盘，231GB/187GB 空闲）：

```
DATA_DIR/
  segments/<device_id>/<date>/<seq>.frec.enc     # 设备上传的密文段
  audio/<device_id>/<date>/<seg>.wav             # 解码后（可按需生成）
  transcripts/<device_id>/<date>.jsonl           # 逐字稿：说话人/情绪/时间戳
  meetings/<date>/<meeting_id>.{md,json}         # 会议纪要
  daily/<date>.md                                # 日报
  index.db                                       # SQLite + FTS5
```

存储量：16kbps 连续录 8h ≈ 58MB/人/天，VAD 后（20%）≈ 12MB/人/天；10 人 × 30 天 ≈ 17GB（全录）/ 3.5GB（VAD 后）。

### 5.8 分析流水线（六级，每级可独立替换）

1. **精细切分**：服务端 silero-vad 把 5 分钟段切成语音活动块。远场不干净加 ClearerVoice-Studio 增强/目标说话人提取。
2. **转写 + 说话人**：按 §5.1 的 provider 走——本地路径用 SenseVoiceSmall（带情绪/事件标签，可叠加 Paraformer-zh）+ 3D-Speaker/sherpa-onnx 分离；API 路径用 MiniMax `asr-1.0` `verbose_json` 一次拿文本+匿名说话人+时间戳。
3. **声纹归因**（决定成败）：先登记佩戴者本人声纹 → 每句话标"我说的/谁说的"；3D-Speaker 1:N 辨认常打交道的人。照 Omi 做法：`speaker_identity` 0.45 阈值做登记声纹校验，`speaker_clustering` 短片段聚类（8 质心上限，满了并入最近质心且不污染均值）。API 路径下 MiniMax 只给匿名 S1/S2，身份映射由 core 主机本地声纹模型完成。
4. **会议切分**（自建缺口）：规则先行——同 device_id 下语音块间隔 <3–5 分钟合并成会话，会话 >5 分钟且 ≥2 说话人标会议候选；**接飞书日历读取**当天日程做对齐（Omi `calendar_meetings` 思路）：命中的用会议名，未命中归"临时对话"。纯声学会议识别无成熟开源，靠"日历 + 规则"。
5. **纪要生成**：每会话喂 LLM，结构化输出 = 标题/参与人（声纹归因）/按时间戳章节摘要/关键决策/待办与责任人/风险点。长会话 MAP→REDUCE 两阶段（先章节原子要点再汇总）。参考 meetily、AudioNotes、Amurex。
6. **日报合成**：每日定时任务（Omi `daily_memory_sweep_job` 模式：独立 job + 每小时调度 + 逐账号游标 + 失败重试文档）扫当天已定稿会话，产出：今日工作内容（时间轴 + 按项目/对象双视角聚合）、参与会议清单、**需要注意的话**、工作重点与待办（跨会议去重、按截止时间排序）、缺口标注。
7. **回写飞书日历**：把分析产出的日程/待办写回飞书——检测到的临时会议（日历上没有的）创建为飞书日历事件；带截止时间的行动项创建为飞书任务/日程。出站走 §11 统一校验（飞书 OpenAPI 公网 host），凭据 app secret 入 env。这是用户明确要的"根据分析结果把日程加进飞书日历"。

"需要注意的话"= 声纹归因（不是佩戴者）+ 情绪标签（负面/愤怒）+ LLM 语义判断（是否指向佩戴者、是否含承诺/追责/投诉/考核用语），输出原文+时间戳+说话人+风险等级。**第一版先做"敏感词 + 情绪异常"粗筛**，可验证可解释；准确率有数据再往"精准识别"走。

检索：SQLite FTS5 全文索引，按人/时间/关键词；回放从对象存储取段、服务端转 WAV。

### 5.9 公网 IP 主机部署（HK 等）

core 装在带固定公网 IP 的主机（HK 服务器 / 任意 VPS）时，工牌从任何网络直传，**不受局域网限制**——这是公网部署相对"core 在局域网 PC"的唯一本质区别。能力后端照常逐槽位选：

- **全 API（多用户默认）**：core 只装几十 MB，无 GPU/模型；ASR/LLM 走 MiniMax 等 API。HK 零算力即可承担全量分析。原始音频出网到 API（§5.6 告警）。
- **全本地**：需主机带 GPU + 装模型（~5–6GB），数据不出主机；既要公网可达又要隐私时用，但带 GPU 的 VPS 少见。
- 设备侧加密（§4.6）保证段在传输与静态存储为密文，解密只在 core 主机。

"哑收件箱"（只存不分析、PC 拉回）不再是独立形态——它等价于"core 在公网主机 + 能力全设为 store-only"，是上面全 API/全本地的一个退化配置，无需单独开发路径。

## 6. API 契约（设备 ↔ 服务端）

设备侧只认 HTTP，两种部署暴露同一份：

- `GET /health` → `{ok, server_time_ms, ...}`（校时 + 探活）
- `POST /rec/segment`：headers `X-Gateway-Token` `X-Device-Id` `X-Segment-Id` `X-Start-Ts` `X-End-Ts` `X-Seq` `X-Bitrate` `X-Battery-Soc`；body = 加密 Opus 段 → `{ok, dedup: bool}`
- `GET /rec/state` → `{pending, backlog}`（设备侧可选查询补传队列）

Web UI（复用网关 `/ui/*` 模式）：

- `GET /rec/days`、`GET /rec/day?date=`
- `GET /rec/audio/<id>`（服务端转 WAV，支持 `206`）
- `GET /rec/transcript/<id>`、`GET /rec/daily/<date>.md`

现有 `gateway.py` 路由（`/health` `/workspaces` `/ui/*` `/ask` `/asr` `/tts` `/prompt`）不变，`/rec/*` 为新命名空间，无冲突。`/health` 增加 `server_time_ms` 字段。

## 7. 配置 schema

```yaml
deployment:
  # core 跑在哪台主机由"你把 core 装在哪"决定（局域网 PC 或公网 IP 主机），不是 core 的模式；
  # 工牌上传地址在设备侧配（见 §4.5 双 endpoint：LAN 优先 / 公网兜底）。
  data_dir: ...
  model_dir: ...
  retention_days: 90
  listen: 0.0.0.0:8788
capabilities:
  asr:
    provider: minimax        # local-sherpa|local-funasr|minimax|openai-compatible|aliyun|tencent|iflytek
    local: {runtime: directml, model: sense-voice-zh-en-ja-ko-yue}
    api: {base_url: https://api.minimaxi.com/v1, api_key_env: MINIMAX_API_KEY, model: asr-1.0, response_format: verbose_json, region: cn}  # region: cn|global，base_url 随之不同
    fallback: []             # 只允许同隐私边界内降级
  diarization: {provider: asr-tags}    # asr=minimax 时复用其 speaker 标签；本地路径填 local-sherpa
  voiceprint:  {provider: local-only}  # 硬约束：永远 core 主机本地，不发第三方 API
  emotion:     {provider: llm-infer}   # asr 走云时无情绪标签→LLM 文本推断；本地路径填 asr-tags
  summarize:
    provider: minimax
    api: {base_url: https://api.minimaxi.com/v1, model: general, api_key_env: MINIMAX_API_KEY}
    local: {provider: ollama, model: qwen2.5:7b-instruct-q4}
    redact: true
  report: {provider: same-as-summarize}
integrations:
  calendar:
    provider: feishu          # feishu | none
    read: true                # 读当天日程做会议对齐（§5.8 步骤4）
    write: true               # 回写检测到的日程/待办（§5.8 步骤7）
    app_id_env: FEISHU_APP_ID
    app_secret_env: FEISHU_APP_SECRET
    # 出站走 §11 统一校验（open.feishu.cn 公网 host）
modules: [core]                          # 已装本地模块；全 API 时只有 core
local_endpoints: []                      # 仅装 local-llm(Ollama) 时填 127.0.0.1:11434
```

密钥只写 `api_key_env`（环境变量名），不落配置明文，沿用既有密钥治理与轮换。

**两个典型配置**（同一套 core，只是 deployment 位置与 capabilities 指向不同）：

公网 IP 主机 + 全 API（多用户默认，HK 零算力、免模型、工牌随处可传）：

```yaml
capabilities:
  asr:        {provider: minimax, api: {model: asr-1.0, response_format: verbose_json, region: cn}}
  diarization:{provider: asr-tags}      # 复用 MiniMax 的匿名 speaker 标签
  voiceprint: {provider: local-only}    # 声纹模型在 core 主机本地，永不出主机
  emotion:    {provider: llm-infer}     # MiniMax 无情绪标签，靠 LLM 文本推断
  summarize:  {provider: minimax, api: {model: general}, redact: true}
  report:     {provider: same-as-summarize}
modules: [core]
```

局域网 PC + 全本地（隐私最强，数据不出局域网，需 GPU+模型）：

```yaml
capabilities:
  asr:        {provider: local-sherpa, local: {runtime: directml, model: sense-voice-zh-en-ja-ko-yue}}
  diarization:{provider: local-sherpa}
  voiceprint: {provider: local-only}
  emotion:    {provider: asr-tags}      # SenseVoice 自带情绪标签
  summarize:  {provider: ollama, local: {model: qwen2.5:7b-instruct-q4}, redact: true}
  report:     {provider: same-as-summarize}
modules: [core, local-asr-directml, local-llm]
local_endpoints: ["127.0.0.1:11434"]
```

`diarization=asr-tags` 与 `emotion=llm-infer` 是 ASR 走云时特有的退化：前者复用 ASR 自带的匿名说话人标签（省一个本地模型），后者因 MiniMax 无情绪标签而退化到 LLM 文本推断。本地路径下两者分别是 `local-sherpa` 和 `asr-tags`（SenseVoice 自带情绪）。

## 8. 里程碑与验收标准

| 阶段 | 内容 | 验收 |
| --- | --- | --- |
| **M0**（1–2 天） | 功耗三步实测 + C3 上 Opus 与 WiFi 并发的 CPU 余量 | 续航从估算变实测；Opus 编码不欠载 |
| **M1**（1 周） | 熄屏状态机 + Opus 分段写 FAT + 掉电恢复 + **校时** | 连续录、断电不丢段、段时间戳可信 |
| **M2**（1 周） | 模块化安装框架（core + `modules.lock.json` + `--selftest` 骨架 + provider 接口）+ 无线电占空比 + 设备侧 AES + 双 endpoint（LAN 优先/公网兜底）+ 网关 ingest 路由 + 本地数据目录 | 同一套 core 在裸机 Windows 与 Docker CPU 都能收段、落盘、跑通 selftest；core 装在公网 IP 主机时工牌可从非局域网直传 |
| **M3**（1–2 周） | **MiniMax 适配器优先**（`asr-1.0` verbose_json + `general` LLM）跑通"公网主机 + 全 API"端到端：段→core→MiniMax→带匿名说话人的逐字稿+纪要；`local-asr` 降为可选隐私加固件 | 全 API 形态出一份带匿名说话人+纪要的逐字稿；MiniMax ASR 单次 ≤500s/50MB 已对齐 5 分钟分段 |
| **M4**（1 周） | 声纹登记（先只登佩戴者，core 主机本地）+ 匿名 S1/S2→身份映射（core 主机本地）+ 会议切分规则 + **飞书日历双向**（读当天日程对齐会议；回写检测到的临时会议/带截止行动项为飞书事件/任务） | 会议自动成篇、说话人可识别、分析产出回写飞书日历 |
| **M5**（1 周） | 纪要 + 日报（含"需要注意的话"粗筛；ASR 走云时情绪靠 LLM 文本推断）+ redact | 每日那份记录 |
| **M6** | 检索/回看 UI + 缺口标注 + 录音时段可查记录 | 可交付形态 |

M0–M2 是设备和链路，风险在功耗、CPU、校时；M3 起全在服务端，风险在声纹归因准确率。两段可并行。M2 必须同时立裸机与 Docker 骨架，别等 M6 补。

## 9. 参考项目清单（GitHub，截至 2026-09-17）

**硬件/固件**：[BasedHardware/omi](https://github.com/BasedHardware/omi)（13.5k★，nRF/Zephyr，后端管线可抄、固件不可搬）、[78/xiaozhi-esp32](https://github.com/78/xiaozhi-esp32)（30k★，ESP32 Opus over WebSocket，`es8311_audio_codec.cc` 同款 codec）、[xinnan-tech/xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server)（10.6k★）、espressif/esp_audio_codec（C3 定点 Opus）、espressif/esp-sr（C3 仅 WakeNet9s，AFE/VADNet 需 S3+）。

**转写/分离**：[modelscope/FunASR](https://github.com/modelscope/FunASR)（20.4k★）、[QwenAudio/SenseVoice](https://github.com/QwenAudio/SenseVoice)（9.3k★，情绪+事件标签）、[k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)（14.8k★，统一 ONNX）、[m-bain/whisperX](https://github.com/m-bain/whisperX)（24k★）、[collabora/WhisperLive](https://github.com/collabora/WhisperLive)、[QuentinFuxa/WhisperLiveKit](https://github.com/QuentinFuxa/WhisperLiveKit)（11k★）、[pyannote/pyannote-audio](https://github.com/pyannote/pyannote-audio)（10.6k★）、[modelscope/3D-Speaker](https://github.com/modelscope/3D-Speaker)（3.1k★，CAM++ 中文 20 万说话人）、[FireRedTeam/FireRedVAD](https://github.com/FireRedTeam/FireRedVAD)、[modelscope/ClearerVoice-Studio](https://github.com/modelscope/ClearerVoice-Studio)（4.5k★）。

**纪要/日报/检索**：[Zackriya-Solutions/meetily](https://github.com/Zackriya-Solutions/meetily)（30.9k★）、[thepersonalaicompany/amurex](https://github.com/thepersonalaicompany/amurex)（2.9k★）、[harry0703/AudioNotes](https://github.com/harry0703/AudioNotes)（2.5k★）、[transcriptionstream/transcriptionstream](https://github.com/transcriptionstream/transcriptionstream)（947★，一体化离线 diarization+LLM 总结骨架）、[screenpipe/screenpipe](https://github.com/screenpipe/screenpipe)（21.6k★，连续采集可检索）。Omi 后端的 `daily_summaries`/`daily_memory_sweep_job`/`calendar_meetings`/`conversation_finalization`/`advice`/`speaker_identity`（0.45 阈值）直接照抄语义。

## 10. 风险与待确认事项

1. **C3 上 Opus + WiFi 持续 + LVGL 并发 CPU 余量**：官方录音笔是离线录音（无 WiFi），xiaozhi 的 C3 板验证过 Opus+WiFi 但无大屏。M0 实测。熄屏后 LVGL 挂起，组合退化为 Opus+WiFi，风险显著降低。
2. **中文声纹 ONNX 精度**：sherpa-onnx 有 diarization/identification，但中文 CAM++ 的 ONNX 版精度未实测。M3 第一件事确认，不够接 3D-Speaker torch 后端（仅 Docker GPU）。
3. **续航 8.7–11.5h 为估算**：必须 M0 实测；功放常通那笔（5–10mA）是估算外固定开销，下一版硬件接 GPIO。
4. **会议识别纯规则 + 日历**：纯声学无成熟开源，靠"日历 + 规则"覆盖大部分。
5. **"需要注意的话"先粗筛**：别一上来承诺"精准识别针对你的话"。
6. **MiniMax ASR 单次 ≤500 秒**：5 分钟分段设计已安全（300s）；若未来延长分段或 VAD 合并长段，必须在调用前 cap 到 ≤500s，超出返回 400 不截断。
7. **ASR 走云时情绪精度下降**：MiniMax ASR 不提供情绪/事件标签，"需要注意的话"只能靠 LLM 从文本推断语气，弱于本地 SenseVoice 的音频情绪信号。
8. **MiniMax 成本与额度**：8h/人/天 ÷ 500s ≈ 57 次 ASR/人/天 + LLM tokens；全 API 部署前先核套餐额度，按人数×天数估月成本。

**已确认决策（2026-09-17）**：

- **上行协议**：HTTP 分段上传（不用 WebSocket）。每段一个请求、文件名即段 ID、幂等可重传；续航走方案 C（8.7–11.5h 估算）。
- **日历源**：飞书日历，且**双向**——读取当天日程做会议对齐（§5.8 步骤 4）；分析产出的日程/待办**回写飞书日历**（检测到的临时会议、带截止时间的行动项 → 创建飞书日历事件/任务）。回写是出站请求，走 §11 的统一出站校验（飞书 OpenAPI 是公网 host）。
- **MiniMax 区域**：cn。`mmx config show` 实测 region=cn 解析到 `base_url=https://api.minimaxi.com`，适配器直接用此 host + 现有 `MINIMAX_API_KEY`（与 mmx 同一条凭据），ASR 端点 `POST /v1/speech_to_text`。
- **试点规模**：仅用户本人。单用户大幅简化：无多租户、存储量可忽略（一人一天 VAD 后约 12MB）、声纹只需区分"佩戴者本人 vs 他人"、diarization 算力无压力。多用户能力（租户隔离、配额）留作后续，不在 M0–M6 范围。

## 11. 实现验收条件（安全/合规）

- 服务端任何主动外发请求（飞书日历读写、MiniMax ASR/LLM、webhook 回调、自配 API 调用）只允许 http/https，发请求前校验 host，拒绝 localhost、环回、私有和保留地址；http 上游钉已校验 IP 直连防 DNS rebinding，不跟随重定向。复用 `gateway/gateway.py` 现成实现。飞书 OpenAPI（open.feishu.cn）与 MiniMax（api.minimaxi.com）均为公网 host，过校验无阻。
- 环回受控例外仅用于本地模型服务（`local-llm` 的 Ollama），只接受 `127.0.0.1`/`::1`，其他私网段一律拒绝；启动时打印允许的环回端点。
- 设备侧不收集用户无关数据；录音时段与设备 ID 在服务端保留可查记录，用于合规举证。
- 密钥只从环境变量/NVS 读（`MINIMAX_API_KEY`、飞书 app secret 等），不落配置明文；沿用既有密钥治理与轮换流程。
