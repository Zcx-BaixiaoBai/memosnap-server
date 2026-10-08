# recorder/webui.py —— 回看 Web UI（v3：底部 Dock 导航 + 动效 + 配置控制台）。
#
# 页面（挂在 ingest 服务的 /rec/ui* 路由，鉴权同 /rec/*，支持 cookie）：
#   GET  /rec/ui                      日期列表（每天→各设备）
#   GET  /rec/ui/day?date=YYYY-MM-DD  当天回看：录音时段 + 逐字稿 + 会话纪要 + 日报
#   GET  /rec/ui/search?q=&device=    检索（FTS5）
#   GET  /rec/ui/voiceprints          声纹管理：入档（上传音频+身份名）+ 命名 + 删除
#   GET  /rec/ui/settings             配置控制台：槽位 provider/api/模型/密钥/阈值
#   POST /rec/ui/voiceprints/*        声纹入档/自动入档/改名/删除
#   POST /rec/ui/conversations/rename 会议重命名
#   POST /rec/ui/speakers/override    说话人手动归因纠正
#   POST /rec/ui/settings             配置保存（provider/api 覆盖存 DB；密钥写 env 文件）
#
# 视觉：纯黑 + 品牌橙（对齐工牌固件 v2）；底部苹果 Dock 式导航（SVG 图标、
# 悬浮上浮+tooltip、当前页图标下小横条）；卡片入场/悬浮动效。
# 安全：所有动态内容一律 html.escape；date/device/identity 先过正则；
# 模板为纯字符串拼接，无文件路径、无 SQL 拼接。

import html
import json
import re
import threading
import time
from urllib.parse import urlparse

from .ingest import SegmentStore

# 请求级导航上下文：ingest 在每个 UI GET 前写入 Referer 推导的"上一页 Dock 序号"，
# 供 _page 决定整页滑入方向与横线同步起点（IAB 等原生导航环境下 pjax 无效时的体验保障）。
_NAV = threading.local()


def set_nav_ctx(referer: str):
    idx = None
    if referer:
        try:
            p = urlparse(referer).path
        except ValueError:
            p = ""
        idx = _idx_of_path(p)
    _NAV.idx = idx


def _idx_of_path(path: str):
    if not path:
        return None
    if path == "/rec/ui" or path.startswith("/rec/ui/day"):
        return 0
    if path == "/rec/ui/search":
        return 1
    if path == "/rec/ui/voiceprints":
        return 2
    if path == "/rec/ui/settings":
        return 3
    return None

_SAFE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_NAME = re.compile(r"^[\w\u4e00-\u9fff·\- ]{1,32}$")

