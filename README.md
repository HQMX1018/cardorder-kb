# 卡序问答库（CardOrder KB）

小白留言提问 → 选择题补全 → 本机 Codex 只读查卡序项目作答（带「文件:行号」证据）。所有问题和后续问答默认收入资料库（.md），登录成员均可在同一线程继续追问。

## 共享问答与默认留档（任务 #47）

- 每个问题及每轮追问记录作者、时间和原文，回答保留版本与证据。原始作者不会因其他成员追问而改变。
- 资料库检索同时覆盖排队、作答、失败和已答记录；问答记录为 internal，仅登录成员可查看。自动收录不等于审核通过。
- 同一问题正在处理时，下一位需等待本轮完成再追问，避免并发覆盖上下文。MCP 可使用 `kb_followup`。
- 启动时幂等迁移：恢复原始问题作者和旧追问审计片段；片段明确标注可能截断，不编造缺失全文。旧人工整理的 KB-* 文件保留，新线程使用 Q-* 条目。
- 校验：`.venv\Scripts\python tests\test_shared_questions.py`。测试使用临时数据库、三个模拟成员及模拟 worker 回传，不访问线上业务数据。

- 网页 / MCP：http://192.168.31.145:8090 （MCP `/mcp/`，请求头 `X-KB-Uid: <研发平台 userid>`）
- 账号：直接用研发平台用户（只读 `~/cardorder-platform/data/platform.db`），cookie `uid` 与研发平台共用
- 设计与实测：桌面《卡序问答库-开发报告.html》

## 目录

| 路径 | 作用 |
|---|---|
| `server/` | 145 服务端：`main.py` 入口、`web.py` 网页、`kb_queue.py` 流程与 worker 接口、`store.py` 数据层、`mcp_kb.py` MCP |
| `worker/` | 本机 worker：`kb_worker.py` 长轮询、`run_codex.py` Codex 调用（L4）、`codebox.py` 读代码唯一通道（L2/L3/配额/审计）、`snapshot.py` 代码快照（L1）、`evidence.py` 证据核对、`redact.py` 脱敏 |
| `skills/` | 追问官 / 研究员提示词 |
| `seed/` | 冷启动资料条目 |
| `tests/` | `test_filters.py` 过滤器单测、`local_smoke.py` 冒烟、`e2e_once.py` 单题端到端 |
| `deploy/run_worker.cmd` | 本机 worker 启动脚本（任务计划 `CardOrderKB-Worker` 登录后自动运行） |

`server/redact.py` 必须与 `worker/redact.py` 一致（145 侧 L5 再过一遍）。

## 145 运维（cardxu 用户，无 sudo）

```
systemctl --user status|restart cardorder-kb      # 端口 8090
journalctl --user -u cardorder-kb -f
~/cardorder-kb/app        代码（从本仓库 server/ 覆盖）
~/cardorder-kb/data       kb.db / entries/*.md / uploads/ / backup/（每天 03:00 打包，留 14 天）
~/cardorder-kb/.env       KB_DATA / KB_WORKER_TOKEN（600，勿外传）
```

更新服务端：
```
tar --exclude=__pycache__ -cf - -C server . | ssh cardxu@192.168.31.145 'tar -xf - -C ~/cardorder-kb/app && systemctl --user restart cardorder-kb'
```

## 本机 worker

- 代码：本仓库 `worker/` + `.venv`；运行目录 `D:\kb-worker\`（`runtime/` 快照与审计、`worker.log`、`token.txt`）
- 每小时 `git fetch origin main` → 重建快照（只动远端引用，不碰工作区）
- 停止：`schtasks /End /TN CardOrderKB-Worker`；查看审计：`D:\kb-worker\runtime\audit\<job>.jsonl`
- Codex 版本锁 0.162.1；升级后先跑 `tests/test_filters.py` 与注入用例

## 测试

```
.venv\Scripts\python tests\test_filters.py <快照目录> <审计输出>
.venv\Scripts\python tests\local_smoke.py prepare runtime\localtest\data   # 再起本地服务 18090
.venv\Scripts\python tests\local_smoke.py web|mcp http://127.0.0.1:18090
```
