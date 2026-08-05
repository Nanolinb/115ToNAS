# QNAP 部署记录（TS-453D / QTS 5.x 实测）

本文记录 2026-07-24/25 在 QNAP TS-453D（8G，QTS 5.2.9）上的真实部署过程与网络适配方案，
供重装或迁移到其他 QNAP 时参考。

## 部署方式

1. 代码放在 **`/share/CACHEDEV1_DATA/Container/115-media-hub`**（真实存储卷），
   `config/` 子目录存数据库与密钥
2. Container Station（docker 27.x）构建镜像：`docker compose build`
3. 启动：`docker compose up -d`，端口 8115
4. 构建期网络：Docker Hub 直连不通时使用 DaoCloud 镜像源
   （`docker.m.daocloud.io/library/python:3.12-slim`）；
   Debian/PyPI 包走 `BUILD_PROXY` 构建参数指定的代理（默认 NAS 上的 mihomo）

### ⚠️ 路径陷阱：/share 是 16MB tmpfs（2026-08-05 实踩）

**`/share` 本身是内存盘**，上面只有 QTS 自动建的共享文件夹符号链接。
`/share/Container` 不是注册的共享文件夹——一次重启后符号链接消失，docker 按
bind 源路径自动创建了**真实的 tmpfs 目录**，之后所有部署/数据库都写进了内存盘
（占满 /share tmpfs 触发「RamDisk 空间不足」系统告警；真断电重启会全丢）。
修复：项目整体迁到真实卷 `/share/CACHEDEV1_DATA/Container/115-media-hub`，
compose/部署/docker cp 全部用真实绝对路径，不再经过 /share 符号链接。
**在任何新 QNAP 上部署时，直接使用 `/share/CACHEDEV1_DATA/...` 真实路径。**

## 网络适配（重点）

### 1. 容器使用主机网络

`network_mode: host`。原因：QTS 容器默认桥接会经过内部虚拟网关
（如 172.29.7.254），实测该网关不稳定，容器外网时通时断；主机模式直接用
NAS 的网络栈，稳定且流媒体传输少一层 NAT。

### 2. 115 API IP 固定

实测部分宽带环境下 115 API 解析到的默认 IP（如 47.113.23.100）被运营商/防火墙
封锁，但同网关的其他 IP（如 47.113.24.196）可达且服务完整。解法是在
docker-compose.yml 中用 extra_hosts 固定：

```yaml
extra_hosts:
  - "qrcodeapi.115.com:47.113.24.196"
  - "webapi.115.com:47.113.24.196"
  - "proapi.115.com:47.113.24.196"
  - "userapi.115.com:47.113.24.196"
  - "passportapi.115.com:47.113.24.196"
  - "uplb.115.com:47.113.24.196"
```

排障方法：先用 `nslookup webapi.115.com` 得到默认 IP，`curl --resolve` 逐个
测试 115.com 等可达 IP 是否也能正常响应 API（HTTP 200 + `state:1`），找到可用
IP 后固定。若 115 日后变更网关，重复此流程更新 IP 即可。

### 3. DNS

- 容器/主机 DNS 建议公共 DNS（223.5.5.5 / 119.29.29.29）
- 若 NAS 装有 Tailscale：执行 `tailscale up --accept-dns=false`，
  避免 MagicDNS 劫持导致公网域名无法解析（不影响 Tailscale 远程访问）

### 4. TMDB（封面源）

api.themoviedb.org 在国内被墙，直连不可用。可选方案：
- 路由器/局域网代理：让 NAS 走代理访问 TMDB（应用读取环境变量
  `HTTPS_PROXY`，在 compose 的 environment 里加一行即可）
- 或暂不刮封面，其余功能不受影响

## 运维命令速查

```bash
# NAS 上 docker 命令前缀（Dash 等非 root 用户）
export PATH=/share/CACHEDEV1_DATA/.qpkg/container-station/usr/bin:$PATH
export DOCKER_CONFIG=/share/CACHEDEV1_DATA/Container/115-media-hub/.docker
export HOME=/tmp

docker logs media-hub-115 --tail 50     # 看日志
docker restart media-hub-115            # 重启
cd /share/CACHEDEV1_DATA/Container/115-media-hub && docker compose up -d --build   # 更新代码后重建
```

注意：非 root 用户必须设置 `DOCKER_CONFIG` 与 `HOME` 到可写目录，
否则 docker 命令会报 `mkdir .../homes/<user>: permission denied`。

## 安全说明（v1.2.0 起）

- TMDB Key / assrt Token / 115 Cookie 均 AES-256-GCM 加密存储，
  密钥在 `config/secret.key`（权限 600）
- 设置接口只返回掩码（`****后4位`），完整密钥不会离开服务端
- 观影端默认免密（可在管理端设观影密码），管理端独立密码
- `config/` 目录含全部敏感数据，已加入 .gitignore，永远不会进入代码仓库
