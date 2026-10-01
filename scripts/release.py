#!/usr/bin/env python3
"""whu_meter 集成发布脚本（跨平台，仅依赖 Python 3.8+ 标准库）

两种用法：

【交互模式】不带任何参数直接运行，进入中文逐步引导（数字选择）：
  python scripts/release.py

  流程：
  第1步  选择新版本号：基于 git 上最新 tag 给出 主版本/次版本/修订号 三种
         预制递增方案，也可自行输入（如 v1.1.4 -> v2.0.0 / v1.2.0 / v1.1.5）；
         另提供「补发布」：为已存在的 tag 补发 Release（不改版本号、不建 commit），
         适用于发版中断（如网络异常）后重试
  第2步  工作区检查：列出未提交文件（已暂存/未暂存/未跟踪），可选
         合并提交 / 撤回 / stash 保留（发版结束后自动复原）
  第3步  发版 commit：选择是否同步「用户使用说明.md」（页首适用版本 +
         页尾版本字样），与 manifest.json 一并提交为
         chore(release): bump version to vX.Y.Z
  第4步  自动在发版 commit 上打 tag
  第5步  Release notes 编写（自动/手写/混合）与发布（支持先 dry-run
         预览再确认执行），发布同时推送发版 commit 与 tag

【命令行模式】适合熟练后快速发布（要求工作区干净）：
  python scripts/release.py v1.1.5              # 直接发布指定版本
  python scripts/release.py v1.1.5 --notes "说明"
  python scripts/release.py v1.1.5 --no-doc     # 不同步使用说明页脚/页首
  python scripts/release.py v1.1.5 --dry-run    # 只打印将执行的操作

GitHub 令牌（用于创建 Release 与上传资产，需 repo 权限），按以下顺序读取：
  1. 环境变量 GITHUB_TOKEN 或 GH_TOKEN
  2. 仓库根目录下的 .github_token 文件（已被 .gitignore 排除，写入一行 token 即可）

关于 TLS：Python 3.13+ 默认启用证书严格校验（VERIFY_X509_STRICT），在部分
Windows 证书库上会误报「Missing Authority Key Identifier」。脚本会自动关闭
这一项额外检查（证书链与主机名校验仍开启），并在需要时回退使用 certifi 的 CA 包。
"""

import argparse
import json
import os
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPONENT_DIR = REPO_ROOT / "custom_components" / "whu_meter"
MANIFEST = COMPONENT_DIR / "manifest.json"
USER_DOC = REPO_ROOT / "docs" / "用户使用说明.md"
EXCLUDE_IN_ZIP = {"__pycache__", ".DS_Store", "Thumbs.db", "desktop.ini"}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".log")
VER_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


# ---------------------------------------------------------------- 基础工具

