"""环境变量的健壮化处理。

## 为什么需要这个模块（真机事故）

第三十三轮：把 `HTTPS_PROXY` 加进环境后，`pp swebench` 直接崩在启动阶段：

```
httpx/_urlparse.py:409 in normalize_port
    port_as_int = int(port)
InvalidURL: Invalid port: ':1]'
```

根因：Windows 环境块里 `NO_PROXY` 含**带方括号的 IPv6**（`::1,[::1]`，代理客户端很爱这么写），
而 httpx 解析 `no_proxy` 时会把 `[::1]` 当 URL 处理，于是把 `:1]` 当端口。
这与 PatchPilot 的代码无关，是上游解析缺陷——但**应用不该因此启动失败**，
而且这个环境是代理客户端自动写的，用户很难自己排查。

## 处理方式

启动时把 `NO_PROXY`/`no_proxy` 里**含方括号**的条目去掉（保留 `127.0.0.1` / `localhost` / `::1`
这类可解析的写法）。只改这两个变量，不碰 `HTTP_PROXY`/`HTTPS_PROXY`。

注意 Windows 上环境块允许**同名不同大小写**的两份（实测本机同时存在
`NO_PROXY` 与 `no_proxy`），所以要两个名字都写一遍。
"""

from __future__ import annotations

import os

#: 会被清理的环境变量名（大小写两份都要写，见模块说明）。
_NAMES = ("NO_PROXY", "no_proxy")


def _is_bracketed(token: str) -> bool:
    return "[" in token or "]" in token


def sanitize_no_proxy() -> list[str]:
    """去掉 `NO_PROXY` 里含方括号的条目，返回被去掉的条目（供日志/测试断言）。

    没有该变量、或没有需要清理的条目时返回空列表（幂等）。
    """
    removed: list[str] = []
    for name in _NAMES:
        raw = os.environ.get(name)
        if not raw:
            continue
        kept: list[str] = []
        for token in raw.split(","):
            stripped = token.strip()
            if not stripped:
                continue
            if _is_bracketed(stripped):
                removed.append(stripped)
                continue
            kept.append(stripped)
        if kept:
            os.environ[name] = ",".join(kept)
        else:
            # 全被清掉时删掉变量本身，避免留下空串让下游误判
            os.environ.pop(name, None)
    return removed


__all__ = ["sanitize_no_proxy"]
