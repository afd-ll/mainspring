---
name: Bug 报告
about: 有东西坏了，或者行为和你预期的不一样
title: '[Bug] '
labels: bug
---

## 你跑的是什么

- Mainspring 版本 / commit：
- Python 版本：
- 操作系统：
- 部署方式：本机直跑 / systemd / 容器

## 复现步骤

1.
2.

## 期望发生什么 / 实际发生了什么

## 先跑一下自检

探针会告诉你坏在哪一层，请贴输出（token 打码）：

```
./venv/bin/python tools/probe.py
```

## 服务日志

systemd 部署的话，日志在 unit 里 `StandardOutput` 指定的文件。贴相关几行。
