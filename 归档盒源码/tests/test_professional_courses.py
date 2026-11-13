"""各专业专业课：课名归到本课，竞赛全名仍归竞赛。"""
from __future__ import annotations

import os
import sys

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SRC)

from classification_config import (  # noqa: E402
    classify_context,
    compile_config,
    default_config,
    validate_config,
)
from general_catalog import GENERAL_COURSE_RULES  # noqa: E402
from professional_catalog import PROFESSIONAL_COURSE_RULES  # noqa: E402


def main() -> None:
    errors = validate_config(default_config())
    if errors:
        raise SystemExit("默认配置不合法：\n" + "\n".join(errors))

    compiled = compile_config(default_config())
    bad = []
    seen = set()
    for keywords, category in GENERAL_COURSE_RULES + PROFESSIONAL_COURSE_RULES:
        for keyword in keywords:
            if keyword in seen:
                bad.append(f"关键词重复：{keyword}")
                continue
            seen.add(keyword)
            match = classify_context(f"{keyword}.pdf", compiled)
            if match.direction != "课程" or match.category != category:
                bad.append(
                    f"{keyword} → {match.direction}/{match.category}，期望 课程/{category}")
    samples = {
        "材料科学基础第一章.pdf": ("课程", "材料科学基础"),
        "微观经济学笔记.pdf": ("课程", "微观经济学"),
        "中级财务会计习题.pdf": ("课程", "中级财务会计"),
        "机械原理作业.pdf": ("课程", "机械原理"),
        "系统解剖学图谱.pdf": ("课程", "系统解剖学"),
        "材料热处理创新创业赛.pdf": (
            "竞赛", "中国大学生机械工程创新创意大赛-材料热处理创新创业赛"),
        "高数作业.pdf": ("课程", "高等数学"),
    }
    for name, (direction, category) in samples.items():
        match = classify_context(name, compiled)
        if match.direction != direction or match.category != category:
            bad.append(
                f"{name} → {match.direction}/{match.category}，期望 {direction}/{category}")
    if bad:
        raise SystemExit("专业课规则不符合预期：\n" + "\n".join(bad[:40]))
    print(f"专业课关键词 {len(seen)} 个，抽样通过")


if __name__ == "__main__":
    main()
