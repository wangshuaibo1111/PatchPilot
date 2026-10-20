"""统一的文本写入：**永远写 LF**，不做平台换行翻译。

## 为什么需要这个模块

Python 的 `Path.write_text` / `open(..., "w")` 在 `newline=None` 下会把 `\\n`
翻译成 `os.linesep`——在 Windows 上写成 `\\r\\n`。对"代码修复"这类工具，这是**静默错误**：

  - 物化评测语料 → 语料变 CRLF，而模型产出的补丁是 LF → `git apply` 找不到上下文行；
  - 把补丁落盘成 `.diff` → 用户拿去 `git apply` 时上下文带 CR，打不上；
  - `fs` 工具写文件 → 调用方给的 `\\n` 被悄悄改成 CRLF，字节与预期不符。

第二十九轮的真机 bug 正是这一族问题的极端形态：`subprocess` 的 `input=str` + text 模式
把补丁的换行翻成 CRLF，导致"合法补丁打不上 LF 仓库、新建文件静默变 CRLF"。

因此：**所有会参与补丁/内容比对的写入都走这里**，显式 `encode("utf-8")`。
"""

from __future__ import annotations

from pathlib import Path


def write_text_lf(path: Path, text: str) -> int:
    """把 `text` 以 **UTF-8 / LF** 写入 `path`，返回写入字节数。

    会先建父目录。刻意**不**做任何换行规范化：调用方给什么就写什么
    （含 `\\r\\n` 的文本会原样保留——需要规范化的地方由调用方显式做）。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    path.write_bytes(data)
    return len(data)


__all__ = ["write_text_lf"]
