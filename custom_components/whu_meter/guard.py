"""把「这个地址是不是外部/生产服务器」收敛到一处判定。

为什么需要它
------------
配置流第一步允许用户填任意 URL。指向本机 / 局域网的地址（mock、自建镜像）可以
直接连；指向外部生产服务器（如武大宿舍付费平台）的地址，集成会在**用户点击
提交后立刻发起真实请求**（先登录，再逐级拉区域 / 楼栋 / 楼层 / 房间 / 表具）。

为了把"对外真实流量"变成**必须由人显式确认**的动作，本模块提供纯函数的地址判定，
配置流据此插一步「警告 + 确认」：未确认前，针对该地址的服务器请求数为 0。

判定规则（纯字符串 / IP 运算，不做 DNS 解析、不发任何网络请求）
--------------------------------------------------------------
1. 地址里命中 RESTRICTED_SUBSTRINGS 任一子串 -> 受限（无论主机名长什么样）。
2. 否则取主机名：
   * loopback（localhost / 127.0.0.1 / ::1 / 0.0.0.0）
   * ``*.local`` / ``*.localhost``
   * Docker / Podman 宿主别名（host.docker.internal 等）
   * RFC1918 私有地址、链路本地、IPv6 私有段
   以上视为**本地**，直接放行；
3. **其余一律视为受限** —— 即使它不是武大服务器。理由：生产环境以外的公网主机，
   在配置集成这个场景里没有正当使用理由，确认一下不亏。
"""
from __future__ import annotations

import ipaddress
import re
import urllib.parse

# 命中即受限（与调试脚本 safety.py 的黑名单保持一致）
RESTRICTED_SUBSTRINGS: tuple[str, ...] = (
    "whu.edu.cn",   # 武大域名（含 zwhqbsd.whu.edu.cn）
    "zwhqbsd",      # 宿舍付费平台主机名
    "115.156.",     # 武大公网段
    "202.114.",     # 武大公网段
    "218.197.",     # 武大公网段
)

LOCAL_HOST_EXACT: frozenset[str] = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
        "0.0.0.0",
        "host.docker.internal",
        "host.containers.internal",
        "gateway.docker.internal",
    }
)

LOCAL_HOST_SUFFIXES: tuple[str, ...] = (".local", ".localhost")

# 合法 ASCII 主机名（含单标签名，如 "myserver"）
_ASCII_HOST_RE = re.compile(
    r"^[a-z0-9]([a-z0-9_-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9_-]*[a-z0-9])?)*$"
)


def host_of(base_url: str) -> str:
    """从用户输入里尽力取出主机名；取不到"像样的主机名"时返回空串。

    容忍没有 scheme 的写法（``zwhqbsd.whu.edu.cn`` -> ``http://…`` 再解析）。
    返回空串意味着"这个输入不像一个地址"，此时 `is_restricted_base_url` 会放行，
    让探测分支去报"无法连接"——而不是拿一个确认对话框去糊弄用户。

    非 ASCII 输入（例如手打的中文）分两种：
      · 含点（像 IDN 域名，如 "电表.中国"）-> 返回原值，**保守判定为受限**，
        避免"用 IDN 绕过确认门"的口子；
      · 不含点（纯乱码）-> 返回空串。
    """
    raw = (base_url or "").strip()
    if not raw or any(ch.isspace() for ch in raw):
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return ""
    host = (parsed.hostname or "").strip().lower()
    if not host or any(ch.isspace() for ch in host):
        return ""

    if not host.isascii():
        return host if "." in host else ""

    # 合法 IP（含 IPv6 字面量）优先放行给 is_local_host 判定
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return host

    return host if _ASCII_HOST_RE.match(host) else ""



def is_local_host(host: str) -> bool:
    """主机名是否属于「本机 / 私有网段」——这类地址不需要确认。"""
    if not host:
        return False
    if host in LOCAL_HOST_EXACT:
        return True
    if host.endswith(LOCAL_HOST_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)


def is_restricted_base_url(base_url: str) -> bool:
    """True = 该地址指向外部/生产服务器，必须先经用户显式确认才能继续。

    取不到主机名（输入明显非法）时返回 False —— 让探测分支去报"无法连接"，
    而不是拿一个确认对话框去糊弄用户。
    """
    raw = (base_url or "").strip().lower()
    if not raw:
        return False
    if any(sub in raw for sub in RESTRICTED_SUBSTRINGS):
        return True
    host = host_of(raw)
    if not host:
        return False
    return not is_local_host(host)
