# 搜索修复：先于外置盘功能完成

在交接前代码基线 `7e77469` 上，Mac已有搜索4项、内容6项自动用例通过，但以下独立反例仍成立。源码入口是 `归档盒源码/search_session.py:run_lookup()` 和 `search_dialog.py:_next_page()`。

| 反例 | 正常预期 | 基线实测 |
| --- | --- | --- |
| 75份都匹配 alpha beta，每页50，offset 0/50/100 | 50/25/0，页间不重复 | 50/50/50，三页ID集合相同 |
| alpha匹配80份，beta仅匹配alpha排序最后一份 | 交集1份 | 返回0份 |
| gamma61份，前60份draft需排除，最后1份keep | 第一页直接返回1份keep | 第一页空，offset50才有keep，界面空页不能继续翻 |

根因方向：多词分支先截每词前50条再求交，并忽略offset；排除条件在分页之后处理。先应用完整包含/排除/范围条件，按文件聚合、稳定排序，再统一分页。单纯提高上限无法修复完整性。

## 便携复现

激活已核对的Windows项目解释器，在仓库根目录执行：

```powershell
python docs/product-development/windows-handoff/2026-11-22/reproduce_search.py
```

脚本自动定位当前仓库源码，全部样本、数据库和JSON输出位于系统临时目录，默认**按正常行为判断**：修复前返回1，三个问题修复后应返回0。

只想核验交接前旧缺陷是否仍全部存在时：

```powershell
python docs/product-development/windows-handoff/2026-11-22/reproduce_search.py --expect-known-bugs
```

该模式返回0仅代表旧缺陷全部复现，不能作为修复成功证据。`mac-baseline-results.json` 是这一模式在Mac的实测，绝对本机路径已去除。不要在修复后继续把诊断模式的0当成正常通过。

## 正式回归还要覆盖

- 多个词分散在文件名、路径和不同正文块。
- 同一文件命中多个块与文件名，按文件聚合后分页。
- 无关键词仅空间/类型/日期筛选的翻页。
- 排除条件匹配的文件不能消耗有效结果的页容量。
- 最后一页、空页、快速查询与迟到结果。
- 修改索引/空间权限后旧结果失效。
- 查找前后来源不变，整理仍独立确认。

使用现有 `test_product_search.py`、`test_product_content.py` 等正确名称，不为迎合旧计划重命名。把新的正常预期写入正式回归，并保留修复前后结果。
