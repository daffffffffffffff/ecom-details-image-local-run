#!/usr/bin/env python3
"""把本地 13 行产物拼成总览图，并与 SkillBot 那 13 行的产物并排。

产物：
  contact-sheet-local.png         13 行 × 4 图（本地）
  contact-sheet-vs-skillbot.png   4 个步骤分块，每块上下两排（SkillBot / 本地）× 13 行
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

RUN_DIR = Path(__file__).resolve().parent.parent
OUT = RUN_DIR / "out"
SKILLBOT_DIR = RUN_DIR.parent / "loomloom-ecom-run"
SKILLBOT_ROWS = SKILLBOT_DIR / "rows-v3-13.json"
SKILLBOT_ARTIFACTS = SKILLBOT_DIR / "artifacts-v3-13"

STEPS = ["stp_t2imain", "stp_i2imain", "stp_i2idetl", "stp_i2ibnner"]
STEP_LABEL = {
    "stp_t2imain": "文生图样张",
    "stp_i2imain": "主图(参考图)",
    "stp_i2idetl": "详情图(参考图)",
    "stp_i2ibnner": "Banner(参考图)",
}
ROWS = list(range(1, 14))

FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
]
BG = (24, 24, 27)
FG = (235, 235, 240)
MUTED = (150, 150, 160)


def font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def skillbot_map() -> dict[tuple[int, str], Path]:
    data = json.loads(SKILLBOT_ROWS.read_text(encoding="utf-8"))
    mapping: dict[tuple[int, str], Path] = {}
    for row in data["rows"]:
        for art in row["artifacts"]:
            if not art["mimeType"].endswith("png"):
                continue
            path = SKILLBOT_ARTIFACTS / f"0000-{art['artifactId']}.png"
            if path.exists():
                mapping[(row["rowIndex"] + 1, art["stepId"])] = path
    return mapping


def thumb(img: Image.Image, width: int) -> Image.Image:
    ratio = width / img.width
    return img.convert("RGB").resize((width, max(1, round(img.height * ratio))), Image.LANCZOS)


def paste_cell(canvas: Image.Image, img_path: Path | None, box: tuple[int, int, int, int],
               label: str, fnt: ImageFont.FreeTypeFont) -> None:
    x0, y0, x1, y1 = box
    draw = ImageDraw.Draw(canvas)
    draw.rectangle(box, outline=(60, 60, 68), width=1)
    draw.text((x0 + 6, y0 + 4), label, font=fnt, fill=MUTED)
    inner_w = x1 - x0 - 12
    inner_y = y0 + 8 + fnt.size + 4
    inner_h = y1 - inner_y - 6
    if img_path is None or not img_path.exists():
        draw.text((x0 + 10, inner_y + 10), "(缺失)", font=fnt, fill=(220, 100, 100))
        return
    with Image.open(img_path) as raw:
        pic = thumb(raw, inner_w)
    if pic.height > inner_h:
        ratio = inner_h / pic.height
        pic = pic.resize((max(1, round(pic.width * ratio)), inner_h), Image.LANCZOS)
    canvas.paste(pic, (x0 + 6 + (inner_w - pic.width) // 2, inner_y))


def build_local_sheet() -> Path:
    cell_w, cell_h, gap, pad = 300, 400, 10, 70
    width = pad * 2 + len(STEPS) * cell_w + (len(STEPS) - 1) * gap
    header = 116
    height = header + len(ROWS) * cell_h + (len(ROWS) - 1) * gap + pad
    canvas = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(canvas)
    f_big, f_small = font(30), font(15)
    draw.text((pad, 24), "本地复刻 · ecom-details-image Pro 强构 · 13 行 × 4 图", font=f_big, fill=FG)
    draw.text((pad, 64), "上游脚本本地增强版 + CogFoundry 路由 | 文本 google/gemini-2.5-pro | 图像 openai/gpt-image-2",
              font=f_small, fill=MUTED)
    for col, step in enumerate(STEPS):
        x = pad + col * (cell_w + gap)
        draw.text((x + 6, header - 26), STEP_LABEL[step], font=f_small, fill=FG)
    for idx, row in enumerate(ROWS):
        y = header + idx * (cell_h + gap)
        draw.text((10, y + cell_h // 2), f"{row:02d}", font=f_small, fill=FG)
        for col, step in enumerate(STEPS):
            x = pad + col * (cell_w + gap)
            paste_cell(canvas, OUT / f"row-{row:02d}" / f"{step}.png",
                       (x, y, x + cell_w, y + cell_h), "", f_small)
    path = RUN_DIR / "contact-sheet-local.png"
    canvas.save(path)
    return path


def build_compare_sheet() -> Path:
    skillbot = skillbot_map()
    cell_w, cell_h, gap = 190, 250, 6
    pad, label_w = 24, 108
    block_h = cell_h * 2 + gap + 34
    width = pad * 2 + label_w + len(ROWS) * cell_w + (len(ROWS) - 1) * gap
    height = 108 + len(STEPS) * block_h + (len(STEPS) - 1) * 28 + pad
    canvas = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(canvas)
    f_big, f_small = font(28), font(13)
    draw.text((pad, 22), "SkillBot vs 本地复刻 · 同 13 行 · 同位置并排", font=f_big, fill=FG)
    draw.text((pad, 60), "每个步骤一块：上排 = 平台 SkillBot（v3，openai/gpt-image-2）｜下排 = 本地复刻",
              font=f_small, fill=MUTED)
    for col, row in enumerate(ROWS):
        x = pad + label_w + col * (cell_w + gap)
        draw.text((x + 4, 86), f"{row:02d}", font=f_small, fill=MUTED)
    for bi, step in enumerate(STEPS):
        by = 108 + bi * (block_h + 28)
        draw.text((pad, by + 6), STEP_LABEL[step], font=font(17), fill=FG)
        for half, (tag, getter) in enumerate((
            ("SkillBot", lambda r, s=step: skillbot.get((r, s))),
            ("本地", lambda r, s=step: OUT / f"row-{r:02d}" / f"{s}.png"),
        )):
            y = by + 30 + half * (cell_h + gap)
            draw.text((pad, y + cell_h // 2), tag, font=f_small,
                      fill=FG if half == 0 else (140, 220, 180))
            for col, row in enumerate(ROWS):
                x = pad + label_w + col * (cell_w + gap)
                paste_cell(canvas, getter(row), (x, y, x + cell_w, y + cell_h), "", f_small)
    path = RUN_DIR / "contact-sheet-vs-skillbot.png"
    canvas.save(path)
    return path


def build_local_only_sheet() -> Path:
    """只放本地这批：4 个步骤各一块，每块 13 行横排，缩略图比对比图更大。"""
    ref_path = RUN_DIR / "reference-product.jpg"
    cell_w, cell_h, gap = 200, 262, 6
    pad, label_w = 24, 132
    block_h = cell_h + 36
    width = pad * 2 + label_w + len(ROWS) * cell_w + (len(ROWS) - 1) * gap
    height = 132 + len(STEPS) * block_h + (len(STEPS) - 1) * 22 + pad
    canvas = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(canvas)
    f_big, f_small, f_mid = font(28), font(13), font(17)
    draw.text((pad, 24), "本地复刻 · 13 行全套效果", font=f_big, fill=FG)
    draw.text((pad, 62), "上游脚本本地增强版 + CogFoundry 路由 | 文本 google/gemini-2.5-pro | "
                         "图像 openai/gpt-image-2（与 SkillBot 同模型）| 13/13 成功 · 52 张",
              font=f_small, fill=MUTED)
    if ref_path.exists():
        with Image.open(ref_path) as raw:
            pic = thumb(raw, 70)
        canvas.paste(pic, (width - pad - pic.width, 22))
        draw.text((width - pad - pic.width - 118, 44), "参考图 →", font=f_small, fill=MUTED)
    for col, row in enumerate(ROWS):
        x = pad + label_w + col * (cell_w + gap)
        draw.text((x + 4, 100), f"{row:02d}", font=f_small, fill=MUTED)
    for bi, step in enumerate(STEPS):
        by = 132 + bi * (block_h + 22)
        draw.text((pad, by + cell_h // 2), STEP_LABEL[step], font=f_mid, fill=FG)
        for col, row in enumerate(ROWS):
            x = pad + label_w + col * (cell_w + gap)
            paste_cell(canvas, OUT / f"row-{row:02d}" / f"{step}.png",
                       (x, by, x + cell_w, by + cell_h), "", f_small)
    path = RUN_DIR / "contact-sheet-local-only.png"
    canvas.save(path)
    return path


def main() -> int:
    for path in (build_local_only_sheet(), build_local_sheet(), build_compare_sheet()):
        with Image.open(path) as img:
            print(f"{path.name}  {img.width}x{img.height}  {path.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
