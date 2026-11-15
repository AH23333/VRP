# -*- coding: utf-8 -*-
"""报告链公共主题令牌层（V-2，构想v9 §10）。

单一权威源：archify 预设的 21 个设计令牌抽取自 render_chain chain/ 页
CSS（#020617 底/#1e293b 边/#22d3ee accent/ui-monospace 标题/胶囊圆角）。
chain 页 CSS 本身即参考实现（历史硬编码色不回改——127 处，重构风险大于
收益）；fusion（render_fusion）三主题与后续报告链主题化均从本模块取值，
测试锁"archify 令牌值 == chain 页实际用色"作同源验证。

复现性：令牌值与序均静态定义，同 facts.db → 字节级同报告不受影响。
"""
from __future__ import annotations

# 令牌序（确定性 CSS 生成用）
TOKEN_ORDER: tuple[str, ...] = (
    "--bg", "--panel", "--card", "--border", "--txt", "--muted",
    "--accent", "--accent-ink", "--code-fg", "--code-bg",
    "--chip-bg", "--chip-bd", "--chip-hover",
    "--tdb", "--hover", "--header", "--h3", "--axline", "--split",
    "--h1font", "--bar-ink",
)

THEMES: dict[str, dict[str, str]] = {
    # archify（默认）：与 chain/ 细节层视觉一致——总览/细节跨层体验统一
    "archify": {
        "--bg": "#020617",
        "--panel": "#0f172a",
        "--card": "#0b1226",
        "--border": "#1e293b",
        "--txt": "#e2e8f0",
        "--muted": "#94a3b8",
        "--accent": "#22d3ee",
        "--accent-ink": "#04121f",
        "--code-fg": "#7dd3fc",
        "--code-bg": "#0c1a2e",
        "--chip-bg": "#0c2233",
        "--chip-bd": "#164e63",
        "--chip-hover": "#083344",
        "--tdb": "#111c2e",
        "--hover": "#0e1730",
        "--header": "rgba(2,6,23,.92)",
        "--h3": "#7dd3fc",
        "--axline": "#334155",
        "--split": "#141d31",
        "--h1font": "ui-monospace,'JetBrains Mono',Consolas,monospace",
        "--bar-ink": "#020617",
    },
    # 纸面：浅色（打印/明环境）
    "paper": {
        "--bg": "#f8fafc",
        "--panel": "#f1f5f9",
        "--card": "#ffffff",
        "--border": "#e2e8f0",
        "--txt": "#0f172a",
        "--muted": "#64748b",
        "--accent": "#0891b2",
        "--accent-ink": "#ffffff",
        "--code-fg": "#0e7490",
        "--code-bg": "#e0f2fe",
        "--chip-bg": "#ecfeff",
        "--chip-bd": "#a5f3fc",
        "--chip-hover": "#cffafe",
        "--tdb": "#e9eef5",
        "--hover": "#f1f5f9",
        "--header": "rgba(248,250,252,.94)",
        "--h3": "#0369a1",
        "--axline": "#cbd5e1",
        "--split": "#e9eef5",
        "--h1font": "system-ui,-apple-system,'Segoe UI',sans-serif",
        "--bar-ink": "#ffffff",
    },
    # ember：琥珀暗色（archify warn 色系）
    "ember": {
        "--bg": "#0f0b08",
        "--panel": "#1a1410",
        "--card": "#16110c",
        "--border": "#33291f",
        "--txt": "#e7e5e4",
        "--muted": "#a8a29e",
        "--accent": "#fb923c",
        "--accent-ink": "#1c0f05",
        "--code-fg": "#fdba74",
        "--code-bg": "#241a10",
        "--chip-bg": "#2a1d10",
        "--chip-bd": "#5a3a1a",
        "--chip-hover": "#3a2814",
        "--tdb": "#241c14",
        "--hover": "#241a12",
        "--header": "rgba(15,11,8,.92)",
        "--h3": "#fdba74",
        "--axline": "#44403c",
        "--split": "#241c14",
        "--h1font": "ui-monospace,'JetBrains Mono',Consolas,monospace",
        "--bar-ink": "#020617",
    },
}

DEFAULT_THEME = "archify"


def theme_root_css() -> str:
    """三主题 CSS 变量块（:root=默认主题 + [data-theme] 预设）。

    确定性：TOKEN_ORDER 静态序 → 同输出字节。
    """
    parts: list[str] = []
    for name, tokens in THEMES.items():
        body = ";".join(f"{k}:{tokens[k]}" for k in TOKEN_ORDER)
        sel = ":root" if name == DEFAULT_THEME else f'[data-theme="{name}"]'
        parts.append(f"{sel}{{{body}}}")
    return "".join(parts)
