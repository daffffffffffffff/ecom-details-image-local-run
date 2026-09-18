#!/usr/bin/env python3
"""从 SkillBot 的 TemplateSpec 里逐字提取 3 条 systemInstruction + 4 个图像步骤的接线。

为什么要脚本提取而不是手抄：
  这几条提示词是 SkillBot 线上跑的那一份，手抄会漂移。runner 只读本脚本的产物。

输入：../loomloom-ecom-run/ecom-pro-v4.spec.json   （SkillBot「ecom-details-image Pro 强构 v4」）
输出：prompts/system-instructions.json
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUN_DIR = HERE.parent
SPEC = RUN_DIR.parent / "loomloom-ecom-run" / "ecom-pro-v4.spec.json"
OUT = RUN_DIR / "prompts" / "system-instructions.json"

# 尺寸照 SkillBot 实测产出比例定（见 REPORT.md / 计划）：
#   stp_t2imain  ≈3:2   stp_i2imain ≈3:4   stp_i2idetl =3:4（13/13）   stp_i2ibnner ≈16:9
IMAGE_STEP_PLAN = {
    "stp_t2imain": {"size": "3:2", "with_reference": False},
    "stp_i2imain": {"size": "3:4", "with_reference": True},
    "stp_i2idetl": {"size": "3:4", "with_reference": True},
    "stp_i2ibnner": {"size": "16:9", "with_reference": True},
}


def main() -> int:
    if not SPEC.exists():
        print(f"找不到 spec：{SPEC}", file=sys.stderr)
        return 1

    raw = SPEC.read_bytes()
    spec = json.loads(raw)

    prompt_steps = {}
    image_steps = {}
    for step in spec["steps"]:
        sid = step["stepId"]
        binding = step.get("executionBinding", {})
        if binding.get("kind") == "capabilityProfile":
            literal = step["inputBindings"]["systemInstruction"]["value"]
            prompt_steps[sid] = {
                "displayName": step["displayName"],
                "profileId": binding.get("profileId"),
                "systemInstruction": literal,
                "systemInstructionSha256": hashlib.sha256(literal.encode("utf-8")).hexdigest(),
                "userTextInputKey": step["inputBindings"]["prompt"]["inputKey"],
                "userImageInputKey": step["inputBindings"]["image"]["inputKey"],
                "dependsOn": step.get("dependsOn", []),
            }
        elif binding.get("kind") == "fixedModelContract":
            plan = IMAGE_STEP_PLAN.get(sid)
            if plan is None:
                print(f"未登记的图像步骤 {sid}，请补 IMAGE_STEP_PLAN", file=sys.stderr)
                return 1
            ports = sorted((step.get("inputBindings") or {}).keys())
            image_steps[sid] = {
                "displayName": step["displayName"],
                "subjectRevisionId": binding.get("subjectRevisionId"),
                "promptSourceStepId": step["inputBindings"]["prompt"]["stepId"],
                "dependsOn": step.get("dependsOn", []),
                "inputPorts": ports,
                **plan,
            }

    doc = {
        "source": str(SPEC.relative_to(RUN_DIR.parent)),
        "sourceSha256": hashlib.sha256(raw).hexdigest(),
        "specName": spec["meta"]["name"],
        "templateInputs": {
            k: {"valueType": v.get("valueType"), "kind": v.get("kind")}
            for k, v in spec.get("templateInputs", {}).items()
        },
        "promptSteps": prompt_steps,
        "imageSteps": image_steps,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"写入 {OUT}")
    print(f"  spec sha256 = {doc['sourceSha256']}")
    print(f"  文本步骤 {len(prompt_steps)} 个：{', '.join(prompt_steps)}")
    print(f"  图像步骤 {len(image_steps)} 个：{', '.join(image_steps)}")
    for sid, s in prompt_steps.items():
        print(f"    {sid:14s} {len(s['systemInstruction']):4d} 字符  sha={s['systemInstructionSha256'][:12]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
