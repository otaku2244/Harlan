#!/usr/bin/env python3
"""用 GitHub API 推送本仓库（无需 git）。

为什么用 Python 而不是 PowerShell：
  本机只有 Windows PowerShell 5.1，它按 ANSI 读取无 BOM 的 .ps1，
  脚本里的中文会被拆成非法 token。Python 的编码处理是确定的。

走 Git Data API：
    每个文件 → blob → 一棵 tree → 一个 commit → 移动分支引用

第一次推送因此是一个**原子提交**，而不是几十个零散提交。
tree 是完整快照，所以本脚本可重复运行（每次覆盖式全量推送）。

用法：
    set GITHUB_TOKEN=github_pat_xxx
    py scripts/push_to_github.py                 # 推送
    py scripts/push_to_github.py --dry-run       # 只看会推什么
    py scripts/push_to_github.py -m "提交信息"

Token 需要 fine-grained、仅授权本仓库、Contents = Read and write。
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ── .gitignore 的等价实现（够用即可，不追求完整语义）──────────
SKIP_DIRS = {
    "__pycache__", "data", ".venv", "venv", "node_modules", ".git",
    ".idea", ".vscode", ".pytest_cache", ".mypy_cache",
}
SKIP_FILES = {".env", ".spike-state.json", ".DS_Store", "Thumbs.db"}
SKIP_EXTS = {".pyc", ".pyo", ".db", ".sqlite3", ".log"}

DEFAULT_MESSAGE = """P0: 后端骨架 + P-1 尖刺

- app/：FastAPI 后端（数据库 / 上下文组装 / 指令机制 / Serein 记忆 / 模型 / WebSocket）
- spike.py：P-1 尖刺，用于在 VPS 上验证 Serein 真链路
- tests/：四套离线测试（尖刺自检 / 尖刺集成 / P0 核心 / P0 HTTP）
- docs/dev-fake-serein.py：假 Serein，本机离线验证 Hook 契约
- Aion_拼装方案_v0.3.md：设计文档
"""


def should_skip(rel: str) -> bool:
    parts = rel.split("/")
    if any(seg in SKIP_DIRS for seg in parts[:-1]):
        return True
    name = parts[-1]
    if name in SKIP_FILES:
        return True
    if Path(name).suffix in SKIP_EXTS:
        return True
    return False


def collect_files() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT).as_posix()
        if not should_skip(rel):
            out.append((rel, path))
    return out


class GitHub:
    def __init__(self, token: str, owner: str, repo: str) -> None:
        self.token = token
        self.api = f"https://api.github.com/repos/{owner}/{repo}"
        self.owner, self.repo = owner, repo

    def call(self, method: str, url: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "aion-push")
        if data is not None:
            req.add_header("Content-Type", "application/json; charset=utf-8")

        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    payload = resp.read().decode("utf-8")
                    return json.loads(payload) if payload else {}
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:600]
                if exc.code == 403 and "rate limit" in detail.lower() and attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"{method} {url}\n  HTTP {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"{method} {url}\n  网络错误: {exc.reason}") from exc
        raise RuntimeError(f"{method} {url} 重试耗尽")


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="用 GitHub API 推送仓库（无需 git）")
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""),
                        help="GitHub token；默认读环境变量 GITHUB_TOKEN")
    parser.add_argument("--owner", default="otaku2244")
    parser.add_argument("--repo", default="Harlan")
    parser.add_argument("--branch", default="main")
    parser.add_argument("-m", "--message", default="")
    parser.add_argument("--dry-run", action="store_true", help="只列出文件，不推送")
    args = parser.parse_args()

    files = collect_files()
    if not files:
        print("没有找到可推送的文件", file=sys.stderr)
        return 1

    total = sum(p.stat().st_size for _, p in files)
    print(f"仓库根目录: {ROOT}")
    print(f"待推送: {len(files)} 个文件，{total / 1024:.1f} KB\n")
    for rel, path in files:
        print(f"  {path.stat().st_size:>8}  {rel}")

    if args.dry_run:
        print("\n--dry-run：未推送。")
        return 0

    if not args.token:
        print("\n缺少 token。设置环境变量后重试：", file=sys.stderr)
        print('  $env:GITHUB_TOKEN = "github_pat_..."', file=sys.stderr)
        return 2

    gh = GitHub(args.token, args.owner, args.repo)
    message = args.message or DEFAULT_MESSAGE

    print("\n[1/5] 校验 token 与仓库…")
    info = gh.call("GET", gh.api)
    print(f"  OK {info['full_name']}（{info['visibility']}）"
          f"{'  [权限不足，无法写入]' if not info.get('permissions', {}).get('push', True) else ''}")

    print(f"[2/5] 上传 {len(files)} 个 blob…")
    tree_items = []
    for index, (rel, path) in enumerate(files, 1):
        blob = gh.call("POST", f"{gh.api}/git/blobs", {
            "content": base64.b64encode(path.read_bytes()).decode("ascii"),
            "encoding": "base64",
        })
        tree_items.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        if index % 5 == 0 or index == len(files):
            print(f"  {index}/{len(files)}", end="\r", flush=True)
    print(f"  OK 已上传 {len(files)} 个 blob          ")

    print("[3/5] 创建 tree…")
    tree = gh.call("POST", f"{gh.api}/git/trees", {"tree": tree_items})
    print(f"  OK tree {tree['sha'][:10]}…")

    print("[4/5] 创建 commit…")
    parent = None
    try:
        parent = gh.call("GET", f"{gh.api}/git/ref/heads/{args.branch}")
    except RuntimeError:
        parent = None
    commit_body: dict = {"message": message, "tree": tree["sha"]}
    if parent:
        commit_body["parents"] = [parent["object"]["sha"]]
        print(f"  父提交 {parent['object']['sha'][:10]}…")
    else:
        print("  空仓库，本次为根提交")
    commit = gh.call("POST", f"{gh.api}/git/commits", commit_body)
    print(f"  OK commit {commit['sha'][:10]}…")

    print("[5/5] 更新分支引用…")
    if parent:
        gh.call("PATCH", f"{gh.api}/git/refs/heads/{args.branch}",
                {"sha": commit["sha"], "force": False})
        print(f"  OK {args.branch} 已更新")
    else:
        gh.call("POST", f"{gh.api}/git/refs",
                {"ref": f"refs/heads/{args.branch}", "sha": commit["sha"]})
        print(f"  OK {args.branch} 已创建")

    print(f"\n推送完成: https://github.com/{args.owner}/{args.repo}/commit/{commit['sha']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
