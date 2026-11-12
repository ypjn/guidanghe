"""生产补丁生成与副本验证（不修改冻结基线）。

流程（每一对 基线→最终 文件都走一遍）：
  1. 记录冻结基线文件 SHA-256；
  2. 从基线 + 最终源码生成 unified diff（统一 a/ b/ 前缀，行尾显式单换行）；
  3. 校验补丁文本每行都合法（杜绝上一轮 P01 第 5 行那种损坏）；
  4. 复制基线到本轮唯一副本目录，`git apply --check` 检查可应用；
  5. 实际 `git apply` 应用，比较应用结果与最终源码逐字节一致；
  6. 复检冻结基线文件哈希未变。
所有命令、cwd、输出、退出码写入 structured results。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import subprocess
import sys

TESTS = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(TESTS)
sys.path.insert(0, SRC)
sys.path.insert(0, TESTS)

from windows_harness import Results, RunDir  # noqa: E402

BASE = os.environ.get("ARCHIVEBOX_TEST_BASE",
                      os.path.join(os.environ.get("TEMP", "/tmp"), "_abx_patch"))
OUT = os.environ.get("ARCHIVEBOX_RESULTS_DIR", os.path.join(BASE, "results"))
os.makedirs(OUT, exist_ok=True)

BASELINE = os.environ.get(
    "ARCHIVEBOX_BASELINE_SRC",
    os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(SRC))),
                 r"archivebox_v17_baseline_20261110"
                 r"\ArchiveBox_V1.7_Windows_Handoff_20261110\归档盒源码"))

rd = RunDir(BASE, "patch")
PATCHES = rd.sub("patches")
VERIFY = rd.sub("verify")
R = Results("patch_verify")
print(f"=== patch_verify run={rd.run_id} ===")
print(f"BASELINE={BASELINE}")


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def read(p):
    with open(p, "r", encoding="utf-8", newline="") as f:
        return f.read()


def make_patch(name: str, rel: str) -> dict:
    base_f = os.path.join(BASELINE, rel)
    final_f = os.path.join(SRC, rel)
    info = {"name": name, "file": rel, "baseline_sha_before": sha(base_f),
            "final_sha": sha(final_f)}
    a = read(base_f).splitlines()
    b = read(final_f).splitlines()
    # 按目标文件的换行风格生成补丁，避免应用后整文件换行被改写
    raw_final = open(final_f, "rb").read()
    eol = "\r\n" if b"\r\n" in raw_final else "\n"
    diff = list(difflib.unified_diff(
        a, b, fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="", n=3))
    text = eol.join(diff) + eol
    path = os.path.join(PATCHES, name)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    info["eol"] = repr(eol)
    info["patch"] = path
    info["text"] = text
    info["bytes"] = len(text.encode("utf-8"))
    info["hunks"] = sum(1 for ln in diff if ln.startswith("@@"))

    # 行合法性：只允许上下文/新增/删除/块头/文件头/diff 头
    ok_lines = []
    for ln in text.splitlines():
        ok_lines.append(ln == "" or ln[0] in " +-@" or
                        ln.startswith("diff ") or ln.startswith("\\ No newline"))
    info["lines_valid"] = all(ok_lines)
    info["bad_lines"] = [ln for ln, ok in zip(text.splitlines(), ok_lines) if not ok]

    # 副本：只放被补丁涉及的文件，模拟真实基线
    dst = os.path.join(VERIFY, name.replace(".patch", ""))
    os.makedirs(dst, exist_ok=True)
    shutil.copy2(base_f, os.path.join(dst, rel))

    def git(*args):
        # 显式关闭本机 git 的自动换行转换（否则应用后整文件被改成 CRLF）
        cmd = ["git", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false"] + list(args)
        p = subprocess.run(cmd, cwd=dst, capture_output=True,
                           text=True, timeout=120)
        return {"cmd": cmd, "cwd": dst,
                "exit": p.returncode, "stdout": p.stdout, "stderr": p.stderr}

    info["apply_check"] = git("apply", "--check", "--verbose", path)
    info["apply"] = git("apply", "--verbose", path)
    applied = os.path.join(dst, rel)
    info["applied_sha"] = sha(applied) if os.path.isfile(applied) else None
    info["matches_final"] = info["applied_sha"] == info["final_sha"]
    info["baseline_sha_after"] = sha(base_f)
    info["baseline_untouched"] = (info["baseline_sha_before"]
                                  == info["baseline_sha_after"])
    # 副本文件的补丁后再 diff 应为空
    info["residual_diff"] = list(difflib.unified_diff(
        read(applied).splitlines(), read(final_f).splitlines(),
        fromfile="applied", tofile="final", lineterm="", n=1))
    return info


CASES = [("P01_protect_config_expanduser.patch", "protect_config.py"),
         ("P02_main_interaction_fixes.patch", "main.py")]

report = []
for name, rel in CASES:
    print(f"\n--- {name} ---")
    try:
        info = make_patch(name, rel)
    except Exception as e:  # noqa: BLE001
        R.check(name + "_gen", "补丁生成成功", False, f"{type(e).__name__}: {e}")
        continue
    report.append({k: v for k, v in info.items() if k != "text"})
    print(f"  bytes={info['bytes']} hunks={info['hunks']} "
          f"lines_valid={info['lines_valid']}")
    print(f"  apply --check exit={info['apply_check']['exit']} "
          f"apply exit={info['apply']['exit']} matches_final={info['matches_final']}")
    tag = os.path.splitext(name)[0]
    R.check(tag + "_diff", "补丁非空且每行合法（无上一轮那种损坏行）",
            info["bytes"] > 0 and info["lines_valid"] and info["hunks"] >= 1,
            f"bytes={info['bytes']} hunks={info['hunks']} bad={info['bad_lines'][:3]}")
    R.check(tag + "_check", "在独立基线副本上 git apply --check 通过",
            info["apply_check"]["exit"] == 0,
            f"exit={info['apply_check']['exit']} err={info['apply_check']['stderr'][-300:]}")
    R.check(tag + "_apply", "在独立基线副本上实际 git apply 通过",
            info["apply"]["exit"] == 0,
            f"exit={info['apply']['exit']} err={info['apply']['stderr'][-300:]}")
    R.check(tag + "_equal", "应用结果与最终源码逐字节一致（SHA-256 相同、残余 diff 为空）",
            info["matches_final"] and not info["residual_diff"],
            f"applied={info['applied_sha']} final={info['final_sha']}")
    R.check(tag + "_frozen", "冻结基线原文未被修改",
            info["baseline_untouched"],
            f"{info['baseline_sha_before']} -> {info['baseline_sha_after']}")

with open(os.path.join(OUT, "patch_verify.json"), "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)

ids = []
for name, _ in CASES:
    tag = os.path.splitext(name)[0]
    ids += [tag + s for s in ("_diff", "_check", "_apply", "_equal", "_frozen")]
sys.exit(R.finalize(os.path.join(OUT, "results.json"), required_ids=ids))
