# Mac DMG 交接（1.8.1）

本云端环境是 Linux，无法调用 `hdiutil` / `codesign`，因此正式 `.dmg` 需在 Apple 芯片 Mac 上生成。

## 已在本分支验证

- 搜索三份反例：`reproduce_search.py` 正常模式退出码 0
- 备份恢复：`test_product_backup.py` 11/11
- 卷身份 / schema 2→3：`test_product_volume.py` 5/5
- 发布验收脚本：`打包/verify_release_features.py` 退出码 0（见 verify_release_features.json）
- 源码发布包：`打包/dist/归档盒-1.8.1-src-with-packaging.tar.gz`
- SHA-256：`196b021b7aaf809b3e9dc940ca90c5c8464febbc23cd8407274e92d81261b9da`
- 分支 tip：`62f0e30eb2bfc2d41eb44589cca1ec48261588ea`

## 在 Mac 上生成 DMG

```sh
git fetch origin
git checkout cursor/batches-05-07-7481
export ARCHIVEBOX_BUILD_PYTHON="/path/to/python-with-PySide6-PyInstaller"
zsh 打包/build_mac.sh
zsh 打包/accept_mac_app.sh 打包/dist/归档盒-1.8.1-macos-arm64.dmg docs/product-development/evidence/batch07
```

或解压源码发布包后执行同一组命令。验收应覆盖：搜索翻页、备份两版恢复、旧索引升级后可查。
