#!/usr/bin/env python3
"""本地复刻 SkillBot「ecom-details-image Pro 强构」的 7 步流程，13 行并行执行。

流程（与 SkillBot 的 steps 一一对应，定义来自 prompts/system-instructions.json）：
  3 个文本步骤（看图写 Prompt）  stp_prmhero / stp_prmdetl / stp_prmbnner
  4 个图像步骤                   stp_t2imain / stp_i2imain / stp_i2idetl / stp_i2ibnner

图像步骤用 GitHub 原脚本 scripts/generate_image.py（逐字节复制，走 cogfoundry 模式）；
文本步骤直接用路由的 OpenAI 兼容 /chat/completions。

跑法：
  python3 run_13rows.py --only-row 1                  # 冒烟，1 行
  python3 run_13rows.py                               # 13 行全量
  python3 run_13rows.py --image-concurrency 3         # 代理不稳时降并发
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

RUN_DIR = Path(__file__).resolve().parent

# 优先读本目录的 .env（开源后别人 clone 下来只需在这一处配 key）；
# 找不到时回落到本机 skill 目录的 .env。
ENV_CANDIDATES = [
    RUN_DIR / ".env",
    RUN_DIR.parent / ".agents" / "skills" / "ecom-details-image" / ".env",
]
SKILL_ENV = next((path for path in ENV_CANDIDATES if path.exists()), ENV_CANDIDATES[0])

PROMPTS = json.loads((RUN_DIR / "prompts" / "system-instructions.json").read_text(encoding="utf-8"))
OUT_DIR = RUN_DIR / "out"
LOG_PATH = RUN_DIR / "run-log.jsonl"

DEFAULT_TEXT_MODEL = "google/gemini-2.5-pro"
PRIMARY_IMAGE_MODEL = "openai/gpt-image-2"
FALLBACK_IMAGE_MODEL = "openai/gpt-image-2.5-flare"

_log_lock = threading.Lock()
_model_lock = threading.Lock()
_costs: list[float] = []
_image_sem: threading.Semaphore | None = None
_current_image_model = PRIMARY_IMAGE_MODEL
_model_switched = False

MODEL_ERROR_HINTS = (
    "unknown parameter",
    "model not found",
    "does not exist",
    "unsupported model",
    "invalid model",
    "no such model",
    "upstream api error",
)


def log(record: dict) -> None:
    record = dict(record)
    record.setdefault("ts", time.strftime("%H:%M:%S"))
    with _log_lock:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(
            f"[{record['ts']}] row {record.get('row', '-'):>2} "
            f"{record.get('stepId', '-'):14s} {record.get('kind', '-'):5s} "
            f"{record.get('status', '-'):7s} {record.get('seconds', 0):6.1f}s "
            f"{record.get('detail', '')}",
            flush=True,
        )


# ── 配置读取 ────────────────────────────────────────────────

def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


ENV = read_env_file(SKILL_ENV)
BASE_URL = ENV.get("IMG_BASE_URL", "").rstrip("/")
API_KEY = ENV.get("IMG_API_KEY", "")
PROXY = ENV.get("IMG_PROXY", "")

# 与 scripts/generate_image.py 用同一个 UA：Cloudflare 会对空/异常 UA 直接返回 1010
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


# ── 文本步骤：路由 /chat/completions ────────────────────────

def _opener() -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = []
    if PROXY:
        handlers.append(urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    return urllib.request.build_opener(*handlers)


def _data_uri(path: Path) -> str:
    mime = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def call_text_model(step: dict, brief: str, ref_image: Path, model: str, timeout: int = 180) -> str:
    content = [
        {"type": "text", "text": brief},
        {"type": "image_url", "image_url": {"url": _data_uri(ref_image)}},
    ]
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": step["systemInstruction"]},
            {"role": "user", "content": content},
        ],
    }).encode("utf-8")

    last_err = ""
    for attempt in range(1, 5):
        req = urllib.request.Request(
            f"{BASE_URL}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "Content-Type": "application/json",
                "User-Agent": UA,
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with _opener().open(req, timeout=timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            message = payload["choices"][0]["message"]["content"]
            if isinstance(message, list):
                message = "".join(part.get("text", "") for part in message if isinstance(part, dict))
            text = (message or "").strip()
            if text:
                return text
            last_err = "empty content"
        except Exception as exc:  # noqa: BLE001 - 网络抖动统一重试
            last_err = f"{type(exc).__name__}: {exc}"
            if isinstance(exc, urllib.error.HTTPError):
                try:
                    last_err += " " + exc.read().decode("utf-8", "replace")[:300]
                except Exception:  # noqa: BLE001
                    pass
        time.sleep(min(2 * attempt, 8))
    raise RuntimeError(f"文本步骤失败：{last_err}")


# ── 图像步骤：GitHub 原脚本 ─────────────────────────────────

def _image_model() -> str:
    with _model_lock:
        return _current_image_model


def _note_image_failure(stderr: str) -> bool:
    """若失败像是模型不被支持，切到回落模型；返回是否发生了切换。"""
    global _current_image_model, _model_switched
    low = stderr.lower()
    if not any(hint in low for hint in MODEL_ERROR_HINTS):
        return False
    with _model_lock:
        if _model_switched or _current_image_model != PRIMARY_IMAGE_MODEL:
            return False
        _current_image_model = FALLBACK_IMAGE_MODEL
        _model_switched = True
        print(f"!! 图像模型 {PRIMARY_IMAGE_MODEL} 被路由拒绝，切换到 {FALLBACK_IMAGE_MODEL}", flush=True)
        return True


def parse_cost(stderr: str) -> float | None:
    """脚本成功时会打印 `[cogfoundry] 任务完成，费用 $0.0123`。"""
    match = re.search(r"费用\s*\$([0-9.]+)", stderr or "")
    return float(match.group(1)) if match else None


def call_image_model(prompt_file: Path, size: str, ref_image: Path | None, model: str,
                     dest: Path, timeout: int = 420) -> tuple[Path, float | None]:
    tmp = RUN_DIR / "out" / ".tmp" / f"{dest.stem}-{time.time_ns()}"
    tmp.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable, str(RUN_DIR / "scripts" / "generate_image.py"),
        "--prompt-file", str(prompt_file),
        "--size", size,
        "--timeout", str(timeout),
        "--output-dir", str(tmp),
        "--env-file", str(SKILL_ENV),
    ]
    if ref_image is not None:
        cmd += ["--image", str(ref_image)]

    env = dict(os.environ)
    env["IMG_MODEL"] = model  # 脚本只会补 .env 里缺的键，已存在的环境变量优先

    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    produced = sorted(p for p in tmp.glob("image-*") if p.is_file())
    if proc.returncode != 0 or not produced:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError((proc.stderr or proc.stdout or "无输出")[-1200:])
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    shutil.move(str(produced[0]), str(dest))
    shutil.rmtree(tmp, ignore_errors=True)
    return dest, parse_cost(proc.stderr)


# ── 单行执行 ────────────────────────────────────────────────

def step_output(row_dir: Path, step_id: str, suffix: str) -> Path:
    return row_dir / f"{step_id}{suffix}"


def run_row(row: dict, args: argparse.Namespace) -> dict:
    row_no = row["row"]
    row_dir = OUT_DIR / f"row-{row_no:02d}"
    row_dir.mkdir(parents=True, exist_ok=True)
    ref_image = (RUN_DIR / row["product_image"]).resolve()
    brief = row["creative_brief"]
    result = {"row": row_no, "text": {}, "image": {}, "errors": []}

    text_steps = PROMPTS["promptSteps"]
    image_steps = PROMPTS["imageSteps"]

    # 1) 三个文本步骤（无依赖，并发）
    def do_text(step_id: str) -> tuple[str, str, float, str]:
        step = text_steps[step_id]
        out = step_output(row_dir, step_id, ".prompt.txt")
        if out.exists() and out.stat().st_size > 0 and args.reuse_prompts:
            return step_id, out.read_text(encoding="utf-8"), 0.0, "已存在"
        started = time.time()
        text = call_text_model(step, brief, ref_image, args.text_model)
        out.write_text(text + "\n", encoding="utf-8")
        return step_id, text, time.time() - started, f"model={args.text_model}"

    with cf.ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(do_text, sid): sid for sid in text_steps}
        for fut in cf.as_completed(futures):
            sid = futures[fut]
            try:
                step_id, text, seconds, detail = fut.result()
                result["text"][step_id] = text
                log({"row": row_no, "stepId": step_id, "kind": "text", "status": "ok",
                     "seconds": seconds, "detail": detail})
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"{sid}: {exc}")
                log({"row": row_no, "stepId": sid, "kind": "text", "status": "FAILED",
                     "seconds": 0, "detail": str(exc)[:200]})

    # 2) 四个图像步骤（各自依赖对应的文本步骤；全局信号量限流）
    def do_image(step_id: str) -> tuple[str, str, str]:
        step = image_steps[step_id]
        final = step_output(row_dir, step_id, ".png")
        if final.exists() and final.stat().st_size > 0:
            return step_id, 0.0, "已存在"
        prompt_file = step_output(row_dir, step["promptSourceStepId"], ".prompt.txt")
        if not prompt_file.exists():
            raise RuntimeError(f"缺少上游 Prompt：{prompt_file.name}")
        use_ref = ref_image if step["with_reference"] else None
        started = time.time()
        assert _image_sem is not None
        for attempt in (1, 2):
            with _image_sem:
                model = _image_model()
                try:
                    _, cost = call_image_model(prompt_file, step["size"], use_ref, model, final,
                                               args.image_timeout)
                    detail = f"model={model}" + (f" cost=${cost:.4f}" if cost is not None else "")
                    if cost is not None:
                        with _log_lock:
                            _costs.append(cost)
                    return step_id, time.time() - started, detail
                except Exception as exc:  # noqa: BLE001
                    err = str(exc)
                    if _note_image_failure(err) and attempt == 1:
                        continue
                    if attempt == 1:
                        time.sleep(5)
                        continue
                    raise
        raise RuntimeError("unreachable")

    with cf.ThreadPoolExecutor(max_workers=len(image_steps)) as pool:
        futures = {pool.submit(do_image, sid): sid for sid in image_steps}
        for fut in cf.as_completed(futures):
            sid = futures[fut]
            try:
                _, seconds, extra = fut.result()
                result["image"][sid] = "ok"
                log({"row": row_no, "stepId": sid, "kind": "image", "status": "ok",
                     "seconds": seconds, "detail": extra})
            except Exception as exc:  # noqa: BLE001
                result["image"][sid] = "failed"
                result["errors"].append(f"{sid}: {exc}")
                log({"row": row_no, "stepId": sid, "kind": "image", "status": "FAILED",
                     "seconds": 0, "detail": str(exc)[:200]})
    return result


# ── 入口 ────────────────────────────────────────────────────

def main() -> int:
    global _image_sem, _current_image_model
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", default=str(RUN_DIR / "rows.jsonl"))
    parser.add_argument("--only-row", type=int, help="只跑某一行（冒烟用）")
    parser.add_argument("--row-concurrency", type=int, default=13, help="行级并发，默认 13")
    parser.add_argument("--image-concurrency", type=int, default=6, help="同时在飞的图像请求数，默认 6")
    parser.add_argument("--text-model", default=DEFAULT_TEXT_MODEL)
    parser.add_argument("--image-model", default=PRIMARY_IMAGE_MODEL)
    parser.add_argument("--image-timeout", type=int, default=420)
    parser.add_argument("--reuse-prompts", action="store_true", default=True)
    parser.add_argument("--fresh-prompts", dest="reuse_prompts", action="store_false")
    args = parser.parse_args()

    if not BASE_URL or not API_KEY:
        print(f"缺少路由配置，检查 {SKILL_ENV}", file=sys.stderr)
        return 2
    _current_image_model = args.image_model

    rows = [json.loads(line) for line in Path(args.rows).read_text(encoding="utf-8").splitlines() if line.strip()]
    if args.only_row:
        rows = [r for r in rows if r["row"] == args.only_row]
    if not rows:
        print("没有要跑的行", file=sys.stderr)
        return 2

    _image_sem = threading.Semaphore(args.image_concurrency)
    print(f"路由 {BASE_URL} | 代理 {PROXY or '(直连)'} | 文本模型 {args.text_model} | "
          f"图像模型 {args.image_model} | 行数 {len(rows)} | 行并发 {args.row_concurrency} | "
          f"图像并发 {args.image_concurrency}", flush=True)

    started = time.time()
    results = []

    def safe_run(row: dict) -> dict:
        try:
            return run_row(row, args)
        except Exception as exc:  # noqa: BLE001 - 单行异常不能拖垮整批
            log({"row": row["row"], "stepId": "-", "kind": "row", "status": "FAILED",
                 "seconds": 0, "detail": str(exc)[:200]})
            return {"row": row["row"], "text": {}, "image": {}, "errors": [str(exc)]}

    with cf.ThreadPoolExecutor(max_workers=args.row_concurrency) as pool:
        results = list(pool.map(safe_run, rows))

    ok_rows = sum(1 for r in results if not r["errors"] and len(r["image"]) == len(PROMPTS["imageSteps"]))
    summary = {
        "rows": len(results),
        "rows_fully_ok": ok_rows,
        "rows_with_failure": len(results) - ok_rows,
        "image_model_used": _image_model(),
        "model_switched": _model_switched,
        "text_model": args.text_model,
        "elapsed_seconds": round(time.time() - started, 1),
        "images_billed": len(_costs),
        "cost_usd": round(sum(_costs), 4) if _costs else None,
        "failures": [{"row": r["row"], "errors": r["errors"]} for r in results if r["errors"]],
    }
    (RUN_DIR / "run-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if ok_rows == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