def run(cmd, cwd=REPO_ROOT, capture=True):
    r = subprocess.run(cmd, cwd=cwd, capture_output=capture, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise SystemExit(f"[错误] 命令失败: {' '.join(cmd)}\n{r.stderr.strip()}")
    return r.stdout.strip()


# ------------------------------------------------------- GitHub API 的 TLS 上下文
#
# Python 3.13 起 ssl.create_default_context() 默认启用 VERIFY_X509_STRICT，
# 该标志会额外要求证书链上每个 CA 证书都携带 authorityKeyIdentifier 扩展。
# 部分 Windows 证书库里的根/中间证书不满足该要求，直连 api.github.com 时会报
#   [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
#    Missing Authority Key Identifier
# 这里只关闭这一项额外的严格扩展检查（证书链与主机名校验仍保持开启）；
# 若系统证书库仍校验不过，再回退尝试 certifi 提供的 CA 包。

_SSL_CONTEXTS = None


def ssl_contexts():
    """按优先级返回 [(说明, SSLContext), ...]，结果缓存"""
    global _SSL_CONTEXTS
    if _SSL_CONTEXTS is not None:
        return _SSL_CONTEXTS
    ctxs = []
    ctx = ssl.create_default_context()
    strict = getattr(ssl, "VERIFY_X509_STRICT", 0)
    if strict:
        ctx.verify_flags &= ~strict
    ctxs.append(("系统证书库", ctx))
    try:
        import certifi  # 可选依赖，未安装则跳过
        ctxs.append(("certifi", ssl.create_default_context(cafile=certifi.where())))
    except Exception:
        pass
    _SSL_CONTEXTS = ctxs
    return ctxs


def ask(prompt, default=None):
    s = input(prompt).strip()
    return s if s else default


def ask_choice(prompt, valid, default=None):
    while True:
        s = ask(prompt, default)
        if s in valid:
            return s
        print(f"  请输入 {'/'.join(valid)} 中的选项")


def ask_multiline(prompt):
    print(prompt + "（输入内容，单独一个空行结束）：")
    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "":
            break
        lines.append(line.rstrip())
    return "\n".join(lines)


def latest_tag():
    """git 上最新的版本 tag（按版本号排序取最高），无 tag 时返回 None"""
    tags = [t for t in run(["git", "tag", "--sort=-v:refname"]).split() if VER_RE.match(t)]
    return tags[0] if tags else None


def bump_version(latest, part):
    m = VER_RE.match(latest)
    x, y, z = (int(m.group(i)) for i in (1, 2, 3))
    if part == "major":
        return f"v{x + 1}.0.0"
    if part == "minor":
        return f"v{x}.{y + 1}.0"
    return f"v{x}.{y}.{z + 1}"


# ---------------------------------------------------------------- 工作区处理

def detect_changes():
    staged = run(["git", "diff", "--cached", "--name-only"]).splitlines()
    unstaged = run(["git", "diff", "--name-only"]).splitlines()
    untracked = run(["git", "ls-files", "--others", "--exclude-standard"]).splitlines()
    return staged, unstaged, untracked


def print_changes(staged, unstaged, untracked):
    for f in staged:
        print(f"  [已暂存] {f}")
    for f in unstaged:
        print(f"  [未暂存] {f}")
    for f in untracked:
        print(f"  [未跟踪] {f}")


def handle_changes(interactive_label="第 2 步 / 5"):
    """处理未提交文件。返回 'merged'（并入发版 commit）/ 'stashed'（结束后复原）/ None"""
    staged, unstaged, untracked = detect_changes()
    if not (staged or unstaged or untracked):
        print(f"\n【{interactive_label}】工作区检查：无未提交文件，跳过")
        return None

    print(f"\n【{interactive_label}】工作区检查：发现未提交文件（相对仓库根目录）：")
    print_changes(staged, unstaged, untracked)
    print("\n  1) 合并提交：与版本号同步一并计入发版 commit")
    print("  2) 撤回：丢弃全部未提交修改（不可恢复！）")
    print("  3) 保留：stash 暂存，发版结束后自动复原")
    act = ask_choice("请选择 [1/2/3] (1): ", {"1", "2", "3"}, default="1")

    if act == "1":
        run(["git", "add", "-u"])  # 暂存所有未暂存的已跟踪文件
        while True:
            _, _, untracked = detect_changes()
            if not untracked:
                break
            print("\n  [警告] 仍存在未跟踪文件（git add -u 不会包含它们）：")
            for f in untracked:
                print(f"  [未跟踪] {f}")
            print("  请自行处理（加入 git add 或删除）后继续。")
            ask("  处理完成后输入 y 继续: ")
        return "merged"

    if act == "2":
        print("\n  [危险] 即将丢弃以下全部未提交内容（不可恢复）：")
        print_changes(staged, unstaged, untracked)
        if ask_choice("  确认撤回？[y/n] (n): ", {"y", "n"}) != "y":
            sys.exit("已取消")
        run(["git", "reset", "--hard", "HEAD"])
        run(["git", "clean", "-fd"])
        print("  工作区已还原到 HEAD")
        return None

    run(["git", "stash", "push", "-u", "-m", "release.py 自动暂存"])
    print("  已 stash 全部未提交内容（含未跟踪），发版结束后自动复原")
    return "stashed"


# ---------------------------------------------------------------- 版本同步

def doc_footer_stale(version):
    if not USER_DOC.exists():
        return False
    text = USER_DOC.read_text(encoding="utf-8")
    has_footer = bool(re.search(r"本文档对应\s*whu_meter\s*v[\d.]+", text))
    return has_footer and not re.search(rf"本文档对应\s*whu_meter\s*v{re.escape(version)}\b", text)


def apply_version_updates(version, sync_doc):
    """将 manifest.json（必改）与使用说明页首/页脚（可选）写到指定版本，返回改动文件路径列表"""
    paths = []
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    if manifest.get("version") != version:
        manifest["version"] = version
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        paths.append(MANIFEST)
    if sync_doc and USER_DOC.exists():
        text = USER_DOC.read_text(encoding="utf-8")
        new = re.sub(r"(本文档对应\s*whu_meter\s*v)[\d.]+", rf"\g<1>{version}", text)
        new = re.sub(r"(适用版本：\s*v)[\d.]+", rf"\g<1>{version}", new)
        if new != text:
            USER_DOC.write_text(new, encoding="utf-8")
            paths.append(USER_DOC)
    return paths


# ---------------------------------------------------------------- 打包 / 发布

def build_zip(tag, dry):
    out = REPO_ROOT / f"whu_meter_{tag}.zip"
    if dry:
        print(f"[4/5] (dry-run) 将生成 {out.name}")
        return out
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(COMPONENT_DIR):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_IN_ZIP]
            for f in files:
                if f.endswith(EXCLUDE_SUFFIX) or f in EXCLUDE_IN_ZIP:
                    continue
                full = Path(root) / f
                z.write(full, full.relative_to(REPO_ROOT).as_posix())
    print(f"[4/5] 已打包 {out.name}（{out.stat().st_size} 字节）")
    return out


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
    last_err = None
    for label, ctx in ssl_contexts():
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        try:
            with opener.open(req, body, timeout=120) as resp:
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return 404, None
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except ValueError:  # 代理/网关可能返回 HTML 错误页
                return e.code, {"message": raw.decode("utf-8", "replace")[:400]}
        except urllib.error.URLError as e:
            if "CERTIFICATE_VERIFY_FAILED" not in str(e) and not isinstance(e.reason, ssl.SSLError):
                raise SystemExit(f"[错误] 无法连接 GitHub API（{label}）：{e}")
            last_err = (label, e)  # 证书校验失败 -> 换下一个证书来源重试
    tried = "、".join(label for label, _ in ssl_contexts())
    raise SystemExit(
        f"[错误] TLS 证书校验失败（已尝试：{tried}）\n"
        f"  最后一次错误：{last_err[1]}\n"
        "  可尝试：安装 certifi（pip install certifi），"
        "或设置环境变量 SSL_CERT_FILE 指向可用的 CA 证书包。")


