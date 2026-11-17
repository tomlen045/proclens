#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""proclens 900x383 封面（PIL 渲染）。

对话钩子形态（自研工具，不晒星数）：白句提问 + 金句反驳 + 三条硬指标。
符号自绘，中文黑体 / ASCII Menlo。自 kcfilet/make_cover.py 改文案而来。
"""
import os

from PIL import Image, ImageDraw, ImageFont

W, H = 900, 383
BG_LEFT = (11, 14, 26)     # #0b0e1a
BG_RIGHT = (26, 15, 46)    # #1a0f2e
GOLD = (240, 180, 41)      # #f0b429
WHITE = (235, 238, 245)
GRAY = (139, 148, 158)     # #8b949e
GREEN = (63, 185, 80)      # #3fb950
RED = (248, 81, 73)        # #f85149

HEITI = "/System/Library/Fonts/STHeiti Medium.ttc"
MENLO = "/System/Library/Fonts/Menlo.ttc"

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.expanduser("~/Desktop/proclens-cover.png")


def f(path, size):
    return ImageFont.truetype(path, size)


def main():
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    # 135deg 渐变
    for x in range(W):
        t = x / W
        r = int(BG_LEFT[0] + (BG_RIGHT[0] - BG_LEFT[0]) * t)
        g = int(BG_LEFT[1] + (BG_RIGHT[1] - BG_LEFT[1]) * t)
        b = int(BG_LEFT[2] + (BG_RIGHT[2] - BG_LEFT[2]) * t)
        d.line([(x, 0), (x, H)], fill=(r, g, b))

    # 左金竖条锚
    d.rectangle([0, 0, 8, H], fill=GOLD)

    # 栏目标签（金，字距 3px 逐字画）
    x = 44
    for ch in "自研工具 · 每天一个":
        d.text((x, 34), ch, font=f(HEITI, 16), fill=GOLD)
        x += d.textlength(ch, font=f(HEITI, 16)) + 3
    d.line([(44, 66), (250, 66)], fill=(60, 66, 90), width=1)

    # 双行大标题（白）—— 对话钩子
    d.text((44, 92), "「服务崩过又被拉起，", font=f(HEITI, 38), fill=WHITE)
    d.text((44, 146), "谁留下证据了？」", font=f(HEITI, 38), fill=WHITE)

    # 副标（灰）
    d.text((44, 208), "僵尸 / 孤儿 / 换血横死 —— proclens 一条命令点名",
           font=f(HEITI, 18), fill=GRAY)

    # 三条硬指标（自绘勾 + 黑体）
    y = 252
    for txt, color in [
        ("49/49 selftest", GREEN),
        ("watch 6 轮换血 21 pid 实锤", GOLD),
        ("真机造罪三连全过", GREEN),
    ]:
        cx, cy = 48, y + 8
        d.line([(cx, cy + 4), (cx + 4, cy + 9), (cx + 13, cy - 2)], fill=color, width=3)
        d.text((72, y), txt, font=f(HEITI, 17), fill=WHITE)
        y += 25

    # 右侧数据区：churn 大字（Menlo ASCII）
    d.text((610, 118), "watch 6 rounds", font=f(MENLO, 18), fill=GRAY)
    d.text((610, 148), "21 pids", font=f(MENLO, 50), fill=RED)
    d.text((610, 226), "CHURN_PID · CRIT", font=f(MENLO, 20), fill=GOLD)
    d.text((610, 258), "exit 0 / 2 / 3", font=f(HEITI, 16), fill=GRAY)

    # 底部信息条（重点行黑衬底）
    d.rectangle([44, 330, 560, 362], fill=(13, 17, 23))
    d.text((56, 336), "github.com/tomlen045/proclens", font=f(MENLO, 18), fill=GOLD)
    d.text((584, 336), "零依赖单文件 CLI", font=f(HEITI, 16), fill=GRAY)

    img.save(OUT)
    print("saved", OUT)


if __name__ == "__main__":
    main()
