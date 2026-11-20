# -*- coding: utf-8 -*-
"""归档盒阶段四（main da08bd7）Windows 引擎级实测。

所有操作都在 round2\work 下的独立副本里完成；配置、方案库、最近空间
全部指向 round2\config，不读写真实个人资料。
"""
from __future__ import annotations

import ctypes
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from ctypes import wintypes
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(BASE, "app")
OUT = os.path.join(BASE, "evidence")
LOGS = os.path.join(BASE, "logs")
os.makedirs(OUT, exist_ok=True)
os.makedirs(LOGS, exist_ok=True)

os.environ["ARCHIVEBOX_APP_DIR"] = os.path.join(BASE, "config", "app")
os.environ["ARCHIVEBOX_SCHEME_DIR"] = os.path.join(BASE, "config", "schemes")
os.environ["ARCHIVEBOX_RECENT_FILE"] = os.path.join(BASE, "config", "recent.json")
sys.path.insert(0, APP)

import ai_fallback  # noqa: E402
import content_extract  # noqa: E402
import duplicate_report  # noqa: E402
import executor  # noqa: E402
import ocr_engine  # noqa: E402
import planner  # noqa: E402
import preferences  # noqa: E402
import scan_cache  # noqa: E402
import scanner  # noqa: E402
import storage as storage_mod  # noqa: E402

STAMP = datetime.now().strftime("%Y%m%d_%H%M%S")
WORK = os.path.join(BASE, "work", "engine_" + STAMP)
os.makedirs(WORK, exist_ok=True)

CHECKS = []


def rec(tid, title, status, detail=""):
    """status: PASS / FAIL / INFO / UNTESTED"""
    CHECKS.append({"id": tid, "title": title, "status": status,
                   "detail": detail})
    tag = {"PASS": "PASS", "FAIL": "FAIL", "INFO": "INFO",
           "UNTESTED": "未测"}[status]
    print(f"[{tag}] {tid} {title}")
    if detail:
        print("       " + str(detail).replace("\n", "\n       "))


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_sha(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".organizer"]
        for name in filenames:
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root).replace("\\", "/")
            try:
                out[rel] = sha(p)
            except OSError as e:
                out[rel] = f"ERR:{e}"
    return out


def fresh(tag):
    root = os.path.join(WORK, tag)
    os.makedirs(root, exist_ok=True)
    return os.path.realpath(root)


def write(root, rel, content="", encoding="utf-8"):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path) or root, exist_ok=True)
    if isinstance(content, bytes):
        with open(path, "wb") as f:
            f.write(content)
    else:
        with open(path, "w", encoding=encoding) as f:
            f.write(content)
    return path


