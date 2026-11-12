# 归档盒 V1.7 — Windows 阶段交付物（stage2 ~ stage7）

本目录收录归档盒 V1.7 在 Windows 平台适配阶段的交付物。

## stage7（散文件，可直接浏览）

`windows_stage7_deliver_20261110_214635/`

- `STAGE7_W1_REPORT.md`：W1 已有功能验收报告（功能覆盖表、逐项结论、剩余项、Mac 复验清单）
- `REBUILD.md`：完整重建与复跑步骤
- `patches/windows_stage7_production_fixes.patch`：2 处生产缺陷的可应用补丁
- `evidence/`：父套件结果、6 个子进程日志与退出码、截图、导出文件
- `EVIDENCE_INDEX.json`：236 个交付文件的 SHA-256 索引（二次读盘复核）
- `归档盒源码/`：最终源码（含新增 `tests/windows_stage7_w1.py` 与 6 个 `tests/stage7_children/` 子套件）

## stage2 ~ stage6b（zip 归档，内容完整）

| 压缩包 | 内容 |
| --- | --- |
| `archivebox_v17_baseline_20261110.zip` | 适配前基线源码快照 |
| `archivebox_v17_windows_adapt_20261110.zip` | Windows 适配工作副本（已排除 .venv / __pycache__） |
| `windows_stage2_deliver_20261110_153858.zip` | stage2 交付物 |
| `windows_stage2_evidence.zip` | stage2 原始证据 |
| `windows_stage2_snapshot_20261110.zip` | stage2 源码快照 |
| `windows_stage3_deliver_20261110_165750.zip` | stage3 交付物（首轮） |
| `windows_stage3_deliver_20261110_165909.zip` | stage3 交付物（复跑） |
| `windows_stage4_deliver_20261110_172314.zip` | stage4 交付物（首轮） |
| `windows_stage4_deliver_20261110_172431.zip` | stage4 交付物（复跑） |
| `windows_stage5_deliver_20261110_191543.zip` | stage5 交付物 |
| `windows_stage6_deliver_20261110_195710.zip` | stage6 交付物 |
| `windows_stage6b_deliver_20261110_202243.zip` | stage6b（顾问独立验收通过的候选基线） |
| `windows_w1_evidence.zip` | W1 早期证据 |
| `windows_stage2_snapshot_20261110_manifest.txt` | stage2 快照清单 |

## stage7 结论摘要

- 父套件 `tests/windows_stage7_w1.py`：20 PASS / 0 FAIL / 0 BLOCK / 0 UNTESTED，真实退出码 0。
- 6 个子进程退出码全为 0，子断言合计 147 条（断言数不等于功能覆盖数）。
- 修复 2 个 W1 阻断级生产缺陷：手动修正入口不可达、行内复选框无法勾选。
- 未测项：Windows 原生拖放、重复核对「打开/定位」真实外部程序唤起、跨盘/链接/长路径/DPI/规模、Mac 端复验。

说明：stage2~stage6b 以 zip 归档，是因为本机环境会把 `.git` 元数据实时移入回收站，无法通过本地 git 提交；
归档内容与原始交付目录逐字节一致（打包时仅排除 `.venv`、`__pycache__`、`*.pyc`）。
