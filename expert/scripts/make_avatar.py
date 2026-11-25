#!/usr/bin/env python3
"""生成专家头像 avatars/expert.png（512×512 PNG，纯标准库，无第三方依赖）。

图案：深色底 + 白色报告卡片 + 三条长度递减的灰线 + 右下角橙色对勾。
一眼能看出「把聊天整理成报告并核对过」。

用法：
    python3 scripts/make_avatar.py            # 写到 avatars/expert.png
    python3 scripts/make_avatar.py out.png    # 写到指定路径
"""

import os
import struct
import sys
import zlib

W = H = 512

BG = (15, 23, 42)          # slate-900
CARD = (248, 250, 252)     # slate-50
LINE = (203, 213, 225)     # slate-300
ACCENT = (37, 99, 235)     # blue-600
CHECK = (245, 158, 11)     # amber-500


def blend(dst, src, alpha):
    return tuple(int(dst[i] * (1 - alpha) + src[i] * alpha) for i in range(3))


class Canvas(object):
    def __init__(self, w, h, color):
        self.w, self.h = w, h
        self.rows = [bytearray(color * w) for _ in range(h)]

    def put(self, x, y, color, alpha=1.0):
        if 0 <= x < self.w and 0 <= y < self.h:
            row = self.rows[y]
            i = x * 3
            if alpha >= 1.0:
                row[i:i + 3] = bytes(color)
            else:
                cur = (row[i], row[i + 1], row[i + 2])
                row[i:i + 3] = bytes(blend(cur, color, alpha))

    def rounded_rect(self, x0, y0, x1, y1, radius, color):
        for y in range(y0, y1):
            for x in range(x0, x1):
                cx = min(max(x, x0 + radius), x1 - radius - 1)
                cy = min(max(y, y0 + radius), y1 - radius - 1)
                dx, dy = x - cx, y - cy
                if dx * dx + dy * dy <= radius * radius:
                    self.put(x, y, color)

    def thick_segment(self, x1, y1, x2, y2, width, color):
        dx, dy = x2 - x1, y2 - y1
        denom = dx * dx + dy * dy
        half = width / 2.0
        lo = int(min(x1, x2) - width)
        hi = int(max(x1, x2) + width)
        top = int(min(y1, y2) - width)
        bot = int(max(y1, y2) + width)
        for y in range(top, bot + 1):
            for x in range(lo, hi + 1):
                if denom == 0:
                    d = ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
                else:
                    t = ((x - x1) * dx + (y - y1) * dy) / denom
                    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                    d = ((x - (x1 + t * dx)) ** 2 + (y - (y1 + t * dy)) ** 2) ** 0.5
                if d <= half:
                    self.put(x, y, color)

    def png(self):
        raw = b"".join(b"\x00" + bytes(row) for row in self.rows)

        def chunk(tag, data):
            body = tag + data
            return (struct.pack(">I", len(data)) + body +
                    struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

        ihdr = struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) +
                chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def build():
    c = Canvas(W, H, BG)
    # 报告卡片
    c.rounded_rect(88, 104, 424, 408, 28, CARD)
    # 顶部色条（表示周期报告的标题栏）
    c.rounded_rect(120, 140, 392, 176, 12, ACCENT)
    # 三条正文线，长度递减
    widths = (272, 232, 180)
    y = 216
    for w in widths:
        c.rounded_rect(120, y, 120 + w, y + 18, 9, LINE)
        y += 52
    # 右下角对勾：橙底圆 + 白勾
    cx, cy, r = 356, 356, 60
    for yy in range(cy - r, cy + r + 1):
        for xx in range(cx - r, cx + r + 1):
            if (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r:
                c.put(xx, yy, CHECK)
    c.thick_segment(cx - 26, cy + 2, cx - 6, cy + 24, 16, (255, 255, 255))
    c.thick_segment(cx - 6, cy + 24, cx + 30, cy - 22, 16, (255, 255, 255))
    return c.png()


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    default = os.path.join(os.path.dirname(here), "avatars", "expert.png")
    out = sys.argv[1] if len(sys.argv) > 1 else default
    folder = os.path.dirname(os.path.abspath(out))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    data = build()
    with open(out, "wb") as f:
        f.write(data)
    print("已生成 {}（{}×{}，{} 字节）".format(out, W, H, len(data)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
