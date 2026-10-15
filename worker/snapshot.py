"""L1 快照过滤：从 origin/main 生成只读代码快照，禁读文件在解包时直接丢弃（物理上不存在）。

  python snapshot.py --repo D:/yuki/aitcg-flutter --out D:/kb-worker/snapshots --profile full
每小时跑一次；保留最近 2 份；current_<profile>.txt 指向最新一份（原子切换）。
"""
import argparse
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

from codebox import _glob_hit


def build(repo, out, profile, profiles_file, ref="origin/main"):
    cfg = json.loads(Path(profiles_file).read_text(encoding="utf-8"))
    prof = cfg["profiles"][profile]
    deny = prof.get("deny", []) + cfg["global_deny"]
    subprocess.run(["git", "-C", repo, "fetch", "-q", "origin", "main"], check=True)
    sha = subprocess.run(["git", "-C", repo, "rev-parse", "--short=9", ref],
                         check=True, capture_output=True, text=True).stdout.strip()
    dest = Path(out) / f"{profile}-{sha}"
    if not dest.exists():
        # core.autocrlf=false：本仓库 autocrlf=true，否则 archive 会输出 CRLF，行号虽不变但引用比对会错位
        tar_bytes = subprocess.run(["git", "-c", "core.autocrlf=false", "-C", repo, "archive", "--format=tar", ref],
                                   check=True, capture_output=True).stdout
        tmp = dest.with_suffix(".tmp")
        shutil.rmtree(tmp, ignore_errors=True)
        kept = dropped = 0
        with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tf:
            for mem in tf.getmembers():
                rel = mem.name.lstrip("./")
                parts = rel.split("/")
                denied = any(_glob_hit("/".join(parts[:i]), deny) for i in range(1, len(parts) + 1))
                if denied or not _glob_hit(rel, prof["allow"]) or mem.issym() or mem.islnk():
                    dropped += mem.isfile()
                    continue
                if mem.isfile():
                    tf.extract(mem, tmp, filter="data")
                    kept += 1
        tmp.rename(dest)
        print(f"snapshot {dest.name}: kept={kept} dropped={dropped}")
    (Path(out) / f"current_{profile}.txt").write_text(f"{dest}\n{sha}\n", encoding="utf-8")
    olds = sorted(Path(out).glob(f"{profile}-*"), key=lambda p: p.stat().st_mtime)[:-2]
    for o in olds:
        shutil.rmtree(o, ignore_errors=True)
    return dest, sha


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--profile", default="full")
    ap.add_argument("--profiles", default=str(Path(__file__).with_name("scope_profiles.json")))
    a = ap.parse_args()
    build(a.repo, a.out, a.profile, a.profiles)
