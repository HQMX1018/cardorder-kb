"""本机 worker：145 经 SSH 反向隧道推题 → Codex 作答 → 证据核对 + 脱敏 → 回传。

  set KB_BASE=http://192.168.31.145:8090
  set KB_WORKER_TOKEN=...            # 与 145 的环境变量一致
  python kb_worker.py --repo D:/yuki/aitcg-flutter --runtime D:/kb-worker/runtime
"""
import argparse
import hmac
import json
import os
import re
import subprocess
import threading
import traceback
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import evidence
import run_codex
import snapshot
from codebox import Box
from redact import redact

BASE = os.environ.get("KB_BASE", "http://192.168.31.145:8090").rstrip("/")
TOKEN = os.environ.get("KB_WORKER_TOKEN", "")
NAME = os.environ.get("KB_WORKER_NAME", "win-" + os.environ.get("COMPUTERNAME", "w1"))
CONCURRENCY = 2
BACKOFF = [5, 10, 20, 40, 60]     # 隧道重连退避
HERE = Path(__file__).resolve().parent

# 145 是内网地址，必须直连：本机开着系统代理（Clash 127.0.0.1:7897）时 urllib 会把请求绕进代理，
# 代理一重启或掐断 25s 长轮询就整夜「145 不可达」。本进程只和 145 通信，全局去掉代理（codebox 同进程共享）。
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
LOCK_EXIT = 3                     # 已有实例在跑：run_worker.cmd 见此退出码即结束循环，不重复拉起


def single_instance(runtime):
    """进程级互斥：锁住 runtime/worker.lock，拿不到说明已有 worker，直接退出。句柄须存活到进程结束。"""
    import msvcrt
    f = open(Path(runtime) / "worker.lock", "a+")
    f.seek(0)
    try:
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        print(time.strftime("%H:%M:%S"), "已有 worker 在运行，本实例退出", flush=True)
        raise SystemExit(LOCK_EXIT)
    return f


def http(method, path, body=None, timeout=40):
    req = urllib.request.Request(BASE + path, method=method, headers={"X-KB-Worker": TOKEN, "Content-Type": "application/json"},
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, (json.loads(r.read()) if r.status == 200 else None)


def heartbeat_loop(job_id, stop):
    while not stop.wait(60):
        try:
            http("POST", f"/api/worker/heartbeat/{job_id}?worker={NAME}")
        except Exception:
            pass                      # 心跳丢一次不要紧，租约 2/15 分钟够覆盖


def current_snapshot(runtime, profile):
    for _ in range(10):               # 指针文件由 snapshot.build 原子替换；仍防御性重试
        parts = (Path(runtime) / "snapshots" / f"current_{profile}.txt").read_text(encoding="utf-8").split()
        if len(parts) >= 2 and Path(parts[0]).is_dir():
            return Path(parts[0]), parts[1]
        time.sleep(1)
    raise RuntimeError(f"代码快照不可用：current_{profile}.txt")


def handle(job, runtime):
    job_id, kind, payload = job["id"], job["kind"], job["payload"]
    profile = payload.get("scope", "full")            # 145 按提问人角色写入，模型改不了
    audit = Path(runtime) / "audit" / f"{job_id}.jsonl"
    stop = threading.Event()
    threading.Thread(target=heartbeat_loop, args=(job_id, stop), daemon=True).start()
    try:                              # 任何异常都必须回传 145，否则题目要等 15 分钟租约过期
        audit.parent.mkdir(parents=True, exist_ok=True)
        snap, sha = current_snapshot(runtime, profile)
        images = []
        for i, url in enumerate(payload.get("images", [])[:3]):   # 用户截图 → 本地临时文件 → codex -i
            req = urllib.request.Request(BASE + url, headers={"X-KB-Worker": TOKEN})
            with urllib.request.urlopen(req, timeout=30) as r:
                p = Path(runtime) / "images" / f"{job_id}-{i}{Path(url).suffix}"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(r.read())
                images.append(p)
        for attempt in (1, 2):
            res, _events = run_codex.run(kind, job_id, payload["card"], snap, profile, audit, images=images)
            calls = len(audit.read_text(encoding="utf-8").splitlines()) if audit.exists() else 0
            if kind == "clarify" or calls > 0:
                break
            # 实测偶发：MCP 未就绪时模型零调用直接作答 → 重跑一次
        body = {"result": res, "commit": sha}
        if kind == "answer":
            box = Box(snap, profile, HERE / "scope_profiles.json", job_id, audit.with_suffix(".check.jsonl"))
            kept, dropped = evidence.check(res, box, audit, job_id)
            res["plain"], _ = redact(res["plain"])
            res["detail"], _ = redact(res["detail"])
            body.update(kept=kept, dropped=dropped)
        http("POST", f"/api/worker/result/{job_id}?worker={NAME}", body)
    except Exception as e:
        traceback.print_exc()
        try:
            http("POST", f"/api/worker/result/{job_id}?worker={NAME}", {"error": f"{type(e).__name__}: {e}"[:1500]})
        except Exception:
            pass                      # 回传失败 → 租约过期后 145 自动重派
    finally:
        stop.set()
        print(time.strftime("%H:%M:%S"), kind, job_id, "done", flush=True)


FIRST_SNAPSHOT = threading.Event()


def snapshot_loop(repo, runtime):
    while True:
        try:
            snapshot.build(repo, Path(runtime) / "snapshots", "full", HERE / "scope_profiles.json")
        except Exception as e:
            print("snapshot failed:", e, flush=True)
        FIRST_SNAPSHOT.set()
        time.sleep(3600)


# ── 推送接收：145 经 SSH 反向隧道把任务推到本机 127.0.0.1:PUSH_PORT（不监听外网，不开入站端口） ──
PUSH_PORT = int(os.environ.get("KB_PUSH_PORT", "8091"))
SSH_EXE = os.environ.get("KB_SSH", r"C:\Program Files\OpenSSH\ssh.exe")
SSH_TARGET = os.environ.get("KB_SSH_TARGET", "cardxu@192.168.31.145")
FORWARD = f"127.0.0.1:0:127.0.0.1:{PUSH_PORT}"     # 远端端口 0 = 由 145 的 sshd 现分配，躲开残留绑定
NO_WINDOW = 0x08000000                            # CREATE_NO_WINDOW：隧道进程不弹黑框


def serve(runtime, pool, busy):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, obj):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authed(self):
            if TOKEN and hmac.compare_digest(self.headers.get("X-KB-Worker", ""), TOKEN):
                return True
            self._reply(401, {"error": "bad worker token"})
            return False

        def do_GET(self):
            if not self._authed():
                return
            if self.path == "/ping":
                return self._reply(200, {"worker": NAME})
            self._reply(404, {})

        def do_POST(self):
            if not self._authed():
                return
            if self.path != "/job":
                return self._reply(404, {})
            job = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if not busy.acquire(blocking=False):       # 两个作答位都满 → 145 退回队列稍后再推
                return self._reply(503, {"error": "busy"})
            print(time.strftime("%H:%M:%S"), "pushed", job.get("kind"), job.get("id"), flush=True)
            pool.submit(handle, job, runtime).add_done_callback(lambda _f: busy.release())
            self._reply(202, {"ok": True})

        def log_message(self, *args):
            pass                      # 每次探活都打一行太吵

    ThreadingHTTPServer(("127.0.0.1", PUSH_PORT), Handler).serve_forever()