# ---- 设计系统（v3）----
_STYLE = """
:root{--bg:#0B0B0C;--card:#17181A;--card2:#1D1F22;--line:#2A2C2E;--accent:#E96A2E;
--accent-soft:rgba(233,106,46,.12);--text:#E8EAEA;--sub:#9BA0A3;--faint:#6E7C8C;
--ok:#4ADE80;--warn:#FBBF24;--err:#F85149;--r:14px}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{font-family:system-ui,-apple-system,'Microsoft YaHei',sans-serif;margin:0;
background:var(--bg);color:var(--text);line-height:1.65;font-size:14px;
padding-bottom:110px}
.stage{width:100%}
.wrap{max-width:960px;margin:0 auto;padding:20px 18px 40px}
.wrap.swap-in{animation:panin var(--pan-dur) var(--pan-ease) both}
.wrap.swap-in-rev{animation:paninrev var(--pan-dur) var(--pan-ease) both}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
h1{font-size:24px;margin:6px 0 18px;font-weight:700;letter-spacing:.5px;
display:flex;align-items:center;gap:10px}
h1::before{content:"";width:4px;height:22px;border-radius:2px;background:var(--accent)}
h2{font-size:17px;margin:26px 0 10px;font-weight:600}
h3{font-size:14px;margin:18px 0 8px;font-weight:600;color:var(--sub)}
h4{font-size:13px;margin:12px 0 4px;font-weight:600;color:var(--sub)}
/* 整页横移（Stage Manager 式）：页面是一张面板，切换=旧面板移出屏+新面板移入屏；
   无组件级入场动画。阻尼曲线：起止减速、中段快——与 Dock 横线共用同一组参数保证同步 */
:root{--pan-dur:.42s;--pan-ease:cubic-bezier(.62,.05,.12,1)}
@keyframes panin{from{transform:translateX(100%)}to{transform:none}}
@keyframes paninrev{from{transform:translateX(-100%)}to{transform:none}}
@keyframes panout{from{transform:none}to{transform:translateX(-100%)}}
@keyframes panoutrev{from{transform:none}to{transform:translateX(100%)}}
@keyframes rise{from{opacity:0;transform:translateX(14px)}to{opacity:1;transform:none}}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
padding:16px 18px;margin:12px 0;transition:transform .18s,border-color .18s,box-shadow .18s}
.card:hover{transform:translateY(-2px);border-color:rgba(233,106,46,.4);
box-shadow:0 10px 30px rgba(0,0,0,.35)}
.card.dim{background:transparent;border-color:var(--line)}
.card.dim:hover{transform:none;box-shadow:none}
/* 顶栏 */
.top{display:flex;align-items:center;gap:10px;padding:16px 2px 6px}
.top .logo{width:16px;height:16px;border:2px solid var(--accent);border-radius:5px;
position:relative;flex:0 0 auto}
.top .logo::after{content:"";position:absolute;left:2.5px;top:2.5px;width:3px;height:3px;
background:var(--accent);border-radius:1px;box-shadow:5px 0 0 var(--accent)}
.top .brand{font-weight:700;font-size:15px;letter-spacing:1.5px}
.top .ctx{margin-left:auto;color:var(--faint);font-size:12px}
/* 底部 Dock 导航：本体永远静止（无进场动画）；只有 glide 横线与橙色高亮随切换移动 */
.dock{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);display:flex;gap:4px;
padding:10px 12px 12px;background:rgba(23,24,26,.7);
backdrop-filter:blur(20px) saturate(1.5);-webkit-backdrop-filter:blur(20px) saturate(1.5);
border:1px solid var(--line);border-radius:24px;box-shadow:0 14px 44px rgba(0,0,0,.55);
z-index:60}
.dock-item{position:relative;display:flex;align-items:center;justify-content:center;
width:48px;height:42px;border-radius:14px;color:var(--sub);
transition:transform .18s cubic-bezier(.2,.8,.3,1.5),color .3s ease}
.dock-item svg{width:22px;height:22px;display:block}
.dock-item:hover{transform:translateY(-7px) scale(1.12);color:var(--text)}
.dock-item:active{transform:translateY(-3px) scale(1.04)}
.dock-item.on{color:var(--accent)}
.dock-bar{position:absolute;bottom:3px;left:50%;width:16px;height:3px;border-radius:2px;
background:var(--accent);transform:translateX(-50%) scaleX(1)}
.dock-glide{display:none;position:absolute;bottom:15px;left:0;width:16px;height:3px;
border-radius:2px;background:var(--accent);
transition:transform var(--pan-dur) var(--pan-ease)}
.dock.js .dock-bar{display:none}
.dock.js .dock-glide{display:block}
/* pjax 切换：旧页整张克隆为 ghost 面板左移出屏，新页同时右移入屏（并排无缝横移） */
.page-ghost{position:fixed;top:0;left:0;width:100%;height:100%;overflow:hidden;
background:var(--bg);z-index:40;pointer-events:none;
animation:panout var(--pan-dur) var(--pan-ease) both}
.page-ghost.rev{animation:panoutrev var(--pan-dur) var(--pan-ease) both}
.page-ghost .wrap{animation:none}
.page-ghost *{transition:none!important}
.dock-tip{position:absolute;bottom:calc(100% + 12px);left:50%;
transform:translate(-50%,6px) scale(.94);opacity:0;pointer-events:none;white-space:nowrap;
font-size:11px;color:var(--text);background:rgba(23,24,26,.95);border:1px solid var(--line);
padding:3px 10px;border-radius:8px;box-shadow:0 6px 18px rgba(0,0,0,.4);
transition:opacity .16s,transform .16s}
.dock-tip::after{content:"";position:absolute;top:100%;left:50%;transform:translateX(-50%);
border:4px solid transparent;border-top-color:rgba(23,24,26,.95)}
.dock-item:hover .dock-tip{opacity:1;transform:translate(-50%,0) scale(1)}
/* 表单 */
form{display:flex;gap:8px;margin:12px 0;flex-wrap:wrap}
input[type=text],input[type=password],input[type=number]{flex:1;min-width:180px;padding:9px 12px;
border:1px solid var(--line);border-radius:10px;background:var(--bg);color:var(--text);
font-size:13px;transition:border-color .15s,box-shadow .15s}
input:focus{border-color:var(--accent);outline:none;box-shadow:0 0 0 3px var(--accent-soft)}
input:disabled{opacity:.55}
input[type=file]{padding:8px;font-size:12px;color:var(--sub);border:1px dashed var(--line);
border-radius:10px;flex:1;min-width:200px;background:var(--bg)}
input[type=checkbox]{accent-color:var(--accent);width:15px;height:15px}
select{padding:8px 10px;border:1px solid var(--line);border-radius:10px;background:var(--bg);
color:var(--text);font-size:13px;transition:border-color .15s,box-shadow .15s}
select:focus{border-color:var(--accent);outline:none;box-shadow:0 0 0 3px var(--accent-soft)}
button{padding:9px 20px;border:0;border-radius:10px;background:var(--accent);color:#fff;
cursor:pointer;font-size:13px;font-weight:600;transition:background .15s,transform .12s,box-shadow .15s}
button:hover{background:#D55A1E;transform:translateY(-1px);box-shadow:0 6px 18px rgba(233,106,46,.3)}
button:active{transform:translateY(0) scale(.97)}
button.ghost{background:transparent;border:1px solid var(--line);color:var(--sub)}
button.ghost:hover{border-color:var(--err);color:var(--err);box-shadow:none}
button.small{padding:5px 12px;font-size:12px}
audio{width:100%;margin-top:8px}
/* 标签/徽章 */
.badge{display:inline-block;padding:2px 9px;border-radius:10px;font-size:11px;
font-weight:500;margin-right:6px}
.badge.ok{background:rgba(74,222,128,.12);color:var(--ok)}
.badge.warn{background:rgba(251,191,36,.12);color:var(--warn)}
.badge.err{background:rgba(248,81,73,.12);color:var(--err)}
.tag{display:inline-block;padding:1px 8px;border-radius:8px;font-size:11px}
.tag.local{background:rgba(74,222,128,.12);color:var(--ok)}
.tag.cloud{background:var(--accent-soft);color:var(--accent)}
.tag.stub{background:rgba(155,160,163,.12);color:var(--sub)}
.meta{color:var(--sub);font-size:12px}
.faint{color:var(--faint);font-size:12px}
/* 内容件 */
.utt{margin:4px 0;font-size:13.5px;padding:3px 8px;border-radius:8px;transition:background .15s}
.utt:hover{background:var(--card2)}
.utt .sp{color:var(--accent);font-weight:600}
.utt .id{color:var(--ok);font-weight:600}
.utt .tm{color:var(--faint);font-family:ui-monospace,monospace;font-size:11.5px;margin-right:6px}
.utt .emo{color:var(--warn);font-size:11px;margin-left:6px}
.gap{background:rgba(251,191,36,.08);border-left:2px solid var(--warn);
padding:6px 10px;margin:6px 0;font-size:12px;color:var(--warn);border-radius:0 8px 8px 0}
.segmark{color:var(--faint);font-size:11px;margin:10px 0 4px;font-family:ui-monospace,monospace}
pre{background:rgba(0,0,0,.25);padding:12px;border-radius:10px;white-space:pre-wrap;
font-size:13px;margin:8px 0;overflow-x:auto}
.msg-ok{background:rgba(74,222,128,.1);border:1px solid rgba(74,222,128,.3);color:var(--ok);
padding:10px 14px;border-radius:10px;margin:12px 0;font-size:13px;animation:rise .35s ease both}
.msg-err{background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.3);color:var(--err);
padding:10px 14px;border-radius:10px;margin:12px 0;font-size:13px;animation:rise .35s ease both}
.empty{text-align:center;padding:48px 0;color:var(--faint);font-size:13px}
table{width:100%;border-collapse:collapse;font-size:13px;margin:8px 0}
td,th{padding:9px 10px;border-bottom:1px solid var(--line);text-align:left}
th{color:var(--sub);font-weight:500;font-size:12px}
/* 日历网格 */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:12px}
.grid .card{margin:0;padding:14px 16px}
.grid .date{font-size:17px;font-weight:700;letter-spacing:.5px;color:var(--accent)}
/* 时间轴 */
.timeline-item{display:flex;align-items:center;gap:10px;padding:10px 0;
border-bottom:1px solid var(--line);font-size:13px}
.timeline-item:last-child{border-bottom:0}
.tl-time{font-family:ui-monospace,monospace;color:var(--sub);font-size:12px;flex:0 0 100px}
.tl-bar{flex:1;height:5px;border-radius:3px;background:var(--line);position:relative;overflow:hidden}
.tl-bar .fill{position:absolute;left:0;top:0;height:100%;border-radius:3px;
background:linear-gradient(90deg,var(--accent),#F08A4B);animation:grow 1s cubic-bezier(.2,.8,.2,1) both}
@keyframes grow{from{width:0}}
/* 声纹行 */
.vp-row{display:flex;align-items:center;gap:12px;padding:12px 4px;
border-bottom:1px solid var(--line);flex-wrap:wrap}
.vp-row:last-child{border-bottom:0}
.vp-name{font-weight:600;font-size:14px;flex:0 0 auto}
.vp-meta{flex:1;color:var(--sub);font-size:12px;min-width:140px}
.vp-actions{display:flex;gap:6px;flex:0 0 auto;flex-wrap:wrap}
.vp-actions input[type=text]{min-width:110px;padding:5px 9px;font-size:12px}
/* 配置控制台 */
form.form-block{display:block}
.slots{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px}
.slot{background:var(--card);border:1px solid var(--line);border-radius:var(--r);
padding:13px 14px;margin:0;transition:transform .18s,border-color .18s,box-shadow .18s}
.slot:hover{transform:translateY(-2px);border-color:rgba(233,106,46,.4);
box-shadow:0 10px 30px rgba(0,0,0,.35)}
.slot h3{margin:0 0 2px;color:var(--text);font-size:14px;display:flex;align-items:center;gap:8px}
.slot .desc{color:var(--faint);font-size:11.5px;margin-bottom:8px}
.frow{display:flex;flex-direction:column;gap:3px;margin:7px 0}
.frow label{font-size:11px;color:var(--sub);letter-spacing:.3px}
.frow input,.frow select{min-width:0;width:100%;padding:7px 10px;font-size:12.5px}
.frow2{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.frow2 .frow{margin:7px 0}
.fcheck{display:flex;align-items:center;gap:8px;margin:8px 0 2px;font-size:12px;color:var(--sub)}
details.card{padding:10px 16px}
details.card summary{cursor:pointer;color:var(--sub);font-size:13px;font-weight:600;
outline:none;transition:color .15s}
details.card summary:hover{color:var(--accent)}
details.card[open] summary{margin-bottom:8px}
.anchor-bar{display:flex;gap:8px;flex-wrap:wrap;margin:4px 0 8px}
.anchor-bar a{font-size:12px;color:var(--sub);border:1px solid var(--line);
padding:3px 12px;border-radius:20px;transition:all .15s}
.anchor-bar a:hover{color:var(--accent);border-color:var(--accent);text-decoration:none;
transform:translateY(-1px)}
"""

