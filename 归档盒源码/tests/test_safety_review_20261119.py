"""归档盒 V1.7 安全复核（2026-11-19）回归测试。

覆盖复核说明中的 01-12 项；全部使用临时目录与临时历史库，
不接触真实资料、不依赖 PySide6。

运行：
    python tests/test_safety_review_20261119.py
退出码：0 = 无 FAIL；1 = 有 FAIL。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

import ai_fallback                                          # noqa: E402
import classification_config as cc                          # noqa: E402
import config_transfer                                      # noqa: E402
import executor                                             # noqa: E402
import intake_model                                         # noqa: E402
import planner                                              # noqa: E402
import protect_config                                      # noqa: E402
import scanner                                              # noqa: E402
import storage as storage_mod                               # noqa: E402
from planner import (                                        # noqa: E402
    DEFAULT_COURSE_RULES, MODE_BY_DIRECTION_SEMESTER, MODE_BY_TYPE,
    build_plan, generate_plan,
)
from storage import Storage                                  # noqa: E402

RESULTS: list = []
WORK = tempfile.mkdtemp(prefix="safety_review_")


def rec(tid: str, title: str, status: str, evidence: str = ""):
    RESULTS.append({"id": tid, "title": title, "status": status,
                    "evidence": evidence})
    print(f"[{status}] {tid} {title}")
    for line in str(evidence).splitlines():
        print(f"        {line}")


def ok(tid: str, title: str, cond: bool, evidence: str = ""):
    rec(tid, title, "PASS" if cond else "FAIL", evidence)
    return bool(cond)


def fresh(name: str) -> str:
    root = os.path.join(WORK, name)
    if os.path.exists(root):
        shutil.rmtree(root)
    os.makedirs(root)
    return root


def mk(root: str, rel: str, content: str = "内容") -> str:
    path = os.path.join(root, rel)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def snapshot(root: str) -> dict:
    out = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            try:
                with open(full, "rb") as f:
                    out[rel] = f.read()
            except OSError:
                out[rel] = None
    return out


def plan_for(root: str, mode: str = MODE_BY_TYPE):
    sr = scanner.scan_directory(root, detect_project_roots=False)
    plan = generate_plan(sr, mode, DEFAULT_COURSE_RULES)
    for item in plan.items:
        item.selected = True
    return plan


def corrupt_files(root: str):
    d = os.path.join(root, ".organizer")
    if not os.path.isdir(d):
        return []
    return [n for n in os.listdir(d) if ".corrupt-" in n]


# ==================== 01 扫描不得删除普通文件 ====================

def t01_scan_readonly():
    root = fresh("t01")
    folder = os.path.join(root, "项目")
    os.makedirs(os.path.join(folder, "docs"), exist_ok=True)
    mk(folder, "pyproject.toml", "[project]\nname='x'\n")
    old = mk(root, "项目.archivetmp.笔记.txt", "这是普通文件，不是临时副本")
    inside = mk(folder, os.path.join("docs", "资料.archivetmp.旧.txt"),
                "项目里的同名样本")
    old_time = time.time() - 3 * 86400
    for path in (old, inside):
        os.utime(path, (old_time, old_time))

    before = snapshot(root)
    sr1 = scanner.scan_directory(root)
    sr2 = scanner.scan_paths(root, [root])
    after = snapshot(root)

    ok_ = (before == after
           and os.path.isfile(old) and read(old).startswith("这是普通文件")
           and os.path.isfile(inside))
    ok("01", "扫描/预览只读：.archivetmp 普通文件不被删除", ok_,
       f"扫描文件数={len(sr1.files)}/{len(sr2.files)}\n"
       f"扫描前后文件清单一致={before == after}\n"
       f"普通文件仍在={os.path.isfile(old)} 受保护项目内样本仍在={os.path.isfile(inside)}")


# ==================== 02 局部扫描遵守项目保护 / 保留目录 ====================

def t02_scan_paths_protection():
    root = fresh("t02")
    project = os.path.join(root, "项目")
    docs = os.path.join(project, "docs")
    os.makedirs(docs)
    mk(project, "pyproject.toml", "[project]\n")
    mk(project, os.path.join("docs", "overview.txt"), "项目里的文档")
    mk(project, os.path.join("docs", "details.txt"), "项目里的文档 2")
    mk(root, os.path.join("_已整理", "旧批次", "归档副本.txt"), "旧副本")
    mk(root, os.path.join(".organizer", "history.txt"), "内部")
    protected = os.path.join(root, "手动保护")
    os.makedirs(protected)
    mk(root, os.path.join("手动保护", "别动.txt"), "保护")
    mk(root, os.path.join("普通资料", "可整理.txt"), "普通文件")

    specs = [(protected, "manual", "用户手动保护")]
    checks = {
        "项目/docs 子目录": scanner.scan_paths(root, [docs]),
        "项目/docs 单文件": scanner.scan_paths(
            root, [os.path.join(docs, "overview.txt")]),
        "项目 父目录": scanner.scan_paths(root, [project]),
        "_已整理 起点": scanner.scan_paths(root, [os.path.join(root, "_已整理")]),
        ".organizer 起点": scanner.scan_paths(
            root, [os.path.join(root, ".organizer")]),
        "手动保护目录": scanner.scan_paths(root, [protected],
                                            protect_dirs=specs),
        "普通目录": scanner.scan_paths(root, [os.path.join(root, "普通资料")]),
    }
    whole = scanner.scan_directory(root)
    whole_rels = {f.rel_path for f in whole.files}

    excluded_ok = all(
        not r.files for name, r in checks.items() if name != "普通目录")
    normal_ok = ([f.rel_path for f in checks["普通目录"].files]
                 == [os.path.join("普通资料", "可整理.txt")])
    whole_ok = (not any(r.startswith("项目") or r.startswith("_已整理")
                        or r.startswith(".organizer") for r in whole_rels))

    # 执行前再次核验保护范围：预览后新增的手动保护也必须生效
    root2 = fresh("t02_exec_guard")
    mk(root2, os.path.join("资料", "论文.txt"), "受保护内容")
    plan = plan_for(root2)
    protect_config.save_manual_protect(root2, [os.path.join(root2, "资料")])
    st = Storage(root2)
    er = executor.execute_plan(plan, st)
    st.close()
    guard_ok = (er.moved == 0 and er.skipped == 1
                and os.path.isfile(os.path.join(root2, "资料", "论文.txt")))

    ok_ = excluded_ok and normal_ok and whole_ok and guard_ok
    ok("02", "整空间/单文件/父目录/子目录共用同一套保护判定", ok_,
       "\n".join(f"{name} → {[f.rel_path for f in r.files]}"
                 for name, r in checks.items())
       + f"\n整空间 → {sorted(whole_rels)}"
       + f"\n执行前保护复核：moved={er.moved} skipped={er.skipped} "
         f"文件仍在={os.path.isfile(os.path.join(root2, '资料', '论文.txt'))}")


# ==================== 03 历史库被占用时不得改名重建 ====================

def t03_storage_busy():
    root = fresh("t03")
    st = Storage(root)
    st.conn.execute(
        "INSERT INTO batches(root, mode, status, created_at, summary) "
        "VALUES (?,?,?,?,?)", (st.root, "by_type", "done", "t", "原批次"))
    st.conn.commit()
    st.close()

    child_code = f"""
