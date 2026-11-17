#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""proclens 实拍图产线：shots_src 真实输出 -> macOS 窗框 + One Dark 证据图。
PIL 符号纪律：特殊符号手绘；含中文行黑体、纯 ASCII 行 Menlo。
"""
import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "shots_src")
OUT = os.path.join(HERE, "shots")
os.makedirs(OUT, exist_ok=True)

BG = (40, 44, 52)
BAR = (33, 37, 43)
FG = (171, 178, 191)
GREEN = (152, 195, 121)
RED = (224, 108, 117)
YELLOW = (229, 192, 123)
BLUE = (97, 175, 239)
GRAY = (92, 99, 112)
WHITE = (220, 223, 228)
MENLO = "/System/Library/Fonts/Menlo.ttc"
HEITI = "/System/Library/Fonts/STHeiti Medium.ttc"


def has_cjk(s):
    return any("\u4e00" <= ch <= "\u9fff" for ch in s)


def font_for(text, size):
    return ImageFont.truetype(HEITI if has_cjk(text) else MENLO, size)


def text_w(d, text, size):
    return d.textlength(text, font=font_for(text, size))


def draw_terminal(title, lines, out_name, width=1400, pad=28):
    fs = 24
    lh = int(fs * 1.75)
    header = 52
    tmp = Image.new("RGB", (10, 10))
    td = ImageDraw.Draw(tmp)
    wrapped = []
    for item in lines:
        text, color = item[0], item[1]
        indent = item[2] if len(item) > 2 else 0
        max_w = width - pad * 2 - indent * 36
        cur = ""
        for ch in text:
            if text_w(td, cur + ch, fs) > max_w and cur:
                wrapped.append((cur, color, indent))
                cur = "  " + ch
            else:
                cur += ch
        wrapped.append((cur, color, indent))
    h = header + pad * 2 + lh * len(wrapped) + pad
    img = Image.new("RGB", (width, h), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, header], fill=BAR)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([20 + i * 30, header // 2 - 8, 36 + i * 30, header // 2 + 8], fill=c)
    f_title = font_for(title, 17)
    tw = d.textlength(title, font=f_title)
    d.text(((width - tw) // 2, (header - 22) // 2 - 1), title, font=f_title, fill=GRAY)
    y = header + pad
    for text, color, indent in wrapped:
        d.text((pad + indent * 36, y), text, font=font_for(text, fs), fill=color)
        y += lh
    out = os.path.join(OUT, out_name)
    img.save(out)
    print("saved", out, img.size)


def read_lines(name):
    with open(os.path.join(SRC, name), encoding="utf-8") as fh:
        return fh.read().rstrip().splitlines()


def rule(text):
    return "—— " + text + " ——"


# 01 全量扫描总览（注释数字从素材头行动态提取，防硬编码失真）
scan = read_lines("scan_full.txt")
hdr = next((ln for ln in scan if "已扫描" in ln), "")
import re as _re
_m = _re.search(r"(\d+)\s*进程已扫描.*发现\s*(\d+)\s*条", hdr)
_nproc = _m.group(1) if _m else "?"
_nfind = _m.group(2) if _m else "?"
lines = [(rule("真实机器全量扫描（%s 进程，exit 0）" % _nproc), YELLOW, 0)]
for ln in scan[:16]:
    c = FG
    if ln.startswith("[INFO]"):
        c = BLUE
    elif "proclens" in ln or "已扫描" in ln:
        c = GREEN
    lines.append((ln, c, 0))
draw_terminal("proclens scan — 谁死得不干净、谁没了爹", lines, "01_scan.png")

# 02 造尸现场
z = read_lines("zombie.txt")
head = z[:2]
zlines = [(rule("现场造尸：子进程 2 秒退场，父进程装死不收尸"), YELLOW, 0)]
for ln in head:
    zlines.append((ln, GREEN, 0))
for i, ln in enumerate(z):
    if "ZOMBIE" in ln:
        zlines += [(x, RED if j == 0 else FG, 0) for j, x in
                   enumerate(z[i:i + 2])]
        break
zlines.append((rule("退出码 2 —— cron 里一条 || 就能接告警"), YELLOW, 0))
for ln in z[-2:]:
    zlines.append((ln, GREEN, 0))
draw_terminal("proclens scan — 僵尸现形", zlines, "02_zombie.png")

# 03 watch 换血实锤
w = read_lines("watch.txt")
wlines = [(rule("现场造风暴：每 0.3 秒重生一个 looper.jar"), YELLOW, 0)]
for ln in w:
    c = GREEN if ln.startswith("round") or ln.startswith("exit") else RED
    wlines.append((ln, c, 0))
wlines.append((rule("名字常驻、pid 换血 —— 池化 worker 反复横死的指纹"), YELLOW, 0))
draw_terminal("proclens watch — 6 轮采样抓反复横死", wlines, "03_churn.png")

# 04 基线确定性
b = read_lines("baseline.txt")
blines = [(rule("存基线 → 2 秒后复测：瞬时工具全部豁免"), YELLOW, 0)]
for ln in b:
    blines.append((ln, GREEN, 0))
draw_terminal("proclens baseline — 复测零噪音", blines, "04_baseline.png")

# 05 自测
st = read_lines("selftest.txt")
slines = [(rule("不变量自测"), YELLOW, 0)]
for ln in st[:3] + ["……"] + st[-3:]:
    slines.append((ln, GREEN if ("PASS" in ln or "GREEN" in ln) else FG, 0))
draw_terminal("proclens selftest — 49/49", slines, "05_selftest.png")