def make_docx(path, text):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml",
                    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        zf.writestr("word/document.xml",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>')


def make_pptx(path, text):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("ppt/slides/slide1.xml",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
                    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
                    f'<p:cSld><p:spTree><p:sp><p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p>'
                    '</p:txBody></p:sp></p:spTree></p:cSld></p:sld>')


def make_xlsx(path, text):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/sharedStrings.xml",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    f'<si><t>{text}</t></si></sst>')
        zf.writestr("xl/worksheets/sheet1.xml",
                    '<?xml version="1.0" encoding="UTF-8"?>'
                    '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                    '<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData></worksheet>')


def plan_for(root, mode=planner.MODE_BY_TYPE, rules=None, **kwargs):
    scan = scanner.scan_directory(root)
    plan = planner.generate_plan(
        scan, mode, rules if rules is not None else list(planner.DEFAULT_COURSE_RULES),
        **kwargs)
    return scan, plan


def open_storage(root):
    return storage_mod.Storage(root)


# ---------------- Windows 文件占用 ----------------
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
INVALID_HANDLE = ctypes.c_void_p(-1).value
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                 wintypes.DWORD, wintypes.LPVOID,
                                 wintypes.DWORD, wintypes.DWORD,
                                 wintypes.HANDLE]
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def lock_exclusive(path):
    handle = kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE, 0,
                                  None, OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE or handle is None:
        raise OSError(ctypes.get_last_error(), "CreateFileW 失败")
    return handle


def unlock(handle):
    kernel32.CloseHandle(handle)


# ---------------- 样本 ----------------
def build_main_tree(tag="main_tree"):
    root = fresh(tag)
    write(root, "课程/数据结构/笔记.txt", "数据结构 第一章\n线性表\n")
    write(root, "课程/高等数学/作业.txt", "高数 作业 1\n")
    write(root, "下载/重复A.txt", "同一份内容 " * 20)
    write(root, "下载/重复B.txt", "同一份内容 " * 20)
    write(root, "下载/同名.txt", "来自下载的版本")
    write(root, "其他/同名.txt", "另一份同名但内容不同")
    write(root, "下载/空文件.txt", "")
    write(root, "下载/无扩展名", "没有扩展名")
    write(root, "下载/.隐藏.txt", "隐藏文件")
    write(root, "下载/只读.txt", "只读内容")
    os.chmod(os.path.join(root, "下载/只读.txt"), stat.S_IREAD)
    deep = os.path.join(root, "下载", *(["很深的目录名称一二三四五六"] * 14))
    os.makedirs("\\\\?\\" + deep, exist_ok=True)
    with open("\\\\?\\" + os.path.join(deep, "长路径笔记.txt"), "w",
              encoding="utf-8") as f:
        f.write("长路径内容")
    write(root, ".organizer/old.json", "{}")
    write(root, ".git/config", "git")
    # 项目目录（requirements.txt 标记）
    write(root, "项目甲/README.md", "# 项目甲")
    write(root, "项目甲/requirements.txt", "requests")
    write(root, "项目甲/src/app.py", "print('hi')")
    # 指向根外的目录联接
    outside = os.path.join(WORK, "outside_target")
    os.makedirs(outside, exist_ok=True)
    with open(os.path.join(outside, "根外文件.txt"), "w", encoding="utf-8") as f:
        f.write("根外内容")
    link = os.path.join(root, "下载/外链目录")
    subprocess.run(["cmd", "/c", "mklink", "/J", link, outside],
                   capture_output=True, text=True)
    return root, outside


def build_clean_tree(tag, count=3, prefix="文件"):
    root = fresh(tag)
    for i in range(count):
        write(root, f"下载/{prefix}{i:02d}.txt", f"内容 {tag} {i}\n")
    return root


# ================= 测试组 =================
def group_scan():
    root, outside = build_main_tree("scan_tree")
    scan = scanner.scan_directory(root)
    rels = {f.rel_path.replace("\\", "/") for f in scan.files}

    ok = not any(r.startswith(".organizer") or r.startswith(".git") for r in rels)
    rec("A1", "扫描排除 .organizer / .git", "PASS" if ok else "FAIL",
        f"共 {len(scan.files)} 个文件；样例={sorted(rels)[:6]}")

    hidden = any(".隐藏" in r for r in rels)
    rec("A2", "隐藏文件扫描行为（记录现状）", "INFO",
        f"隐藏文件 .隐藏.txt 是否被扫描：{hidden}")

    long_rel = [r for r in rels if "长路径笔记" in r]
    abs_len = max((len(os.path.join(root, r.replace("/", os.sep)))
                   for r in long_rel), default=0)
    rec("A3", "超过 260 字符的长路径被扫描",
        "PASS" if long_rel and abs_len > 260 else "FAIL",
        f"命中 {len(long_rel)} 个，绝对路径长度 {abs_len}")

    outside_hit = [r for r in rels if "根外文件" in r]
    rec("A4", "目录联接不越出整理根", "PASS" if not outside_hit else "FAIL",
        f"根外文件出现在扫描结果：{outside_hit}；"
        f"junction 计数={len(scan.junction_dirs)}；"
        f"跳过目录={[os.path.basename(p) for p, _ in scan.skipped_dirs][:4]}")

    project_hit = [r for r in rels if r.startswith("项目甲/")]
    rec("A5", "含 requirements.txt 的项目目录整体保护",
        "PASS" if not project_hit else "FAIL",
        f"项目甲文件进入扫描：{project_hit[:4]}；"
        f"保护项={[os.path.basename(p) for p in scan.protected_roots][:5]}；"
        f"保护文件数={scan.protected_count}")

    write(root, "图片/课程截图.png", b"\x89PNG\r\n\x1a\n")
    scan2 = scanner.scan_directory(root)
    rels2 = {f.rel_path.replace("\\", "/") for f in scan2.files}
    exts = {os.path.splitext(f.rel_path)[1].lower() for f in scan2.files}
    rec("A6", "空文件/无扩展名/只读/图片都进入扫描", "INFO",
        f"扩展名集合={sorted(exts)}；"
        f"无扩展名={'下载/无扩展名' in rels2}；"
        f"空文件={'下载/空文件.txt' in rels2}；"
        f"只读={'下载/只读.txt' in rels2}")


def group_plan():
    root, _ = build_main_tree("plan_tree")
    scan, plan = plan_for(root, planner.MODE_BY_TYPE)
    targets = [i.target_rel_path.replace("\\", "/") for i in plan.items]
    under_root = all(t.startswith("_已整理/") or t == "" for t in targets)
    rec("B1", "按类型计划全部落到 _已整理/", "PASS" if under_root else "FAIL",
        f"示例={targets[:5]}")

    _, plan2 = plan_for(root, planner.MODE_BY_COURSE_THEN_TYPE)
    course_hit = [i for i in plan2.items if "笔记.txt" in i.file.filename
                  and "高等数学" in (i.target_rel_path + i.rule_applied)]
    rec("B2", "按课程模式用所在目录名作为分类证据",
        "PASS" if course_hit else "FAIL",
        f"课程/高等数学/作业.txt 的目标="
        f"{[(i.file.rel_path, i.target_rel_path) for i in plan2.items if i.file.filename == '作业.txt']}")

    # 对照：文件名本身含关键词时必须能命中
    write(root, "下载/高数作业.txt", "高数 作业")
    _, plan_ctrl = plan_for(root, planner.MODE_BY_COURSE_THEN_TYPE)
    ctrl = [i for i in plan_ctrl.items if i.file.filename == "高数作业.txt"]
    rec("B2b", "对照：文件名含「高数」可命中",
        "PASS" if ctrl and "高等数学" in (ctrl[0].course or "") else "FAIL",
        f"目标={[(i.course, i.target_rel_path) for i in ctrl]}")

    same = [i for i in plan2.items if i.file.filename == "同名.txt"]
    same_targets = {i.target_rel_path for i in same}
    rec("B3", "不同目录同名文件目标不冲突",
        "PASS" if len(same) == 2 and len(same_targets) == 2 else "FAIL",
        f"同名条目={[(i.file.rel_path, i.target_rel_path) for i in same]}")

    dups = [i for i in plan2.items if i.dup_count > 1]
    rec("B4", "内容重复识别", "PASS" if len(dups) >= 2 else "FAIL",
        f"重复条目={[(i.file.rel_path, i.dup_group_id, i.dup_count) for i in dups]}")
    empty = [i for i in plan2.items if i.file.filename == "空文件.txt"]
    rec("B5", "空文件不计入内容重复", "PASS" if empty and empty[0].dup_count == 1 else "FAIL",
        f"空文件 dup_count={empty[0].dup_count if empty else '未进入计划'}")

    before = tree_sha(root)
    plan_for(root, planner.MODE_BY_TYPE)
    after = tree_sha(root)
    rec("B6", "生成计划不改变磁盘文件", "PASS" if before == after else "FAIL",
        f"变更={[k for k in before if before.get(k) != after.get(k)][:5]}")


def group_execute_undo():
    root = build_clean_tree("exec_basic", 4)
    scan, plan = plan_for(root)
    before = {i.file.rel_path: sha(os.path.join(root, i.file.rel_path))
              for i in plan.items}
    st = open_storage(root)
    res = executor.execute_plan(plan, st, )
    moved_ok = res.moved == 4 and res.failed == 0
    sha_ok = all(os.path.exists(os.path.join(root, i.target_rel_path))
                 and sha(os.path.join(root, i.target_rel_path)) == before[i.file.rel_path]
                 for i in plan.items)
    src_gone = all(not os.path.exists(os.path.join(root, i.file.rel_path))
                   for i in plan.items)
    rec("C1", "执行 4 项：数量/内容/源清理",
        "PASS" if moved_ok and sha_ok and src_gone else "FAIL",
        f"结果={res.summary()}；SHA一致={sha_ok}；源已移走={src_gone}")

    undo = executor.undo_latest_batch(st)
    restored_ok = all(os.path.exists(os.path.join(root, i.file.rel_path))
                      and sha(os.path.join(root, i.file.rel_path)) == before[i.file.rel_path]
                      for i in plan.items)
    target_gone = all(not os.path.exists(os.path.join(root, i.target_rel_path))
                      for i in plan.items)
    rec("C2", "撤销 4 项：路径与内容恢复",
        "PASS" if undo.restored == 4 and restored_ok and target_gone else "FAIL",
        f"撤销={undo.summary()}；is_complete={undo.is_complete}；"
        f"SHA一致={restored_ok}；目标清空={target_gone}；"
        f"批次状态={st.get_batch(res.batch_id).status}")
    st.close()


def group_occupied():
    root = build_clean_tree("occupied", 2)
    scan, plan = plan_for(root)
    lock_path = os.path.join(root, plan.items[0].file.rel_path)
    handle = lock_exclusive(lock_path)
    st = open_storage(root)
    try:
        res = executor.execute_plan(plan, st)
        detail = f"结果={res.summary()}；错误={res.errors[:2]}"
        ok = res.moved == 1 and (res.failed + res.skipped) == 1
        rec("C3", "单文件被占用不中断整批", "PASS" if ok else "FAIL", detail)
        src_kept = os.path.exists(lock_path)
        rec("C4", "被占用文件保留原位置", "PASS" if src_kept else "FAIL",
            f"占用文件仍在={src_kept}")
    finally:
        unlock(handle)
        st.close()


def group_source_changed():
    root = build_clean_tree("changed", 2)
    scan, plan = plan_for(root)
    victim = plan.items[0]
    with open(os.path.join(root, victim.file.rel_path), "a", encoding="utf-8") as f:
        f.write("计划后追加")
    st = open_storage(root)
    res = executor.execute_plan(plan, st)
    skipped_ok = res.moved == 1 and res.skipped == 1
    rec("C5", "计划后源文件变化 → 跳过该项", "PASS" if skipped_ok else "FAIL",
        f"结果={res.summary()}；错误={res.errors[:2]}")
    st.close()


def group_target_preempted():
    root = build_clean_tree("preempt", 1)
    scan, plan = plan_for(root)
    item = plan.items[0]
    target = os.path.join(root, item.target_rel_path)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as f:
        f.write("执行前占位内容")
    st = open_storage(root)
    res = executor.execute_plan(plan, st)
    with open(target, encoding="utf-8") as f:
        content = f.read()
    rec("C6", "目标位置被抢占时不覆盖", "PASS" if content == "执行前占位内容" else "FAIL",
        f"结果={res.summary()}；目标内容={content!r}；错误={res.errors[:2]}")
    st.close()


def group_cancel():
    root = build_clean_tree("cancel", 30, prefix="批次")
    scan, plan = plan_for(root)
    st = open_storage(root)
    ev = threading.Event()

    def progress(i, total, rel):
        if i >= 3:
            ev.set()

    res = executor.execute_plan(plan, st, progress_cb=progress, cancel_event=ev)
    rec("C7", "执行中取消：移走 3、其余取消",
        "PASS" if res.moved == 3 and res.cancelled == 27 else "FAIL",
        f"结果={res.summary()}；batch={res.batch_id}；"
        f"批次状态={st.get_batch(res.batch_id).status}")
    undo = executor.undo_latest_batch(st)
    rec("C8", "取消后的批次仍可撤销已移动项",
        "PASS" if undo.restored == 3 else "FAIL",
        f"撤销={undo.summary()}；refused={undo.refused}；错误={undo.errors_list[:2]}")
    st.close()


def group_kill_recover():
    root = build_clean_tree("kill", 40, prefix="中断")
    flag = os.path.join(WORK, "kill_counter.txt")
    child = os.path.join(BASE, "_kill_child.py")
    env = os.environ.copy()
    proc = subprocess.Popen([sys.executable, child, root, APP, flag],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace",
                            env=env)
    deadline = time.time() + 60
    while time.time() < deadline:
        if os.path.exists(flag):
            try:
                with open(flag, encoding="utf-8") as f:
                    if int(f.read().strip() or 0) >= 5:
                        break
            except (OSError, ValueError):
                pass
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    proc.kill()
    try:
        out = proc.communicate(timeout=20)[0]
    except subprocess.TimeoutExpired:
        out = ""
    rec("D1", "执行中被强杀后进程确实终止", "PASS" if proc.returncode is not None else "FAIL",
        f"returncode={proc.returncode}；子进程输出尾部={out[-300:] if out else ''}")

    st = open_storage(root)
    before_status = st.get_latest_batch().status if st.get_latest_batch() else None
    rec_batch = executor.recover_batch(st)
    after_status = st.get_batch(rec_batch.batch_id).status if rec_batch.batch_id else None
    actions = st.get_actions(rec_batch.batch_id) if rec_batch.batch_id else []
    counts = {}
    for a in actions:
        counts[a.status] = counts.get(a.status, 0) + 1
    undo = executor.undo_latest_batch(st)
    converged = not undo.refused
    rec("D2", "中断恢复后批次可收束并允许撤销",
        "PASS" if converged else "FAIL",
        f"恢复前批次={before_status}；{rec_batch.summary()}；"
        f"恢复后={after_status}；条目状态={counts}；"
        f"撤销拒绝={undo.refused}；撤销信息={undo.errors_list[:1]}")
    st.close()


def group_cache():
    root = build_clean_tree("cache", 4)
    scan, plan = plan_for(root)
    files = {}
    for item in plan.items:
        ident = scan_cache.file_identity(os.path.join(root, item.file.rel_path))
        if ident:
            ident = dict(ident)
            ident["sha256"] = item.source_sha256
            ident["text"] = "缓存正文"
            ident["kind"] = "正文"
            files[item.file.rel_path.replace("\\", "/")] = ident

    direct_err = ""
    dir_open_ok = False
    os.makedirs(os.path.join(root, ".organizer"), exist_ok=True)
    try:
        fd = os.open(os.path.join(root, ".organizer"), os.O_RDONLY)
        dir_open_ok = True
        os.close(fd)
    except OSError as e:
        direct_err = f"{type(e).__name__}: errno={getattr(e, 'errno', None)} {e}"

    save_ok = True
    save_err = ""
    try:
        scan_cache.save(root, files)
    except Exception as e:
        save_ok = False
        save_err = f"{type(e).__name__}: errno={getattr(e, 'errno', None)} {e}"
    rec("E1", "Windows 上目录 fd 可打开（缓存写入前提）",
        "PASS" if dir_open_ok else "FAIL",
        f"os.open('.organizer') -> {direct_err or '成功'}")
    rec("E2", "增量扫描缓存能在 Windows 写入",
        "PASS" if save_ok else "FAIL",
        f"scan_cache.save -> {save_err or '成功'}；"
        f"缓存文件存在={os.path.exists(scan_cache.cache_path(root))}")

    if save_ok:
        loaded, note = scan_cache.load(root)
        rec("E3", "缓存可再次读回", "PASS" if loaded else "FAIL",
            f"note={note}；条目={len(loaded)}")
        # 内容改写但大小/mtime 不变 → ctime 变化
        rel = plan.items[0].file.rel_path
        p = os.path.join(root, rel)
        st0 = os.stat(p)
        with open(p, "r+", encoding="utf-8") as f:
            data = f.read()
            f.seek(0)
            f.write(("改" * len(data))[:len(data)].ljust(len(data)))
        os.utime(p, ns=(st0.st_atime_ns, st0.st_mtime_ns))
        cur = scan_cache.file_identity(p)
        old = files[rel.replace("\\", "/")]
        rec("E4", "同大小同 mtime 但内容变化会失效缓存",
            "PASS" if not scan_cache.identity_matches(old, cur) else "FAIL",
            f"旧 ctime={old.get('ctime_ns')} 新 ctime={(cur or {}).get('ctime_ns')}")
        with open(scan_cache.cache_path(root), "w", encoding="utf-8") as f:
            f.write("{损坏")
        loaded2, note2 = scan_cache.load(root)
        rec("E5", "损坏缓存回退全量扫描",
            "PASS" if not loaded2 and "损坏" in note2 else "FAIL", f"note={note2}")
    else:
        rec("E3", "缓存读回/失效/损坏回退", "UNTESTED", "缓存无法写入，后续项无法验证")

    # .organizer 为目录联接时不得写到根外
    root2 = fresh("cache_junction")
    write(root2, "a.txt", "a")
    outside = os.path.join(WORK, "cache_outside")
    os.makedirs(outside, exist_ok=True)
    org = os.path.join(root2, ".organizer")
    subprocess.run(["cmd", "/c", "mklink", "/J", org, outside],
                   capture_output=True, text=True)
    escaped = True
    escape_msg = ""
    try:
        scan_cache.save(root2, files)
        escape_msg = "save 未拒绝"
    except Exception as e:
        escaped = not os.listdir(outside)
        escape_msg = f"{type(e).__name__}: {e}"
    if os.path.isdir(org) and os.path.islink(org) is False:
        subprocess.run(["cmd", "/c", "rmdir", org], capture_output=True)
    rec("E6", ".organizer 被换成根外联接时不写出根外",
        "PASS" if escaped and not os.listdir(outside) else "FAIL",
        f"{escape_msg}；根外目录内容={os.listdir(outside)}")


def group_content():
    root = fresh("content")
    docx = write(root, "课程/报告.docx", b"")
    make_docx(docx, "数据结构 实验一 线性表")
    pptx = write(root, "课程/答辩.pptx", b"")
    make_pptx(pptx, "高等数学 期末展示")
    xlsx = write(root, "课程/成绩.xlsx", b"")
    make_xlsx(xlsx, "概率论 成绩单")
    write(root, "课程/损坏.docx", b"not a zip")
    write(root, "课程/说明.pdf", b"%PDF-1.4 fake")
    write(root, "课程/普通.txt", "普通正文内容")
    files = [scanner.FileEntry(
        abs_path=os.path.join(root, rel.replace("/", os.sep)),
        rel_path=rel.replace("/", os.sep),
        filename=os.path.basename(rel),
        file_type=scanner.classify_type(os.path.basename(rel)),
        size=os.path.getsize(os.path.join(root, rel.replace("/", os.sep))),
        size_display="", mtime_ns=0)
        for rel in ["课程/报告.docx", "课程/答辩.pptx", "课程/成绩.xlsx",
                    "课程/损坏.docx", "课程/说明.pdf", "课程/普通.txt"]]
    extracted = content_extract.extract_contents(files, root=root)
    texts, errors = extracted.texts, extracted.errors
    rec("F1", "docx/pptx/xlsx 正文解析",
        "PASS" if all(any(k.endswith(e) for k in texts) for e in
                      [".docx", ".pptx", ".xlsx"]) else "FAIL",
        f"texts={ {k: v[:20] for k, v in texts.items()} }")
    rec("F2", "损坏 Office/PDF 给原因而不是静默失败",
        "PASS" if len(errors) >= 2 else "FAIL",
        f"errors={errors}")
    rec("F3", "普通 txt 仍可读取",
        "PASS" if any(k.endswith(".txt") for k in texts) else "FAIL",
        f"txt 摘录={[v[:12] for k, v in texts.items() if k.endswith('.txt')]}")

    # 正文读取不越界
    outside = os.path.join(WORK, "content_outside")
    os.makedirs(outside, exist_ok=True)
    op = os.path.join(outside, "外部.txt")
    with open(op, "w", encoding="utf-8") as f:
        f.write("根外正文")
    link = os.path.join(root, "课程/外链.txt")
    try:
        os.symlink(op, link)
    except OSError as e:
        rec("F4", "正文读取不跟随根外链接", "UNTESTED", f"无法创建符号链接：{e}")
    else:
        text, note, reached = content_extract.read_text_excerpt(link)
        rec("F4", "正文读取不跟随根外链接",
            "PASS" if "根外正文" not in (text or "") else "FAIL",
            f"读取结果={text!r}；说明={note}")


def group_preferences():
    root = fresh("prefs")
    write(root, "课程/笔记.txt", "笔记")
    pref = preferences.Preference(
        preference_id="pref-test-1",
        name="测试偏好",
        match_type=preferences.MATCH_DIR_SCOPE,
        source_path="课程",
        include_subdirs=True,
        keywords=[],
        direction="课程",
        semester="2026秋-大二上",
        category="数据结构",
        reason="实测",
        confirmed_at=datetime.now().isoformat(timespec="seconds"),
        source_kind=preferences.SOURCE_USER,
        scope=preferences.SCOPE_SPACE,
        space_root=root,
    )
    saved = True
    err = ""
    try:
        preferences.save_preference(root, pref)
    except Exception as e:
        saved = False
        err = f"{type(e).__name__}: {e}"
    rec("G1", "偏好可保存到空间偏好库", "PASS" if saved else "FAIL",
        f"{err or '已保存'}；库路径={preferences.space_db_path(root)}")
    if saved:
        lst = preferences.list_space_preferences(root)
        rec("G2", "偏好可再次读取", "PASS" if any(p.preference_id == "pref-test-1" for p in lst) else "FAIL",
            f"条目={[(p.preference_id, p.name) for p in lst]}")
        preferences.set_preference_enabled(root, "pref-test-1", False)
        eff = preferences.list_effective_preferences(root)
        rec("G3", "停用后不出现在生效偏好里",
            "PASS" if not any(p.preference_id == "pref-test-1" for p in eff) else "FAIL",
            f"生效条目={[p.preference_id for p in eff]}")
        bad = preferences.Preference(preference_id="bad", name="bad",
                                     direction="不存在的方向")
        rejected = False
        try:
            preferences.save_preference(root, bad)
        except preferences.PreferenceError:
            rejected = True
        rec("G4", "非法方向被校验拒绝", "PASS" if rejected else "FAIL", "")
        deleted = preferences.delete_preference(root, "pref-test-1")
        rec("G5", "偏好可删除", "PASS" if deleted else "FAIL", "")

    # JSON 长期规则一次性迁移
    from classification_config import (
        ClassificationConfig, LongTermRule, default_config, load_config,
        save_config,
    )
    root2 = fresh("prefs_migrate")
    write(root2, "课程/笔记.txt", "x")
    cfg = default_config()
    cfg.long_term_rules.append(LongTermRule(
        rule_id="lt-migrate-1", name="开发文档按项目",
        match_type=preferences.MATCH_DIR_SCOPE, source_path="课程",
        direction="课程", semester="2026秋-大二上", category="数据结构",
        reason="迁移测试"))
    save_config(root2, cfg)
    loaded_cfg, source = load_config(root2)
    inserted = preferences.ensure_migrated(root2, loaded_cfg)
    again = preferences.ensure_migrated(root2, loaded_cfg)
    after_cfg, _ = load_config(root2)
    kept = [r.rule_id for r in after_cfg.long_term_rules]
    rec("G6", "JSON 长期规则一次性迁移且保留原 JSON",
        "PASS" if inserted == 1 and again == 0 and "lt-migrate-1" in kept else "FAIL",
        f"首次插入={inserted}，再次={again}，JSON 规则={kept}，来源={source}")


def group_duplicates():
    root = fresh("dups")
    write(root, "下载/甲.txt", "重复内容" * 30)
    write(root, "下载/乙.txt", "重复内容" * 30)
    write(root, "其他/丙.txt", "重复内容" * 30)
    write(root, "下载/同名.txt", "版本一")
    write(root, "其他/同名.txt", "版本二")
    write(root, "下载/空1.txt", "")
    write(root, "其他/空2.txt", "")
    scan, plan = plan_for(root)
    entries = duplicate_report.entries_from_plan(plan, scan)
    duplicate_report.verify_entries(entries)
    report = duplicate_report.assemble_report(
        root, entries, scope_note="整个整理空间", source="plan")
    content = [g for g in report.groups if g.kind == duplicate_report.KIND_CONTENT]
    same = [g for g in report.groups if g.kind == duplicate_report.KIND_SAME_NAME_DIFF]
    rec("H1", "内容重复分组（空文件不计）",
        "PASS" if len(content) == 1 and len(content[0].entries) == 3 else "FAIL",
        f"内容组={[(g.label, [e.rel_path for e in g.entries]) for g in content]}")
    rec("H2", "同名不同内容分组",
        "PASS" if len(same) == 1 and len(same[0].entries) == 2 else "FAIL",
        f"同名组={[(g.label, [e.rel_path for e in g.entries]) for g in same]}")
    before = tree_sha(root)
    csv_text = duplicate_report.export_csv_text(report)
    json_text = duplicate_report.export_json_text(report)
    rec("H3", "导出不改变文件", "PASS" if before == tree_sha(root) else "FAIL",
        f"CSV {len(csv_text)} 字符 / JSON {len(json_text)} 字符")


def group_ocr():
    rec("I1", "OCR 引擎可用性", "INFO",
        f"supported={ocr_engine.ocr_supported()}；label={ocr_engine.engine_label()}；"
        f"候选 png={ocr_engine.is_ocr_candidate('a.png', 100)}；"
        f"候选 txt={ocr_engine.is_ocr_candidate('a.txt', 100)}")
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        rec("I2", "图片中文识别", "UNTESTED", "没有 Pillow")
        return
    root = fresh("ocr")
    path = os.path.join(root, "截图.png")
    img = Image.new("RGB", (900, 260), "white")
    draw = ImageDraw.Draw(img)
    font_path = r"C:\Windows\Fonts\msyh.ttc"
    font = ImageFont.truetype(font_path, 64)
    draw.text((40, 80), "高等数学 作业 第三章", fill="black", font=font)
    img.save(path)
    try:
        text = ocr_engine.recognize_image(path)
        rec("I2", "Windows 原生 OCR 识别中文截图（记录原文）", "INFO",
            f"识别结果={text!r}")
    except Exception as e:
        rec("I2", "Windows 原生 OCR 识别中文截图", "FAIL",
            f"{type(e).__name__}: {e}")
        return
    # OCR 文本是否还能被关键词命中
    root2 = fresh("ocr_plan")
    img = os.path.join(root2, "下载", "截图.png")
    os.makedirs(os.path.dirname(img), exist_ok=True)
    img_obj = Image.new("RGB", (900, 260), "white")
    ImageDraw.Draw(img_obj).text((40, 80), "高等数学 作业 第三章",
                                 fill="black", font=font)
    img_obj.save(img)
    scan2 = scanner.scan_directory(root2)
    rel = [f.rel_path for f in scan2.files if f.filename == "截图.png"]
    if not rel:
        rec("I3", "OCR 文本参与分类", "FAIL", "截图未进入扫描")
        return
    rel = rel[0]
    plan_raw = planner.generate_plan(
        scan2, planner.MODE_BY_COURSE_THEN_TYPE,
        list(planner.DEFAULT_COURSE_RULES),
        ocr_texts={rel: text})
    item_raw = [i for i in plan_raw.items if i.file.filename == "截图.png"][0]
    normalized = "".join(text.split())
    plan_norm = planner.generate_plan(
        scan2, planner.MODE_BY_COURSE_THEN_TYPE,
        list(planner.DEFAULT_COURSE_RULES),
        ocr_texts={rel: normalized})
    item_norm = [i for i in plan_norm.items if i.file.filename == "截图.png"][0]
    rec("I3", "OCR 识别文本能被课程关键词命中",
        "PASS" if "高等数学" in (item_raw.course or "") else "FAIL",
        f"OCR 原文→课程={item_raw.course!r}；去空格后→课程={item_norm.course!r}；"
        f"原文={text!r}")


def group_ai():
    settings = ai_fallback.load_settings()
    rec("J1", "AI 默认关闭且无密钥", "PASS" if not settings.enabled and not settings.ready() else "FAIL",
        f"enabled={settings.enabled}；ready={settings.ready()}")
    root = build_clean_tree("ai", 2)
    scan, plan = plan_for(root, planner.MODE_BY_COURSE_THEN_TYPE)
    called = []

    def forbidden_transport(*_a, **_k):
        called.append(True)
        raise AssertionError("AI 关闭时不应发起网络请求")

    msg = ai_fallback.apply_ai_fallback(plan, settings, transport=forbidden_transport,
                                        require_evidence=True)
    rec("J2", "AI 关闭时不发起请求、不推翻本地分类",
        "PASS" if not called else "FAIL", f"提示={msg}；网络调用={bool(called)}")


def group_long_readonly():
    root = fresh("longmove")
    deep = os.path.join(root, *(["一二三四五六七八九十"] * 20))
    os.makedirs("\\\\?\\" + deep)
    src = os.path.join(deep, "超长路径作业.txt")
    with open("\\\\?\\" + src, "w", encoding="utf-8") as f:
        f.write("超长路径内容")
    before = sha(src)
    scan, plan = plan_for(root)
    rel_len = max((len(i.file.rel_path) for i in plan.items), default=0)
    st = open_storage(root)
    res = executor.execute_plan(plan, st)
    ok = res.moved == 1 and res.failed == 0
    rec("C9", "长路径（绝对路径 > 260）移动",
        "PASS" if ok else "FAIL",
        f"rel_len={rel_len}；结果={res.summary()}；错误={res.errors[:2]}")
    if ok:
        undo = executor.undo_latest_batch(st)
        restored = os.path.exists("\\\\?\\" + src) and sha("\\\\?\\" + src) == before
        rec("C10", "长路径撤销", "PASS" if restored else "FAIL",
            f"撤销={undo.summary()}；恢复={restored}")
    st.close()

    root2 = fresh("readonly")
    p = write(root2, "下载/只读.txt", "只读内容")
    os.chmod(p, stat.S_IREAD)
    scan2, plan2 = plan_for(root2)
    st2 = open_storage(root2)
    res2 = executor.execute_plan(plan2, st2)
    rec("C11", "只读文件可移动", "PASS" if res2.moved == 1 else "FAIL",
        f"结果={res2.summary()}；错误={res2.errors[:2]}")
    st2.close()

    # 撤销时原位置已被新文件占用：不覆盖新文件
    root4 = fresh("undo_conflict")
    p4 = write(root4, "下载/原件.txt", "原件内容")
    scan4, plan4 = plan_for(root4)
    st4 = open_storage(root4)
    executor.execute_plan(plan4, st4)
    with open(p4, "w", encoding="utf-8") as f:
        f.write("撤销前新写入的内容")
    undo4 = executor.undo_latest_batch(st4)
    with open(p4, encoding="utf-8") as f:
        now = f.read()
    rec("C13", "撤销遇到原位置占用时不覆盖新文件",
        "PASS" if now == "撤销前新写入的内容" else "FAIL",
        f"撤销={undo4.summary()}；conflict={undo4.conflict}；"
        f"原位置内容={now!r}")
    st4.close()

    # ACL 拒绝读取
    root3 = fresh("acl")
    p3 = write(root3, "下载/拒绝读取.txt", "拒绝读取内容")
    user = os.environ.get("USERNAME", "")
    deny = subprocess.run(["icacls", p3, "/deny", f"{user}:(R)"],
                          capture_output=True, text=True)
    if deny.returncode != 0:
        rec("C12", "ACL 拒绝读取的文件跳过并继续", "UNTESTED",
            f"icacls 失败：{deny.stdout} {deny.stderr}")
    else:
        try:
            scan3, plan3 = plan_for(root3)
            st3 = open_storage(root3)
            res3 = executor.execute_plan(plan3, st3)
            skipped = res3.skipped + res3.failed
            rec("C12", "ACL 拒绝读取的文件跳过并继续",
                "PASS" if skipped == 1 and res3.moved == 0 else "FAIL",
                f"结果={res3.summary()}；错误={res3.errors[:2]}")
            st3.close()
        finally:
            subprocess.run(["icacls", p3, "/remove:d", user],
                           capture_output=True, text=True)


def main():
    groups = [group_scan, group_plan, group_execute_undo, group_occupied,
              group_source_changed, group_target_preempted, group_cancel,
              group_kill_recover, group_cache, group_content,
              group_preferences, group_duplicates, group_ocr, group_ai,
              group_long_readonly]
    for fn in groups:
        try:
            fn()
        except Exception:
            rec(fn.__name__, f"{fn.__name__} 执行异常", "FAIL",
                traceback.format_exc())
    summary = {}
    for c in CHECKS:
        summary[c["status"]] = summary.get(c["status"], 0) + 1
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "work_dir": WORK,
        "summary": summary,
        "checks": CHECKS,
    }
    out = os.path.join(OUT, "engine_results_round2.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print("\n=== 合计 %d 项：PASS %d / FAIL %d / INFO %d / UNTESTED %d ===" % (
        len(CHECKS), summary.get("PASS", 0), summary.get("FAIL", 0),
        summary.get("INFO", 0), summary.get("UNTESTED", 0)))
    print("结果文件：" + out)


if __name__ == "__main__":
    main()
