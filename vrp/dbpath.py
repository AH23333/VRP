"""sqlite 只读连接共享助手（R-S 批）。

SQLite file: URI 语法给三个字符赋予结构含义：'?'（query 分隔符）、
'#'（fragment 起点，之后整段被忽略）、'%'（百分号转义，%XX 解码）。
裸 f-string 内插路径时，仓库路径含这些字符即触发（Windows 目录名合法
字符；'#' 常见于 C#/C++ 目录名）：

- '#' 截断 URI → 静默打开错误/空库（"no such table"）——最坏失败类，
  数据错误而非报错；
- '%XX' 序列被解码成别的字符 → 打不开或打开错库；
- '?' 截断成 query（POSIX 路径面）。

统一 percent-encode 后构造 URI，三类字符免疫；mode=ro 防误写；
uri=True 封装在助手内——调用方不可能忘（忘则 URI 字符串被当字面
文件名打开）。
"""

from __future__ import annotations

import sqlite3
import urllib.parse
from pathlib import Path


def ro_connect(path: Path | str) -> sqlite3.Connection:
    """mode=ro 只读连接：路径 percent-encode（'#'/'%'/'?' 免疫）。

    等价于旧形态 ``sqlite3.connect(f"file:{path}?mode=ro", uri=True)``
    在干净路径上的行为；含特殊字符路径上前者错库/报错、本助手正确。
    row_factory 等连接属性由调用方自行设置。
    """
    posix = Path(path).as_posix()
    uri = "file:" + urllib.parse.quote(posix, safe="/:") + "?mode=ro"
    return sqlite3.connect(uri, uri=True)
