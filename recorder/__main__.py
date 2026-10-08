# recorder/__main__.py —— CLI 入口。
#
#   python -m recorder serve       --config config.yaml        起 ingest 服务
#   python -m recorder selftest    --config config.yaml        部署能力报告
#   python -m recorder ingest-file --config config.yaml \
#          --device <id> --file <bin> [--seq N]                模拟设备传一段（不走 HTTP，直接入库）
#   python -m recorder process     --config config.yaml \
#          [--date YYYY-MM-DD] [--device <id>]                转写待处理段并重建当日逐字稿（M3）
#   python -m recorder meetings    --config config.yaml \
#          [--date] [--device] [--min-sec N] [--min-speakers N] [--skip-llm] [--show-only]
#                                                                切会话+生成纪要（M4/M5）；--show-only 只打印
#   python -m recorder report      --config config.yaml [--date] [--device] [--show-only]
#                                                                合成当日日报（M5）；--show-only 只打印
#   python -m recorder dump        --config config.yaml \
#          [--date] [--device]                                  打印逐字稿（调试）
#
import argparse
import os
import time

from .config import load_config
from .ingest import IngestServer, SegmentStore


def main():
    ap = argparse.ArgumentParser(prog="recorder", description="工牌录音服务端 core")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("serve", "selftest", "ingest-file", "process", "meetings", "report",
                 "search", "timeline", "enroll", "voiceprints", "unenroll", "dump"):
        p = sub.add_parser(name)
        p.add_argument("--config", default="config.yaml")
        if name == "ingest-file":
            p.add_argument("--device", required=True, help="模拟设备 ID（[A-Za-z0-9_-]）")
            p.add_argument("--file", required=True, help="段文件（加密或任意字节，原样入库）")
            p.add_argument("--seq", type=int, default=0)
            p.add_argument("--start-ts", type=int, default=0, help="段起始墙钟毫秒；0=现在")
        if name in ("process", "dump", "meetings", "report", "search", "timeline"):
            p.add_argument("--date", default=None, help="YYYY-MM-DD；缺省=全部")
            p.add_argument("--device", default=None, help="设备 ID；缺省=全部")
        if name == "search":
            p.add_argument("--query", required=True, help="检索词（按空白拆词，多词 AND）")
            p.add_argument("--limit", type=int, default=50)
        if name == "enroll":
            p.add_argument("--identity", required=True, help="身份名（如 self / 工程部主管）")
            p.add_argument("--file", required=True, help="登记音频（ogg/opus/wav，≥1.5s，建议 5s+）")
        if name == "unenroll":
            p.add_argument("--identity", required=True, help="要删除的身份名")
        if name in ("meetings", "report"):
            p.add_argument("--show-only", action="store_true", help="只打印不重建")
        if name == "meetings":
            p.add_argument("--min-sec", type=int, default=300,
                           help="会议候选的最小会话时长（秒）")
            p.add_argument("--min-speakers", type=int, default=2,
                           help="会议候选的最小说话人数")
            p.add_argument("--chunk-chars", type=int, default=6000,
                           help="对话超过此字符数走 MAP→REDUCE 分章节")
            p.add_argument("--skip-llm", action="store_true", help="只切分不生成纪要")
    args = ap.parse_args()

    if args.cmd == "selftest":
        from .selftest import run
        raise SystemExit(run(args.config))

    app = load_config(args.config)

    # 函数级统一导入（后续各分支共用；避免分支内局部 import 遮蔽函数作用域）
    from .ingest import SegmentStore

    if args.cmd == "serve":
        IngestServer(app).serve_forever()

    elif args.cmd == "process":
        from .worker import process
        stats = process(app, date=args.date, device=args.device)
        print("done: %s" % stats)

    elif args.cmd == "meetings":
        from . import meetings
        if not args.show_only:
            stats = meetings.build(app, date=args.date, device=args.device,
                                   min_sec=args.min_sec, min_speakers=args.min_speakers,
                                   skip_llm=args.skip_llm, chunk_chars=args.chunk_chars)
            print("done: %s" % stats)
        meetings.show(app, date=args.date, device=args.device)

    elif args.cmd == "report":
        from . import report
        if not args.show_only:
            stats = report.build(app, date=args.date, device=args.device)
            print("done: %s" % stats)
        report.show(app, date=args.date, device=args.device)

    elif args.cmd == "search":
        import time as _t
        from .ingest import SegmentStore
        store = SegmentStore(app.db_path)
        n = store.rebuild_fts()          # 单用户数据量小，先重建保证与最新逐字稿一致
        rows = store.search_utterances(args.query, date=args.date,
                                       device=args.device, limit=args.limit)
        print("[search] 索引 %d 天，命中 %d 条" % (n, len(rows)))
        for r in rows:
            print("  %s %s [%s] %s：%s" % (r["local_date"],
                                            _t.strftime("%H:%M:%S", _t.localtime(r["start_ts"] / 1000)),
                                            r["device_id"], r["speaker"], r["text"]))

    elif args.cmd == "enroll":
        # M4 声纹登记（local-only）：登记库在 index.db 的 voiceprints 表
        from .ingest import SegmentStore
        from .providers import build_providers
        store = SegmentStore(app.db_path)
        vp = build_providers(app, store=store)["voiceprint"]
        if not getattr(vp, "available", False):
            raise SystemExit("声纹不可用：%s" % getattr(vp, "reason", "未知"))
        with open(args.file, "rb") as f:
            body = f.read()
        if not vp.enroll(args.identity, body):
            raise SystemExit("登记失败：音频太短（需 ≥1.5s，建议 5s+）或解码失败")
        print("已登记声纹：%s（%dB 音频）" % (args.identity, len(body)))
        rows = vp.enrolled()
        for r in rows:
            print("  - %s（%d 维，登记于 %s）" %
                  (r["identity"], r["dim"], time.strftime("%Y-%m-%d %H:%M",
                                                           time.localtime(r["created_at"] / 1000))))

    elif args.cmd == "voiceprints":
        from .ingest import SegmentStore
        store = SegmentStore(app.db_path)
        rows = store.list_voiceprints()
        if not rows:
            print("声纹库为空（enroll 登记后，process 会自动把匿名 S1/S2 映射成身份）")
        for r in rows:
            print("  - %s（%d 维，登记于 %s）" %
                  (r["identity"], r["dim"], time.strftime("%Y-%m-%d %H:%M",
                                                           time.localtime(r["created_at"] / 1000))))

    elif args.cmd == "unenroll":
        from .ingest import SegmentStore
        from .providers import build_providers
        store = SegmentStore(app.db_path)
        vp = build_providers(app, store=store)["voiceprint"]
        if not vp.remove(args.identity):
            raise SystemExit("未找到身份：%s" % args.identity)
        print("已删除声纹：%s" % args.identity)

    elif args.cmd == "timeline":
        # 录音时段可查记录（合规）：何时录了多久，基于 segments 表，不依赖转写进度
        import time as _t
        from .ingest import SegmentStore
        store = SegmentStore(app.db_path)
        for d in store.days():
            if args.date and d["local_date"] != args.date:
                continue
            for dev in store.day_devices(d["local_date"]):
                if args.device and dev["device_id"] != args.device:
                    continue
                tl = store.recording_timeline(dev["device_id"], d["local_date"])
                print("=== %s %s：共 %d 段，合计 %s" %
                      (dev["device_id"], d["local_date"], tl["segment_count"],
                       _t.strftime('%H:%M:%S', _t.gmtime(tl["recorded_sec"]))))
                for p in tl["periods"]:
                    print("  录音 %s → %s（%d 分钟）" %
                          (_t.strftime("%H:%M", _t.localtime(p["start_ts"] / 1000)),
                           _t.strftime("%H:%M", _t.localtime(p["end_ts"] / 1000)),
                           round((p["end_ts"] - p["start_ts"]) / 60000)))
                for g in tl["gaps"]:
                    print("  缺口 %s → %s（%d 分钟）" %
                          (_t.strftime("%H:%M", _t.localtime(g["start_ts"] / 1000)),
                           _t.strftime("%H:%M", _t.localtime(g["end_ts"] / 1000)),
                           round((g["end_ts"] - g["start_ts"]) / 60000)))

    elif args.cmd == "dump":
        from .worker import dump
        dump(app, date=args.date, device=args.device)

    elif args.cmd == "ingest-file":
        with open(args.file, "rb") as f:
            body = f.read()
        start = args.start_ts or int(time.time() * 1000)
        store = SegmentStore(app.db_path)
        dedup = store.put(args.device, os.path.splitext(os.path.basename(args.file))[0],
                          start, start + 300_000, args.seq, 16000, body)
        print("ingested: device=%s file=%s bytes=%d dedup=%s" %
              (args.device, args.file, len(body), dedup))
        print("stored segments: %d" % store.count_stored())


if __name__ == "__main__":
    main()