def tag_exists(t):
    r = subprocess.run(["git", "rev-parse", "-q", "--verify", t + "^{commit}"],
                       cwd=REPO_ROOT, capture_output=True, text=True)
    return r.returncode == 0


def auto_notes(tag):
    """自上一个版本 tag 以来的 commit 列表"""
    cur = VER_RE.match(tag)
    prev = None
    for t in run(["git", "tag", "--sort=-v:refname"]).split():
        m = VER_RE.match(t)
        if m and tuple(map(int, m.groups())) < tuple(map(int, cur.groups())):
            prev = t
            break
    # dry-run 预览时 tag 可能尚未创建，用 HEAD 代替
    end = tag if tag_exists(tag) else "HEAD"
    rng = f"{prev}..{end}" if prev else end
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
    print(f"[5/5] Release notes:\n{'-' * 40}\n{body}\n{'-' * 40}")
    return body, not no_generate


def push_all(tag, dry):
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if dry:
        print(f"[推送] (dry-run) 将推送 {branch} 与 tag {tag} 到 origin")
        return
    run(["git", "push", "origin", branch])
    run(["git", "push", "origin", tag, "--force"])
    print(f"[推送] 已同步远端：{branch} -> 发版 commit，tag {tag}")


def upload_release(token, owner, repo, tag, zip_path, body, generate, dry):
    name = f"whu_meter {tag}"
    if dry:
        print(f"[Release] (dry-run) 将在 {owner}/{repo} 创建/更新 Release {tag} 并上传 {zip_path.name}")
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
        print(f"[Release] Release 已更新：{html_url}")
    else:
        status, rel = gh_api(token, f"https://api.github.com/repos/{owner}/{repo}/releases", method="POST",
                             data={"tag_name": tag, "name": name, "body": body,
                                   "generate_release_notes": generate})
        if status not in (200, 201):
            raise SystemExit(f"[错误] 创建 Release 失败（HTTP {status}）：{rel}")
        release_id, html_url = rel["id"], rel["html_url"]
        print(f"[Release] Release 已创建：{html_url}")
    data = zip_path.read_bytes()
    status, resp = gh_api(token, f"https://uploads.github.com/repos/{owner}/{repo}/releases/{release_id}/assets"
                          f"?name={zip_path.name}", method="POST", data=data,
                          content_type="application/zip", accept="application/vnd.github+json")
    if status not in (200, 201):
        raise SystemExit(f"[错误] 上传资产失败（HTTP {status}）：{resp}")
    print(f"      资产已上传：{zip_path.name}（{len(data)} 字节）")
    print("\n完成。发布页：" + html_url)