import sys
sys.path.insert(0, {SRC!r})
from storage import Storage
st = Storage({root!r})
st.conn.execute("BEGIN IMMEDIATE")
print("READY", flush=True)
sys.stdin.readline()
st.conn.rollback()
st.close()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    ready = child.stdout.readline().strip() if child.stdout else ""
    if ready != "READY":
        err = child.stderr.read() if child.stderr else ""
        child.kill()
        ok("03", "历史库被占用时不改名重建", False,
           f"子进程未就绪：{ready!r}\n{err}")
        return

    busy_error = ""
    t0 = time.time()
    try:
        second = Storage(root)
        second.close()
    except storage_mod.StorageBusyError as e:
        busy_error = str(e)
    except Exception as e:                       # noqa: BLE001
        busy_error = f"{type(e).__name__}: {e}"
    waited = time.time() - t0
    corrupt_while_locked = corrupt_files(root)

    child.stdin.write("\n")
    child.stdin.flush()
    child.wait(timeout=20)

    st2 = Storage(root)
    n = st2.conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
    st2.close()
    corrupt_after = corrupt_files(root)

    # 权限/IO 错误同样不能改名：用 connect 抛 OSError 模拟
    root_io = fresh("t03_io")
    st_io = Storage(root_io)
    st_io.close()
    before_io = corrupt_files(root_io)
    real_connect = storage_mod.sqlite3.connect

    def denied(*_a, **_k):
        raise PermissionError("模拟权限错误")

    storage_mod.sqlite3.connect = denied
    io_error = ""
    try:
        Storage(root_io)
    except storage_mod.StorageAccessError as e:
        io_error = str(e)
    except Exception as e:                       # noqa: BLE001
        io_error = f"{type(e).__name__}: {e}"
    finally:
        storage_mod.sqlite3.connect = real_connect
    io_ok = bool(io_error) and corrupt_files(root_io) == before_io

    # 确认损坏（文件头不是 SQLite）才允许改名重建
    root_bad = fresh("t03_bad")
    os.makedirs(os.path.join(root_bad, ".organizer"))
    with open(os.path.join(root_bad, ".organizer", "history.db"), "wb") as f:
        f.write(b"this is not sqlite at all")
    st_bad = Storage(root_bad)
    rebuilt_ok = bool(st_bad.recovery_note) and bool(corrupt_files(root_bad))
    st_bad.close()

    ok_ = (bool(busy_error) and waited < 10 and not corrupt_while_locked
           and n == 1 and not corrupt_after and io_ok and rebuilt_ok)
    ok("03", "忙/权限错误不改名；确认损坏才改名重建", ok_,
       f"错误类型={busy_error!r} 等待={waited:.1f}s\n"
       f"持锁期间 corrupt 文件={corrupt_while_locked}\n"
       f"释放后批次数={n} corrupt 文件={corrupt_after}\n"
       f"权限错误不改名={io_ok} 错误={io_error!r}\n"
       f"损坏文件已改名重建={rebuilt_ok}")


# ==================== 04 核对 / 取消 / 部分执行 / 暂停可恢复 ====================

