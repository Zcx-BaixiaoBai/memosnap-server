# recorder/safe_upstream.py —— 出站请求统一校验与执行。
#
# 语义照搬 firmware/ai-passport/gateway/gateway.py 的实现（已在 ZCode 工牌网关上运行）：
# - 仅 http/https；
# - 公网上游：host 必须可解析，解析结果一律拒绝 localhost/环回/私有/保留/链路本地/组播（SSRF 防护）；
# - http:// 直连已校验 IP（http.client 不做第二次 DNS 解析，杜绝 DNS rebinding；天然不跟随重定向）；
# - https:// 用无重定向 opener，3xx 直接抛错（TLS 需按域名握手，无法钉 IP；
#   校验与连接之间的重解析窗口极小，且目标为操作者配置的固定上游）；
# - 非 2xx 一律抛错，不吞响应。
#
# 本文件在其上补两类能力（design §5.4）：
# - multipart POST（MiniMax /v1/speech_to_text 是 multipart 上传）；
# - 环回受控例外：local_endpoints 只接受 127.0.0.1/::1（config.py 已在加载时校验），
#   供 local-llm 的 Ollama（127.0.0.1:11434）使用；同样钉 IP、不跟随重定向。
#
# 注意：核心侧**不在**这里给"内网 FunASR"等私网地址放行——本地推理走进程内库
# （sherpa-onnx 直接 import），不当出站上游。私网放行会直接削弱 SSRF 防护。

import http.client
import ipaddress
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse


class UpstreamError(Exception):
    pass


def _resolve_public_ips(url, source):
    """公网上游校验：返回解析出的公网 IP 列表；任何私网/环回/保留地址直接拒绝。"""
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise UpstreamError("上游仅允许 http/https（收到 %r, source=%s）" % (u.scheme, source))
    host = u.hostname
    if not host:
        raise UpstreamError("上游 URL 缺少 host（source=%s）" % source)
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise UpstreamError("上游 host 解析失败：%s（source=%s）" % (e, source))
    ips = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_reserved
                or ip.is_link_local or ip.is_multicast or ip.is_unspecified):
            raise UpstreamError("拒绝私有/环回/保留地址：%s（source=%s）" % (ip, source))
        ips.append(str(ip))
    if not ips:
        raise UpstreamError("上游 host 无可用地址（source=%s）" % source)
    return ips, u


def _resolve_loopback_ip(url, allowed_endpoints, source):
    """环回例外校验：URL 的 host 必须是 allowed_endpoints 里显式声明的环回地址之一。"""
    u = urlparse(url)
    if u.scheme not in ("http", "https"):
        raise UpstreamError("环回上游仅允许 http/https（收到 %r）" % u.scheme)
    host = (u.hostname or "").strip("[]")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise UpstreamError("环回例外仅接受 IP host，收到 %r（source=%s）" % (host, source))
    if not ip.is_loopback:
        raise UpstreamError("环回例外收到非环回地址：%s（source=%s）" % (host, source))
    allowed_hosts = {ep.rsplit(":", 1)[0].strip("[]") for ep in allowed_endpoints}
    if host not in allowed_hosts:
        raise UpstreamError("环回地址 %s 不在 local_endpoints 白名单 %s" % (host, sorted(allowed_hosts)))
    return str(ip), u


def _split_target(u):
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    return path


def safe_http_request(method, url, *, body=None, headers=None, timeout=60,
                      loopback_endpoints=()):
    """统一出口。返回 (status, resp_headers, payload_bytes)。非 2xx 抛 UpstreamError。

    loopback_endpoints 非空且 URL 命中环回白名单时走环回通道；否则走公网校验通道。
    """
    headers = dict(headers or {})
    is_loopback = False
    try:
        ip, u = _resolve_loopback_ip(url, loopback_endpoints, "loopback")
        is_loopback = True
    except UpstreamError:
        ips, u = _resolve_public_ips(url, "upstream")
        ip = ips[0]

    if u.scheme == "http":
        # 钉已校验 IP，不做第二次 DNS；http.client 不跟随重定向
        conn = http.client.HTTPConnection(ip, u.port or 80, timeout=timeout)
        h = dict(headers)
        h["Host"] = u.hostname
        try:
            conn.request(method, _split_target(u), body=body, headers=h)
            resp = conn.getresponse()
            return resp.status, dict(resp.getheaders()), resp.read()
        finally:
            conn.close()
    # https：无重定向 opener，3xx 直接抛错
    h = dict(headers)
    if body is not None and "Content-Type" not in h and method in ("POST", "PUT", "PATCH"):
        h["Content-Type"] = "application/octet-stream"
    req = urllib.request.Request(url, data=body if method in ("POST", "PUT", "PATCH") else None,
                                 headers=h, method=method)
    opener = urllib.request.build_opener(_NoRedirectHandler())
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        # 3xx 在这里按"拒绝"处理；4xx/5xx 把响应体带回去给调用方辨认
        if 300 <= e.code < 400:
            raise UpstreamError("https 上游 %d 重定向被拒绝（%s）" % (e.code, url))
        return e.code, dict(e.headers or {}), (e.read() or b"")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def multipart_body(fields, files):
    """构造 multipart/form-data。fields: {name: value}; files: {name: (filename, bytes, ctype)}"""
    boundary = "----recorderBoundary7MA4YWxkTrZu0gW"
    out = []
    for name, value in fields.items():
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                    % (boundary, name, value)).encode("utf-8"))
    for name, (filename, data, ctype) in files.items():
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                    "Content-Type: %s\r\n\r\n" % (boundary, name, filename, ctype)).encode("utf-8"))
        out.append(data)
        out.append(b"\r\n")
    out.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(out), "multipart/form-data; boundary=%s" % boundary
