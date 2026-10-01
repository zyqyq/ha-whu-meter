#!/usr/bin/env python3
"""whu_meter 集成发布脚本（跨平台，仅依赖 Python 3.8+ 标准库）

在打完 tag 后手动运行，一次性完成：
  1. 版本号同步：将 manifest.json 的 version 与 tag 对齐（不一致时自动提交并把 tag 移到新提交）
  2. 打包：生成 whu_meter_vX.X.X.zip（目录结构与手动安装要求一致）
  3. 推送：推送当前分支与 tag 到 origin
  4. Release：创建（或更新）GitHub Release，上传 zip 资产
  5. Release notes：默认综合「上一个 tag 以来的 commit 记录」自动生成；
     也可用 --notes 手动编写（手动内容在前，自动生成的 What's Changed 仍会追加，
     如需完全手写可加 --no-generate）

用法（在仓库任意位置）：
  python scripts/release.py                     # 发布当前 HEAD 所在的最新 tag
  python scripts/release.py v1.1.4              # 发布指定 tag
  python scripts/release.py --notes "修复了..."  # 手动编写 release notes
  python scripts/release.py --no-generate       # 完全使用手写 notes，不自动追加
  python scripts/release.py --dry-run           # 只打印将执行的操作，不做任何修改

GitHub 令牌（用于创建 Release 与上传资产，需 repo 权限），按以下顺序读取：
  1. 环境变量 GITHUB_TOKEN 或 GH_TOKEN
  2. 仓库根目录下的 .github_token 文件（已被 .gitignore 排除，写入一行 token 即可）
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPONENT_DIR = REPO_ROOT / "custom_components" / "whu_meter"
MANIFEST = COMPONENT_DIR / "manifest.json"
EXCLUDE_IN_ZIP = {"__pycache__", ".DS_Store", "Thumbs.db", "desktop.ini"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".log")


def run(cmd, cwd=REPO_ROOT, capture=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=capture, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise SystemExit(f"[错误] 命令失败: {' '.join(cmd)}\n{r.stderr.strip()}")
    return r.stdout.strip()


def resolve_tag(explicit):
    if explicit:
        return explicit
    tags_on_head = run(["git", "tag", "--points-at", "HEAD"]).split()
    if not tags_on_head:
        raise SystemExit("[错误] 当前 HEAD 上没有 tag。请先打 tag，例如：git tag v1.1.4")
    if len(tags_on_head) > 1:
        raise SystemExit(f"[错误] 当前 HEAD 上有多个 tag：{tags_on_head}，请显式指定一个")
    return tags_on_head[0]


def sync_version(tag, version, dry):
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("version") == version:
        print(f"[1/5] 版本号已同步：manifest.json = {version}")
        return
    if dry:
        print(f"[1/5] (dry-run) 将把 manifest.json 版本号 {manifest.get('version')} -> {version} 并提交、移动 tag")
        return
    manifest["version"] = version
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    run(["git", "add", str(MANIFEST.relative_to(REPO_ROOT).as_posix())])
    run(["git", "commit", "-m", f"chore(release): 同步集成版本号至 {tag} [skip ci]"])
    run(["git", "tag", "-f", tag])
    print(f"[1/5] manifest.json 版本号已更新为 {version}，tag 已移动到新提交")


def build_zip(tag, version, dry):
    out = REPO_ROOT / f"whu_meter_{tag}.zip"
    if dry:
        print(f"[2/5] (dry-run) 将生成 {out.name}")
        return out
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(COMPONENT_DIR):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_IN_ZIP]
            for f in files:
                if f.endswith(EXCLUDE_SUFFIX) or f in EXCLUDE_IN_ZIP:
                    continue
                full = Path(root) / f
                z.write(full, full.relative_to(REPO_ROOT).as_posix())
    print(f"[2/5] 已打包 {out.name}（{out.stat().st_size} 字节）")
    return out


def push(tag, dry):
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if dry:
        print(f"[3/5] (dry-run) 将推送 {branch} 与 tag {tag} 到 origin")
        return
    run(["git", "push", "origin", branch])
    run(["git", "push", "origin", tag, "--force"])
    print(f"[3/5] 已推送 {branch} 与 {tag} 到 origin")


def get_token():
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok.strip()
    f = REPO_ROOT / ".github_token"
    if f.exists():
        t = f.read_text(encoding="utf-8").strip()
        if t:
            return t
    raise SystemExit(
        "[错误] 未找到 GitHub 令牌。两种提供方式：\n"
        "  1. 设置环境变量 GITHUB_TOKEN（需 repo 权限的 PAT）\n"
        "  2. 在仓库根目录新建 .github_token 文件，第一行写入 PAT"
    )


def parse_owner_repo():
    url = run(["git", "remote", "get-url", "origin"])
    # 支持 git@github.com:owner/repo.git 与 https://github.com/owner/repo.git
    for sep in (":github.com" + ":", "github.com/"):
        pass
    if "github.com" in url:
        part = url.split("github.com")[-1].lstrip(":/")
        owner, repo = part.split("/")[:2]
        return owner, repo.removesuffix(".git")
    raise SystemExit(f"[错误] 无法从 origin 解析 owner/repo：{url}")


def gh_api(token, url, method="GET", data=None, content_type="application/json", accept=None):
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", accept or "application/vnd.github+json")
    if content_type:
        req.add_header("Content-Type", content_type)
    body = json.dumps(data).encode() if isinstance(data, dict) else data
    try:
        with urllib.request.urlopen(req, body) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        return e.code, (json.loads(e.read() or b"{}") if e.code != 404 else None)


def auto_notes(tag):
    tags = run(["git", "tag", "--sort=-creatordate"]).split()
    if tag not in tags:
        return ""
    idx = tags.index(tag)
    prev = tags[idx + 1] if idx + 1 < len(tags) else None
    rng = f"{prev}..{tag}" if prev else tag
    log = run(["git", "log", rng, "--no-merges", "--pretty=- %h %s"])
    header = f"自上一个版本 {prev} 以来的变更：" if prev else "首个版本的变更："
    return f"{header}\n\n{log}" if log else ""


def build_notes(tag, manual, no_generate):
    parts = []
    if manual:
        parts.append(manual)
    auto = auto_notes(tag)
    if auto:
        parts.append(auto)
    if not parts:
        parts.append(" maintenance release ")
    body = "\n\n".join(parts)
    print(f"[4/5] Release notes:\n{'-' * 40}\n{body}\n{'-' * 40}")
    return body, not no_generate


def upload_release(token, owner, repo, tag, name, zip_path, body, generate, dry):
    if dry:
        print(f"[5/5] (dry-run) 将在 {owner}/{repo} 创建/更新 Release {tag} 并上传 {zip_path.name}")
        return
    status, rel = gh_api(token, f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}")
    if status == 200 and rel:
        release_id, html_url = rel["id"], rel["html_url"]
        gh_api(token, f"https://api.github.com/repos/{owner}/{repo}/releases/{release_id}",
               method="PATCH", data={"name": name, "body": body})
        for a in rel.get("assets", []):
            if a["name"] == zip_path.name:
                gh_api(token, f"https://api.github.com/repos/{owner}/{repo}/releases/assets/{a['id']}", method="DELETE")
                print(f"      已删除旧资产 {a['name']}")
        print(f"[5/5] Release 已更新：{html_url}")
    else:
        status, rel = gh_api(token, f"https://api.github.com/repos/{owner}/{repo}/releases", method="POST",
                             data={"tag_name": tag, "name": name, "body": body,
                                   "generate_release_notes": generate})
        if status not in (200, 201):
            raise SystemExit(f"[错误] 创建 Release 失败（HTTP {status}）：{rel}")
        release_id, html_url = rel["id"], rel["html_url"]
        print(f"[5/5] Release 已创建：{html_url}")
    # 上传 zip 资产
    data = zip_path.read_bytes()
    status, resp = gh_api(token, f"https://uploads.github.com/repos/{owner}/{repo}/releases/{release_id}/assets"
                          f"?name={zip_path.name}", method="POST", data=data,
                          content_type="application/zip", accept="application/vnd.github+json")
    if status not in (200, 201):
        raise SystemExit(f"[错误] 上传资产失败（HTTP {status}）：{resp}")
    print(f"      资产已上传：{zip_path.name}（{len(data)} 字节）")
    print("\n完成。发布页：" + html_url)


def main():
    ap = argparse.ArgumentParser(description="whu_meter 集成发布脚本")
    ap.add_argument("tag", nargs="?", help="要发布的 tag（默认取当前 HEAD 上的 tag）")
    ap.add_argument("--notes", default="", help="手动编写的 release notes")
    ap.add_argument("--no-generate", action="store_true", help="不自动追加 GitHub 生成的 What's Changed")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不实际执行")
    args = ap.parse_args()

    tag = resolve_tag(args.tag)
    if not tag.startswith("v"):
        raise SystemExit(f"[错误] tag 应以 v 开头（当前：{tag}）")
    version = tag[1:]
    print(f"发布目标：{tag}（集成版本号 {version}）\n")

    sync_version(tag, version, args.dry_run)
    zip_path = build_zip(tag, version, args.dry_run)
    push(tag, args.dry_run)

    token = get_token()
    owner, repo = parse_owner_repo()
    print(f"[4/5] 目标仓库：{owner}/{repo}")
    body, generate = build_notes(tag, args.notes, args.no_generate)
    name = f"whu_meter {tag}"
    upload_release(token, owner, repo, tag, name, zip_path, body, generate, args.dry_run)


if __name__ == "__main__":
    main()
