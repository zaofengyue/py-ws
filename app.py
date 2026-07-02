"""app.py — py-ws 单文件版

基于 xray 的多协议代理服务，单文件实现：VMess / VLESS / Trojan 三协议，
统一走 WebSocket，不依赖 Cloudflare Argo 隧道。

与 py-argo 的区别：
  py-argo 用 Cloudflare Argo 隧道自动获得公网域名和 TLS 终止；
  py-ws 假定部署平台本身就提供公网域名 + TLS 终止（容器只需监听平台
  转发过来的内部端口）。域名优先读 DOMAIN 环境变量；若未设置，会
  按 Railway / Render / Zeabur / Koyeb / Cloud Foundry / Fly.io 等常见
  平台的环境变量自动探测；如果仍然探测不到域名，会退化为"公网 IP +
  明文 ws（不走 TLS）"兜底方案，保证在没有平台域名的裸机/VPS 环境下
  依然可以直接出节点。

其余部分（xray 下载与配置、分享链接与订阅生成、部署后清理等）与
py-argo 保持一致。
"""
import base64
import json
import logging
import os
import platform
import re
import socket
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ============================================================================
# 配置区（可在这里直接填写，优先级高于环境变量；留空则读环境变量，都没有则自动生成/使用默认值）
# ============================================================================
CONF_UUID = ""            # VMess/VLESS 统一 ID
CONF_TROJAN_PASS = ""     # Trojan 独立密码（留空则自动生成并持久化）
CONF_PORT = ""            # 容器内实际监听端口（部署平台通常会通过 PORT 环境变量注入）
CONF_NODE_PORT = ""       # 分享链接里使用的对外端口（客户端连接的端口）；留空则自动决定：
                          # 有域名（TLS）时用 443，纯 IP 兜底（明文）时用实际监听端口
CONF_DOMAIN = ""          # 可选：部署平台分配/绑定的公网域名；留空则自动探测，
                          # 探测不到时退化为公网 IP + 明文 ws
CONF_FRONT_HOST = ""      # 可选：如果域名前面还套了一层自己的 CDN，这里填 CDN 域名作为连接地址；
                          # 留空则直接用 DOMAIN/自动探测结果作为连接地址
CONF_NAME = ""            # 节点名称前缀（留空则自动识别 国家-平台/ASN）
CONF_SUB = ""             # 订阅路径，默认 sub
CONF_CLEANUP_AFTER_DEPLOY = True  # 部署成功后自动清理不再需要的临时文件
# ============================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
log = logging.getLogger("py-ws")

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
HOME = Path(os.environ.get("HOME", "/tmp"))
APP_DIR = HOME / "py-ws"
UUID_FILE = APP_DIR / "uuid.txt"
TROJAN_FILE = APP_DIR / "trojan.txt"
XRAY_CONFIG_FILE = APP_DIR / "xray-config.json"
XRAY_DIR = APP_DIR / "xray"
XRAY_BIN_PATH = XRAY_DIR / "xray"
SUB_FILE = APP_DIR / "sub.txt"
INDEX_HTML_FILE = Path.cwd() / "index.html"

WS_PATH_VMESS = "/py-ws-vm"
WS_PATH_VLESS = "/py-ws-vl"
WS_PATH_TROJAN = "/py-ws-tr"

V_VMESS_PORT = 10000
V_VLESS_PORT = 10001
V_TROJAN_PORT = 10002

PATH_TO_PORT = {
    WS_PATH_VMESS: V_VMESS_PORT,
    WS_PATH_VLESS: V_VLESS_PORT,
    WS_PATH_TROJAN: V_TROJAN_PORT,
}

XRAY_ARCH_MAP = {
    "x86_64": "linux-64", "amd64": "linux-64",
    "aarch64": "linux-arm64-v8a", "arm64": "linux-arm64-v8a",
    "armv7l": "linux-arm32-v7a",
}

IP_RE = re.compile(
    r"^(\d{1,3}\.){3}\d{1,3}$|^[0-9a-fA-F:]+$"
)

