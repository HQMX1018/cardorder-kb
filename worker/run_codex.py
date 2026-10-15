"""用 Codex CLI 跑一道题（追问 / 作答）。L4 工具过滤全部在命令行参数里，不依赖提示词。

已在 codex-cli 0.162.1（Windows）实测：
  - --disable shell_tool / unified_exec 后没有 shell；
  - code mode 的 exec 里无 require / fetch，读不到本地文件、连不了网；
  - -s read-only 下 apply_patch 写文件被拒；
  - MCP 工具需 default_tools_approval_mode="approve" 才能免审批调用。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import schemas

# 版本锁定：npm i -g @openai/codex@0.162.1；升级后必须重跑验收测试。
# Windows 上不要调 codex.cmd：参数里的 JSON 引号会被 cmd.exe 二次解析，改为 node 直调 codex.js。
CODEX = [shutil.which("node") or "node", str(Path(shutil.which("codex") or "codex").parent / "node_modules/@openai/codex/bin/codex.js")]
DISABLE = ["shell_tool", "unified_exec", "multi_agent", "apps", "browser_use", "computer_use",
           "image_generation", "in_app_browser", "view_image", "skill_search", "tool_suggest",
           "goals", "sleep_tool", "hooks", "memories"]
HERE = Path(__file__).resolve().parent
SKILLS = HERE.parent / "skills"


def build_cmd(kind, job, snapshot, profile, audit, out_file, schema_file, empty_dir, model=None, images=()):
    mcp_args = [str(HERE / "codebox.py"), "--root", str(snapshot), "--profile", profile,
                "--profiles", str(HERE / "scope_profiles.json"), "--job", job, "--audit", str(audit),
                "--tools", "clarify" if kind == "clarify" else "all", "--kbconf", str(Path(out_file).with_name("kb.json"))]
    cmd = [*CODEX, "exec", "--ignore-user-config", "--ignore-rules", "--ephemeral", "--skip-git-repo-check",
           "-s", "read-only", "-C", str(empty_dir)]
    for f in DISABLE:
        cmd += ["--disable", f]
    cmd += ["-c", 'web_search="disabled"',
            "-c", f"mcp_servers.codebox.command={json.dumps(sys.executable)}",
            "-c", f"mcp_servers.codebox.args={json.dumps(mcp_args)}",
            "-c", "mcp_servers.codebox.startup_timeout_sec=60",
            "-c", "mcp_servers.codebox.tool_timeout_sec=30",
            "-c", 'mcp_servers.codebox.default_tools_approval_mode="approve"',
            "--output-schema", str(schema_file), "-o", str(out_file), "--json"]
    if model:
        cmd += ["-m", model]
    for img in images:          # 用户截图：只作为模型输入，不经过 codebox
        cmd += ["-i", str(img)]
    return cmd + ["-"]          # 提示词走 stdin，不进命令行


def run(kind, job, task, snapshot, profile, audit, timeout=900, model=None, images=()):
    """kind: clarify | answer。返回 (结果 dict, 事件流文本)。"""
    work = Path(tempfile.mkdtemp(prefix=f"kb-{job}-"))
    empty = work / "empty"           # 工作目录是空目录：即便有内置工具漏网也读不到东西
    empty.mkdir()
    schema_file = work / "schema.json"
    schema_file.write_text(json.dumps(schemas.CLARIFY if kind == "clarify" else schemas.ANSWER), encoding="utf-8")
    out_file = work / "last.json"
    # 问答库地址与令牌经临时文件交给 codebox（实测：给 MCP 设 env 会冲掉 Windows 系统变量，codebox 起不来）
    (work / "kb.json").write_text(json.dumps({"base": os.environ.get("KB_BASE", ""),
                                              "token": os.environ.get("KB_WORKER_TOKEN", "")}), encoding="utf-8")
    skill = (SKILLS / ("kb-clarify.md" if kind == "clarify" else "kb-answer.md")).read_text(encoding="utf-8")
    prompt = skill + "\n\n## 本题\n```json\n" + json.dumps(task, ensure_ascii=False, indent=1) + "\n```\n"
    cmd = build_cmd(kind, job, snapshot, profile, audit, out_file, schema_file, empty, model, images)
    cp = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=timeout)
    if cp.returncode != 0 or not out_file.exists():
        (work / "stderr.txt").write_text(cp.stderr, encoding="utf-8")   # 失败现场留在 work 目录
        raise RuntimeError(f"codex 退出码 {cp.returncode}: {cp.stderr[-800:]}")
    result = json.loads(out_file.read_text(encoding="utf-8"))
    if not Path(audit).exists():   # codebox 零调用：留现场（stderr 里有 MCP 启动失败原因）
        dbg = Path(audit).parent.parent / "debug"
        dbg.mkdir(parents=True, exist_ok=True)
        (dbg / f"{job}.stderr.txt").write_text(cp.stderr, encoding="utf-8")
        (dbg / f"{job}.stdout.jsonl").write_text(cp.stdout, encoding="utf-8")
    shutil.rmtree(work, ignore_errors=True)
    return result, cp.stdout
