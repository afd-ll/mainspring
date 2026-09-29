# 参与进来

欢迎。这个项目想做的是一件小事：**让"完成"这句话重新可信** —— agent 干完必须交结论，由人签字收口。

所以最需要的**不是更多功能，是更多用法**。

## 先跑起来（5 分钟）

```bash
git clone https://github.com/afd-ll/mainspring.git mainspring && cd mainspring
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env                 # 至少填 MS_USER / MS_PASS
./venv/bin/python ms_server.py       # → http://127.0.0.1:8787
```

跑一遍自检，确认你的环境是好的：

```bash
./venv/bin/python tools/probe.py                  # 端到端探针（28 项）
./venv/bin/python tests/test_db.py                # 数据层单元测试（118 条）
./venv/bin/python tests/test_wake.py              # 唤醒链路（26 项）
./venv/bin/python tools/doc_check.py              # 文档里的命令/文件名还对得上吗
```

（完整清单以 README「开发与自检」为准。）

## 代码地图

| 文件 | 干什么 | 改它的风险 |
|---|---|---|
| `ms_db.py` | **数据层**。SQLite 唯一真值源、字段映射、事件流水、编号分配、token、唤醒 | ⚠️ 高 —— 所有东西都压在它上面 |
| `ms_api.py` | REST API + Cookie/token 鉴权 + SSE 事件总线 + 项目文件夹读写 | 中 |
| `ms_mcp.py` | 把 `ms_db` 暴露成 MCP 工具（8 个） | 中 —— **别改已有工具的名字和签名**，有人在用 |
| `ms_server.py` | **进程入口**。组装 app、放行公网 Host、挂中间件 | 低 |
| `ms_guide.py` | 内置接入指南（单一真值源，三个出口） | 低 —— 改它等于改所有出口的文案 |
| `web/index.html` | 整个前端（静态，原生 JS，无构建） | 低 |
| `tools/probe.py` | 端到端探针（28 项），出问题先跑它 | —— |
| `tools/seed_demo.py` | 一键生成中性演示库（20 条 / 6 种状态） | —— |
| `tools/backup.sh` | 每日备份（SQLite 在线备份 API） | —— |
| `tools/privacy_scan.py` | 发布前隐私自检（公网 IP / 邮箱 / 家目录 / 手机号） | —— |
| `tools/doc_check.py` | 文档一致性自检（下面提到的文件是否真的在） | —— |
| `tests/test_db.py` | 数据层单元测试 118 条（临时库 + 真库指纹隔离自证） | —— |
| `tests/test_wake.py` | 唤醒链路集成测试 26 条（起桩服务，真的 POST 出去） | —— |

## 两条硬规则

**1. SQLite 是唯一真值源。**
`projects/` 下的 `.md` 是**单向导出**，直接改文件会在下次同步时被覆盖。要持久化就改库。

**2. 凭据永不进仓库。**
`.env`、`ms.sqlite3`、`backups/`、`*.log` 都在 `.gitignore` 里。**新加任何配置项，都要同时更新 `.env.example`** —— 那是别人能跑起来的唯一线索。

## 提 PR 之前

```bash
./venv/bin/python tools/probe.py        # 应该 PASS（FAIL 会给非 0 退出码）
./venv/bin/python tests/test_db.py      # 数据层：应该 FAIL 0
./venv/bin/python tests/test_wake.py    # 唤醒链路：应该 FAIL 0
./venv/bin/python tools/doc_check.py    # 文档一致性：应该 PASS
```

动了数据库层或 API 的话，**请说明你怎么验证的**：什么命令、看到什么输出。这个项目对"看起来是对的"容忍度很低 —— 结论要能被复现。

## 想要什么（按当前需要排序）

1. **用法反馈**：你把它接到了什么 agent 上？哪一步卡住了？比任何 feature request 都值钱
2. **前端的测试**：`web/index.html` 是一整块原生 JS，目前**零测试覆盖** —— 探针只验了它能返回 200，
   没人验那些交互（筛选、详情弹窗、Token 复制、登录流程）在改动之后还好不好使
3. **多用户 / OAuth**：现在适合单人自托管
4. **界面截图**：README 里现在一张图都没有 —— 你跑一遍 `./venv/bin/python tools/seed_demo.py`
   （一键生成演示数据），顺手截看板 / 详情弹窗 / 事件流几张放进 PR，就是实打实的贡献
5. **主题配置化**：把界面文案与配色抽出来，方便换皮

## 不想要的

- 引入前端构建链（这个项目的"零构建"是刻意的，不是没来得及）
- 把 SQLite 换成外部数据库（单文件自持是核心卖点）
- 为兼容而保留死代码（用不到的分支就该删掉）

## 提 PR 的流程

1. 开一个 issue 说清你要解决什么（大改动先聊，别写完再推翻）
2. 一个 PR 做一件事
3. 提交信息写清「为什么」，不只是「改了什么」
4. 涉及行为变更的，在 PR 描述里贴上验证命令和输出
