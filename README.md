# py-ws

基于 xray 的多协议代理工具，支持 VMess / VLESS / Trojan 三协议，统一走 WebSocket。

与姊妹项目 [py-argo](../py-argo) 的区别：py-argo 用 Cloudflare Argo 隧道自动获得公网域名和
TLS 终止；py-ws 去掉了 Argo 隧道，假定**部署平台本身就会分配公网域名并做 TLS 终止**（容器只需
监听平台转发过来的内部端口）。适合部署在 Render / Railway / Zeabur / Koyeb / Fly.io / Cloud
Foundry 等自带域名+HTTPS 边缘的平台。

域名不是必填项，`DOMAIN` 环境变量按以下优先级解析：

1. 显式设置的 `DOMAIN`（或 `app.py` 里的 `CONF_DOMAIN`）
2. 常见部署平台的环境变量自动探测（Railway / Render / Zeabur / Koyeb / Fly.io / Cloud Foundry）
3. 公网 IP 兜底：探测不到平台域名时，退化为「公网 IP + 明文 ws（不走 TLS）」，节点依然可用，
   但流量未加密，仅建议临时/测试场景使用
4. 占位域名兜底：连公网 IP 都探测不到时，用占位域名继续启动（不会报错退出），但生成的节点无法
   直接连接，需要尽快手动设置 `DOMAIN` 后重启

## 目录结构

```
app.py       全部逻辑：配置加载、xray 下载、xray 配置与启动、
             分享链接与订阅生成、HTTP/WS 转发服务、部署后清理、主入口
Dockerfile   Docker 部署用
index.html   可选，自定义落地页（与 app.py 同目录放置即可，不放则使用内置极简状态页）
```

## 部署方式

### 方式一：Docker

**直接拉取已构建好的镜像**（推荐，CI 已自动构建并推送到 GHCR）：

```bash
docker pull ghcr.io/你的GitHub用户名/py-ws:latest
docker run -d \
  -e DOMAIN=你的域名 \
  -p 3000:3000 \
  -e PORT=3000 \
  ghcr.io/你的GitHub用户名/py-ws:latest
```

**或者本地自行构建**：

```bash
docker build -t py-ws .
docker run -d \
  -e DOMAIN=你的域名 \
  -p 3000:3000 \
  -e PORT=3000 \
  py-ws
```

> 注意：这里的 `PORT`/容器监听端口是**内部端口**，实际客户端连接用的是 `DOMAIN` + `NODE_PORT`
> （有域名时默认 443）。部署平台负责把公网流量转发到容器的内部端口，容器本身不需要、也不做
> TLS。`DOMAIN` 不是必填，不设置会自动探测；但裸机/自建 Docker 环境通常探测不到平台域名，建议
> 显式设置。

### 方式二：源码文件上传部署

将 `app.py` 上传到目标平台，确保运行环境有 Python 3.9+ 且能访问 GitHub（用于首次运行时下载
xray），设置好 `DOMAIN` 环境变量后运行：

```bash
python app.py
```

## 环境变量

| 变量名 | 说明 | 默认值 |
|---|---|---|
| `DOMAIN` | 可选。部署平台分配/绑定的公网域名，用作节点的 TLS SNI 与 Host；留空则按上方优先级自动探测 | 自动探测，见上方说明 |
| `UUID` | VMess/VLESS 统一 ID | 自动生成并持久化 |
| `TROJAN_PASS` | Trojan 密码，与 `UUID` 相互独立 | 自动生成并持久化 |
| `PORT` | 容器内实际监听端口（部署平台通常会自动注入） | 自动分配空闲端口 |
| `NODE_PORT` | 分享链接里客户端连接用的对外端口 | 未显式设置时自动决定：有域名（TLS）用 `443`，纯 IP 兜底（明文）用实际监听端口 |
| `FRONT_HOST` | 可选，如果 `DOMAIN` 前面自己还套了一层 CDN，这里填 CDN 域名作为连接地址；`DOMAIN` 仍作为 SNI/Host。**注意**：如果这层 CDN 是 Cloudflare 橙云代理（TLS 在 CF 边缘终止），正确做法通常是 SNI/Host 也要用 CDN 域名而不是 `DOMAIN`，当前版本暂不支持这种模式，需要拿到分享链接后手动改 `sni`/`host` 字段 | 留空则直接用 `DOMAIN` |
| `NAME` | 节点名称前缀 | 自动识别（国家代码-ASN 运营商，如 `US-Cloudflare`），识别失败则为 `xray` |
| `SUB` | 订阅路径 | `sub` |
| `CLEANUP_AFTER_DEPLOY` | 部署成功、生成订阅后是否自动清理 xray 发行包里用不到的附带文件，设为 `0`/`false`/`no` 可关闭 | `true` |

也可以不设环境变量，直接改 `app.py` 开头的 `CONF_*` 常量，优先级高于环境变量。

## 数据文件位置

运行时数据默认存放在 `~/py-ws/`：
- `uuid.txt`：持久化的 UUID
- `trojan.txt`：持久化的 Trojan 密码（与 UUID 独立）
- `xray-config.json`：生成的 xray 配置
- `xray/`：下载的 xray 二进制
- `sub.txt`：生成的订阅内容（base64）

## 注意事项

- xray 首次运行会自动下载，需要能访问 GitHub。
- 不设置 `DOMAIN` 也能启动，程序会自动探测平台域名 / 公网 IP，最坏情况下用占位域名继续运行；
  但只有真正拿到一个能用的域名（或至少公网 IP），生成的节点才连得通，建议尽量显式设置 `DOMAIN`。
- 部署平台需要把公网 443（或你配置的 `NODE_PORT`）流量转发到容器监听的内部端口，并自行完成
  TLS 终止；如果平台不提供这个能力，也没有公网 IP 可用，py-ws 这种架构不适用，请考虑用 py-argo。
- 三个协议路径只接受合法的 WebSocket 握手请求（带 `Upgrade: websocket`）；普通 HTTP 请求命中
  这几个路径会直接返回 400，不会被转发给 xray，避免探测流量被当成代理流量处理。
- xray 的运行日志会实时输出到控制台；xray 进程如果意外退出，主进程会记录错误并用 xray 的真实
  退出码退出（不会统一吞成固定值），便于部署平台自动重启，也方便通过退出码排查是配置错误、
  端口占用还是被信号杀死。
- 部署成功、生成订阅后会自动清理 xray 发行包里用不到的附带文件（geoip.dat/geosite.dat/LICENSE
  等），不会动 `uuid.txt`/`trojan.txt`/`xray-config.json`/`sub.txt`。如需保留这些文件排查问题，
  设置环境变量 `CLEANUP_AFTER_DEPLOY=false` 关闭。
- 如果 `app.py` 同目录下放了 `index.html`，会作为首页返回；不放则使用内置的极简状态页。
- 仅供学习研究使用，部署前请确认符合所在平台和当地法律法规的要求。
