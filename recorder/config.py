# recorder/config.py —— 配置加载与校验。
#
# 约定：
# - 密钥优先通过 *_env 填"环境变量名"运行时取值；config.yaml 里不允许出现明文密钥。
#   WebUI 配置页填入的密钥存本机 DB（slot_overrides.api_json 的 api_key 字段，
#   与录音/声纹数据同权限类），api_key() 取值顺序：环境变量 > DB 内联。
# - provider 取值属于有限集合（见 VALID_PROVIDERS）；diarization/voiceprint/emotion 有
#   组合约束（voiceprint 硬约束 local-only，代码层拒绝其它取值）。
# - 校验失败直接抛 ConfigError，宁可启动失败也不要带病运行。

import json
import os
from dataclasses import dataclass, field

import yaml


class ConfigError(Exception):
    pass


VALID_PROVIDERS = {
    "asr": {"local-sherpa", "local-funasr", "minimax", "sherpa", "openai-compatible",
            "aliyun", "tencent", "iflytek"},
    "diarization": {"local-sherpa", "asr-tags", "none"},
    # voiceprint 是硬约束：无 API provider，永远 core 主机本地（design §5.1/§5.6）
    "voiceprint": {"local-only"},
    "emotion": {"asr-tags", "llm-infer"},
    "summarize": {"minimax", "openai-compatible", "ollama", "same-as-summarize"},
    "report": {"minimax", "openai-compatible", "ollama", "same-as-summarize"},
}


@dataclass
class CapabilityCfg:
    slot: str
    provider: str
    local: dict = field(default_factory=dict)
    api: dict = field(default_factory=dict)
    fallback: list = field(default_factory=list)
    # summarize/report 专用：出网脱敏（design §5.6）。redact 默认 True（设计文档默认混合脱敏）；
    # redact_names = {人名: 角色标签}，在 LLM 适配器出网前替换。report 用 same-as-summarize 时继承。
    redact: bool = True
    redact_names: dict = field(default_factory=dict)


@dataclass
class CalendarCfg:
    provider: str = "none"
    read: bool = False
    write: bool = False
    app_id_env: str = ""
    app_secret_env: str = ""


@dataclass
class AppConfig:
    data_dir: str
    model_dir: str
    retention_days: int
    listen_host: str
    listen_port: int
    token: str
    capabilities: dict          # slot -> CapabilityCfg
    calendar: CalendarCfg
    modules: list
    local_endpoints: list
    raw: dict = field(default_factory=dict, repr=False)
    config_path: str = ""      # 来源 yaml 路径（WebUI 配置页回写用）

    @property
    def db_path(self) -> str:
        """index.db 完整路径的唯一拼接点（各模块一律用 app.db_path，不自拼）。"""
        return os.path.join(self.data_dir.rstrip("/\\"), "index.db")


def _parse_listen(listen):
    """'0.0.0.0:8788' -> ('0.0.0.0', 8788)。缺端口用默认 8788。"""
    if not listen:
        return "0.0.0.0", 8788
    if ":" not in listen:
        return "0.0.0.0", int(listen)
    host, _, port = listen.rpartition(":")
    host = host.strip("[]") or "0.0.0.0"        # 允许 [::1]:8788 形式
    return host, int(port)


def _load_capability(slot: str, body: dict, resolved: set) -> CapabilityCfg:
    if not isinstance(body, dict) or "provider" not in body:
        raise ConfigError("capabilities.%s 缺少 provider" % slot)
    provider = body["provider"]
    valid = VALID_PROVIDERS.get(slot)
    if valid is None or provider not in valid:
        raise ConfigError("capabilities.%s.provider=%r 不在允许集合 %s" % (slot, provider, valid))
    # same-as-* 引用其它槽位时按后者校验（先解析被引用者）
    if provider == "same-as-summarize" and slot != "summarize":
        if "summarize" not in resolved:
            raise ConfigError("capabilities.%s 引用 summarize，但 summarize 尚未解析" % slot)
    return CapabilityCfg(
        slot=slot,
        provider=provider,
        local=body.get("local") or {},
        api=body.get("api") or {},
        fallback=body.get("fallback") or [],
        redact=bool(body.get("redact", True)),
        redact_names=body.get("redact_names") or {},
    )