# ---- Dock 导航（SVG 图标）----

_ICONS = {
    "回看": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
            'stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" '
            'height="16" rx="3"/><path d="M8 3v4M16 3v4M3 10h18"/><path d="M8 14h3M8 17h6"/></svg>',
    "检索": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
            'stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="M16.6 16.6 21 21"/></svg>',
    "声纹": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
            'stroke-linecap="round"><path d="M5 11a7 7 0 0 1 14 0"/><path d="M8 11a4 4 0 0 1 8 0c0 '
            '3-.5 5.6-1.3 8"/><path d="M12 11v3c0 2.6-.4 5-1.1 7"/><path d="M17.5 15c-.2 1.6-.6 '
            '3.4-1.1 5"/></svg>',
    "配置": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
            'stroke-linecap="round"><path d="M4 7h9M17 7h3M4 17h3M11 17h9"/><circle cx="15" cy="7" '
            'r="2.2"/><circle cx="8.5" cy="17" r="2.2"/></svg>',
}
_DOCK = [("回看", "/rec/ui"), ("检索", "/rec/ui/search"),
         ("声纹", "/rec/ui/voiceprints"), ("配置", "/rec/ui/settings")]


def _dock(active: str = "") -> str:
    parts = ['<nav class=dock><span class=dock-glide></span>']
    for label, href in _DOCK:
        cls = ' class="dock-item on"' if label == active else ' class="dock-item"'
        parts.append('<a href="%s"%s>%s<span class=dock-tip>%s</span>'
                     '<span class=dock-bar></span></a>' % (href, cls, _ICONS[label], label))
    parts.append("</nav>")
    return "".join(parts)


def _top(ctx: str = "") -> str:
    return ('<div class=top><span class=logo></span>'
            '<span class=brand>秒忆卡 MemoSnap</span>'
            '<span class=ctx>%s</span></div>' % _esc(ctx))


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _fmt_time(ts_ms: int, with_sec=True) -> str:
    return time.strftime("%H:%M:%S" if with_sec else "%H:%M",
                         time.localtime(ts_ms / 1000))


