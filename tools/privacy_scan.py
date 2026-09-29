#!/usr/bin/env python3
"""发布前隐私自检 —— 扫 git 跟踪文件里**不该出门**的东西。

用法：
    venv/bin/python tools/privacy_scan.py                      # 只跑通用模式
    venv/bin/python tools/privacy_scan.py --words 张三,myhost  # 追加你自己的词表

退出码：命中 = 1，干净 = 0（可以直接挂进 CI / pre-push）。

⚠️ 通用模式**故意不硬编码任何人的名字、主机名、账号名** —— 否则这份扫描脚本本身
   就把它们公开了。项目自己的词表请用 `--words` 传，或放在本地文件里（别提交）。

📌 两条别删的设计（都是踩过的坑）：
   1. **二进制文件跳过** —— 图片/字体/压缩包按 UTF-8 读会读出一堆随机串，
      历史上真的被误报成过「邮箱」和「Windows 绝对路径」，逼得每次发布都要人工
      忽略同一批噪音；假阳性一多，真阳性就没人看了。这里按扩展名 + NUL 字节双判。
      （二进制里真藏了隐私——水印、EXIF 时间戳——正则也扫不出来，得另外看。）
   2. **Windows 路径要求盘符后跟「像路径」的首字符** —— 否则前端正则里的
      `\\s*\\|` 会被当成盘符 `s:` + 反斜杠 + `-`，报出一条根本不存在的路径。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 私有/保留网段：公网 IP 规则要把它们排除掉，否则同一个地址被两条规则各报一次
_PRIVATE = r"(?!127\.|0\.0\.0\.0|255\.|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)"

PATTERNS = [
    ("公网 IP（内网段除外）", re.compile(
        r"\b" + _PRIVATE + r"((?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")),
    ("邮箱", re.compile(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}")),
    ("Linux 家目录", re.compile(r"/home/[A-Za-z0-9_.-]+")),
    # (?<!\\) ⇒ 排除前面是反斜杠的（正则转义）；首字符限制 ⇒ 排除 `s:\-` 这种
    ("Windows 绝对路径", re.compile(
        r"(?<!\\)\b[A-Za-z]:\\(?:[A-Za-z0-9_$\u4e00-\u9fff~.][^\\\s\"'|]*)")),
    ("中国大陆手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("内网网段", re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")),
]

BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".bmp", ".svgz",
    ".pdf", ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".jar",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".so", ".o", ".a", ".pyc", ".class", ".wasm",
    ".mp3", ".mp4", ".webm", ".mov", ".wav",
    ".db", ".sqlite", ".sqlite3", ".pack", ".idx",
}


def looks_binary(path: str, sample: int = 8192) -> bool:
    """扩展名命中直接判；否则看头部有没有 NUL 字节（文本文件不会有）。"""
    if os.path.splitext(path)[1].lower() in BINARY_EXT:
        return True
    try:
        with open(path, "rb") as fp:
            return b"\x00" in fp.read(sample)
    except OSError:
        return True


def main() -> int:
    ap = argparse.ArgumentParser(description="发布前隐私自检")
    ap.add_argument("--words", default="", help="额外要盯的词，逗号分隔（不写进仓库，命令行传）")
    ap.add_argument("--limit", type=int, default=200, help="最多报多少处")
    a = ap.parse_args()

    # -z：文件名带空格也不会被切碎
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True).stdout
    files = [f for f in out.split("\0") if f]

    pats = list(PATTERNS)
    extra = [w.strip() for w in a.words.split(",") if w.strip()]
    if extra:
        pats.append(("自定义词", re.compile("|".join(re.escape(w) for w in extra))))

    print("扫 %d 个跟踪文件" % len(files))
    if extra:
        print("自定义词：%s（未落盘）" % len(extra))
    print()

    total = 0
    skipped = []
    for f in files:
        full = os.path.join(ROOT, f)
        if looks_binary(full):
            skipped.append(f)
            continue
        try:
            with open(full, encoding="utf-8", errors="ignore") as fp:
                lines = fp.read().splitlines()
        except OSError:
            continue
        hits = []
        for i, line in enumerate(lines, 1):
            for label, rx in pats:
                for m in rx.finditer(line):
                    hits.append((i, label, m.group(0), line.strip()[:120]))
        if hits:
            print("── %s" % f)
            for i, label, got, ctx in hits:
                total += 1
                if total <= a.limit:
                    print("   %5d  [%-12s] %-28s | %s" % (i, label, got, ctx))
            print()

    if skipped:
        print("跳过 %d 个二进制文件（图片/字体/压缩包，按文本读只会产假阳性）：" % len(skipped))
        for f in skipped[:12]:
            print("   %s" % f)
        if len(skipped) > 12:
            print("   … 还有 %d 个" % (len(skipped) - 12))
        print()

    print("=" * 60)
    if total:
        print("命中 %d 处 —— 发布前逐条确认：是示例值就能留，是真实信息就得改。" % total)
        return 1
    print("零命中 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