def kill_stray_tunnels():
    """上一个 worker 被强杀时，它拉起的 ssh 会成孤儿继续占着隧道，先清掉。"""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='ssh.exe'\" | "
          f"Where-Object {{ $_.CommandLine -like '*{FORWARD}*' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=NO_WINDOW, capture_output=True, timeout=30)


def register(port, proc):
    """隧道建好后向 145 登记端口；145 会经隧道回探一次才生效。145 暂时不可达就一直重试，直到隧道断。"""
    for i in range(10 ** 9):
        if proc.poll() is not None:
            return
        try:
            http("POST", f"/api/worker/register?port={port}&worker={NAME}", {}, timeout=20)
            print(time.strftime("%H:%M:%S"), "tunnel registered, 145 port", port, flush=True)
            return
        except Exception as e:
            if i == 0:
                print(time.strftime("%H:%M:%S"), f"登记失败（{e}），每 10s 重试", flush=True)
            time.sleep(10)


def tunnel_loop():
    kill_stray_tunnels()
    fails = 0
    while True:
        t0 = time.time()
        proc = subprocess.Popen(
            [SSH_EXE, "-N", "-T", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes", "-o", "ConnectTimeout=10",
             "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3", "-R", FORWARD, SSH_TARGET],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=NO_WINDOW, text=True, encoding="utf-8", errors="replace")
        for line in proc.stderr:      # 「Allocated port N for remote forward to …」
            line = line.strip()
            m = re.search(r"Allocated port (\d+) for remote forward", line)
            if m:
                threading.Thread(target=register, args=(int(m.group(1)), proc), daemon=True).start()
            elif line:
                print(time.strftime("%H:%M:%S"), "ssh:", line, flush=True)
        proc.wait()
        fails = 0 if time.time() - t0 > 300 else fails + 1     # 稳定跑过 5 分钟才算恢复
        wait = BACKOFF[min(fails, len(BACKOFF) - 1)]
        print(time.strftime("%H:%M:%S"), f"隧道断开（ssh 退出码 {proc.returncode}），{wait}s 后重连", flush=True)
        time.sleep(wait)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--runtime", required=True)
    a = ap.parse_args()
    _lock = single_instance(a.runtime)  # noqa: F841  持有到进程退出
    threading.Thread(target=snapshot_loop, args=(a.repo, a.runtime), daemon=True).start()
    FIRST_SNAPSHOT.wait(600)          # 首次快照（fetch + 可能重建）完成后再接题
    while not (Path(a.runtime) / "snapshots" / "current_full.txt").exists():
        time.sleep(2)
    pool = ThreadPoolExecutor(CONCURRENCY)
    busy = threading.Semaphore(CONCURRENCY)
    threading.Thread(target=serve, args=(a.runtime, pool, busy), daemon=True).start()
    print(time.strftime("%H:%M:%S"), "worker ready", NAME, f"push 127.0.0.1:{PUSH_PORT}", flush=True)
    tunnel_loop()                     # 主线程守着隧道，永不返回


if __name__ == "__main__":
    main()
