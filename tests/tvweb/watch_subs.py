#!/usr/bin/env python3
"""字幕长时观察：播放 15 秒 + 模拟拖动，确认没有字幕轨被浏览器自动点亮叠屏。

用法：
    NAS=http://192.168.2.109:8115 python watch_subs.py [id]
    默认 media id 26。
"""
import os
import sys
from playwright.sync_api import sync_playwright

NAS = os.environ.get("NAS", "http://192.168.2.109:8115")
MID = int(sys.argv[1]) if len(sys.argv) > 1 else 26

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1600, "height": 900})
    page.add_init_script(
        "window.MediaHubNative = { getResume: function() { return '{}'; } };"
    )
    page.goto(NAS + "/", wait_until="load")
    page.wait_for_timeout(2500)
    page.evaluate(f"playMedia({MID})")
    for t in (3, 8, 15):
        page.wait_for_timeout(t * 1000 if t == 3 else (t - (3 if t == 8 else 8)) * 1000)
        info = page.evaluate("""() => {
            const v = document.querySelector('#playerVideo');
            return { rs: v.readyState, t: Math.round(v.currentTime),
                     showing: Array.from(v.textTracks)
                        .filter(x => x.mode === 'showing').map(x => x.label) };
        }""")
        print(f"{t}s:", info)
    # 模拟转码流拖动（自绘进度条会重启 src，Chrome 可能重选轨道）
    page.evaluate("""() => {
        const bar = document.querySelector('#seekBar, .seek-bar, [data-seek]');
        if (bar) { const r = bar.getBoundingClientRect();
            bar.dispatchEvent(new MouseEvent('click',
                {clientX: r.left + r.width * 0.6, bubbles: true})); }
    }""")
    page.wait_for_timeout(5000)
    info = page.evaluate("""() => {
        const v = document.querySelector('#playerVideo');
        return { rs: v.readyState,
                 showing: Array.from(v.textTracks)
                    .filter(x => x.mode === 'showing').map(x => x.label) };
    }""")
    print("after seek:", info)
    browser.close()
