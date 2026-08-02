# TV/Web 端自动化验证（Playwright）

海报墙/网页播放器的真实浏览器回归测试。本项目 TV App 是 WebView 壳，
海报墙/弹窗/网页播放器的 HTML/CSS/JS 全部由服务端 `static/` 提供，
所以改 `static/` 后**先跑这里的验证再热更/发布**，不要凭感觉交付。

## 首次安装

```bash
cd tests/tvweb
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
# 国内网络用镜像下载浏览器（否则容易超时）
PLAYWRIGHT_DOWNLOAD_HOST=https://registry.npmmirror.com/-/binary/playwright \
  ./venv/bin/playwright install chromium
# 需要复现 Safari 行为时再装：./venv/bin/playwright install webkit
```

## 使用

所有脚本用环境变量 `NAS` 指定服务端（默认 `http://192.168.2.109:8115`，改成你的）：

```bash
# 1. TV 主页布局截图（卡片数量/左栏收拢/Hero 位置）
NAS=http://<NAS的IP>:8115 ./venv/bin/python shot.py xgimi    # 极米 960x540(dsf=2)
NAS=http://<NAS的IP>:8115 ./venv/bin/python shot.py xiaomi   # 小米 1280x720
NAS=http://<NAS的IP>:8115 ./venv/bin/python shot.py raw4k    # 3840 极端视口

# 2. 字幕轨道验证（默认只有一条 showing，外挂轨 cues>0，内嵌轨齐全）
NAS=http://<NAS的IP>:8115 ./venv/bin/python verify_subs.py 26 27   # 参数=media id

# 3. 字幕长时观察（15 秒播放 + 模拟拖动，forced 轨不得叠屏）
NAS=http://<NAS的IP>:8115 ./venv/bin/python watch_subs.py 26
```

media id 在管理端影片详情 URL 或数据库里查。

## 原理与坑

- 脚本注入 `window.MediaHubNative` 桩激活 `tv-mode`（TV 布局）；
  `verify_subs.py` 的桩**故意不带 `play()`**，否则会走原生桥路径、
  网页播放器不初始化（字幕无从验起）。
- 页面有轮询，`goto` 必须用 `wait_until="load"`，用 `networkidle` 会卡死。
- Playwright 无法直接模拟 Android WebView 的 `meta viewport` 缩放行为，
  视口尺寸/dsf 按真机实测值配置（极米 R10 Plus = 960x540 dsf=2）。
- 截图产物 `shot-*.png` 与 `venv/` 已加入 .gitignore，不要提交。