def t04_recover_states():
    # a) 取消核对：不写 cancelled_clean，批次保持待核对
    root = fresh("t04_cancel")
    mk(root, "一.txt", "一")
    mk(root, "二.txt", "二")
    plan = plan_for(root)
    st = Storage(root)
    bid = st.create_batch(plan, "测试")
    st.create_actions_bulk(bid, plan.items,
                           [i.source_sha256 for i in plan.items])
    st.update_batch_status(bid, "executing")
    ev = threading.Event()
    ev.set()
    rr = executor.recover_batch(st, bid, cancel_event=ev)
    status_cancel = st.get_batch(bid).status
    a_ok = (rr.was_cancelled and status_cancel == "executing"
            and status_cancel != "cancelled_clean")

    # b) 再核对一次（未取消）：仍全部在原处 → 干净取消；planned 已标记核对
    rr2 = executor.recover_batch(st, bid)
    actions = st.get_actions(bid)
    b_ok = (rr2.batch_status_after == "cancelled_clean"
            and all(a.status == "planned" and a.verified for a in actions))
    st.close()

    # c) paused 再次核对：内容指纹对上就按磁盘事实改状态
    root2 = fresh("t04_paused")
    mk(root2, "资料.txt", "内容一致")
    plan2 = plan_for(root2)
    st2 = Storage(root2)
    bid2 = st2.create_batch(plan2, "测试")
    st2.create_actions_bulk(bid2, plan2.items,
                            [i.source_sha256 for i in plan2.items])
    st2.update_batch_status(bid2, "executing")
    item = plan2.items[0]
    target = os.path.join(root2, item.target_rel_path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    os.replace(os.path.join(root2, item.file.rel_path), target)
    st2.conn.execute(
        "UPDATE actions SET status='paused', verified=0 WHERE batch_id=?",
        (bid2,))
    st2.conn.commit()
    rr3 = executor.recover_batch(st2, bid2)
    a2 = st2.get_actions(bid2)[0]
    c_ok = (a2.status == "moved" and rr3.recovered_moved == 1
            and rr3.batch_status_after == "done")
    st2.close()

    # d) moved + 已核对的 planned 可以撤销，不再整批拒绝
    root3 = fresh("t04_undo_mixed")
    mk(root3, "甲.txt", "甲")
    mk(root3, "乙.txt", "乙")
    plan3 = plan_for(root3)
    st3 = Storage(root3)
    er = executor.execute_plan(plan3, st3)
    actions3 = st3.get_actions(er.batch_id)
    # 把其中一个文件放回原位，并模拟“已核对、仍在原处”
    moved_action = next(a for a in actions3 if a.status == "moved")
    os.makedirs(os.path.dirname(os.path.join(
        root3, moved_action.source_rel)), exist_ok=True)
    os.replace(os.path.join(root3, moved_action.target_rel),
               os.path.join(root3, moved_action.source_rel))
    st3.conn.execute(
        "UPDATE actions SET status='planned', verified=1 "
        "WHERE batch_id=? AND action_index=?",
        (er.batch_id, moved_action.action_index))
    st3.conn.commit()
    ur = executor.undo_latest_batch(st3, batch_id=er.batch_id)
    batch3 = st3.get_batch(er.batch_id)
    d_ok = (ur.restored == 1 and ur.is_complete and not ur.refused
            and batch3.status == "undone"
            and os.path.isfile(os.path.join(root3, moved_action.source_rel)))
    st3.close()

    # e) paused 但文件仍在原处、指纹一致：改为 planned+verified，
    #    不再占用核对入口；同批已移动的另一份仍可撤销
    root4 = fresh("t04_paused_at_source")
    mk(root4, "已移动.txt", "移动内容")
    mk(root4, "留在原处.txt", "原处内容")
    plan4 = plan_for(root4)
    st4 = Storage(root4)
    bid4 = st4.create_batch(plan4, "测试")
    st4.create_actions_bulk(bid4, plan4.items,
                            [i.source_sha256 for i in plan4.items])
    acts4 = st4.get_actions(bid4)
    moved_act = acts4[0]
    stayed_act = acts4[1]
    target4 = os.path.join(root4, moved_act.target_rel)
    os.makedirs(os.path.dirname(target4), exist_ok=True)
    os.replace(os.path.join(root4, moved_act.source_rel), target4)
    st4.conn.execute(
        "UPDATE actions SET status='moved' WHERE batch_id=? AND action_index=?",
        (bid4, moved_act.action_index))
    st4.conn.execute(
        "UPDATE actions SET status='paused', verified=0 "
        "WHERE batch_id=? AND action_index=?",
        (bid4, stayed_act.action_index))
    st4.update_batch_status(bid4, "executing")

    rr4 = executor.recover_batch(st4, bid4)
    after4 = {a.action_index: a for a in st4.get_actions(bid4)}
    stayed_after = after4[stayed_act.action_index]
    e_ok = (stayed_after.status == "planned" and stayed_after.verified
            and rr4.paused == 0
            and rr4.batch_status_after == "partial"
            and not st4.batch_needs_verification(bid4)
            and bid4 not in st4.batch_ids_needing_verification()
            and st4.batch_is_undoable(bid4))
    needs_after4 = st4.batch_needs_verification(bid4)
    ur4 = executor.undo_latest_batch(st4, batch_id=bid4)
    batch4 = st4.get_batch(bid4)
    e_ok = (e_ok and ur4.restored == 1 and ur4.is_complete
            and batch4.status == "undone"
            and os.path.isfile(os.path.join(root4, moved_act.source_rel))
            and not os.path.exists(target4))
    st4.close()

    # f) 指纹不符、或源与目标同时存在：继续暂停，仍占用核对入口
    root5 = fresh("t04_paused_unclear")
    mk(root5, "不符.txt", "原始内容")
    mk(root5, "双份.txt", "同样的内容")
    plan5 = plan_for(root5)
    st5 = Storage(root5)
    bid5 = st5.create_batch(plan5, "测试")
    st5.create_actions_bulk(bid5, plan5.items,
                            [i.source_sha256 for i in plan5.items])
    acts5 = st5.get_actions(bid5)
    mismatch_act, both_act = acts5[0], acts5[1]
    target5a = os.path.join(root5, mismatch_act.target_rel)
    os.makedirs(os.path.dirname(target5a), exist_ok=True)
    os.replace(os.path.join(root5, mismatch_act.source_rel), target5a)
    with open(target5a, "w", encoding="utf-8") as f:
        f.write("被改过的内容")
    target5b = os.path.join(root5, both_act.target_rel)
    os.makedirs(os.path.dirname(target5b), exist_ok=True)
    shutil.copy2(os.path.join(root5, both_act.source_rel), target5b)
    st5.conn.execute(
        "UPDATE actions SET status='paused', verified=0 WHERE batch_id=?",
        (bid5,))
    st5.update_batch_status(bid5, "executing")
    executor.recover_batch(st5, bid5)
    after5 = {a.action_index: a for a in st5.get_actions(bid5)}
    f_ok = (after5[mismatch_act.action_index].status == "paused"
            and after5[both_act.action_index].status == "paused"
            and st5.batch_needs_verification(bid5))
    st5.close()

    ok_ = a_ok and b_ok and c_ok and d_ok and e_ok and f_ok
    ok("04", "取消核对保持待核对；paused 重新核对；moved+planned 可撤销", ok_,
       f"取消核对：was_cancelled={rr.was_cancelled} 状态={status_cancel}\n"
       f"完整核对：状态={rr2.batch_status_after} 全部 planned 已核对={b_ok}\n"
       f"paused 重核（目标在）：状态={a2.status} 补记={rr3.recovered_moved} "
       f"批次={rr3.batch_status_after}\n"
       f"混合撤销：恢复={ur.restored} complete={ur.is_complete} "
       f"批次={batch3.status}\n"
       f"paused 重核（原处在+指纹一致）：状态={stayed_after.status} "
       f"verified={stayed_after.verified} 批次={rr4.batch_status_after} "
       f"需核对={needs_after4}\n"
       f"同批已移动可撤销：恢复={ur4.restored} complete={ur4.is_complete} "
       f"批次={batch4.status}\n"
       f"指纹不符/双份继续暂停={f_ok}")


# ==================== 05 写库失败必须停止并待核对 ====================

def t05_db_failure():
    # a) 执行写库失败：停止后续文件，批次保持待核对
    root = fresh("t05_exec")
    mk(root, "一.txt", "一")
    mk(root, "二.txt", "二")
    mk(root, "三.txt", "三")
    plan = plan_for(root)
    st = Storage(root)
    real_bulk = st.update_actions_bulk

    def failing_bulk(batch_id, updates, commit=True):
        if any(status == "moved" for _idx, status, _err in updates):
            raise RuntimeError("模拟执行写库失败")
        return real_bulk(batch_id, updates, commit=commit)

    st.update_actions_bulk = failing_bulk
    er = executor.execute_plan(plan, st)
    st.update_actions_bulk = real_bulk
    batch = st.get_batch(er.batch_id)
    moved_now = sum(1 for _dp, _dn, fs in os.walk(os.path.join(root, "_已整理"))
                    for _f in fs)
    a_ok = (er.db_write_failed and er.moved == 1 and moved_now == 1
            and batch.status == "executing"
            and st.batch_needs_verification(batch.batch_id)
            and any("停止后续文件操作" in e for e in er.errors))
    st.close()

    # b) 撤销写库失败：is_complete 必须为假，批次不能标 undone
    root2 = fresh("t05_undo")
    mk(root2, "甲.txt", "甲")
    mk(root2, "乙.txt", "乙")
    plan2 = plan_for(root2)
    st2 = Storage(root2)
    er2 = executor.execute_plan(plan2, st2)
    real_bulk2 = st2.update_actions_bulk

    def failing_bulk2(batch_id, updates, commit=True):
        if any(status == "undone" for _idx, status, _err in updates):
            raise RuntimeError("模拟撤销写库失败")
        return real_bulk2(batch_id, updates, commit=commit)

    st2.update_actions_bulk = failing_bulk2
    ur = executor.undo_latest_batch(st2, batch_id=er2.batch_id)
    st2.update_actions_bulk = real_bulk2
    batch2 = st2.get_batch(er2.batch_id)
    b_ok = (ur.db_write_failed and not ur.is_complete and bool(ur.errors_list)
            and batch2.status != "undone")
    st2.close()

    # c) 进程在移动后、写库前中断：重新打开后核对补记
    root3 = fresh("t05_interrupt")
    mk(root3, "资料.txt", "中断前内容")
    plan3 = plan_for(root3)
    st3 = Storage(root3)
    er3 = executor.execute_plan(plan3, st3)
    item = plan3.items[0]
    st3.conn.execute(
        "UPDATE actions SET status='planned', verified=0 WHERE batch_id=?",
        (er3.batch_id,))
    st3.update_batch_status(er3.batch_id, "executing")
    st3.close()
    st3b = Storage(root3)
    rr = executor.recover_batch(st3b, er3.batch_id)
    a3 = st3b.get_actions(er3.batch_id)[0]
    c_ok = (rr.recovered_moved == 1 and a3.status == "moved" and a3.verified)
    st3b.close()

    # d) 撤销中断（已回原位、结果没落库）：核对补记 undone
    root4 = fresh("t05_undo_interrupt")
    mk(root4, "资料.txt", "撤销中断")
    plan4 = plan_for(root4)
    st4 = Storage(root4)
    er4 = executor.execute_plan(plan4, st4)
    a4 = st4.get_actions(er4.batch_id)[0]
    os.makedirs(os.path.dirname(os.path.join(root4, a4.source_rel)),
                exist_ok=True)
    os.replace(os.path.join(root4, a4.target_rel),
               os.path.join(root4, a4.source_rel))
    st4.conn.execute(
        "UPDATE actions SET status='restoring' WHERE batch_id=?",
        (er4.batch_id,))
    st4.conn.commit()
    st4.close()
    st4b = Storage(root4)
    rr4 = executor.recover_batch(st4b, er4.batch_id)
    a4b = st4b.get_actions(er4.batch_id)[0]
    d_ok = (a4b.status == "undone" and rr4.batch_status_after == "undone")
    st4b.close()

    ok_ = a_ok and b_ok and c_ok and d_ok
    ok("05", "执行/撤销写库失败立即停止且不报成功；中断后可核对补记", ok_,
       f"执行失败：db_failed={er.db_write_failed} moved={er.moved} "
       f"实际移动文件={moved_now} 批次={batch.status}\n"
       f"撤销失败：db_failed={ur.db_write_failed} complete={ur.is_complete} "
       f"批次={batch2.status}\n"
       f"执行中断补记：moved={a3.status} verified={a3.verified}\n"
       f"撤销中断补记：状态={a4b.status} 批次={rr4.batch_status_after}")


# ==================== 06 目标已存在必须失败 + 空间锁 ====================

def t06_no_overwrite_and_lock():
    # a) 执行：目标占位文件不被覆盖，源文件保留
    root = fresh("t06_exec")
    mk(root, "报告.txt", "源内容")
    plan = plan_for(root)
    item = plan.items[0]
    target = os.path.join(root, item.target_rel_path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write("占位内容")
    st = Storage(root)
    er = executor.execute_plan(plan, st)
    a_ok = (er.moved == 0 and er.skipped == 1
            and read(os.path.join(root, "报告.txt")) == "源内容"
            and read(target) == "占位内容")
    st.close()

    # b) 撤销：原位置占位不被覆盖，目标文件保留
    root2 = fresh("t06_undo")
    mk(root2, "报告.txt", "原始内容")
    plan2 = plan_for(root2)
    st2 = Storage(root2)
    er2 = executor.execute_plan(plan2, st2)
    item2 = plan2.items[0]
    source2 = os.path.join(root2, item2.file.rel_path)
    target2 = os.path.join(root2, item2.target_rel_path)
    with open(source2, "w", encoding="utf-8") as f:
        f.write("新占位")
    ur = executor.undo_latest_batch(st2, batch_id=er2.batch_id)
    b_ok = (ur.restored == 0 and ur.conflict == 1
            and read(source2) == "新占位" and os.path.isfile(target2))
    st2.close()

    # c) 空间锁：另一个进程持锁时直接拒绝
    root3 = fresh("t06_lock")
    mk(root3, "资料.txt", "锁测试")
    plan3 = plan_for(root3)
    child_code = f"""
import sys
sys.path.insert(0, {SRC!r})
from executor import space_lock
import time
with space_lock({root3!r}):
    print("READY", flush=True)
    sys.stdin.readline()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    ready = child.stdout.readline().strip() if child.stdout else ""
    lock_error = ""
    if ready == "READY":
        st3 = Storage(root3)
        er3 = executor.execute_plan(plan3, st3)
        lock_error = " | ".join(er3.errors)
        batches = st3.conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
        st3.close()
    else:
        batches = -1
        lock_error = (child.stderr.read() if child.stderr else "")
    if child.poll() is None:
        if child.stdin:
            child.stdin.write("\n")
            child.stdin.flush()
        child.wait(timeout=20)
    c_ok = (ready == "READY" and "另一个归档盒进程" in lock_error
            and batches == 0)

    # d) 复制中断：源文件仍在，目标不能是空的/半截
    base = fresh("t06_copy_interrupt")
    src = os.path.join(base, "源.bin")
    dst = os.path.join(base, "目标.bin")
    with open(src, "wb") as f:
        f.write("完整内容".encode("utf-8") * 100)
    real_copy = executor.shutil.copy2

    def broken_copy(a, b, *args, **kwargs):
        with open(b, "wb") as f:
            f.write("半截".encode("utf-8"))
        raise OSError("模拟复制中断")

    executor.shutil.copy2 = broken_copy
    try:
        moved, msg, _conflict = executor._copy_then_replace(src, dst)
    finally:
        executor.shutil.copy2 = real_copy
    d_ok = (moved is False and os.path.isfile(src)
            and not os.path.exists(dst)
            and not os.path.exists(dst + ".archivetmp"))

    ok_ = a_ok and b_ok and c_ok and d_ok
    ok("06", "目标已存在不覆盖；空间锁拒绝并发；复制中断不产生假目标", ok_,
       f"执行占位：moved={er.moved} skipped={er.skipped} "
       f"源={read(os.path.join(root, '报告.txt'))!r} 占位={read(target)!r}\n"
       f"撤销占位：restored={ur.restored} conflict={ur.conflict}\n"
       f"空间锁：ready={ready} batches={batches} 错误={lock_error!r}\n"
       f"复制中断：moved={moved} 源仍在={os.path.isfile(src)} "
       f"目标存在={os.path.exists(dst)} 消息={msg!r}")


# ==================== 07 没有有效指纹就不能移动 / 不能当重复 ====================

def t07_fingerprints():
    root = fresh("t07")
    mk(root, "唯一.txt", "只有一个文件的内容")
    mk(root, "空一.bin", "")
    mk(root, "空二.bin", "")
    sr = scanner.scan_directory(root, detect_project_roots=False)
    plan = generate_plan(sr, MODE_BY_TYPE, DEFAULT_COURSE_RULES)
    sha_map = {i.file.rel_path: i.source_sha256 for i in plan.items}
    a_ok = (all(sha_map.values()) and not plan.dup_groups)

    # 同尺寸文件都算不出指纹时，不能并成空指纹重复组
    real_sha = planner.sha256_file
    planner.sha256_file = lambda *_a, **_k: None
    try:
        sr2 = scanner.scan_directory(root, detect_project_roots=False)
        plan2 = generate_plan(sr2, MODE_BY_TYPE, DEFAULT_COURSE_RULES)
    finally:
        planner.sha256_file = real_sha
    b_ok = (not plan2.dup_groups
            and all(not i.source_sha256 for i in plan2.items)
            and all(i.dup_group_id is None for i in plan2.items))

    # 没有指纹的条目执行时直接跳过，文件留在原处
    root2 = fresh("t07_exec")
    mk(root2, "无指纹.txt", "内容")
    plan3 = plan_for(root2)
    plan3.items[0].source_sha256 = ""
    st = Storage(root2)
    er = executor.execute_plan(plan3, st)
    c_ok = (er.moved == 0 and er.skipped == 1
            and os.path.isfile(os.path.join(root2, "无指纹.txt"))
            and any("缺少内容指纹" in e for e in er.errors))
    st.close()

    ok_ = a_ok and b_ok and c_ok
    ok("07", "0 字节/单尺寸也建指纹；空指纹不成重复组且不执行", ok_,
       f"指纹齐全={a_ok} 重复组={plan.dup_groups}\n"
       f"空指纹重复组={plan2.dup_groups} 组标记={[i.dup_group_id for i in plan2.items]}\n"
       f"执行跳过：moved={er.moved} skipped={er.skipped}")


# ==================== 08 撤销必须使用同一个批次 ====================

def t08_undo_batch_id():
    root = fresh("t08")
    mk(root, "甲.txt", "甲")
    st = Storage(root)
    er1 = executor.execute_plan(plan_for(root), st)

    # 第二批：源文件执行前消失，全部跳过（零移动）
    mk(root, "乙.txt", "乙")
    plan2 = plan_for(root)
    plan2.items = [i for i in plan2.items if i.file.filename == "乙.txt"]
    os.remove(os.path.join(root, "乙.txt"))
    er2 = executor.execute_plan(plan2, st)

    undoable = st.get_undoable_batch()
    zero_undoable = st.batch_is_undoable(er2.batch_id)
    a_ok = (er2.batch_id > er1.batch_id
            and undoable is not None
            and undoable.batch_id == er1.batch_id
            and not zero_undoable)

    # 新批次：一个已移动 + 一个未核对 planned → 不做撤销候选，进入核对
    mk(root, "丙.txt", "丙")
    mk(root, "丁.txt", "丁")
    plan3 = plan_for(root)
    plan3.items = [i for i in plan3.items
                   if i.file.filename in ("丙.txt", "丁.txt")]
    er3 = executor.execute_plan(plan3, st)
    acts = st.get_actions(er3.batch_id)
    st.conn.execute(
        "UPDATE actions SET status='planned', verified=0 "
        "WHERE batch_id=? AND action_index=?",
        (er3.batch_id, acts[0].action_index))
    st.conn.commit()
    check_batch = st.get_batch_needing_check()
    check_undoable = st.batch_is_undoable(er3.batch_id)
    b_ok = (check_batch is not None
            and check_batch.batch_id == er3.batch_id
            and not check_undoable)

    ur = executor.undo_latest_batch(st, batch_id=er1.batch_id)
    c_ok = (ur.batch_id == er1.batch_id and ur.is_complete
            and st.get_batch(er1.batch_id).status == "undone")
    st.close()

    ok_ = a_ok and b_ok and c_ok
    ok("08", "零移动批次不挡撤销；核对批次不入撤销；确认与后台同一个 id", ok_,
       f"批次：整理#{er1.batch_id} 零移动#{er2.batch_id} 待核对#{er3.batch_id}\n"
       f"get_undoable_batch={undoable.batch_id if undoable else None} "
       f"零移动可撤销={zero_undoable}\n"
       f"待核对批次={check_batch.batch_id if check_batch else None} "
       f"可撤销={check_undoable}\n"
       f"显式撤销批次={ur.batch_id} complete={ur.is_complete}")


# ==================== 09 撤销不删除用户原有空目录 ====================

def t09_cleanup_dirs():
    # a) 用户事先建好的空 _已整理 必须保留
    root = fresh("t09_existing")
    os.makedirs(os.path.join(root, "_已整理"))
    assert os.listdir(os.path.join(root, "_已整理")) == []
    mk(root, "笔记.txt", "笔记")
    st = Storage(root)
    er = executor.execute_plan(plan_for(root), st)
    ur = executor.undo_latest_batch(st, batch_id=er.batch_id)
    a_ok = (ur.is_complete
            and os.path.isdir(os.path.join(root, "_已整理"))
            and os.listdir(os.path.join(root, "_已整理")) == [])
    st.close()

    # b) 本次新建的空目录要被清掉
    root2 = fresh("t09_created")
    mk(root2, "笔记.txt", "笔记")
    st2 = Storage(root2)
    er2 = executor.execute_plan(plan_for(root2), st2)
    ur2 = executor.undo_latest_batch(st2, batch_id=er2.batch_id)
    b_ok = ur2.is_complete and not os.path.exists(
        os.path.join(root2, "_已整理"))
    st2.close()

    ok_ = a_ok and b_ok
    ok("09", "只清理本次新建的空目录，保留用户原有空父目录", ok_,
       f"原有空目录仍在={a_ok}\n本次新建目录已清理={b_ok}")


# ==================== 10 待整理目录变化时旧扫描失效 ====================

def t10_intake_fingerprint():
    root = fresh("t10")
    scope = os.path.join(root, "待整理")
    os.makedirs(scope)
    mk(scope, "a.txt", "a")
    sig1 = intake_model.scope_fingerprint(root, [scope])
    mk(scope, "b.txt", "b")
    sig2 = intake_model.scope_fingerprint(root, [scope])
    os.rename(os.path.join(scope, "b.txt"), os.path.join(scope, "c.txt"))
    sig3 = intake_model.scope_fingerprint(root, [scope])
    os.makedirs(os.path.join(scope, "子目录"))
    mk(scope, os.path.join("子目录", "d.txt"), "d")
    sig4 = intake_model.scope_fingerprint(root, [scope])
    os.remove(os.path.join(scope, "a.txt"))
    sig5 = intake_model.scope_fingerprint(root, [scope])
    ok_ = len({sig1, sig2, sig3, sig4, sig5}) == 5
    ok("10", "范围指纹能发现新增/改名/目录结构变化/删除", ok_,
       "\n".join(f"阶段{i} 条目数={len(s[1])}"
                 for i, s in enumerate((sig1, sig2, sig3, sig4, sig5), 1))
       + f"\n指纹两两不同={ok_}")


# ==================== 11 备份轮换与恢复使用同一套有效性 ====================

def t11_backup_rotation():
    root = fresh("t11")
    cc.save_config(root, cc.default_config())
    good = config_transfer.create_backup(root)

    # 当前配置变成坏 JSON；再放几份“合法 JSON、配置无效”的新备份
    with open(cc.config_path(root), "w", encoding="utf-8") as f:
        f.write("{坏掉的 JSON")
    backup_dir = config_transfer.backup_dir(root)
    invalid_paths = []
    for i in range(3):
        name = f"config-backup-29990101-00000{i}-000000.json"
        path = os.path.join(backup_dir, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"state": "custom",
                       "config": {"version": 1, "semester_map": "不是数组"},
                       "created_at": "2999-01-01"}, f, ensure_ascii=False)
        invalid_paths.append(path)

    config_transfer.create_backup(root, keep=2)
    valid = [i for i in config_transfer.list_backups(root) if i.valid]
    a_ok = (os.path.isfile(good) and len(valid) >= 1
            and all(not config_transfer._backup_payload_readable(p)
                    for p in invalid_paths))

    err = ""
    try:
        state, msg = config_transfer.restore_latest_backup(root)
    except Exception as e:                       # noqa: BLE001
        state, msg, err = "", "", str(e)
    b_ok = (not err and state in ("custom", "default") and "已恢复" in msg)
    ok_ = a_ok and b_ok
    ok("11", "无效但合法的 JSON 备份不算可恢复；至少保留一份可恢复备份", ok_,
       f"原可用备份仍在={os.path.isfile(good)} 当前有效备份数={len(valid)}\n"
       f"无效备份被判无效={a_ok}\n恢复结果={state} 错误={err!r}")


# ==================== 12 AI 缓存不串空间、不改已明确分类 ====================

def _ai_item(name, direction, category, rule_key, ocr_error=""):
    entry = scanner.FileEntry(
        abs_path=os.path.join(WORK, name), rel_path=name, filename=name,
        file_type="图片", size=12, size_display="12 B", mtime_ns=123)
    return planner.PlanItem(
        file=entry, course=category or direction, file_type="图片",
        rule_applied=rule_key, target_dir_rel="_已整理/待定",
        target_filename=name, target_rel_path="_已整理/待定/" + name,
        direction=direction, category=category, rule_key=rule_key,
        ocr_error=ocr_error)


def t12_ai_cache():
    ai_fallback._result_cache.clear()
    ai_fallback._result_cache_order.clear()
    pairs = ai_fallback.allowed_pairs(None)
    allowed = set(pairs)

    # 同一相对路径、不同整理空间不能命中同一缓存
    def plan_in(root):
        item = _ai_item("shot.png", "待定", "", "fallback")
        return planner.OrganizePlan(
            root=root, mode=MODE_BY_DIRECTION_SEMESTER, rules=[],
            items=[item])

    plan_a = plan_in(os.path.join(WORK, "ai_space_a"))
    plan_b = plan_in(os.path.join(WORK, "ai_space_b"))
    key_a = ai_fallback._cache_key(plan_a, plan_a.items[0], pairs)
    key_b = ai_fallback._cache_key(plan_b, plan_b.items[0], pairs)
    a_ok = key_a != key_b

    # 候选规则本身不同（不是个数）键就不同
    key_c = ai_fallback._cache_key(
        plan_a, plan_a.items[0], [("课程", "高等数学")])
    b_ok = key_a != key_c

    # 命中结果不在当前允许集合时作废并重新询问
    ai_fallback._result_cache.clear()
    ai_fallback._result_cache_order.clear()
    ai_fallback._result_cache[key_a] = ("课程", "不存在的类别")
    ai_fallback._result_cache_order.append(key_a)
    calls = []

    def transport(_settings, messages):
        calls.append(messages)
        return '{"direction":"课程","category":"高等数学"}'

    settings = ai_fallback.AiSettings(api_key="test-key", enabled=True)
    ai_fallback.apply_ai_fallback(plan_a, settings, transport=transport)
    c_ok = (len(calls) == 1
            and plan_a.items[0].category == "高等数学")

    # 规则已给出明确类别：OCR 失败也不问、更不能改写
    definite = _ai_item("笔记.png", "课程", "高等数学",
                        "keyword_unmatched:1:高数", ocr_error="识别失败")
    d_needs = ai_fallback.needs_ai(definite, MODE_BY_DIRECTION_SEMESTER)
    d_applied = ai_fallback._apply_choice(
        definite, MODE_BY_DIRECTION_SEMESTER, "课程", "数据结构")
    d_ok = (not d_needs and d_applied is False
            and definite.category == "高等数学")
    plan_d = planner.OrganizePlan(
        root=os.path.join(WORK, "ai_space_d"),
        mode=MODE_BY_DIRECTION_SEMESTER, rules=[], items=[definite])
    calls2 = []

    def transport2(_settings, messages):
        calls2.append(messages)
        return '{"direction":"课程","category":"数据结构"}'

    note = ai_fallback.apply_ai_fallback(plan_d, settings, transport=transport2)
    d_ok = d_ok and not calls2 and "规则已经覆盖" in note

    ok_ = a_ok and b_ok and c_ok and d_ok
    ok("12", "AI 缓存按空间/规则/指纹区分；命中越界作废；明确分类不被改写", ok_,
       f"跨空间键不同={a_ok} 候选规则不同={b_ok}\n"
       f"越界缓存作废后重新询问={len(calls)} 结果={plan_a.items[0].category}\n"
       f"明确分类 needs_ai={d_needs} apply={d_applied} 调用={len(calls2)} "
       f"说明={note}")


# ==================== 13 原生界面复核后的接线回归 ====================

def t13_ui_wiring():
    """无 PySide6 时用源码接线检查复现三个界面问题。

    1) 「撤销上一次」不再把 clicked(bool) 的 False 当批次 id；
    2) 打开空间后先刷新按钮，核对提示选「否」也能进历史；
    3) 历史“需核对”与 batch_needs_verification 同源，弹窗保留撤销引导。
    """
    text = read(os.path.join(SRC, "main.py"))
    a_ok = ("self.btn_undo.clicked.connect(self.on_undo)" not in text
            and "lambda _checked=False: self.on_undo()" in text
            and "if not batch_id:" in text)
    # 打开空间流程：核对提示前后都刷新按钮状态
    block = ("self._update_button_states()\n"
             "        self._check_pending_batch()\n"
             "        self._update_button_states()")
    b_ok = block in text
    c_ok = ("self._needs_check = st.batch_ids_needing_verification()" in text
            and 'attention = "⚠ 需核对" if b.batch_id in self._needs_check'
                in text
            and "如需把批次 #{result.batch_id} 已移动的文件放回原位置"
                in text)
    ok_ = a_ok and b_ok and c_ok
    ok("13", "主界面撤销传参、首开按钮刷新、历史/弹窗提示同源", ok_,
       f"撤销按钮不再直连 on_undo 且忽略 clicked(bool)={a_ok}\n"
       f"打开空间核对前后刷新按钮={b_ok}\n"
       f"历史需核对/撤销引导同源={c_ok}")


def main() -> int:
    print(f"工作目录：{WORK}\n")
    for fn in (t01_scan_readonly, t02_scan_paths_protection, t03_storage_busy,
               t04_recover_states, t05_db_failure, t06_no_overwrite_and_lock,
               t07_fingerprints, t08_undo_batch_id, t09_cleanup_dirs,
               t10_intake_fingerprint, t11_backup_rotation, t12_ai_cache,
               t13_ui_wiring):
        try:
            fn()
        except Exception:
            rec(fn.__name__, "执行测试时异常", "FAIL", traceback.format_exc())

    fails = [r for r in RESULTS if r["status"] == "FAIL"]
    print(f"\n结果：PASS {len(RESULTS) - len(fails)} / FAIL {len(fails)}")
    if not fails:
        shutil.rmtree(WORK, ignore_errors=True)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
