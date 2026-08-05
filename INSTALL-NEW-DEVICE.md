# 新设备安装指南（投影仪 / 第二台 QNAP）

本文记录把 Dash Spark Media 安装到新设备的完整步骤，2026-07-30 以
极米 RS10 投影仪 + QNAP TS-464C 为例编写。备份包制作方式：
电视端 `adb pull` APK；NAS 端打包 `/share/CACHEDEV1_DATA/Container/115-media-hub`
（数据库用 SQLite backup API 做一致性快照，见仓库备份记录）。

## 一、QNAP 部署服务端（TS-464C）

前置：App Center 装好 Container Station；片源共享文件夹已建好
（默认 `/share/Multimedia`，没有就在 File Station 建一个，或后续改 compose）。

1. **拷贝项目文件**：备份包 `115-media-hub-backup-vX.XX.XX.tar.gz`
   传到 464C 的 `/share/CACHEDEV1_DATA/Container/` 下解压：

   ```bash
   cd /share/CACHEDEV1_DATA/Container && tar xzf 115-media-hub-backup-vX.XX.XX.tar.gz
   ```

   得到 `/share/CACHEDEV1_DATA/Container/115-media-hub/`。也可以直接 `git clone`
   仓库——备份包的好处是连数据库一起带过来。

2. **（可选）迁移旧数据**：要用备份里的数据库（影片库、115 登录态）
   而不是从零开始：

   ```bash
   cd /share/CACHEDEV1_DATA/Container/115-media-hub/config
   mv secret-backup.key secret.key && chmod 600 secret.key
   mv data-snapshot.db data.db   # 一致性快照覆盖，WAL 残件删掉
   rm -f data.db-shm data.db-wal
   ```

   注意：`secret.key` 和 `data.db` 必须来自同一份备份，否则凭据解不开。

3. **检查 `docker-compose.yml`**：确认 `volumes` 里
   `- /share/Multimedia:/media` 左边是本机真实的片源路径；
   下载目录结构和原 NAS 一致时，数据库指向记录可直接复用。

4. **构建启动**（SSH 进 NAS）：

   ```bash
   cd /share/CACHEDEV1_DATA/Container/115-media-hub
   docker compose build
   docker compose up -d
   ```

   Docker Hub 拉不动基础镜像时按 DEPLOY-QNAP.md 改用 DaoCloud 镜像源。

5. **验证**：浏览器开 `http://<NAS的IP>:8115` 能进观影页、`/admin`
   能进管理端。全新安装：管理端扫码登录 115 → 设置下载目录 → 手动扫描一次。

6. **网络适配**：115 API 不通时按 DEPLOY-QNAP.md 第 2 节用
   `curl --resolve` 重测可用 IP 更新 compose 的 `extra_hosts`；
   TMDB 刮不到封面就配 `HTTPS_PROXY`。

## 二、投影仪 / 电视安装 APK（极米 RS10）

1. APK（如 `DashSparkMedia-1.1.0-embedded.apk`）拷到 **U盘根目录**，
   插上投影仪 USB 口。
2. 投影仪打开「资源管理器/文件管理」→ U盘 → 点 APK 安装；
   首次会提示**允许安装未知来源应用**，按提示开权限。
   GMUI 拦截不让装时：把文件名改成短一点的英文名再试，
   或先装「当贝市场」再从里面装。
3. 装完打开 App，**首次启动会弹「服务器地址」输入框**
   （默认是旧 NAS 地址），改成新 NAS 的地址，如 `http://192.168.1.20:8115`。
   地址存错没关系：连不上时会自动再弹输入框。
4. 投影仪必须和服务端 NAS 在**同一局域网**；千兆内网直连播放不需要外网。

## 备注

- 中高端投影（极米 RS10 及以上）杜比相关解码调用代码里都保留，
  直接装即可；个别无声影片在管理端开实时音频转码（服务端开关）。
- 多台 NAS 同时跑没问题，但数据库各自独立——影片库、观看进度不互通，
  投影仪指向哪台就播哪台的库。