def publish(tag, manual, no_generate, dry):
    """推送 commit/tag 并创建 Release（dry=True 时只打印）"""
    zip_path = build_zip(tag, dry)
    push_all(tag, dry)
    token = get_token()
    owner, repo = parse_owner_repo()
    print(f"[Release] 目标仓库：{owner}/{repo}")
    body, generate = build_notes(tag, manual, no_generate)
    upload_release(token, owner, repo, tag, zip_path, body, generate, dry)


# ---------------------------------------------------------------- 交互模式

def choose_version():
    """返回 (tag, republish)：republish=True 表示补发布已有 tag（不改版本号）"""
    latest = latest_tag()
    base = latest or "v0.0.0"
    print("\n【第 1 步 / 5】选择新版本号（git 上当前最新版本：" + base + "）")
    print(f"  1) {bump_version(base, 'major')}（主版本 +1）")
    print(f"  2) {bump_version(base, 'minor')}（次版本 +1）")
    print(f"  3) {bump_version(base, 'patch')}（修订号 +1）")
    print("  4) 自定义输入")
    print("  5) 补发布：为已存在的 tag 补发 Release（不改版本号、不新建 commit）")
    sel = ask_choice("请选择 [1/2/3/4/5] (3): ", {"1", "2", "3", "4", "5"}, default="3")
    if sel == "5":
        return choose_existing_tag(), True
    if sel != "4":
        return bump_version(base, {"1": "major", "2": "minor", "3": "patch"}[sel]), False
    while True:
        tag = ask("请输入版本 tag（以 v 开头，如 v1.1.4）: ")
        if VER_RE.match(tag):
            break
        print("  格式应为 vX.Y.Z（如 v1.2.3），请重试")
    if latest and tuple(map(int, VER_RE.match(tag).groups())) <= \
            tuple(map(int, VER_RE.match(latest).groups())):
        print(f"  [提示] {tag} 不高于当前最新版本 {latest}，发布时将覆盖已有 tag / Release")
        if ask_choice("  确认使用？[y/n] (n): ", {"y", "n"}) != "y":
            sys.exit("已取消")
    return tag, False


