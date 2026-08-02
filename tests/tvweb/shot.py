#!/usr/bin/env python3
"""TV 主页布局截图：多视口模拟，验证卡片数量/左栏收拢/Hero 位置。

用法：
    NAS=http://192.168.2.109:8115 python shot.py [xgimi|xiaomi|raw4k]

    xgimi  = 960x540 dsf=2（极米 R10 Plus 实测 WebView 视口）
    xiaomi = 1280x720 dsf=1（小米电视参照）
    raw4k  = 3840x2160 dsf=1（未按 dsf 缩放的极端 4K）
截图保存为 shot-<mode>.png。浏览器内注入 MediaHubNative 桩以激活 tv-mode。
"""
import os
import sys
from playwright.sync_api import sync_playwright

NAS = os.environ.get("NAS", "http://192.168.2.109:8115")

CONF = {
    "xgimi": {"width": 960, "height": 540, "dsf": 2},
    "xiaomi": {"width": 1280, "height": 720, "dsf": 1},
    "raw4k": {"width": 3840, "height": 2160, "dsf": 1},
}

mode = sys.argv[1] if len(sys.argv) > 1 else "xgimi"
c = CONF[mode]

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(
        viewport={"width": c["width"], "height": c["height"]},
        device_scale_factor=c["dsf"],
    )
    page.add_init_script(
        "window.MediaHubNative = { getResume: function() { return '{}'; } };"
    )
    page.goto(NAS + "/", wait_until="load")
    page.wait_for_timeout(2500)
    page.screenshot(path=f"shot-{mode}.png")
    browser.close()
    print(f"saved shot-{mode}.png  ({c['width']}x{c['height']} dsf={c['dsf']})")