FALLBACK_STATUS_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>py-ws</title></head>
<body style="font-family:sans-serif;max-width:600px;margin:60px auto;line-height:1.6">
<h1>py-ws</h1>
<p>This host is running a personal xray + WebSocket proxy service.</p>
<p>Subscription endpoint: <code>{sub_path}</code></p>
</body></html>
"""


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------
def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _read_or_create(path: Path, generator) -> str:
    if path.exists():
        return path.read_text().strip()
    value = generator()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    return value


def _http_get_text(url: str, timeout: int = 5) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode().strip()
    except Exception:
        return ""


def is_ip_address(host: str) -> bool:
    return bool(host) and bool(IP_RE.match(host))


# 常见部署平台：env 变量名 -> 平台展示名。按顺序探测，命中即用。
PLATFORM_DOMAIN_ENV = (
    ("RAILWAY_PUBLIC_DOMAIN", "Railway"),
    ("RENDER_EXTERNAL_HOSTNAME", "Render"),
    ("ZEABUR_DOMAIN", "Zeabur"),
    ("KOYEB_PUBLIC_DOMAIN", "Koyeb"),
    ("FLY_APP_NAME", "Fly.io"),          # 特殊：只给了 app 名，域名要拼 .fly.dev
)


def detect_platform_domain() -> tuple[str, str]:
    """按常见部署平台的环境变量自动探测公网域名。

    返回 (domain, platform_name)；探测不到时返回 ("", "")。
    """
    for env_name, platform_name in PLATFORM_DOMAIN_ENV:
        val = os.environ.get(env_name, "").strip()
        if not val:
            continue
        if env_name == "FLY_APP_NAME":
            return f"{val}.fly.dev", platform_name
        return val, platform_name

    # Cloud Foundry: 域名藏在 VCAP_APPLICATION 这个 JSON 里
    vcap = os.environ.get("VCAP_APPLICATION", "")
    if vcap:
        try:
            uris = json.loads(vcap).get("application_uris") or []
            if uris:
                return uris[0], "CloudFoundry"
        except Exception:
            pass

    return "", ""


def detect_public_ip() -> str:
    return (
        _http_get_text("https://api.ipify.org")
        or _http_get_text("https://ip.sb")
        or _http_get_text("https://ifconfig.co/ip")
    )


def resolve_domain(configured_domain: str) -> tuple[str, str, bool]:
    """解析最终使用的连接地址。

    优先级：显式配置的 DOMAIN > 平台自动探测 > 公网 IP 兜底。
    返回 (host, platform_name, is_ip)。
    """
    if configured_domain:
        return configured_domain, "", is_ip_address(configured_domain)

    domain, platform_name = detect_platform_domain()
    if domain:
        return domain, platform_name, is_ip_address(domain)

    ip = detect_public_ip()
    if ip:
        log.warning(
            "未探测到部署平台域名，退化为公网 IP + 明文 ws 兜底方案（无 TLS）：%s", ip
        )
        return ip, "", True

    return "", "", False


def detect_node_name(country: str, platform_name: str) -> str:
    """自动识别节点名：优先 国家-平台名，退化为 国家-ASN 运营商。"""
    if platform_name:
        return f"{country}-{platform_name}" if country else platform_name

    asn_org = _http_get_text("https://ipinfo.io/org") or _http_get_text("https://ifconfig.co/org")
    if asn_org:
        asn_org = re.sub(r"^AS\d+\s+", "", asn_org)
        asn_org = re.sub(r",?\s*Inc\.?$", "", asn_org)
        asn_org = re.sub(r",?\s*LLC\.?", "", asn_org)
        asn_org = re.sub(r",?\s*Ltd\.?", "", asn_org)
        asn_org = re.sub(r",?\s*Corp\.?", "", asn_org)
        asn_org = asn_org.strip()[:20]

    if country and asn_org:
        return f"{country}-{asn_org}"
    if country:
        return f"{country}-xray"
    return "xray"


@dataclass
class Settings:
    uuid: str
    trojan_pass: str
    inbound_port: int
    node_port: str
    domain: str
    sub_path: str
    tls: bool = True
    name: str = ""
    front_host: str = field(default="")
    cleanup_after_deploy: bool = True


def load_settings() -> Settings:
    APP_DIR.mkdir(parents=True, exist_ok=True)

    # UUID：配置区 > 环境变量 > 本地持久化文件 > 自动生成
    env_uuid = CONF_UUID or os.environ.get("UUID", "")
    node_uuid = env_uuid or _read_or_create(UUID_FILE, lambda: str(uuid.uuid4()))
    if env_uuid:
        UUID_FILE.write_text(env_uuid)

    # Trojan 密码：独立于 UUID，配置区 > 环境变量 > 本地持久化文件 > 自动生成
    env_trojan = CONF_TROJAN_PASS or os.environ.get("TROJAN_PASS", "")
    trojan_pass = env_trojan or _read_or_create(TROJAN_FILE, lambda: os.urandom(16).hex())
    if env_trojan:
        TROJAN_FILE.write_text(env_trojan)

    port_env = CONF_PORT or os.environ.get("PORT", "")
    inbound_port = int(port_env) if port_env else get_free_port()

    configured_domain = CONF_DOMAIN or os.environ.get("DOMAIN", "")
    domain, detected_platform, is_ip = resolve_domain(configured_domain)
    tls = not is_ip  # 有域名走 TLS(wss)；纯 IP 兜底走明文 ws

    front_host = CONF_FRONT_HOST or os.environ.get("FRONT_HOST", "")

    # 对外端口：显式配置优先；否则按是否 TLS 自动决定
    node_port_env = CONF_NODE_PORT or os.environ.get("NODE_PORT", "")
    if node_port_env:
        node_port = node_port_env
    else:
        node_port = "443" if tls else str(inbound_port)

    sub_raw = CONF_SUB or os.environ.get("SUB", "sub")
    sub_path = "/" + sub_raw.lstrip("/")

    name = CONF_NAME or os.environ.get("NAME", "")
    if not name:
        log.info("auto-detecting node name...")
        country = _http_get_text("https://ipinfo.io/country") or _http_get_text("https://ifconfig.co/country-iso")
        name = detect_node_name(country, detected_platform)

    cleanup_env = os.environ.get("CLEANUP_AFTER_DEPLOY", "")
    cleanup_after_deploy = (
        cleanup_env.strip().lower() not in ("0", "false", "no")
        if cleanup_env else CONF_CLEANUP_AFTER_DEPLOY
    )

    return Settings(
        uuid=node_uuid, trojan_pass=trojan_pass, inbound_port=inbound_port,
        node_port=node_port, domain=domain, sub_path=sub_path, tls=tls, name=name,
        front_host=front_host, cleanup_after_deploy=cleanup_after_deploy,
    )


# ---------------------------------------------------------------------------
# 二进制下载
# ---------------------------------------------------------------------------
def ensure_xray() -> Path:
    if XRAY_BIN_PATH.exists():
        return XRAY_BIN_PATH

    plat = XRAY_ARCH_MAP.get(platform.machine(), "linux-64")
    log.info("downloading xray for %s", plat)

    version = "v25.4.30"
    release_json = _http_get_text("https://api.github.com/repos/XTLS/Xray-core/releases/latest")
    if release_json:
        try:
            version = json.loads(release_json).get("tag_name", version)
        except Exception:
            pass

    url = f"https://github.com/XTLS/Xray-core/releases/download/{version}/Xray-{plat}.zip"
    XRAY_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = APP_DIR / "xray.zip"
    urllib.request.urlretrieve(url, zip_path)

    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(XRAY_DIR)

    XRAY_BIN_PATH.chmod(XRAY_BIN_PATH.stat().st_mode | stat.S_IEXEC)
    zip_path.unlink(missing_ok=True)
    log.info("xray ready at %s", XRAY_BIN_PATH)
    return XRAY_BIN_PATH


def find_system_xray() -> str:
    import shutil
    for candidate in ("xray", "/usr/local/bin/xray", "/usr/bin/xray"):
        path = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if path:
            return path
    return ""


# ---------------------------------------------------------------------------
# 部署后清理
# ---------------------------------------------------------------------------
def cleanup_deploy_artifacts():
    """清理部署完成后不再需要的临时/附带文件，减少数据目录体积。

    只清理明确用不到的内容，不会碰持久化文件（uuid.txt / trojan.txt）、
    运行时必需文件（xray-config.json）或结果文件（sub.txt）：
      - 残留的下载压缩包（正常流程 ensure_xray 已经删过，这里做兜底）
      - xray 官方发行包里附带的 geoip.dat / geosite.dat / LICENSE / README 等，
        当前配置的出站是 freedom 且没有路由规则，这些 geo 数据文件用不到
    """
    removed = []

    zip_path = APP_DIR / "xray.zip"
    if zip_path.exists():
        try:
            zip_path.unlink()
            removed.append(zip_path.name)
        except OSError as e:
            log.debug("cleanup: failed to remove %s: %s", zip_path, e)

    unused_names = ("geoip.dat", "geosite.dat", "LICENSE", "LICENSE.txt", "README.md", "README.zh-CN.md")
    if XRAY_DIR.exists():
        for name in unused_names:
            p = XRAY_DIR / name
            if p.exists() and p != XRAY_BIN_PATH:
                try:
                    p.unlink()
                    removed.append(p.name)
                except OSError as e:
                    log.debug("cleanup: failed to remove %s: %s", p, e)

    if removed:
        log.info("cleanup: removed unused file(s): %s", ", ".join(removed))
    else:
        log.info("cleanup: nothing to remove")


# ---------------------------------------------------------------------------
# xray 配置与启动
# ---------------------------------------------------------------------------
def build_xray_config(settings: Settings) -> dict:
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {
                "port": V_VMESS_PORT, "listen": "127.0.0.1", "protocol": "vmess",
                "settings": {"clients": [{"id": settings.uuid, "alterId": 0}]},
                "streamSettings": {"network": "ws", "wsSettings": {"path": WS_PATH_VMESS}},
            },
            {
                "port": V_VLESS_PORT, "listen": "127.0.0.1", "protocol": "vless",
                "settings": {"clients": [{"id": settings.uuid, "flow": ""}], "decryption": "none"},
                "streamSettings": {"network": "ws", "wsSettings": {"path": WS_PATH_VLESS}},
            },
            {
                "port": V_TROJAN_PORT, "listen": "127.0.0.1", "protocol": "trojan",
                "settings": {"clients": [{"password": settings.trojan_pass}]},
                "streamSettings": {"network": "ws", "wsSettings": {"path": WS_PATH_TROJAN}},
            },
        ],
        "outbounds": [{"protocol": "freedom", "settings": {}}],
    }


def _stream_xray_logs(proc: subprocess.Popen):
    """把 xray 的 stdout/stderr 转发到本进程日志，避免被 DEVNULL 吞掉。"""
    def _pump(pipe, level):
        for line in iter(pipe.readline, ""):
            line = line.rstrip()
            if line:
                log.log(level, "[xray] %s", line)
        pipe.close()

    if proc.stdout:
        threading.Thread(target=_pump, args=(proc.stdout, logging.INFO), daemon=True).start()
    if proc.stderr:
        threading.Thread(target=_pump, args=(proc.stderr, logging.WARNING), daemon=True).start()


def start_xray(settings: Settings) -> subprocess.Popen:
    cfg = build_xray_config(settings)
    XRAY_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    XRAY_CONFIG_FILE.write_text(json.dumps(cfg, indent=2))

    xray_bin = find_system_xray() or str(ensure_xray())
    log.info("starting xray: %s run -config %s", xray_bin, XRAY_CONFIG_FILE)
    proc = subprocess.Popen(
        [xray_bin, "run", "-config", str(XRAY_CONFIG_FILE)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    _stream_xray_logs(proc)
    return proc


def watch_xray(proc: subprocess.Popen, on_exit):
    """存活监控：xray 意外退出时触发回调，把真实退出码原样透传给回调，
    避免上层统一吞成 1，方便根据退出码排查（如配置错误 vs 端口占用 vs 被信号杀死）。
    """
    def _watch():
        code = proc.wait()
        log.error("xray process exited unexpectedly with code %s", code)
        on_exit(code)

    threading.Thread(target=_watch, daemon=True).start()


# ---------------------------------------------------------------------------
# 分享链接 / 订阅内容
# ---------------------------------------------------------------------------
def build_links(settings: Settings, name: str) -> str:
    host = settings.domain
    # 连接地址：默认直接用部署平台域名（或探测到的公网 IP）；
    # 如果自己在前面套了 CDN，用 front_host 覆盖
    front_domain = settings.front_host or host
    port = settings.node_port
    security = "tls" if settings.tls else "none"

    vmess_obj = {
        "v": "2", "ps": name, "add": front_domain, "port": port,
        "id": settings.uuid, "aid": "0", "scy": "auto", "net": "ws", "type": "none",
        "host": host, "path": WS_PATH_VMESS, "tls": ("tls" if settings.tls else ""),
    }
    if settings.tls:
        vmess_obj["sni"] = host
    vmess_link = "vmess://" + base64.b64encode(json.dumps(vmess_obj).encode()).decode()

    sni_part = f"&sni={host}" if settings.tls else ""
    vless_link = (
        f"vless://{settings.uuid}@{front_domain}:{port}"
        f"?encryption=none&security={security}{sni_part}&type=ws&host={host}"
        f"&path={urllib.parse.quote(WS_PATH_VLESS)}#{urllib.parse.quote(name)}"
    )

    trojan_link = (
        f"trojan://{settings.trojan_pass}@{front_domain}:{port}"
        f"?security={security}{sni_part}&type=ws&host={host}"
        f"&path={urllib.parse.quote(WS_PATH_TROJAN)}#{urllib.parse.quote(name)}"
    )

    return "\n".join([vmess_link, vless_link, trojan_link])


def build_subscription(links_text: str) -> str:
    return base64.b64encode(links_text.encode()).decode()


# ---------------------------------------------------------------------------
# HTTP / WebSocket 转发服务
# 直接监听部署平台转发过来的端口，一个服务同时承担三件事：
#   1. 订阅路径 -> 返回订阅内容
#   2. WS 协议路径 + 合法的 Upgrade: websocket 请求 -> 原样转发给 xray 对应的本地入站端口
#   3. WS 协议路径但不是 Upgrade 请求 -> 400（避免把普通 HTTP 探测流量也怼给 xray）
#   4. 其它路径 -> 落地页（index.html 或内置状态页）
# ---------------------------------------------------------------------------
def _pipe(src: socket.socket, dst: socket.socket):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _recv_headers(sock: socket.socket, max_size: int = 65536) -> bytes:
    """循环读取直到拿到完整请求头（\\r\\n\\r\\n），避免分片/长请求头解析失败。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf += chunk
        if len(buf) > max_size:
            break
    return buf


