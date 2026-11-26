# SARIF CI 消费指南（G17 出圈）

> 构想v10 G17：SARIF 2.1.0 已产（`vrp report`），本文档是**外部项目 CI 消费指南**——
> vrp 作为工具支持别人的 CI；本仓自身不做 CI（零服务端红线不动）。
> 判定：**绕**（不造新代码，跨仓 dogfood 一次验证即可——2026-11-18 完成，见下"实测"节）。

## 产链三步

```bash
# 1. 装好 vrp（本仓 python -m vrp.cli 直跑；外部项目按其发布形态引入）
# 2. 全链：S0→S1→S1.5→S2→S3→S6 出 facts.db
python -m vrp.cli run <repo>
# 3. 静态报告：.vrp/report/findings.sarif（SARIF 2.1.0）
python -m vrp.cli report <repo>
```

产物：`<repo>/.vrp/report/findings.sarif`（另有 report.html / IR / Mermaid，非 CI 消费面）。

## GitHub Actions 消费样例

学 `github/codeql-action/upload-sarif` 官方形态（CodeQL 是 SARIF 产者标杆——
结果质量与规则元数据形态的参照，不引入）：

```yaml
name: vrp
on: [push]
jobs:
  vrp:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - run: python -m vrp.cli run .          # 全链出 facts.db
      - run: python -m vrp.cli report .       # 出 findings.sarif
      - uses: github/codeql-action/upload-sarif@v3
        with:
          sarif_file: .vrp/report/findings.sarif
```

上传后结果出现在仓库 **Security → Code scanning alerts**，逐条可下钻到文件行。

## 字段面对照（GitHub 消费 ↔ vrp 产出）

| GitHub code scanning 消费字段 | vrp SARIF 产出 |
|---|---|
| `version` 必须为 `2.1.0` | `2.1.0`（`$schema` 钉 schemastore） |
| `runs[0].tool.driver.name` | `vrp` |
| `runs[0].tool.driver.informationUri` | 本仓 GitHub 地址 |
| `runs[0].tool.driver.version` | vrp 版本号 |
| `results[].ruleId` | 病理码（P1/P2/…，`render.PATHOLOGY_LEGEND` 单源） |
| `results[].level` | `warning`（按病理分级映射） |
| `results[].message.text` | 人读病理描述 |
| `results[].locations[0].physicalLocation.artifactLocation.uri` | **仓库根相对路径**（GitHub 解析要求相对根） |
| `results[].locations[0].region.startLine` | 锚定行 |

契约面已被 tests/test_render.py 既有测试锁死（`test_sarif` /
`test_sarif_truncation_annotated` / `test_build_sarif_information_uri_points_to_real_repo`）。

## 两层截断的诚实链

1. **vrp 产面 cap**：`SARIF_FINDINGS_CAP = 2000` 条/文件——超限截断并在
   `runs[0].properties`（SARIF propertyBag 标准字段）标注
   `{"findings_total": N, "results_cap": 2000}`——**截断可见，总数不撒谎**。
2. **GitHub 消费面上限**：每次上传最多 5000 条 results（GitHub 官方限制）。
   vrp 的 2000 cap 天然低于该线；`findings_total` 让"报告之外的存量"可审计。

## 跨仓 dogfood 实测（2026-11-18）

| 仓 | findings | SARIF results | 文件大小 | 路径 |
|---|---|---|---|---|
| `verify/code-atlas-lusiem`（TS 小仓） | 1064 | 1064（无截断） | 507 KB | 契约面全过 |
| `verify/tree-sitter-analyzer`（Python 大仓） | 15741 | 2000（cap 截断+propertyBag 标注 15741） | 979 KB | 契约面全过 |

两仓均：`version=2.1.0`、每条 result 带 ruleId/level/message/uri（相对根）/startLine，
消费契约断言全绿。lusiem 仓 archify 布局验证失败时**分层概览诚实降级**（SARIF 面不受影响）。

## 通用 CI（GitLab / 其他）

SARIF 是 OASIS 标准，任何能上传 Artifact 的 CI 都能消费：把
`.vrp/report/findings.sarif` 作为构建产物归档即可人工下载/下游解析；
GitLab 官方 SARIF 消费（`SAST` 报告解析）按其文档接入，形态同理，不在此展开。

## 边界（诚实声明）

- vrp **不替你跑 CI、不上传任何东西**（零服务端红线）；上传动作与 token 归消费方。
- `vrp report` 是静态产物面：同一 facts.db 重跑字节级同 SARIF（无时间戳）——
  CI 缓存与增量消费友好。
- 病理分级（level）当前统一 `warning`；error 级映射（如 P0 阻断类）待真实
  消费反馈后再定，不预先造。
