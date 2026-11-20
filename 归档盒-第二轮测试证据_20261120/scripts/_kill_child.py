# -*- coding: utf-8 -*-
"""在子进程里执行整理，供父进程在移动过程中强杀。"""
import os
import sys

root, app, flag = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, app)

from scanner import scan_directory          # noqa: E402
from planner import generate_plan, DEFAULT_COURSE_RULES, MODE_BY_TYPE  # noqa: E402
from storage import Storage                 # noqa: E402
from executor import execute_plan           # noqa: E402

scan = scan_directory(root)
plan = generate_plan(scan, MODE_BY_TYPE, list(DEFAULT_COURSE_RULES))
st = Storage(root)


def progress(i, total, rel):
    with open(flag, "w", encoding="utf-8") as f:
        f.write(str(i))


try:
    result = execute_plan(plan, st, progress_cb=progress)
    print("child finished", result.summary())
finally:
    st.close()