def _fmt_duration(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return "%d:%02d:%02d" % (h, m, s) if h else "%d分%02d秒" % (m, s)


def _md_to_html(md: str) -> str:
    out, in_ul, in_ol = [], False, False
    for raw in (md or "").splitlines():
        line = _esc(raw.rstrip())
        bold = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", line)
        if bold.startswith("### "):
            out.append("<h4>%s</h4>" % bold[4:])
        elif bold.startswith("## "):
            out.append("<h3>%s</h3>" % bold[3:])
        elif bold.startswith("# "):
            out.append("<h2>%s</h2>" % bold[2:])
        elif bold.startswith("- "):
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append("<li>%s</li>" % bold[2:])
        elif re.match(r"^\d+\. ", bold):
            if not in_ol:
                out.append("<ol>"); in_ol = True
            out.append("<li>%s</li>" % re.sub(r"^\d+\. ", "", bold))
        elif bold == "":
            out.append("")
        else:
            out.append("<p>%s</p>" % bold)
    if in_ul: out.append("</ul>")
    if in_ol: out.append("</ol>")
    return "\n".join(out)


# 轻量 pjax：站内链接/表单 fetch 后只替换 .wrap（旧内容左滑淡出→新内容右滑淡入，
# 无空窗）；Dock 常驻不重绘，橙色指示条点击瞬间滑向目标图标；history/后退/地址栏
# 正常；fetch 失败回退整页跳转。
_PJAX = """
<script>
(function(){
  var wrap=document.querySelector('.wrap');
  var dock=document.querySelector('.dock');
  if(!wrap||!dock||!window.fetch||!window.DOMParser)return;
  var glide=dock.querySelector('.dock-glide');
  dock.classList.add('js');
  function glideTo(el){
    if(!glide||!el)return;
    glide.style.transform='translateX('+(el.offsetLeft+el.offsetWidth/2-8)+'px)';
  }
  function activeItem(path){
    var hit=null;
    dock.querySelectorAll('.dock-item').forEach(function(a){
      var h=a.getAttribute('href');
      if(h===path||(path.indexOf('/rec/ui/day')===0&&h==='/rec/ui'))hit=a;
    });
    return hit;
  }
  var DOCK_PATHS=['/rec/ui','/rec/ui/search','/rec/ui/voiceprints','/rec/ui/settings'];
  var curIdx=0;
  function idxOf(path){
    if(path.indexOf('/rec/ui/day')===0)return 0;
    var i=DOCK_PATHS.indexOf(path);
    return i;
  }
  function setActive(url){
    var path=new URL(url,location.origin).pathname;
    dock.querySelectorAll('.dock-item').forEach(function(a){
      a.classList.toggle('on',a===activeItem(path));
    });
    glideTo(activeItem(path));
    var i=idxOf(path);
    if(i>=0)curIdx=i;
  }
  function swap(html,url,scroll,dir){
    var doc=new DOMParser().parseFromString(html,'text/html');
    var nw=doc.querySelector('.wrap');
    if(!nw){location.assign(url);return;}
    document.title=doc.title||document.title;
    // 旧页整张面板克隆为 ghost 移出屏，新页反向移入（并排无缝横移；方向跟导航走）
    var stage=document.querySelector('.stage')||wrap;
    var ghost=stage.cloneNode(true);
    ghost.classList.add('page-ghost');
    if(dir<0)ghost.classList.add('rev');
    var gw=ghost.querySelector('.wrap');
    if(gw)gw.classList.remove('swap-in','swap-in-rev');
    document.body.appendChild(ghost);
    setTimeout(function(){if(ghost.parentNode)ghost.parentNode.removeChild(ghost);},480);
    wrap.innerHTML=nw.innerHTML;
    wrap.classList.remove('swap-in','swap-in-rev');
    void wrap.offsetWidth;
    wrap.classList.add(dir<0?'swap-in-rev':'swap-in');
    try{history.pushState({u:url},' ',url);}catch(e){}
    setActive(url);
    if(scroll!==false&&!new URL(url,location.origin).hash)window.scrollTo({top:0});
  }
  function load(url,scroll,dir){
    fetch(url,{headers:{'Accept':'text/html'}}).then(function(r){
      if(!r.ok){location.assign(url);return;}
      return r.text().then(function(t){swap(t,r.url,scroll,dir);});
    }).catch(function(){location.assign(url);});
  }
  document.addEventListener('click',function(e){
    var a=e.target.closest?a.target.closest('a[href^="/rec/"]'):null;
    if(!a||e.metaKey||e.ctrlKey||e.shiftKey||e.altKey)return;
    var href=a.getAttribute('href');
    var u=new URL(href,location.origin);
    if(u.hash&&u.pathname===location.pathname)return;   // 同页锚点走平滑滚动
    e.preventDefault();
    var tgt=idxOf(u.pathname);
    var dir=(tgt<0||tgt>=curIdx)?1:-1;      // 与横线滑动同方向
    if(a.classList.contains('dock-item')){
      // 点击瞬间：高亮开始向目标过渡 + 指示条滑动（Dock 本体不动）
      dock.querySelectorAll('.dock-item').forEach(function(t){
        t.classList.toggle('on',t===a);
      });
      glideTo(a);
    }
    load(href,undefined,dir);
  });
  document.addEventListener('submit',function(e){
    var f=e.target;
    if(!wrap.contains(f))return;
    e.preventDefault();
    var fd=new FormData(f);
    var method=(f.getAttribute('method')||'get').toLowerCase();
    var url=f.action||location.href;
    if(method==='get'){
      var q=new URLSearchParams(fd).toString();
      load(url.split('?')[0]+(q?'?'+q:''));
      return;
    }
    fetch(url,{method:'POST',body:fd}).then(function(r){
      if(!r.ok){location.assign(url);return;}
      return r.text().then(function(t){swap(t,r.url,false);});
    }).catch(function(){f.submit();});
  });
  window.addEventListener('popstate',function(){
    var tgt=idxOf(location.pathname);
    var dir=(tgt>=0&&tgt<curIdx)?-1:1;      // 后退=反向滑
    load(location.href,false,dir);
  });
  window.addEventListener('resize',function(){setActive(location.href);});
  // 首次定位横线不做过渡动画（避免从 0 滑过来的入场感）
  if(glide){
    glide.style.transition='none';
    setActive(location.href);
    void glide.offsetWidth;
    glide.style.transition='';
  }else{
    setActive(location.href);
  }
})();
</script>
"""


# 整页加载时的横线同步：从"上一页图标位置"无过渡起跳 → 下一帧滑动到当前图标，
# 与 wrap 的滑入同曲线同耗时，视觉上是横线陪着页面一起横移。
_GLIDE_SYNC = """
<script>
(function(){
  var f=document.body.getAttribute('data-from');
  var dock=document.querySelector('.dock');
  if(f===null||!dock)return;
  var items=dock.querySelectorAll('.dock-item');
  var g=dock.querySelector('.dock-glide');
  var ai=-1;
  for(var i=0;i<items.length;i++){if(items[i].classList.contains('on'))ai=i;}
  var fi=parseInt(f,10);
  if(!g||isNaN(fi)||fi<0||fi>=items.length||ai<0||fi===ai)return;
  function x(el){return el.offsetLeft+el.offsetWidth/2-8;}
  g.style.transition='none';
  g.style.transform='translateX('+x(items[fi])+'px)';
  void g.offsetWidth;
  g.style.transition='';
  requestAnimationFrame(function(){
    requestAnimationFrame(function(){g.style.transform='translateX('+x(items[ai])+'px)';});
  });
})();
</script>
"""

_DOCK_IDX = {"回看": 0, "检索": 1, "声纹": 2, "配置": 3}


def _page(title: str, body: str, active: str = "", ctx: str = "") -> bytes:
    aidx = _DOCK_IDX.get(active, -1)
    from_idx = getattr(_NAV, "idx", None)
    if from_idx is None:
        wcls = "swap-in"                    # 首次打开：从右滑入
        data_from = ""
    elif from_idx == aidx:
        wcls = ""                           # 同页刷新：不动
        data_from = ""
    else:
        wcls = "swap-in" if aidx > from_idx else "swap-in-rev"
        data_from = ' data-from="%d"' % from_idx
    return ('<!doctype html><html lang=zh><head><meta charset=utf-8>'
            '<meta name=viewport content="width=device-width,initial-scale=1">'
            '<title>%s</title><style>%s</style></head><body%s>'
            '<div class=stage><div class="wrap %s">%s%s</div></div>%s%s%s</body></html>'
            % (_esc(title), _STYLE, data_from, wcls, _top(ctx), body,
               _dock(active), _PJAX, _GLIDE_SYNC)).encode("utf-8")


# ---- 日期列表（首页）----

def days_page(store: SegmentStore) -> bytes:
    items = []
    for d in store.days():
        date = d["local_date"]
        devs = "、".join("%s(%d段)" % (r["device_id"], r["n"])
                        for r in store.day_devices(date))
        items.append(
            '<div class=card><a href="/rec/ui/day?date=%s"><span class=date>%s</span></a>'
            '<div class=meta style="margin-top:4px">%d段 · %s</div></div>'
            % (_esc(date), _esc(date), d["n"], _esc(devs)))
    body = ["<h1>录音日历</h1>"]
    if items:
        body.append('<div class=grid>%s</div>' % "".join(items))
    else:
        body.append('<div class="card empty">暂无录音数据<br>'
                    '<span class=faint>工牌录几段后自动出现</span></div>')
    return _page("秒忆卡", "".join(body), "回看", "录音日历")


# ---- 当天回看 ----

def day_page(store: SegmentStore, date: str, msg: str = "", msg_err: str = "") -> bytes:
    if not _SAFE_DATE.match(date):
        return _page("参数错误", '<div class=card>date 非法</div>', "回看")
    devs = store.day_devices(date)
    parts = ["<h1>%s</h1>" % _esc(date)]
    if msg:
        parts.append('<div class=msg-ok>%s</div>' % _esc(msg))
    if msg_err:
        parts.append('<div class=msg-err>%s</div>' % _esc(msg_err))
    if devs:
        parts.append('<div class=anchor-bar>'
                     '<a href="#spk">说话人标注</a><a href="#tl">录音时段</a>'
                     '<a href="#tr">逐字稿</a><a href="#conv">会话与纪要</a>'
                     '<a href="#daily">日报</a></div>')
    for d in devs:
        dev = d["device_id"]
        parts.append("<h2>%s <span class=meta>%d段</span></h2>" % (_esc(dev), d["n"]))
        parts.append('<div id=spk>%s</div>' % _speakers_panel(store, dev, date))
        parts.append('<div id=tl>%s</div>' % _timeline_html(store, dev, date))
        parts.append('<div id=tr>%s</div>' % _transcript_html(store, dev, date))
        parts.append('<div id=conv>%s</div>' % _conversations_html(store, dev, date))
        parts.append('<div id=daily>%s</div>' % _daily_html(store, dev, date))
    if not devs:
        parts.append('<div class="card empty">该日无数据</div>')
    return _page("%s回看" % date, "".join(parts), "回看", date)


def _speakers_panel(store: SegmentStore, dev: str, date: str) -> str:
    """说话人标注面板：入档（声纹）+ 手动改归因（优先级高于声纹）。"""
    jsonl = store.get_transcript(dev, date)
    if not jsonl:
        return ""
    speakers = {}
    for raw in jsonl.splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if rec.get("type") != "utterance":
            continue
        sp_full = rec.get("speaker", "")
        identity = rec.get("identity", "")
        sp = sp_full.split("/")[-1] if "/" in sp_full else sp_full
        if not sp:
            continue
        if sp not in speakers:
            speakers[sp] = {"count": 0, "sample": rec.get("text", "")[:60],
                            "identity": identity}
        speakers[sp]["count"] += 1
    if not speakers:
        return ""

    enrolled = {r["identity"] for r in store.list_voiceprints()}
    overrides = store.get_speaker_overrides(dev, date)

    parts = ["<h3>说话人标注</h3>", '<div class="card dim">']
    parts.append('<div class=faint style="margin-bottom:10px">'
                 '入档=提取声纹后自动全库归因；归因错了用"改归因"手动纠正'
                 '（当天该标签全部发言改判，优先级高于声纹），或"清除手动"恢复。</div>')
    for label, info in sorted(speakers.items()):
        ov = overrides.get(label)
        known = ov or info["identity"] or (label if label in enrolled else "")
        if known:
            badge = ('<span class="badge warn">手动</span>' if ov
                     else '<span class="badge ok">声纹</span>')
            parts.append(
                '<div class=vp-row>'
                '<span class=vp-name>%s</span>%s'
                '<span class=vp-meta>%s · %d条 · %s</span>'
                '<form action="/rec/ui/speakers/override" method=post '
                'style="margin:0;display:flex;gap:6px;align-items:center">'
                '<input type=hidden name=device value="%s">'
                '<input type=hidden name=date value="%s">'
                '<input type=hidden name=speaker value="%s">'
                '<input type=text name=identity value="%s" required '
                'pattern="[\\w\\u4e00-\\u9fff\\- ]{1,32}" maxlength=32 '
                'style="min-width:80px;padding:5px 9px;font-size:12px">'
                '<button class="small" type=submit>改归因</button></form>'
                % (_esc(label), badge, _esc(known), info["count"],
                   _esc(info["sample"]), _esc(dev), _esc(date),
                   _esc(label), _esc(known)))
            if ov:
                parts.append(
                    '<form action="/rec/ui/speakers/override" method=post '
                    'style="margin:0;display:flex;gap:6px;align-items:center">'
                    '<input type=hidden name=device value="%s">'
                    '<input type=hidden name=date value="%s">'
                    '<input type=hidden name=speaker value="%s">'
                    '<input type=hidden name=identity value="">'
                    '<button class="small ghost" type=submit>清除手动</button></form>'
                    % (_esc(dev), _esc(date), _esc(label)))
            parts.append('</div>')
        else:
            parts.append(
                '<div class=vp-row>'
                '<span class=vp-name>%s</span>'
                '<span class=vp-meta>%d条 · %s</span>'
                '<form action="/rec/ui/voiceprints/auto-enroll" method=post '
                'style="margin:0;display:flex;gap:6px;align-items:center">'
                '<input type=hidden name=device value="%s">'
                '<input type=hidden name=date value="%s">'
                '<input type=hidden name=speaker value="%s">'
                '<input type=text name=identity placeholder="是谁？" required '
                'pattern="[\\w\\u4e00-\\u9fff\\- ]{1,32}" maxlength=32 '
                'style="min-width:80px;padding:5px 9px;font-size:12px">'
                '<button class="small" type=submit>入档</button></form>'
                '</div>'
                % (_esc(label), info["count"], _esc(info["sample"]),
                   _esc(dev), _esc(date), _esc(label)))
    parts.append("</div>")
    return "".join(parts)


def _timeline_html(store: SegmentStore, dev: str, date: str) -> str:
    tl = store.recording_timeline(dev, date)
    if not tl["periods"] and not tl["gaps"]:
        return ""
    rows = []
    for p in tl["periods"]:
        mins = round((p["end_ts"] - p["start_ts"]) / 60000)
        rows.append(
            '<div class=timeline-item>'
            '<span class=tl-time>%s-%s</span>'
            '<span class=tl-bar><span class=fill style="width:%d%%"></span></span>'
            '<span class=meta>%d分钟 %d段</span></div>'
            % (_fmt_time(p["start_ts"], False), _fmt_time(p["end_ts"], False),
               min(100, int(mins / 6)), mins, p["segments"]))
    for g in tl["gaps"]:
        mins = round((g["end_ts"] - g["start_ts"]) / 60000)
        if mins < 1:
            continue
        rows.append(
            '<div class=timeline-item>'
            '<span class=tl-time>%s-%s</span>'
            '<span class=gap style="flex:1;border-radius:8px;padding:3px 10px">'
            '%d分钟缺口</span></div>'
            % (_fmt_time(g["start_ts"], False), _fmt_time(g["end_ts"], False), mins))
    return ("<h3>录音时段 <span class=faint>合规记录</span></h3>"
            '<div class="card dim">' + "".join(rows) + '</div>'
            '<div class=faint>合计 %s；依据 segments 表</div>'
            % _fmt_duration(tl["recorded_sec"]))


def _transcript_html(store: SegmentStore, dev: str, date: str) -> str:
    jsonl = store.get_transcript(dev, date)
    if not jsonl:
        return ""
    lines = ["<h3>逐字稿</h3>", '<div class="card dim">']
    for raw in jsonl.splitlines():
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        t = rec.get("type")
        if t == "segment":
            lines.append('<div class=segmark>── 段 %s ──</div>'
                         % _esc(rec.get("segment_id", "")))
        elif t == "gap":
            lines.append('<div class=gap>缺口 %d秒</div>' % int(rec.get("seconds", 0)))
        elif t == "utterance":
            identity = rec.get("identity", "")
            sp_cls = "id" if identity else "sp"
            sp_name = identity if identity else rec.get("speaker", "")
            emotion = rec.get("emotion", "")
            emo_html = ('<span class="emo">%s</span>' % _esc(emotion)
                        if emotion else "")
            lines.append(
                '<div class=utt><span class=tm>%s</span>'
                '<span class="%s">%s</span>：%s%s</div>'
                % (_fmt_time(rec.get("start_ts", 0)), sp_cls, _esc(sp_name),
                   _esc(rec.get("text", "")), emo_html))
    lines.append("</div>")
    return "".join(lines)


def _conversations_html(store: SegmentStore, dev: str, date: str) -> str:
    rows = store.list_conversations(date, dev)
    if not rows:
        return ""
    parts = ["<h3>会话与纪要</h3>"]
    for r in rows:
        speakers = json.loads(r["speakers_json"])
        idents = [s if isinstance(s, str) else s.get("identity", s.get("speaker", ""))
                  for s in (speakers if isinstance(speakers, list) else [])]
        sp_list = "、".join(idents) if idents and idents[0] else "%d人" % len(speakers)
        is_meeting = r["is_meeting"]
        badge = ('<span class="badge ok">会议</span>' if is_meeting
                 else '<span class="badge warn">对话</span>')
        head = ("conv%d %s-%s %s %s"
                % (r["conv_idx"], _fmt_time(r["start_ts"], False),
                   _fmt_time(r["end_ts"], False),
                   _fmt_duration((r["end_ts"] - r["start_ts"]) / 1000), sp_list))
        title = (r.get("title") or "").strip()
        parts.append('<div class=card>%s<b style="font-size:14.5px">%s</b>'
                     % (badge, _esc(title or head)))
        parts.append('<div class=meta>%s</div>' % _esc(head))
        parts.append(
            '<form action="/rec/ui/conversations/rename" method=post '
            'style="margin:10px 0;display:flex;gap:6px;align-items:center">'
            '<input type=hidden name=device value="%s">'
            '<input type=hidden name=date value="%s">'
            '<input type=hidden name=conv value="%d">'
            '<input type=text name=title value="%s" placeholder="重命名（如：工程例会）" '
            'maxlength=60 style="min-width:140px;padding:5px 10px;font-size:12.5px">'
            '<button class=small type=submit>重命名</button></form>'
            % (_esc(dev), _esc(date), r["conv_idx"], _esc(title)))
        md = store.get_conversation_minutes(dev, date, r["conv_idx"])
        if md:
            parts.append(_md_to_html(md))
        else:
            parts.append('<div class=meta>无纪要（未跑 meetings 或 LLM 失败）</div>')
        parts.append("</div>")
    return "".join(parts)


def _daily_html(store: SegmentStore, dev: str, date: str) -> str:
    md = store.get_daily(dev, date)
    if not md:
        return ""
    return "<h3>日报</h3><div class=card>" + _md_to_html(md) + "</div>"


# ---- 检索 ----

def search_page(store: SegmentStore, q: str, device: str, date: str) -> bytes:
    store.rebuild_fts()
    dev = device if _SAFE_ID.match(device or "") else None
    dt = date if _SAFE_DATE.match(date or "") else None
    rows = store.search_utterances(q, date=dt, device=dev, limit=100) if q.strip() else []
    items = []
    for r in rows:
        identity = r.get("identity", "") if isinstance(r, dict) else ""
        sp_cls = "id" if identity else "sp"
        sp_name = identity if identity else r.get("speaker", "")
        items.append(
            '<div class=card><span class=meta>%s %s [%s]</span><br>'
            '<span class="%s">%s</span>：%s</div>'
            % (_esc(r["local_date"]), _fmt_time(r["start_ts"]),
               _esc(r["device_id"]), sp_cls, _esc(sp_name), _esc(r["text"])))
    body = ['<h1>检索</h1>',
            '<form action="/rec/ui/search" method=get style="justify-content:center">'
            '<input type=text name=q placeholder="全文检索（多词 AND）" value="%s" '
            'style="max-width:420px">'
            '<input type=hidden name=device value="%s"><button>检索</button></form>'
            % (_esc(q), _esc(device))]
    if q.strip():
        body.append('<div class=meta style="text-align:center">命中 %d 条</div>' % len(rows))
    body.extend(items)
    return _page("检索 %s" % q, "".join(body), "检索", "全文检索")


# ---- 声纹管理 ----

def voiceprints_page(store: SegmentStore, msg: str = "", msg_err: str = "") -> bytes:
    rows = store.list_voiceprints()
    parts = ["<h1>声纹管理</h1>"]
    if msg:
        parts.append('<div class=msg-ok>%s</div>' % _esc(msg))
    if msg_err:
        parts.append('<div class=msg-err>%s</div>' % _esc(msg_err))

    parts.append("""
<h3>入档新声纹</h3>
<div class=card>
<form action="/rec/ui/voiceprints/enroll" method=post enctype="multipart/form-data">
<input type=text name=identity placeholder="身份名（如：本人 / 张主管）" required
       pattern="[\\w\\u4e00-\\u9fff\\- ]{1,32}" maxlength=32>
<input type=file name=audio accept=".opus,.ogg,.wav,.mp3,.flac,.m4a" required>
<button type=submit>入档</button>
</form>
<div class=faint>上传一段该人的语音（opus/wav/mp3，时长至少3秒，建议10秒+、单人安静环境）。
入档后该人的发言在逐字稿中自动标注身份；历史录音由调度器自动重归因。</div>
</div>
""")

    parts.append("<h3>已入档声纹（%d）</h3>" % len(rows))
    if rows:
        parts.append('<div class="card dim">')
        for r in rows:
            created = time.strftime("%Y-%m-%d %H:%M",
                                    time.localtime(r["created_at"] / 1000))
            parts.append(
                '<div class=vp-row>'
                '<span class=vp-name>%s</span>'
                '<span class=vp-meta>%d维 · %s</span>'
                '<span class=vp-actions>'
                '<form action="/rec/ui/voiceprints/rename" method=post style="margin:0">'
                '<input type=hidden name=old value="%s">'
                '<input type=text name=new placeholder="新名" required '
                'pattern="[\\w\\u4e00-\\u9fff\\- ]{1,32}" maxlength=32 '
                'style="min-width:80px">'
                '<button class="small ghost" type=submit>改名</button></form>'
                '<form action="/rec/ui/voiceprints/delete" method=post style="margin:0">'
                '<input type=hidden name=identity value="%s">'
                '<button class="small ghost" type=submit>删除</button></form>'
                '</span></div>'
                % (_esc(r["identity"]), r["dim"], created,
                   _esc(r["identity"]), _esc(r["identity"])))
        parts.append("</div>")
    else:
        parts.append('<div class="card empty">还没有入档声纹<br>'
                     '<span class=faint>上传音频为每个人建立声纹身份</span></div>')

    parts.append('<div class=faint style="margin-top:16px">'
                 '声纹数据只存本机（不出服务器）· 逐字稿中的说话人标签'
                 '（C1/C2）经声纹比对后映射为身份名</div>')
    return _page("声纹管理", "".join(parts), "声纹", "声纹管理")


# ---- 配置控制台 ----

_SLOT_META = [
    ("asr", "语音转写 ASR", "录音段转文字（含说话人标签）"),
    ("diarization", "说话人分离", "同段音频内切分说话人（asr-tags=复用 ASR 标签）"),
    ("voiceprint", "声纹归因", "匿名说话人映射到登记身份（硬约束：本地）"),
    ("emotion", "情绪标签", "逐句情绪标注"),
    ("summarize", "纪要 LLM", "结构化逐字稿生成会议纪要"),
    ("report", "日报 LLM", "合成当日工作日报"),
]

_PROV_LABEL = {
    "sherpa": "本地 SenseVoice（sherpa-onnx）",
    "local-sherpa": "本地 sherpa-onnx",
    "local-funasr": "本地 FunASR",
    "minimax": "MiniMax 云端",
    "openai-compatible": "OpenAI 兼容 API",
    "ollama": "本地 Ollama",
    "asr-tags": "复用 ASR 输出标签",
    "none": "不启用",
    "local-only": "本地（硬约束）",
    "llm-infer": "LLM 推断",
    "aliyun": "阿里云",
    "tencent": "腾讯云",
    "iflytek": "讯飞",
    "same-as-summarize": "同 summarize 槽",
}

_LOCAL_PROV = {"sherpa", "local-sherpa", "local-funasr", "ollama",
               "asr-tags", "none", "local-only"}

_IMPLEMENTED = {("asr", "minimax"), ("asr", "sherpa"),
                ("diarization", "asr-tags"), ("diarization", "none"),
                ("voiceprint", "local-only"), ("emotion", "asr-tags"),
                ("summarize", "minimax"), ("report", "minimax")}

# 各 provider 的默认密钥环境变量名（前端填密钥时写入 env 文件）
_KEY_ENV_DEFAULT = {"minimax": "MINIMAX_API_KEY",
                    "openai-compatible": "OPENAI_API_KEY"}


def _frow(label, inner):
    return '<div class=frow><label>%s</label>%s</div>' % (_esc(label), inner)


def _slot_card(app, slot, name, desc):
    from .config import VALID_PROVIDERS
    cap = app.capabilities[slot]
    cur = cap.provider
    out = ['<div class=slot><h3>%s'
           '<span class="tag %s">%s</span></h3><div class=desc>%s</div>'
           % (_esc(name), "local" if cur in _LOCAL_PROV else "cloud",
              "本地" if cur in _LOCAL_PROV else "出网", _esc(desc))]
    if slot == "voiceprint":
        out.append(_frow("实现（硬约束：本地，不可改）",
                         '<input type=hidden name=voiceprint value="local-only">'
                         '<input type=text value="本地嵌入比对（CAM++/ERes2Net）" disabled>'))
        out.append(_frow("模型路径",
                         '<input type=text name=voiceprint__model value="%s">'
                         % _esc(cap.local.get("model", ""))))
        out.append(_frow("匹配阈值 threshold（越高越严，宁匿名不误判）",
                         '<input type=number step=0.05 min=0 max=1 name=voiceprint__threshold '
                         'value="%s">' % _esc(cap.local.get("threshold", ""))))
        out.append(_frow("领先裕度 min_margin（top1-top2 小于此值不判）",
                         '<input type=number step=0.01 min=0 max=1 name=voiceprint__min_margin '
                         'value="%s">' % _esc(cap.local.get("min_margin", ""))))
        out.append("</div>")
        return "".join(out)

    opts = []
    for prov in sorted(VALID_PROVIDERS[slot]):
        sel = ' selected' if prov == cur else ''
        tag = "本地" if prov in _LOCAL_PROV else "云端"
        stub = "" if (slot, prov) in _IMPLEMENTED else " · 预留"
        opts.append('<option value="%s"%s>%s（%s%s）</option>'
                    % (_esc(prov), sel, _esc(_PROV_LABEL.get(prov, prov)), tag, stub))
    out.append(_frow("provider（模型来源）",
                     '<select name=%s>%s</select>' % (slot, "".join(opts))))
    if cur in ("minimax", "openai-compatible"):
        out.append('<div class=frow2>'
                   + _frow("base_url",
                           '<input type=text name=%s__base_url value="%s" '
                           'placeholder="https://api.minimaxi.com">'
                           % (slot, _esc(cap.api.get("base_url", ""))))
                   + _frow("model",
                           '<input type=text name=%s__model value="%s" '
                           'placeholder="asr-1.0 / MiniMax-M3">'
                           % (slot, _esc(cap.api.get("model", ""))))
                   + '</div>')
        env_name = cap.api.get("api_key_env") or _KEY_ENV_DEFAULT.get(cur, "")
        held = "已持有（env 或本机 DB），留空=保持不变"
        out.append(_frow("API Key（存本机数据库；同名环境变量优先；留空=不修改）",
                         '<input type=password name=%s__key autocomplete=off '
                         'placeholder="%s">' % (slot, _esc(env_name + "：" + held)
                                                if env_name else held)))
        out.append('<div class=fcheck><input type=checkbox name=%s__keyclear value=1>'
                   '<span>清除已存密钥（回退环境变量）</span></div>' % slot)
    if slot == "asr" and cur == "sherpa":
        out.append(_frow("模型目录（model.onnx + tokens.txt）",
                         '<input type=text name=asr__model_dir value="%s">'
                         % _esc(cap.api.get("model_dir", ""))))
    if slot in ("summarize", "report"):
        chk = ' checked' if cap.redact else ''
        out.append('<div class=fcheck><input type=checkbox name=%s__redact value=1%s>'
                   '<span>出网前脱敏（人名→角色标签替换）</span></div>' % (slot, chk))
    out.append("</div>")
    return "".join(out)


def settings_page(app, msg: str = "", msg_err: str = "") -> bytes:
    parts = ["<h1>配置控制台</h1>"]
    if msg:
        parts.append('<div class=msg-ok>%s</div>' % _esc(msg))
    if msg_err:
        parts.append('<div class=msg-err>%s</div>' % _esc(msg_err))
    parts.append('<div class=faint style="margin-bottom:12px">'
                 '每个能力槽位独立配置模型来源与 API 参数（base_url / model / 密钥 / 阈值）。'
                 '覆盖与密钥存本机数据库（与录音数据同权限类；同名环境变量优先于库内密钥）。'
                 '保存后本服务立即生效，CLI 分析进程下次运行自动读取。</div>')
    parts.append('<form class=form-block action="/rec/ui/settings" method=post>'
                 '<div class=slots>')
    for slot, name, desc in _SLOT_META:
        parts.append(_slot_card(app, slot, name, desc))
    parts.append('</div><div style="margin-top:16px;display:flex;gap:10px;'
                 'align-items:center"><button type=submit>保存配置</button>'
                 '<span class=faint>密钥留空=不修改</span></div></form>')
    parts.append('<details class="card dim"><summary>部署信息</summary>'
                 '<div class=meta>数据目录：%s<br>监听：%s:%d<br>保留：%d 天<br>'
                 '配置文件：%s</div></details>'
                 % (_esc(app.data_dir), _esc(app.listen_host), app.listen_port,
                    app.retention_days, _esc(app.config_path)))
    parts.append('<div class=faint style="margin-top:12px">'
                 '声纹槽位为产品硬约束：声纹特征永不出本机，无云端选项。</div>')
    return _page("配置控制台", "".join(parts), "配置", "配置控制台")
