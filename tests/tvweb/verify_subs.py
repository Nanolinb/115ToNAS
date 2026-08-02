#!/usr/bin/env python3
"""字幕轨道验证：真实浏览器播放指定 media id，检查 textTracks 状态。

用法：
    NAS=http://192.168.2.109:8115 python verify_subs.py [id ...]
    默认验证 media id 26 27。
重点看：默认只有一条轨 showing（forced 内嵌轨不得叠屏）、
外挂轨 cues 数量（0 = 转换/加载失败）、内嵌轨是否全部列出。
"""
import os
import sys
from playwright.sync_api import sync_playwright

NAS = os.environ.get("NAS", "http://192.168.2.109:8115")
IDS = [int(x) for x in sys.argv[1:]] or [26, 27]

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 900})
    # 桩不带 play()：强制走网页播放器（带 play 会走原生桥路径）
    page.add_init_script(
        "window.MediaHubNative = { getResume: function() { return '{}'; } };"
    )
    page.goto(NAS + "/", wait_until="load")
    page.wait_for_timeout(2000)
    for mid in IDS:
        page.evaluate(f"playMedia({mid})")
        page.wait_for_timeout(3500)
        info = page.evaluate("""() => {
            const v = document.querySelector('#playerVideo');
            if (!v) return {err: 'no video el'};
            return {
                readyState: v.readyState,
                tracks: Array.from(v.textTracks).map(t => ({
                    label: t.label, mode: t.mode,
                    cues: t.cues ? t.cues.length : null,
                })),
            };
        }""")
        print(f"media {mid}: readyState={info.get('readyState')}")
        for t in info.get("tracks", []):
            print(f"   {t['mode']:9} cues={t['cues']}  {t['label']}")
        page.evaluate("""() => {
            const v = document.querySelector('#playerVideo');
            if (v) v.pause();
            const m = document.querySelector('#playerMask');
            if (m) m.classList.add('hidden');
        }""")
        page.wait_for_timeout(500)
    browser.close()
