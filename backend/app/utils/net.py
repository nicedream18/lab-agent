"""出网环境适配：让指定主机强制走 IPv4。

## 为什么需要这个文件

`api.agnes-ai.cn` 同时有 A 和 AAAA 记录，但它对外的 IPv6 入口不接受
公网建连（TLS 握手直接被 RST）。这本来不该成为问题 —— 正常客户端会在
IPv6 失败后回退 IPv4。区别在于两个客户端的回退能力不一样：

    httpx.Client（同步）           → socket.create_connection 逐个地址试，能回退 ✅
    httpx.AsyncClient（异步）      → anyio 拿到 AAAA 后直接建连，被 RST ❌

结果就是一个非常迷惑的现象：
    `llm.invoke()` 正常，`llm.astream()` 报 ConnectError。

异常链长这样，很容易被误判成"网络抖动"：

    httpx.ConnectError
      └─ httpcore.ConnectError(EndOfStream)
           └─ SSLEOFError(8, '[SSL: UNEXPECTED_EOF_WHILE_READING]')

## 做法

只对**显式登记过的主机**过滤掉 AAAA 记录，不碰全局解析行为，
避免影响 MySQL（127.0.0.1 是字面量，本来也不走解析）和其它出站请求。

## 一个踩过的坑

httpx 的异步链路里，主机名是**以 bytes 传下来的**：

    httpcore.URL.host -> b'api.agnes-ai.cn'
    asyncio.loop.getaddrinfo(bytes, 443, ...)

所以匹配时必须先把 host 归一化。否则补丁看起来装了、实际从不命中，
而且是静默失效 —— 现象和没打补丁一模一样，极难排查。

## 第二个坑：uvloop 会绕过 socket.getaddrinfo

这是本项目里最隐蔽的一个 bug。上面那套 `socket.getaddrinfo` 补丁：

    python -m uvicorn ...        （uvicorn 默认 loop=auto，装了 uvloop 就用它）
        → 补丁失效，流式请求走 IPv6，TLS 被 RST，报 "Connection error."
    python -m uvicorn --loop asyncio
        → 补丁生效，一切正常

uvloop 有自己的 C 层解析器，**根本不经过 Python 的 `socket.getaddrinfo`**，
所以补丁不会报错、也不会命中，是彻底的静默失效。而同步链路（httpx.Client）
在普通线程里调 `socket.getaddrinfo`，补丁一直有效 —— 于是现象就变成
「同一个域名，同步能通、流式不通」，非常反直觉。

对策：uvloop 的 `Loop.getaddrinfo` 是一个 Python 可见的方法，
可以在类上包一层（实测可行，uvloop 0.22）。这样无论 uvicorn 选哪个事件循环，
白名单主机都只会解析出 A 记录。
"""

from __future__ import annotations

import logging
import socket
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# 需要强制 IPv4 的主机名集合
_IPV4_ONLY_HOSTS: set[str] = set()

# 保存原始实现，补丁里要回调它
_original_getaddrinfo = socket.getaddrinfo

# uvloop 补丁是否已装（幂等标记）
_uvloop_patched = False


def _normalize_host(host) -> str:
    """把 host 归一化成可用于比较的字符串。

    httpx/httpcore 走异步链路时传的是 bytes，同步链路传的是 str，
    另外还可能出现大小写差异和结尾的点（"example.com."）。
    """
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if not isinstance(host, str):
        # None 或其它奇怪类型：交给原始实现去报错
        return ""
    return host.rstrip(".").lower()


def _patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    results = _original_getaddrinfo(host, port, family, type, proto, flags)
    if family in (0, socket.AF_UNSPEC) and _normalize_host(host) in _IPV4_ONLY_HOSTS:
        ipv4_only = [item for item in results if item[0] == socket.AF_INET]
        # 过滤后为空说明该主机根本没有 A 记录，这时保留原结果兜底
        if ipv4_only:
            return ipv4_only
    return results


def _patch_uvloop() -> bool:
    """让 uvloop 也遵守 IPv4 白名单。

    为什么必须单独做这一步：uvloop 用自己的 C 层解析器，
    不经过 `socket.getaddrinfo`，上面那个补丁在它面前等于没装。
    （uvicorn 的 `--loop auto` 默认就会选 uvloop，所以这不是边缘情况，
    而是"本地能跑、服务器上流式必挂"的默认路径。）

    返回是否成功接管；uvloop 未安装时返回 False（正常情况，不算错误）。
    """
    global _uvloop_patched
    if _uvloop_patched:
        return True

    try:
        import uvloop
    except ImportError:
        return False

    loop_cls = getattr(uvloop, "Loop", None)
    original = getattr(loop_cls, "getaddrinfo", None)
    if loop_cls is None or original is None:
        # uvloop 版本差异：拿不到可替换的方法时放弃，
        # 由调用方（启动日志）提醒使用者改用 --loop asyncio。
        return False

    async def patched_getaddrinfo(
        loop_self, host, port, family=0, type=0, proto=0, flags=0
    ):
        if (
            family in (0, socket.AF_UNSPEC)
            and _normalize_host(host) in _IPV4_ONLY_HOSTS
        ):
            family = socket.AF_INET
        return await original(
            loop_self, host, port, family=family, type=type, proto=proto, flags=flags
        )

    loop_cls.getaddrinfo = patched_getaddrinfo
    _uvloop_patched = True
    logger.info("已为 uvloop 安装 IPv4 优先补丁（否则异步流式请求会走 IPv6 失败）")
    return True


def uvloop_patch_active() -> bool:
    """uvloop 是否已安装且已接管（供启动自检使用）。"""
    return _uvloop_patched


def ipv4_only_hosts() -> set[str]:
    """当前 IPv4 白名单的只读快照（供启动自检展示）。"""
    return set(_IPV4_ONLY_HOSTS)


def is_uvloop(loop) -> bool:
    """判断事件循环是不是 uvloop 实现。

    uvloop 不经过 `socket.getaddrinfo`，是唯一需要额外照顾的实现，
    所以启动自检要知道当前跑的是不是它。
    """
    return type(loop).__module__.startswith("uvloop")


def prefer_ipv4(base_url: str) -> str | None:
    """把 base_url 对应的主机名加入 IPv4 白名单（幂等，可重复调用）。

    返回被登记的主机名；URL 无法解析主机时返回 None。
    """
    host = urlparse(base_url or "").hostname
    if not host:
        return None
    host = host.rstrip(".").lower()

    _IPV4_ONLY_HOSTS.add(host)
    # 只在第一次真正替换 socket.getaddrinfo，重复调用是安全的
    if socket.getaddrinfo is not _patched_getaddrinfo:
        socket.getaddrinfo = _patched_getaddrinfo
    # 同步链路靠上面的 socket 补丁，异步链路（uvicorn 默认 uvloop）靠这个
    _patch_uvloop()
    return host
