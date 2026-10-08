# recorder/selftest.py —— 部署能力报告（design §5.6 的"哪些能力会出网"的唯一依据）。
#
# 只测**已装模块**（modules.lock.json 的职责，M2 先读 config.modules）；
# 未装槽位报"未实现/未配置"，不报错。配置非法才非零退出。

import os
import sys
import tempfile

from .config import AppConfig, api_key, load_config
from .providers import build_providers

_SLOT_ORDER = ("asr", "diarization", "voiceprint", "emotion", "summarize", "report", "calendar")


def run(config_path: str) -> int:
    try:
        app: AppConfig = load_config(config_path)
    except Exception as e:
        print("[selftest] 配置加载失败：%s" % e)
        return 2

    print("[selftest] config=%s  modules=%s" % (config_path, app.modules))
    print("[selftest] deployment: data_dir=%s listen=%s:%d" %
          (app.data_dir, app.listen_host, app.listen_port))

    # 数据目录可写
    try:
        os.makedirs(app.data_dir, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=app.data_dir, delete=True) as f:
            f.write(b"ok")
        print("[selftest] data_dir 可写 ✓")
    except OSError as e:
        print("[selftest] data_dir 不可写：%s" % e)
        return 2

    providers = build_providers(app)
    # 声纹登记库在 index.db——传 store 让 selftest 能真实探测模型可用性
    from .ingest import SegmentStore
    providers["voiceprint"] = build_providers(app, store=SegmentStore(app.db_path))["voiceprint"]
    print("\n%-14s %-22s %-9s %-9s %s" % ("槽位", "provider", "出网", "可用", "说明"))
    print("-" * 88)
    egress_slots = []
    for slot in _SLOT_ORDER:
        p = providers.get(slot)
        if p is None:
            print("%-14s %-22s %-9s %-9s %s" % (slot, "-", "-", "-", "未配置"))
            continue
        egress = bool(getattr(p, "egress", False))
        if egress:
            egress_slots.append(slot)
        avail = getattr(p, "available", None)
        avail_str = "?" if avail is None else ("✓" if avail else "✗")
        note = ""
        if not avail:
            note = getattr(p, "reason", "")
        if slot == "asr" and not avail:
            _key = getattr(p, "_key", None)
            if isinstance(_key, str) and not _key:
                note = "环境变量 %s 未设置" % (app.capabilities["asr"].api.get("api_key_env") or "?")
            elif hasattr(p, "_err") and p._err:
                note = p._err
        print("%-14s %-22s %-9s %-9s %s" % (slot, getattr(p, "name", "?"),
                                            "是" if egress else "否", avail_str, note))

    # 密钥检查（只报在/不在，不打印值）
    key_env = app.capabilities["asr"].api.get("api_key_env")
    if key_env:
        print("\n[selftest] %s: %s" % (key_env, "已设置 ✓" if os.environ.get(key_env) else "未设置 ✗"))

    print("\n[selftest] 出网槽位：%s" % ("、".join(egress_slots) if egress_slots else "无"))
    if "asr" in egress_slots:
        print("[selftest] ⚠ asr 走 API = 原始音频将上传到 %s" %
              app.capabilities["asr"].api.get("base_url", "?"))
    return 0