def choose_existing_tag():
    """从本地已有 tag 中挑选一个，用于补发布"""
    tags = run(["git", "tag", "--sort=-v:refname"]).split()
    if not tags:
        raise SystemExit("[错误] 本地没有任何 tag，无法补发布；请改用「新建版本号」流程")
    at_head = set(run(["git", "tag", "--points-at", "HEAD"]).split())
    print("\n  可补发布的 tag（本地已有）：")
    for i, t in enumerate(tags, 1):
        print(f"    {i}) {t}{'  <- 当前 HEAD' if t in at_head else ''}")
    sel = ask_choice(f"  请选择 [1-{len(tags)}]: ", {str(i) for i in range(1, len(tags) + 1)})
    return tags[int(sel) - 1]


def ask_notes():
    """第 5 步：Release notes 编写方式，返回 (manual, no_generate)"""
    print("\n【第 5 步 / 5】Release notes 编写方式")
    print("  1) 自动生成：综合上一个版本 tag 以来的 commit 记录")
    print("  2) 手动编写：自己输入说明文字")
    print("  3) 混合：手动内容在前，自动生成的变更列表追加在后（推荐）")
    mode = ask_choice("请选择 [1/2/3] (3): ", {"1", "2", "3"}, default="3")
    manual = ask_multiline("\n请输入手写说明：") if mode in ("2", "3") else ""
    return manual, (mode == "2")


def run_republish(tag):
    """补发布模式：tag 已存在，只推送并按需创建 / 更新 Release"""
    if not tag_exists(tag):
        raise SystemExit(f"[错误] 本地不存在 tag {tag}")
    manifest_ver = json.loads(MANIFEST.read_text(encoding="utf-8")).get("version")
    print(f"\n【补发布】tag：{tag}")
    print(f"  本地 HEAD       : {run(['git', 'rev-parse', '--short', 'HEAD'])}")
    print(f"  manifest 版本号 : {manifest_ver}")
    if manifest_ver != tag[1:]:
        print(f"  [提示] manifest.json 版本号（{manifest_ver}）与 tag（{tag}）不一致；"
              "补发布不会修改任何文件，如需同步请改用「新建版本号」流程")

    manual, no_generate = ask_notes()

    print("\n发布摘要")
    print(f"  - 版本 tag    : {tag}")
    print(f"  - 安装包      : whu_meter_{tag}.zip")
    print("  - 操作        : 不新建 commit、不移动 tag，仅同步远端并创建/更新 Release")
    print("\n  1) 直接发布")
    print("  2) 先 dry-run 预览（不推送、不上传）")
    print("  3) 取消")
    act = ask_choice("请选择 [1/2/3] (1): ", {"1", "2", "3"}, default="1")
    if act == "3":
        print("已取消。")
        return
    if act == "2":
        publish(tag, manual, no_generate, dry=True)
        if ask_choice("\n按预览执行发布？[y/n] (y): ", {"y", "n"}, default="y") != "y":
            print("已取消。")
            return
    publish(tag, manual, no_generate, dry=False)


