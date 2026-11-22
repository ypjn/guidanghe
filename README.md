# 归档盒

本地桌面文件整理工具，当前版本 **V1.8.1（1.8.1）**。用 Python、PySide6 和 SQLite。默认离线：不调用付费接口，不上传文件正文，也不做云盘。

选一个文件夹后，可以预览整理方案，确认后才移动文件。每次移动都有历史，可以撤销。模型和建议只生成方案，文件变更仍然走预览、确认、执行和撤销。

## 现在能做什么

- 按课程、类型或学期规则整理文件，并撤销到原来的位置
- 从改单记录里提出学习建议；确认后才写成偏好
- 用一句话查找课程材料；整理和查找分开
- 登记多个文件夹，做本地索引和增量更新
- 在已登记的空间里按文件名和正文搜索，包括 PDF 里可复制的文字和中文
- 受保护的项目可以只读纳入索引，但不会因此被移动

扫描版 PDF 不渲染、也不做文字识别。语义检索还没有做，搜索窗口里写了这一点。

## 运行

需要 Python 3.14，以及 `归档盒源码/requirements.txt` 里的依赖。在 macOS 上还需要系统自带的 Vision（通过 `pyobjc`）。仓库里可以没有 `.venv`。

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r 归档盒源码/requirements.txt
cd 归档盒源码
../.venv/bin/python main.py
```

界面是中文的。先选择要整理的文件夹，再生成预览。

## 数据放在哪里

| 内容 | 位置 |
| --- | --- |
| 方案库、全局偏好 | macOS：`~/Library/Application Support/归档盒` |
| 启动日志 | macOS：`~/Library/Logs/归档盒` |
| 某个文件夹的整理历史、偏好、撤销 | 该文件夹下的 `.organizer` |
| 多空间索引和正文片段 | 应用数据目录里的 `index/index.db` |

这些都不写进 `.app`、DMG 或源码包。索引可以重建。整理历史、偏好和撤销记录在升级时保留。

测试时可以用 `ARCHIVEBOX_APP_DIR`、`ARCHIVEBOX_LOG_DIR`、`ARCHIVEBOX_INDEX_DB` 把上述路径指到临时目录。

## 测试

在 `归档盒源码` 目录下执行。界面测试使用离屏 Qt。

```sh
QT_QPA_PLATFORM=offscreen ../.venv/bin/python -m unittest discover -s tests -p 'test_product_*.py' -v
```

更早的阶段测试仍在 `tests/` 里，文件名带阶段或日期。

## 打包

macOS（Apple 芯片）的 `.app` 和只读 DMG 由 `打包/build_mac.sh` 生成。解释器通过 `ARCHIVEBOX_BUILD_PYTHON` 传入，脚本不写死某台电脑的路径。步骤、依赖和验收命令见 [打包/构建说明.md](打包/构建说明.md)。

请使用本仓库最新提交按 打包/构建说明.md 重新构建 DMG；旧的第 01 批安装包不含搜索修复与第05–06批。Apple 开发者签名和公证还没做。

## 目录

- `归档盒源码/`：程序和测试
- `打包/`：macOS 图标、构建脚本和验收脚本
- `docs/product-development/`：桌面产品的进度和索引说明
- `课程交付/`、`归档盒-项目设计/`：课程材料和历史设计，产品开发不改这些记录

## 还没做

Apple 开发者签名/公证，以及 Windows 真盘拔插与原生窗口验收。同步、NAS 和手机不在这一版里。