def _parse_headers(header_part: bytes) -> dict:
    """把原始请求头解析成 {lower_case_name: value} 字典，用于判断 Upgrade 等。"""
    headers = {}
    lines = header_part.split(b"\r\n")[1:]  # 跳过 request line
    for line in lines:
        if b":" not in line:
            continue
        k, _, v = line.partition(b":")
        try:
            headers[k.strip().lower().decode()] = v.strip().decode()
        except UnicodeDecodeError:
            continue
    return headers


def _is_websocket_upgrade(headers: dict) -> bool:
    """标准 WebSocket 握手判定：Connection 里包含 Upgrade，且 Upgrade: websocket。

    Connection 头允许是逗号分隔的多个 token（如 "keep-alive, Upgrade"），
    这里做大小写不敏感、按逗号切分后再比较，避免用简单的子串匹配误判。
    """
    connection_tokens = {t.strip().lower() for t in headers.get("connection", "").split(",")}
    upgrade_val = headers.get("upgrade", "").strip().lower()
    return "upgrade" in connection_tokens and upgrade_val == "websocket"


def _forward_to_xray(client_sock: socket.socket, header_part: bytes, rest: bytes, target_port: int):
    client_sock.settimeout(None)
    try:
        upstream = socket.create_connection(("127.0.0.1", target_port), timeout=5)
    except OSError as e:
        log.debug("failed to connect upstream xray port %s: %s", target_port, e)
        client_sock.close()
        return

    upstream.sendall(header_part + b"\r\n\r\n" + rest)
    t1 = threading.Thread(target=_pipe, args=(client_sock, upstream), daemon=True)
    t2 = threading.Thread(target=_pipe, args=(upstream, client_sock), daemon=True)
    t1.start(); t2.start()
    t1.join(); t2.join()
    client_sock.close()
    upstream.close()


