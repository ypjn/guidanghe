"""
归档盒 — 独立文件清单工具（验收专用，独立于生产扫描器）

用途：对任意目录从文件系统独立枚举实际文件，建立
      相对路径 → [大小, SHA-256] 映射，用于整理/撤销/取消/关闭
      测试后的完整性核验。不复用 ScanResult、OrganizePlan 或数据库
      动作列表作为枚举来源——那些只能核对“当初登记过的路径”，
      无法发现新增文件或归档残留文件。

枚举边界（显式列出，不得隐式扩大）：
1. 以 os.walk(root, topdown=True, followlinks=False) 从文件系统独立枚举。
2. 唯一排除项：{root}/.organizer（程序自身运行记录：
   history.db、protect_config.json）。用户文件一律纳入，
   不通过扩大排除范围隐藏差异。
3. 符号链接（无论指向文件还是目录）一律不跟随：
   记录为 ["link", 链接目标文本]；指向目录的链接不会枚举其目标内容，
   因此不会越界到根目录之外。
4. 只记录文件与链接，不记录目录本身；空目录不计入清单
   （撤销后允许残留空父目录，不把空目录判为文件丢失或多余）。
5. 覆盖 _已整理 内的文件（无论执行前已有还是撤销后意外残留）。
6. 目录无法进入（如权限拒绝）、getsize / readlink / open / SHA-256 计算
   失败时记入 errors，绝不静默忽略；调用方必须把非空 errors 判为核验失败。

运行：无需 Qt，纯标准库。
"""

from __future__ import annotations

import hashlib
import os
import time
from collections import Counter

# 唯一排除的目录名（相对根目录第一层）
EXCLUDED_DIR_NAMES = {".organizer"}


def sha256_file_fast(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def fs_manifest(root, progress_every=2000):
    """从文件系统独立枚举 root，返回 (manifest, errors, elapsed)。

    manifest: {相对路径: [大小, sha256]}；符号链接为 ["link", 目标]。
    errors:   [(相对路径, 错误信息)]，非空即核验失败。
    """
    root = os.path.abspath(root)
    manifest: dict = {}
    errors: list = []
    n = 0
    t0 = time.time()

    def _on_walk_error(err: OSError):
        # os.walk 默认静默跳过无法进入的目录（如权限拒绝），
        # 这里必须显式记录，否则该目录内的全部文件都会从清单里消失。
        try:
            rel = os.path.relpath(err.filename, root)
        except Exception:
            rel = err.filename
        errors.append((rel, f"目录无法访问: {type(err).__name__}: {err}"))

    for dirpath, dirnames, filenames in os.walk(root, topdown=True,
                                                 followlinks=False,
                                                 onerror=_on_walk_error):
        rel_dir = os.path.relpath(dirpath, root)

        # 排除程序自身运行记录目录（唯一排除项）
        if rel_dir.split(os.sep)[0] in EXCLUDED_DIR_NAMES:
            dirnames[:] = []
            continue

        # 指向目录的符号链接：记录为链接，不下降
        for d in list(dirnames):
            full = os.path.join(dirpath, d)
            if os.path.islink(full):
                rel = os.path.relpath(full, root)
                try:
                    manifest[rel] = ["link", os.readlink(full)]
                except OSError as e:
                    errors.append((rel, f"readlink 失败: {e}"))
                dirnames.remove(d)

        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            n += 1
            try:
                if os.path.islink(full):
                    manifest[rel] = ["link", os.readlink(full)]
                    continue
                size = os.path.getsize(full)
                sha = sha256_file_fast(full)
            except OSError as e:
                errors.append((rel, f"{type(e).__name__}: {e}"))
                continue
            manifest[rel] = [size, sha]
        if progress_every and n and n % progress_every == 0:
            print(f"        清单进度：已枚举 {n} 个文件 …")
    return manifest, errors, time.time() - t0


def multiset(manifest):
    """内容指纹多重集合：文件按 (大小, sha)，链接按 ("link", 目标)。

    重复副本必须计数：同一指纹出现 k 次计 k。
    """
    c = Counter()
    for v in manifest.values():
        if not v:
            continue
        if v[0] == "link":
            c[("link", v[1])] += 1
        else:
            c[(v[0], v[1])] += 1
    return c


def dup_stats(manifest):
    """重复统计（仅真实文件，不含链接）。

    返回 (重复组数, 重复组内文件份数)：
    - 重复组数：出现次数 > 1 的内容指纹个数（组数）；
    - 组内文件份数：这些组包含的文件总数（份数）。
    例：3 个相同文件构成 1 组、3 份。
    """
    c = Counter()
    for v in manifest.values():
        if v and v[0] != "link":
            c[(v[0], v[1])] += 1
    groups = sum(1 for k in c if c[k] > 1)
    copies = sum(c[k] for k in c if c[k] > 1)
    return groups, copies


def compare_manifests(before, after, errs_before=(), errs_after=()):
    """比较两份独立清单，返回差异统计。

    ok 为 True 当且仅当：无缺失、无多余、无内容变化、
    内容指纹多重集合一致、两侧 errors 均为空。
    """
    keys_b, keys_a = set(before), set(after)
    missing = sorted(keys_b - keys_a)
    extra = sorted(keys_a - keys_b)
    changed = sorted(k for k in (keys_b & keys_a) if before[k] != after[k])
    ms_b, ms_a = multiset(before), multiset(after)
    g_b, f_b = dup_stats(before)
    g_a, f_a = dup_stats(after)
    errs = list(errs_before) + list(errs_after)
    ok = (not missing and not extra and not changed
          and ms_b == ms_a and not errs)
    return {
        "missing": missing[:10], "extra": extra[:10], "changed": changed[:10],
        "missing_n": len(missing), "extra_n": len(extra),
        "changed_n": len(changed),
        "multiset_equal": ms_b == ms_a,
        "dup_groups_before": g_b, "dup_files_before": f_b,
        "dup_groups_after": g_a, "dup_files_after": f_a,
        "errors": [f"{p}: {msg}" for p, msg in errs][:10],
        "errors_n": len(errs),
        "ok": ok,
    }