def interactive():
    print("=" * 56)
    print("  whu_meter 集成发布向导")
    print("=" * 56)

    # 第 1 步：版本号 / 补发布
    tag, republish = choose_version()
    if republish:
        run_republish(tag)
        return
    version = tag[1:]

    # 第 2 步：工作区
    stash_used = handle_changes() == "stashed"

    # 第 3 步：发版 commit
    print(f"\n【第 3 步 / 5】发版 commit")
    sync_doc = ask_choice(
        "是否同步「用户使用说明.md」（页首适用版本 + 页尾版本字样）？[y/n] (y): ",
        {"y", "n"}, default="y") == "y"
    changed = apply_version_updates(version, sync_doc)
    names = "、".join(p.name for p in changed) if changed else "（版本号均已一致，无文件改动）"
    print(f"  将更新：{names}")
    if run(["git", "status", "--porcelain"]):
        print(f"  commit 信息：chore(release): bump version to {tag}")
        run(["git", "commit", "-m", f"chore(release): bump version to {tag}"])
        print(f"  发版 commit 已创建：{run(['git', 'rev-parse', '--short', 'HEAD'])}")
    else:
        print("  版本号已一致且无其他改动，无需新建 commit，tag 将直接指向当前 HEAD")

    # 第 4 步：打 tag
    print(f"\n【第 4 步 / 5】打 tag")
    run(["git", "tag", "-f", tag])
    print(f"  tag {tag} 已指向发版 commit")

    # 第 5 步：notes 与发布
    manual, no_generate = ask_notes()

    print("\n发布摘要")
    print(f"  - 版本 tag    : {tag}")
    print(f"  - 安装包      : whu_meter_{tag}.zip")
    print(f"  - 发版 commit : {run(['git', 'rev-parse', '--short', 'HEAD'])}")
    print("\n  1) 直接发布（推送 commit + tag，创建/更新 Release）")
    print("  2) 先 dry-run 预览（不推送、不上传）")
    print("  3) 取消（commit 与 tag 已在本地，可稍后重跑本脚本用「补发布」补上 Release）")
    act = ask_choice("请选择 [1/2/3] (1): ", {"1", "2", "3"}, default="1")

    try:
        if act == "3":
            print("已跳过发布。发版 commit 与 tag 已在本地，随时可重新运行脚本或 git push 手动发布。")
            return
        if act == "2":
            publish(tag, manual, no_generate, dry=True)
            if ask_choice("\n按预览执行发布？[y/n] (y): ", {"y", "n"}, default="y") != "y":
                print("已跳过发布。发版 commit 与 tag 已在本地，可稍后重跑本脚本补发布。")
                return
        publish(tag, manual, no_generate, dry=False)
    finally:
        if stash_used:
            out = run(["git", "stash", "list"])
            if "release.py 自动暂存" in out:
                run(["git", "stash", "pop"])
                print("\n[复原] 已从 stash 恢复发版前的工作区改动")


def release_cli(args):
    """命令行模式：要求工作区干净，直接完成 commit/tag/发布"""
    staged, unstaged, untracked = detect_changes()
    if staged or unstaged or untracked:
        raise SystemExit(
            "[错误] 工作区存在未提交文件（已暂存/未暂存/未跟踪），命令行模式不予处理。\n"
            "  请先提交或清理，或改用交互模式：python scripts/release.py")

    tag = args.tag
    if not tag:
        raise SystemExit("[错误] 命令行模式需指定版本 tag，如：python scripts/release.py v1.1.5")
    if not VER_RE.match(tag):
        raise SystemExit(f"[错误] tag 格式应为 vX.Y.Z（当前：{tag}）")
    version = tag[1:]

    if args.dry_run:
        print(f"[dry-run] 将同步 manifest.json{'' if args.no_doc else '、docs/用户使用说明.md 页首/页脚'}"
              f" -> {version}，创建发版 commit（chore(release): bump version to {tag}）并打 tag {tag}")
    else:
        changed = apply_version_updates(version, not args.no_doc)
        run(["git", "add", *(p.relative_to(REPO_ROOT).as_posix() for p in changed)])
        if run(["git", "status", "--porcelain"]):
            run(["git", "commit", "-m", f"chore(release): bump version to {tag}"])
        run(["git", "tag", "-f", tag])
        print(f"发版 commit 与 tag {tag} 已就绪\n")
    publish(tag, args.notes, args.no_generate, args.dry_run)


def main():
    # 不带参数 -> 交互模式
    if len(sys.argv) == 1:
        interactive()
        return

    ap = argparse.ArgumentParser(description="whu_meter 集成发布脚本（不带参数运行进入交互向导）")
    ap.add_argument("tag", nargs="?", help="要发布的版本 tag（格式 vX.Y.Z，自动创建发版 commit 与 tag）")
    ap.add_argument("--notes", default="", help="手动编写的 release notes")
    ap.add_argument("--no-generate", action="store_true", help="不自动追加 GitHub 生成的 What's Changed")
    ap.add_argument("--no-doc", action="store_true", help="不同步「用户使用说明.md」的版本字样")
    ap.add_argument("--dry-run", action="store_true", help="只预览，不实际执行")
    args = ap.parse_args()
    release_cli(args)


if __name__ == "__main__":
    main()
