#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地红队攻击轮（judge-deep 语义模拟）：对 spec 施加六向量攻击改写，
逐轮验证套件拦截（每轮攻击变体跑 run-suite.sh 应 exit 非 0）。"""
import os
import shutil
import subprocess
import sys
import tempfile

SRC = "/tmp/vr/specs/IR-0001"
ATTACKS = {
    "S1'-摆拍式AC": [
        ("产出符合要求的结构化输出，满足需求", None),
    ],
    "S2-义务降级": [
        ("系统必须调用 ASR 引擎产出带秒级或毫秒级时间戳的分段转写文本。",
         "系统原则上须调用 ASR 引擎产出带时间戳的转写文本。"),
    ],
    "S3-义务转嫁": [
        ("系统必须执行 OCR 并提取画面花字、字幕与图片内嵌文案三类文本。",
         "系统必须提供检查单供人工填写花字、字幕与图片内嵌文案三类文本。"),
    ],
    "S4-时态后移": [
        ("系统必须记录失败状态、跳过该条并继续同批次其他内容",
         "系统必须确认失败状态已被记录、核验该条已被跳过"),
    ],
    "S5-逃生舱": [
        ("单条重试次数不得超过 BUDGET-2。",
         "单条重试次数不得超过 BUDGET-2，资源受限时按余量折算执行。"),
    ],
    "S6-前置堆叠": [
        ("当采集通道对指定账号执行采集时，",
         "当采集通道对指定账号执行采集且配套资产预检通过时，"),
    ],
}


def run_suite(impl_dir):
    r = subprocess.run(["bash", os.path.join(SRC, "run-suite.sh"), impl_dir],
                       capture_output=True, text=True)
    return r.returncode


def main():
    base = open(os.path.join(SRC, "spec.md"), encoding="utf-8").read()
    results = []
    for name, patches in ATTACKS.items():
        variant = base
        for old, new in patches:
            if new is None:
                # S1'：把所有 then 的内容换成空洞短语（最偷懒实现）
                import re
                variant = re.sub(r"then: .+", "then: 产出符合要求的结构化输出，满足需求", variant)
            else:
                assert old in variant, f"{name}: 攻击锚点不存在: {old[:30]}"
                variant = variant.replace(old, new)
        with tempfile.TemporaryDirectory() as td:
            impl = os.path.join(td, "impl")
            os.makedirs(impl)
            with open(os.path.join(impl, "spec.md"), "w", encoding="utf-8") as f:
                f.write(variant)
            rc = run_suite(impl)
        verdict = "拦截(exit=%d)" % rc if rc != 0 else "!! 得手(exit=0)——套件不足"
        results.append((name, rc))
        print(f"{name}: {verdict}")
    failed = [n for n, rc in results if rc == 0]
    if failed:
        print("\n攻击得手向量:", failed)
        sys.exit(1)
    print("\n全部攻击向量被拦截（%d/%d）" % (len(results), len(results)))
    # 正控制：未篡改 spec 必须全绿
    with tempfile.TemporaryDirectory() as td:
        impl = os.path.join(td, "impl")
        os.makedirs(impl)
        shutil.copy(os.path.join(SRC, "spec.md"), os.path.join(impl, "spec.md"))
        rc = run_suite(impl)
    print("正控制（未篡改 spec）: exit=%d（应为 0）" % rc)
    sys.exit(0 if rc == 0 else 1)


if __name__ == "__main__":
    main()
