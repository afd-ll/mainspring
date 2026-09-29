#!/usr/bin/env python3
"""文档一致性自检 —— 几份文档各抄一遍同一件事，就一定会漂移；这个脚本负责抓漂移。

检查三件事：
  ① 文档里写到的 `tools/…` / `tests/…` 路径是不是**真的存在**
     （曾经把 `docs/` 这种压根没有的目录写进 CONTRIBUTING）
  ② README「开发与自检」那一节，是否**列全了** `tests/` 下每个 `test_*.py`
     （新增测试忘了写进文档 ⇒ 这里直接红）
  ③ CONTRIBUTING「提 PR 之前」是否也列全了同一组测试

用法：
    venv/bin/python tools/doc_check.py        # PASS 退 0 / 有问题退 1
"""
from __future__ import annotations

import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

DOCS = [
    "README.md",
    "CONTRIBUTING.md",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/ISSUE_TEMPLATE/bug_report.md",
    ".github/ISSUE_TEMPLATE/feature_request.md",
]
PATH_RE = re.compile(r"\b((?:tools|tests)/[A-Za-z0-9_./-]+\.(?:py|sh|md|example))\b")

PASS = FAIL = 0


def ck(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [OK]   %-46s %s" % (name, detail))
    else:
        FAIL += 1
        print("  [FAIL] %-46s %s" % (name, detail))


def section(text, heading):
    """取 `## <heading>` 到下一个同级标题之间的内容。"""
    m = re.search(r"^##\s*%s\s*$" % re.escape(heading), text, re.M)
    if not m:
        return ""
    rest = text[m.end():]
    nxt = re.search(r"^##\s", rest, re.M)
    return rest[: nxt.start()] if nxt else rest


def main() -> int:
    print("=" * 68)
    print("文档一致性自检")
    print("=" * 68)

    print("\n[1] 文档里提到的路径是否真实存在")
    bad = []
    for d in DOCS:
        p = os.path.join(ROOT, d)
        if not os.path.exists(p):
            ck("%s 存在" % d, False, "文件都没有")
            continue
        with open(p, encoding="utf-8") as f:
            text = f.read()
        found = sorted(set(PATH_RE.findall(text)))
        missing = [x for x in found if not os.path.exists(os.path.join(ROOT, x))]
        ck("%s（%d 处路径引用）" % (d, len(found)), not missing,
           "缺：%s" % missing if missing else "")
        bad += missing

    tests_dir = os.path.join(ROOT, "tests")
    want_tests = sorted(f for f in os.listdir(tests_dir)
                        if f.startswith("test_") and f.endswith(".py")) if os.path.isdir(tests_dir) else []
    print("\n    仓库里的测试文件：%s" % ", ".join(want_tests))

    print("\n[2] README「开发与自检」是否列全了 tests/")
    with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
        readme = f.read()
    sec = section(readme, "开发与自检")
    ck("找得到「开发与自检」一节", bool(sec), "%d 字符" % len(sec))
    missing = [t for t in want_tests if t not in sec]
    ck("测试文件一个不漏", not missing, "缺：%s" % missing if missing else "共 %d 个" % len(want_tests))

    print("\n[3] CONTRIBUTING「提 PR 之前」是否列全了 tests/")
    with open(os.path.join(ROOT, "CONTRIBUTING.md"), encoding="utf-8") as f:
        contrib = f.read()
    sec2 = section(contrib, "提 PR 之前")
    ck("找得到「提 PR 之前」一节", bool(sec2), "%d 字符" % len(sec2))
    missing2 = [t for t in want_tests if t not in sec2]
    ck("测试文件一个不漏", not missing2, "缺：%s" % missing2 if missing2 else "共 %d 个" % len(want_tests))

    print("\n[4] PR 模板不该自己抄一份命令清单（会跟 README 漂移）")
    with open(os.path.join(ROOT, ".github/PULL_REQUEST_TEMPLATE.md"), encoding="utf-8") as f:
        pr = f.read()
    ck("PR 模板引用 README 而不是重抄命令", "./venv/bin/python" not in pr,
       "仍有 %d 行命令" % pr.count("./venv/bin/python") if "./venv/bin/python" in pr else "只引用，不重抄")
    ck("PR 模板确实指向了 README", "README" in pr, "")

    print("\n" + "=" * 68)
    print("PASS %d · FAIL %d" % (PASS, FAIL))
    print("=" * 68)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