def _send_bad_request(client_sock: socket.socket, reason: str = "Bad Request"):
    body = reason.encode()
    resp = (
        "HTTP/1.1 400 Bad Request\r\nContent-Type: text/plain; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    ).encode() + body
    try:
        client_sock.sendall(resp)
    except OSError:
        pass
    client_sock.close()


def _load_index_html(sub_path: str) -> str:
    if INDEX_HTML_FILE.exists():
        try:
            return INDEX_HTML_FILE.read_text(encoding="utf-8")
        except Exception as e:
            log.warning("failed to read index.html: %s", e)
    return FALLBACK_STATUS_PAGE.format(sub_path=sub_path)


def run_public_server(settings: Settings, sub_content_holder: dict):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("0.0.0.0", settings.inbound_port))
    srv.listen(128)
    log.info("public server listening on 0.0.0.0:%s", settings.inbound_port)

    index_html = _load_index_html(settings.sub_path)

    def handle(client_sock: socket.socket):
        try:
            client_sock.settimeout(10)
            buf = _recv_headers(client_sock)
            if b"\r\n\r\n" not in buf:
                client_sock.close()
                return

            header_part, _, rest = buf.partition(b"\r\n\r\n")
            request_line = header_part.split(b"\r\n", 1)[0].decode(errors="ignore")
            try:
                _, path, _ = request_line.split(" ", 2)
            except ValueError:
                client_sock.close()
                return
            path = path.split("?")[0]

            target_port = PATH_TO_PORT.get(path)
            if target_port is not None:
                headers = _parse_headers(header_part)
                if not _is_websocket_upgrade(headers):
                    # 命中协议路径但不是合法的 WS 握手请求（比如探测器直接 GET），
                    # 明确拒绝，不要把非 WS 流量转发给 xray。
                    _send_bad_request(client_sock, "Upgrade Required")
                    return
                _forward_to_xray(client_sock, header_part, rest, target_port)
                return

            if path == settings.sub_path:
                body = sub_content_holder.get("content", "").encode()
                headers = (
                    "HTTP/1.1 200 OK\r\nContent-Type: text/plain; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n"
                ).encode()
            else:
                body = index_html.encode("utf-8")
                headers = (
                    "HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n"
                ).encode()

            client_sock.sendall(headers + body)
            client_sock.close()
        except Exception as e:
            log.debug("public server error: %s", e)
            client_sock.close()

    while True:
        client, _ = srv.accept()
        threading.Thread(target=handle, args=(client,), daemon=True).start()


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def main():
    settings = load_settings()

    if not settings.domain:
        log.error(
            "未设置 DOMAIN，且自动探测平台域名 / 公网 IP 均失败，"
            "请设置环境变量 DOMAIN（或填 CONF_DOMAIN）后重试"
        )
        sys.exit(1)

    log.info(
        "settings: inbound_port=%s node_port=%s domain=%s tls=%s sub_path=%s name=%s front_host=%s",
        settings.inbound_port, settings.node_port, settings.domain, settings.tls,
        settings.sub_path, settings.name, settings.front_host,
    )
    if not settings.tls:
        log.warning(
            "当前为 IP + 明文 ws 兜底模式（无 TLS）：流量未加密，仅建议在临时/测试场景使用；"
            "如需加密传输，请设置 DOMAIN 为一个已经在部署平台配置好 TLS 的域名。"
        )

    xray_proc = start_xray(settings)
    log.info("xray started, pid=%s", xray_proc.pid)

    def _on_xray_exit(code):
        # 原样透传 xray 的真实退出码，而不是统一吞成 1，
        # 方便通过退出码区分配置错误 / 端口占用 / 被信号杀死等情况。
        os._exit(code if isinstance(code, int) else 1)

    watch_xray(xray_proc, _on_xray_exit)

    sub_holder = {"content": ""}
    threading.Thread(target=run_public_server, args=(settings, sub_holder), daemon=True).start()

    links_text = build_links(settings, settings.name)
    sub_b64 = build_subscription(links_text)
    sub_holder["content"] = sub_b64
    SUB_FILE.write_text(sub_b64)

    scheme = "https" if settings.tls else "http"
    print("================= 订阅内容 =================")
    print(sub_b64)
    print("============================================")
    print(f"订阅地址: {scheme}://{settings.domain}{settings.sub_path}")
    print(f"节点文件: {SUB_FILE}")

    if settings.cleanup_after_deploy:
        cleanup_deploy_artifacts()

    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        log.info("shutting down")
        xray_proc.terminate()


if __name__ == "__main__":
    main()
