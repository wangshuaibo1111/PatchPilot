"""`NO_PROXY` 健壮化的测试（真机事故的钉子）。

事故形态：代理客户端把 `NO_PROXY` 写成 `127.0.0.1,localhost,::1,[::1]`，
httpx 解析时把 `[::1]` 当 URL、把 `:1]` 当端口，于是 **创建客户端就抛**
`InvalidURL: Invalid port: ':1]'`，整个 `pp swebench` 还没开始跑就崩了。

这里的用例同时验证"清理函数正确"和"真的能救回客户端"——后者才是事故的直接症状。
"""

from __future__ import annotations

import os

import pytest

from patchpilot.core.netenv import sanitize_no_proxy


@pytest.fixture(autouse=True)
def _restore_env():
    before = {name: os.environ.get(name) for name in ("NO_PROXY", "no_proxy")}
    yield
    for name, value in before.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def test_removes_bracketed_ipv6_entries() -> None:
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1,[::1]"
    os.environ["no_proxy"] = "127.0.0.1,localhost,::1,[::1]"

    removed = sanitize_no_proxy()

    assert "[::1]" in removed
    assert "[" not in os.environ["NO_PROXY"]
    assert "[" not in os.environ["no_proxy"]
    assert os.environ["NO_PROXY"] == "127.0.0.1,localhost,::1", "可解析的写法要保留"


def test_is_idempotent_and_harmless_without_proxy() -> None:
    os.environ.pop("NO_PROXY", None)
    os.environ.pop("no_proxy", None)

    assert sanitize_no_proxy() == []
    assert "NO_PROXY" not in os.environ


def test_drops_variable_when_every_entry_is_bracketed() -> None:
    os.environ["NO_PROXY"] = "[::1],[::2]"
    os.environ["no_proxy"] = "[::1],[::2]"

    sanitize_no_proxy()

    assert os.environ.get("NO_PROXY") is None, "全被清掉时应删除变量而不是留空串"


def test_keeps_plain_ipv6_and_hosts() -> None:
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1,*.internal"
    os.environ["no_proxy"] = "127.0.0.1,localhost,::1,*.internal"

    removed = sanitize_no_proxy()

    assert removed == []
    assert os.environ["NO_PROXY"] == "127.0.0.1,localhost,::1,*.internal"


def test_httpx_client_can_be_created_after_sanitizing() -> None:
    """**事故的直接症状**：清理前构造客户端抛 InvalidURL，清理后正常。"""
    httpx = pytest.importorskip("httpx")
    os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7897"
    os.environ["HTTP_PROXY"] = "http://127.0.0.1:7897"
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,::1,[::1]"
    os.environ["no_proxy"] = "127.0.0.1,localhost,::1,[::1]"

    with pytest.raises(Exception) as broken:
        httpx.Client(timeout=0.5).close()
    assert "port" in str(broken.value).lower()

    sanitize_no_proxy()

    httpx.Client(timeout=0.5).close()