def load_config(path: str) -> AppConfig:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ConfigError("配置根节点必须是 mapping")

    dep = raw.get("deployment") or {}
    data_dir = dep.get("data_dir") or ""
    if not data_dir:
        raise ConfigError("deployment.data_dir 必填（音频/逐字稿/日报都落这里）")
    host, port = _parse_listen(dep.get("listen"))
    token = dep.get("token") or ""
    if not token:
        # ingest 对外监听却无鉴权令牌 = 任何拿到地址的设备都能塞录音，直接拒启动。
        raise ConfigError("deployment.token 必填（设备上传 X-Gateway-Token）")

    caps_raw = raw.get("capabilities") or {}
    if not caps_raw:
        raise ConfigError("缺少 capabilities 段")
    caps: dict = {}
    resolved: set = set()
    # 顺序解析，保证 same-as-summarize 引用先就绪
    for slot in ("asr", "diarization", "voiceprint", "emotion", "summarize", "report"):
        body = caps_raw.get(slot)
        if body is None:
            raise ConfigError("capabilities.%s 缺失" % slot)
        caps[slot] = _load_capability(slot, body, resolved)
        resolved.add(slot)

    cal_raw = (raw.get("integrations") or {}).get("calendar") or {}
    calendar = CalendarCfg(
        provider=cal_raw.get("provider") or "none",
        read=bool(cal_raw.get("read")),
        write=bool(cal_raw.get("write")),
        app_id_env=cal_raw.get("app_id_env") or "",
        app_secret_env=cal_raw.get("app_secret_env") or "",
    )
    if calendar.provider not in ("feishu", "none"):
        raise ConfigError("integrations.calendar.provider 仅支持 feishu|none")
    if calendar.provider == "feishu" and not (calendar.app_id_env and calendar.app_secret_env):
        raise ConfigError("calendar=feishu 需要 app_id_env / app_secret_env")

    local_endpoints = raw.get("local_endpoints") or []
    # 环回受控例外：只接受 127.0.0.1 / ::1，其它一律拒绝（design §5.4）
    import ipaddress
    for ep in local_endpoints:
        host_part = ep.rsplit(":", 1)[0].strip("[]")
        try:
            ip = ipaddress.ip_address(host_part)
        except ValueError:
            raise ConfigError("local_endpoints 仅接受 IP:端口形式，收到 %r" % ep)
        if not ip.is_loopback:
            raise ConfigError("local_endpoints 仅接受环回地址（127.0.0.1/::1），收到 %r" % ep)

    app = AppConfig(
        data_dir=data_dir,
        model_dir=dep.get("model_dir") or "",
        retention_days=int(dep.get("retention_days") or 90),
        listen_host=host,
        listen_port=port,
        token=token,
        capabilities=caps,
        calendar=calendar,
        modules=raw.get("modules") or ["core"],
        local_endpoints=local_endpoints,
        raw=raw,
        config_path=path,
    )

    # WebUI 配置页的槽位覆盖（存 index.db，优先级高于 yaml）：每个进程启动时读，
    # serve/CLI 无需重启互相感知。表不存在（首跑）或 DB 不可读时退回 yaml 值。
    import sqlite3
    try:
        with sqlite3.connect(app.db_path) as db:
            rows = db.execute(
                "SELECT slot, provider FROM provider_overrides").fetchall()
        for slot, provider in rows:
            if slot in caps and provider in VALID_PROVIDERS.get(slot, set()):
                caps[slot].provider = provider
    except sqlite3.Error:
        pass
    # v3 配置控制台：整槽覆盖（provider + api + local + redact）
    try:
        with sqlite3.connect(app.db_path) as db:
            rows = db.execute(
                "SELECT slot, provider, api_json, local_json, redact"
                " FROM slot_overrides").fetchall()
        for slot, provider, api_json, local_json, redact in rows:
            if slot not in caps:
                continue
            if provider and provider in VALID_PROVIDERS.get(slot, set()):
                caps[slot].provider = provider
            if api_json:
                caps[slot].api.update(json.loads(api_json))
            if local_json:
                caps[slot].local.update(json.loads(local_json))
            if redact is not None:
                caps[slot].redact = bool(redact)
    except sqlite3.Error:
        pass
    return app


def api_key(cfg: CapabilityCfg, env_name: str = "api_key_env") -> str:
    """取实际密钥值：环境变量（*_env 约定）优先，其次 WebUI 存的本机 DB 内联值。
    缺失返回空串。"""
    var = cfg.api.get(env_name) or ""
    if var:
        v = os.environ.get(var, "")
        if v:
            return v
    return cfg.api.get("api_key") or ""
