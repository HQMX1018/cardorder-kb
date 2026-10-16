"""本机 worker：长轮询 145 领题 → Codex 作答 → 证据核对 + 脱敏 → 回传。

  set KB_BASE=http://192.168.31.145:8090
  set KB_WORKER_TOKEN=...            # 与 145 的环境变量一致
  python kb_worker.py --repo D:/yuki/aitcg-flutter --runtime D:/kb-worker/runtime
"""
import argparse
import json
import os
import threading
import traceback
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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
BACKOFF = [5, 10, 20, 40, 60]
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--runtime", required=True)
    ap.add_argument("--once", action="store_true", help="只处理一题后退出（验收用）")
    a = ap.parse_args()
    _lock = single_instance(a.runtime)  # noqa: F841  持有到进程退出
    threading.Thread(target=snapshot_loop, args=(a.repo, a.runtime), daemon=True).start()
    FIRST_SNAPSHOT.wait(600)          # 首次快照（fetch + 可能重建）完成后再领题
    while not (Path(a.runtime) / "snapshots" / "current_full.txt").exists():
        time.sleep(2)
    print(time.strftime("%H:%M:%S"), "worker ready", NAME, flush=True)
    pool = ThreadPoolExecutor(CONCURRENCY)
    busy = threading.Semaphore(CONCURRENCY)
    fails = 0
    while True:
        busy.acquire()                # 满 2 题时不再领新题
        try:
            status, job = http("GET", f"/api/worker/next?wait=25&worker={NAME}", timeout=40)
            fails = 0
        except (urllib.error.URLError, OSError) as e:
            busy.release()
            wait = BACKOFF[min(fails, len(BACKOFF) - 1)]
            fails += 1
            print(f"145 不可达（{e}），{wait}s 后重试")
            time.sleep(wait)
            continue
        if status != 200:
            busy.release()
            continue                  # 204 = 25 秒内无题，立即再挂
        fut = pool.submit(handle, job, a.runtime)
        fut.add_done_callback(lambda _f: busy.release())
        if a.once:
            fut.result()
            return


if __name__ == "__main__":
    main()
