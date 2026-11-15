ECharts 5.6.0（Apache License 2.0）官方 dist 最小化构建，vendored 供 fusion 报告内联。

- 来源：https://cdn.jsdelivr.net/npm/echarts@5.6.0/dist/echarts.min.js
- sha256：e84270bd0cd5bdf60fefc26d00c2a391cb2e81f4d26a7a9ee16185a54773a3cf
- 大小：1,030,855 字节
- 用途：vrp/render_fusion.py 读取后内联进 fusion.html（单 HTML 静态产物红线——
  不外链 CDN、无构建链）
- 升级流程：下载新版 → 覆盖本文件 → 更新本 README 的版本/sha256/大小 →
  probe_v92 布局验证器重跑（不同版本布局引擎可能影响渲染，不影响嵌入数据）

构想v9 §3 技术底座：ECharts 单库收敛（pyvis 退役）；§6 复现性：vendored 钉版本
保证跨环境字节级同报告。
