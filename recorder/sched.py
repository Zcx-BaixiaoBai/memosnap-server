# recorder/sched.py —— 服务内自动分析调度。
#
# 背景：分析链路原是 CLI 三命令（process/meetings/report）手动驱动；serve 部署后
# 只收录音不分析（用户反馈"只能看到录音"）。本模块在 serve 进程内起常驻线程：
#   - 每 PROCESS_EVERY 秒扫转写 status='stored' 段（幂等；失败段下轮重试；
#     date=None 全日期扫，覆盖脱网补传/迟到段）；
#   - 本轮有新转写（或首次启动）时重建 今天+昨天 的会话/纪要/日报。
# 成本说明：meetings 会重建当日全部会话纪要（LLM），触发频率=新段到达频率
# （单用户试点可接受）；LLM 不可用时仅切分且保留旧纪要（COALESCE 语义）。

import datetime
import threading
import time

PROCESS_EVERY = 600          # 转写扫描间隔（秒）


def _day(offset: int) -> str:
    return (datetime.date.today() + datetime.timedelta(days=offset)).isoformat()


def run_cycle(app) -> int:
    """一轮扫转写；返回本轮新转写段数。ASR 不可用抛异常由调用方兜底。"""
    from . import worker
    st = worker.process(app)
    return int((st or {}).get("processed", 0))


def run_deep(app):
    """会话切分+纪要+日报（今天+昨天，覆盖跨天补传）。逐阶段独立兜底。"""
    from . import meetings, report
    for off in (0, -1):
        d = _day(off)
        try:
            meetings.build(app, date=d)
        except Exception as e:
            print("[sched] meetings %s 失败：%s" % (d, e), flush=True)
        try:
            report.build(app, date=d)
        except Exception as e:
            print("[sched] report %s 失败：%s" % (d, e), flush=True)


def _loop(app):
    first = True
    last_vp_sig = None
    while True:
        try:
            n = run_cycle(app)
        except Exception as e:
            print("[sched] process 失败：%s" % e, flush=True)
            n = 0
        if first or n > 0:
            run_deep(app)
            first = False
        # 声纹库变化（入档/改名/删除）→ 历史段重归因 + 重建纪要/日报
        try:
            from .ingest import SegmentStore
            from . import meetings, report, worker
            sig = SegmentStore(app.db_path).voiceprint_signature()
        except Exception as e:
            print("[sched] 声纹签名读取失败：%s" % e, flush=True)
            sig = None
        if sig and sig[0] > 0 and sig != last_vp_sig:
            # 首轮只记基线（重启不空跑全量重归因）；运行期变化才触发
            if last_vp_sig is not None:
                print("[sched] 声纹库变化 %s → 历史段重归因" % (sig,), flush=True)
                try:
                    touched = worker.reannotate(app)
                    for day in sorted({d for _d, d in touched}):
                        try:
                            meetings.build(app, date=day)
                            report.build(app, date=day)
                        except Exception as e:
                            print("[sched] 重建 %s 失败：%s" % (day, e), flush=True)
                except Exception as e:
                    print("[sched] 重归因失败：%s" % e, flush=True)
        last_vp_sig = sig
        time.sleep(PROCESS_EVERY)


def start(app):
    t = threading.Thread(target=_loop, args=(app,),
                         name="analysis-sched", daemon=True)
    t.start()
    print("[sched] 自动分析已启动：每 %ds 扫转写，有新内容自动纪要/日报"
          % PROCESS_EVERY, flush=True)
