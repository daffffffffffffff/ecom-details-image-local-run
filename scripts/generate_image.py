#!/usr/bin/env python3
"""统一图像生成脚本，同时兼容 OpenAI 同步 API 和 apimart.ai 异步 API。

自动检测模式：
  - 基础 URL 包含 "apimart" → 异步轮询模式
  - 其他 → OpenAI 同步模式
  - 也可通过 --mode sync|async 强制指定

配置来自环境变量或 .env 文件：
- IMG_BASE_URL: API 根地址
- IMG_MODEL: 图片模型名
- IMG_API_KEY: API key
- IMG_API_MODE（可选）: sync 或 async，覆盖自动检测
"""

from __future__ import annotations

import argparse
import base64
import binascii
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ENV_BASE_URL = "IMG_BASE_URL"
ENV_MODEL = "IMG_MODEL"
ENV_API_KEY = "IMG_API_KEY"
ENV_PROXY = "IMG_PROXY"
ENV_ALIASES = {
    ENV_BASE_URL: ("OPENAI_BASE_URL", "OPENAI_API_BASE", "BASE_URL"),
    ENV_MODEL: ("OPENAI_IMAGE_MODEL", "IMAGE_MODEL", "OPENAI_MODEL"),
    ENV_API_KEY: ("OPENAI_API_KEY", "API_KEY"),
    ENV_PROXY: ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"),
}

# CogFoundry 路由（gpt-image-2.5-flare）只接受「宽x高」像素尺寸和 images 数组做参考图
COGFOUNDRY_RATIO_TO_SIZE = {
    "1:1": "2048x2048",
    "3:2": "2048x1360",
    "2:3": "1360x2048",
    "4:3": "2048x1536",
    "3:4": "1536x2048",
    "16:9": "2048x1152",
    "9:16": "1152x2048",
    "2:1": "2048x1024",
    "1:2": "1024x2048",
}

# 网络抖动重试次数（本地代理路由到 Cloudflare 时约有一半请求会被重置）
HTTP_ATTEMPTS = 8

VALID_RATIOS = ("auto", "1:1", "3:2", "2:3", "4:3", "3:4", "5:4", "4:5",
                "16:9", "9:16", "2:1", "1:2", "21:9", "9:21")
VALID_RESOLUTIONS = ("1k", "2k", "4k")

PIXEL_TO_RATIO: dict[str, str] = {
    "1024x1024": "1:1", "2048x2048": "1:1",
    "1536x1024": "3:2", "2048x1360": "3:2",
    "1024x1536": "2:3", "1360x2048": "2:3",
    "1024x768": "4:3", "2048x1536": "4:3",
    "768x1024": "3:4", "1536x2048": "3:4",
    "1280x1024": "5:4", "2560x2048": "5:4",
    "1024x1280": "4:5", "2048x2560": "4:5",
    "1536x864": "16:9", "2048x1152": "16:9", "3840x2160": "16:9",
    "864x1536": "9:16", "1152x2048": "9:16", "2160x3840": "9:16",
    "2048x1024": "2:1", "2688x1344": "2:1", "3840x1920": "2:1",
    "1024x2048": "1:2", "1344x2688": "1:2", "1920x3840": "1:2",
    "2016x864": "21:9", "2688x1152": "21:9", "3840x1648": "21:9",
    "864x2016": "9:21", "1152x2688": "9:21", "1648x3840": "9:21",
}

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


def fail(message: str, exit_code: int = 1) -> None:
    print(f"错误：{message}", file=sys.stderr)
    raise SystemExit(exit_code)


# ── 配置与环境 ──────────────────────────────────────────────

def read_prompt(args: argparse.Namespace) -> str:
    if args.prompt:
        prompt = args.prompt.strip()
    else:
        try:
            prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
        except OSError as exc:
            fail(f"无法读取 prompt 文件：{exc}")
    if not prompt:
        fail("prompt 不能为空。")
    return prompt


def strip_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def find_default_env_file() -> Path | None:
    # 1) 从当前工作目录向上查找（CLI 约定）
    for directory in (Path.cwd(), *Path.cwd().parents):
        env_file = directory / ".env"
        if env_file.is_file():
            return env_file
    # 2) 回退到 skill 目录下的 .env（scripts/ 的上一级），使任意 cwd 都能直接生图
    skill_env = Path(__file__).resolve().parent.parent / ".env"
    if skill_env.is_file():
        return skill_env
    return None


def load_env_file(env_file: Path | None) -> None:
    if env_file is None:
        return
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        fail(f"无法读取 .env 文件：{exc}")
    for line_number, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            fail(f".env 第 {line_number} 行格式不正确，应为 KEY=value。")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            fail(f".env 第 {line_number} 行缺少变量名。")
        if key not in os.environ:
            os.environ[key] = strip_env_value(value)


def require_config(name: str) -> str:
    candidates = (name, *ENV_ALIASES.get(name, ()))
    for candidate in candidates:
        value = os.environ.get(candidate, "").strip()
        if value:
            return value
    accepted = "、".join(candidates)
    fail(
        f"缺少配置 {name}。请在 .env 中设置 IMG_BASE_URL、IMG_MODEL、IMG_API_KEY；"
        f"也兼容这些变量名：{accepted}。"
    )


# ── 模式检测 ──────────────────────────────────────────────

def detect_mode(base_url: str, explicit_mode: str | None) -> str:
    if explicit_mode in ("sync", "async", "cogfoundry"):
        return explicit_mode
    lower = base_url.lower()
    if "cogfoundry" in lower:
        return "cogfoundry"
    if "apimart" in lower:
        return "async"
    return "sync"


def size_to_ratio(size: str) -> str:
    if ":" in size:
        return size
    lower = size.lower()
    if lower in PIXEL_TO_RATIO:
        return PIXEL_TO_RATIO[lower]
    fail(f"无法将像素尺寸 '{size}' 转换为比例。请直接使用比例格式，如 1:1、16:9、2:3。")


# ── 图片编码 ──────────────────────────────────────────────

def encode_image_object(image_path: str) -> dict[str, str]:
    path = Path(image_path)
    if not path.is_file():
        fail(f"参考图片不存在：{image_path}")
    suffix = path.suffix.lower().lstrip(".")
    mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "webp": "image/webp", "gif": "image/gif"}
    mime = mime_map.get(suffix)
    if not mime:
        fail(f"不支持的图片格式：.{suffix}，仅支持 png/jpg/jpeg/webp/gif。")
    try:
        data = path.read_bytes()
    except OSError as exc:
        fail(f"无法读取参考图片：{exc}")
    return {"type": mime, "data": base64.b64encode(data).decode("ascii")}


def encode_image_data_uri(image_path: str) -> str:
    path = Path(image_path)
    if not path.is_file():
        fail(f"参考图片不存在：{image_path}")
    suffix = path.suffix.lower().lstrip(".")
    mime_map = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "webp": "image/webp", "gif": "image/gif"}
    mime = mime_map.get(suffix)
    if not mime:
        fail(f"不支持的图片格式：.{suffix}，仅支持 png/jpg/jpeg/webp/gif。")
    try:
        data = path.read_bytes()
    except OSError as exc:
        fail(f"无法读取参考图片：{exc}")
    b64 = base64.b64encode(data).decode("ascii")
    return f"data:{mime};base64,{b64}"


# ── HTTP 工具 ──────────────────────────────────────────────

def install_proxy() -> str | None:
    """部分网络直连路由域名会解析到被 Cloudflare 限制的边缘 IP（error code:1034）。
    在 .env 里配 IMG_PROXY=http://127.0.0.1:7890（或设置 HTTPS_PROXY）即可走本地代理。"""
    proxy = ""
    for name in (ENV_PROXY, *ENV_ALIASES.get(ENV_PROXY, ())):
        value = os.environ.get(name, "").strip()
        if value:
            proxy = value
            break
    if not proxy:
        return None
    handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    urllib.request.install_opener(urllib.request.build_opener(handler))
    return proxy


def _transient(exc: BaseException) -> bool:
    """区分「网络抖动」和「接口真的返回了错误」：只有前者值得重试。"""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    if isinstance(exc, (urllib.error.URLError, http.client.RemoteDisconnected, TimeoutError, ConnectionError, OSError)):
        text = f"{exc}"
        return not any(marker in text for marker in ("SSLCertVerificationError", "certificate verify failed"))
    return False


def http_post(url: str, api_key: str, payload: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": UA},
        method="POST",
    )
    raw = _fetch_raw(request, timeout, "提交")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        fail(f"接口返回的不是有效 JSON：{raw[:500]}")
    if not isinstance(parsed, dict):
        fail("接口返回格式不正确：顶层结果不是对象。")
    return parsed


def http_get(url: str, api_key: str, timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {api_key}", "User-Agent": UA}, method="GET",
    )
    raw = _fetch_raw(request, timeout, "查询")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        fail(f"查询接口返回的不是有效 JSON：{raw[:500]}")
    return parsed


def _fetch_raw(request: urllib.request.Request, timeout: int, action: str) -> str:
    last: BaseException | None = None
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            fail(f"接口返回 HTTP {exc.code}：{detail}")
        except BaseException as exc:  # noqa: BLE001 - 网络层异常统一判断是否可重试
            if not _transient(exc):
                fail(f"{action}失败：{exc}")
            last = exc
            if attempt < HTTP_ATTEMPTS:
                time.sleep(min(1.5 * attempt, 5))
    fail(f"{action}失败，已重试 {HTTP_ATTEMPTS} 次：{last}")


# ── 同步模式（OpenAI 兼容）──────────────────────────────────

def build_sync_payload(args: argparse.Namespace, prompt: str, model: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": model, "prompt": prompt, "n": args.n, "size": args.size}
    if args.quality:
        payload["quality"] = args.quality
    if args.image:
        payload["image_urls"] = [encode_image_data_uri(args.image)]
    return payload


def run_sync(base_url: str, api_key: str, payload: dict[str, Any],
             output_dir: Path, fmt: str) -> list[Path]:
    endpoint = f"{base_url}/images/generations"
    print(f"[sync] 提交生成请求到 {endpoint}...", file=sys.stderr)
    result = http_post(endpoint, api_key, payload, timeout=120)
    return save_sync_images(result, output_dir, fmt)


def filename_for(suffix: str) -> str:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    return f"image-{timestamp}-01.{suffix.lstrip('.')}"


def save_sync_images(result: dict[str, Any], output_dir: Path, fmt: str) -> list[Path]:
    data = result.get("data")
    if not isinstance(data, list) or not data:
        fail(f"接口返回中没有 data 图片数组：{json.dumps(result)[:300]}")
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, item in enumerate(data):
        if not isinstance(item, dict):
            fail("接口返回格式不正确：data 中包含非对象项目。")
        if item.get("b64_json"):
            encoded = item["b64_json"]
            try:
                image_bytes = base64.b64decode(encoded)
            except (binascii.Error, ValueError) as exc:
                fail(f"无法解码 b64_json 图片：{exc}")
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            p = output_dir / f"image-{timestamp}-{index + 1:02d}.{fmt.lstrip('.')}"
            p.write_bytes(image_bytes)
            paths.append(p)
        elif item.get("url"):
            image_url = item["url"]
            suffix = _suffix_from_url(image_url, fmt)
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            p = output_dir / f"image-{timestamp}-{index + 1:02d}.{suffix}"
            dl_req = urllib.request.Request(image_url, headers={"User-Agent": UA})
            with urllib.request.urlopen(dl_req, timeout=120) as resp:
                p.write_bytes(resp.read())
            paths.append(p)
        else:
            fail("图片结果既没有 b64_json，也没有 url。")
    return paths


# ── 异步模式（apimart.ai）──────────────────────────────────

def build_async_payload(args: argparse.Namespace, prompt: str, model: str) -> dict[str, Any]:
    ratio = size_to_ratio(args.size)
    payload: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1, "size": ratio, "resolution": args.resolution}
    if args.image:
        payload["image_urls"] = [encode_image_data_uri(args.image)]
    return payload


def run_async(base_url: str, api_key: str, payload: dict[str, Any],
              output_dir: Path, fmt: str, poll_interval: int, timeout: int) -> list[Path]:
    endpoint = f"{base_url}/images/generations"
    print(f"[async] 提交异步任务到 {endpoint}...", file=sys.stderr)
    result = http_post(endpoint, api_key, payload, timeout=30)

    code = result.get("code")
    if code and code != 200:
        error = result.get("error", {})
        fail(f"提交失败（code={code}）：{error.get('message', json.dumps(result))}")

    data = result.get("data")
    if not isinstance(data, list) or not data:
        fail(f"提交响应缺少 data 数组：{json.dumps(result)[:300]}")
    task_id = data[0].get("task_id")
    if not task_id:
        fail(f"提交响应缺少 task_id：{json.dumps(data[0])[:300]}")

    print(f"[async] 任务已提交: {task_id}，等待 15s 后开始轮询...", file=sys.stderr)
    time.sleep(15)

    task_data = _poll_task(base_url, api_key, task_id, poll_interval, timeout)
    actual_time = task_data.get("actual_time", 0)
    cost = task_data.get("cost", 0)
    print(f"[async] 任务完成，耗时 {actual_time}s，费用 ${cost:.4f}", file=sys.stderr)

    return _save_async_images(task_data, output_dir, fmt)


def _poll_task(base_url: str, api_key: str, task_id: str,
               poll_interval: int, timeout: int) -> dict[str, Any]:
    url = f"{base_url}/tasks/{task_id}"
    start = time.time()
    while True:
        elapsed = time.time() - start
        if elapsed > timeout:
            fail(f"任务 {task_id} 超时（{timeout}s），请稍后手动查询。")
        result = http_get(url, api_key)
        task_data = result.get("data", {})
        status = task_data.get("status", "")
        if status == "completed":
            return task_data
        if status == "failed":
            error = task_data.get("error", {})
            fail(f"任务 {task_id} 失败：{error.get('message', json.dumps(task_data)[:300])}")
        progress = task_data.get("progress", 0)
        print(f"  轮询中... 状态={status} 进度={progress}% 耗时={elapsed:.0f}s", file=sys.stderr)
        time.sleep(poll_interval)


def _save_async_images(task_data: dict[str, Any], output_dir: Path, fmt: str) -> list[Path]:
    result = task_data.get("result", {})
    images = result.get("images")
    if not isinstance(images, list) or not images:
        fail(f"任务结果中缺少 images 数组：{json.dumps(task_data)[:300]}")
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for img_item in images:
        url_list = img_item.get("url")
        if not isinstance(url_list, list) or not url_list:
            fail(f"图片结果缺少 url 数组：{json.dumps(img_item)[:300]}")
        image_url = url_list[0]
        suffix = _suffix_from_url(image_url, fmt)
        output_path = output_dir / filename_for(suffix)
        print(f"  下载图片: {image_url}", file=sys.stderr)
        dl_req = urllib.request.Request(image_url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(dl_req, timeout=120) as resp:
                output_path.write_bytes(resp.read())
        except urllib.error.URLError as exc:
            fail(f"无法下载图片：{exc.reason}")
        except TimeoutError:
            fail("下载图片超时。")
        paths.append(output_path)
    return paths


# ── CogFoundry 路由（异步任务：提交 → 轮询）────────────────

def cogfoundry_size(size: str) -> str:
    if "x" in size.lower() and ":" not in size:
        return size.lower()
    mapped = COGFOUNDRY_RATIO_TO_SIZE.get(size)
    if mapped:
        return mapped
    fail(
        f"CogFoundry 不支持比例 '{size}'。请直接传像素尺寸（如 2048x2048、2048x1360），"
        f"或使用这些比例：{'、'.join(COGFOUNDRY_RATIO_TO_SIZE)}。"
    )


def build_cogfoundry_payload(args: argparse.Namespace, prompt: str, model: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1, "size": cogfoundry_size(args.size)}
    if args.image:
        payload["images"] = [encode_image_data_uri(args.image)]
    return payload


def run_cogfoundry(base_url: str, api_key: str, payload: dict[str, Any], output_dir: Path,
                   fmt: str, poll_interval: int, timeout: int) -> list[Path]:
    endpoint = f"{base_url}/tasks/generations"
    print(f"[cogfoundry] 提交任务到 {endpoint} ...", file=sys.stderr)
    result = http_post(endpoint, api_key, payload, timeout=60)
    if result.get("code") not in (None, "success"):
        fail(f"提交失败：{result.get('message') or json.dumps(result)[:300]}")
    data = result.get("data")
    if not isinstance(data, dict) or not data.get("request_id"):
        fail(f"提交响应缺少 request_id：{json.dumps(result)[:300]}")
    request_id = data["request_id"]
    print(f"[cogfoundry] 任务已提交: {request_id}（{data.get('status', '?')}），开始轮询...", file=sys.stderr)

    task_data = _poll_cogfoundry(base_url, api_key, request_id, poll_interval, timeout)
    cost = task_data.get("cost") or 0
    print(f"[cogfoundry] 任务完成，费用 ${cost:.4f}", file=sys.stderr)
    return _save_cogfoundry_images(task_data, output_dir, fmt)


def _poll_cogfoundry(base_url: str, api_key: str, request_id: str,
                     poll_interval: int, timeout: int) -> dict[str, Any]:
    url = f"{base_url}/tasks/generations/{request_id}"
    start = time.time()
    time.sleep(min(poll_interval, 5))
    while True:
        elapsed = time.time() - start
        if elapsed > timeout:
            fail(f"任务 {request_id} 超时（{timeout}s），可用 GET {url} 手动查询。")
        result = http_get(url, api_key)
        task_data = result.get("data") or {}
        status = str(task_data.get("status", "")).upper()
        if status in ("COMPLETED", "SUCCEEDED", "SUCCESS"):
            return task_data
        if status in ("FAILED", "ERROR", "CANCELED", "CANCELLED"):
            fail(f"任务 {request_id} 失败：{task_data.get('fail_reason') or json.dumps(task_data)[:300]}")
        print(f"  轮询中... 状态={status} 进度={task_data.get('progress', '?')} 耗时={elapsed:.0f}s", file=sys.stderr)
        time.sleep(poll_interval)


def _save_cogfoundry_images(task_data: dict[str, Any], output_dir: Path, fmt: str) -> list[Path]:
    urls = (task_data.get("data") or {}).get("image_urls")
    if not isinstance(urls, list) or not urls:
        fail(f"任务结果中缺少 image_urls：{json.dumps(task_data)[:300]}")
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for index, image_url in enumerate(urls, start=1):
        suffix = _suffix_from_url(image_url, fmt)
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        output_path = output_dir / f"image-{timestamp}-{index:02d}.{suffix}"
        request = urllib.request.Request(image_url, headers={"User-Agent": UA})
        output_path.write_bytes(_fetch_bytes(request, 180))
        print(f"  已保存: {output_path}", file=sys.stderr)
        paths.append(output_path)
    return paths


def _fetch_bytes(request: urllib.request.Request, timeout: int) -> bytes:
    last: BaseException | None = None
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in (403, 408, 429, 500, 502, 503, 504):
                detail = exc.read().decode("utf-8", errors="replace")
                fail(f"下载图片返回 HTTP {exc.code}：{detail[:300]}")
            last = exc
        except BaseException as exc:  # noqa: BLE001
            if not _transient(exc):
                fail(f"下载图片失败：{exc}")
            last = exc
        if attempt < HTTP_ATTEMPTS:
            time.sleep(min(1.5 * attempt, 5))
    fail(f"下载图片失败，已重试 {HTTP_ATTEMPTS} 次：{last}")


# ── 工具函数 ──────────────────────────────────────────────

def _suffix_from_url(url: str, fallback: str) -> str:
    path = urllib.parse.urlparse(url).path
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix in {"png", "jpg", "jpeg", "webp"}:
        return "jpg" if suffix == "jpeg" else suffix
    return fallback


# ── CLI ──────────────────────────────────────────────

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="统一图像生成脚本，自动兼容 OpenAI 同步 API、apimart.ai 异步 API 和 CogFoundry 路由（提交 → 轮询）。"
    )
    prompt_group = parser.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt", help="直接传入图片生成 Prompt。")
    prompt_group.add_argument("--prompt-file", help="从文本文件读取图片生成 Prompt。")
    parser.add_argument("--output-dir", default="generated-images", help="图片输出目录，默认 generated-images。")
    parser.add_argument("--env-file", help="指定 .env 配置文件；不指定时从当前目录向上查找。")
    parser.add_argument("--mode", choices=("sync", "async", "cogfoundry"), help="API 模式。不指定时自动检测（含 cogfoundry → cogfoundry，含 apimart → async，其他 → sync）。")
    parser.add_argument("--size", default="1:1", help="图片尺寸。CogFoundry：像素（如 2048x2048、2048x1360）或比例；apimart：比例格式；同步模式：像素格式。默认 1:1。")
    parser.add_argument("--resolution", default="2k", choices=VALID_RESOLUTIONS, help="apimart 异步模式分辨率档位，默认 2k（CogFoundry 用 --size 控制，忽略此项）。")
    parser.add_argument("--quality", help="同步模式图片质量参数，例如 low、medium、high。")
    parser.add_argument("--n", type=int, default=1, help="同步模式生成图片数量，默认 1。")
    parser.add_argument("--image", help="参考产品图片路径，传入以提升产品一致性。")
    parser.add_argument("--poll-interval", type=int, default=5, help="异步模式轮询间隔秒数，默认 5。")
    parser.add_argument("--timeout", type=int, default=180, help="异步模式轮询超时秒数，默认 180。")
    parser.add_argument("--format", choices=("png", "jpeg", "webp"), default="png", help="图片保存格式，默认 png。")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    env_file = Path(args.env_file) if args.env_file else find_default_env_file()
    load_env_file(env_file)
    prompt = read_prompt(args)
    base_url = require_config(ENV_BASE_URL).rstrip("/")
    model = require_config(ENV_MODEL)
    api_key = require_config(ENV_API_KEY)

    mode = detect_mode(base_url, args.mode)
    proxy = install_proxy()
    print(f"API 模式: {mode} | base_url={base_url} | model={model}"
          + (f" | 代理={proxy}" if proxy else ""), file=sys.stderr)

    if mode == "cogfoundry":
        payload = build_cogfoundry_payload(args, prompt, model)
        paths = run_cogfoundry(base_url, api_key, payload, Path(args.output_dir),
                               args.format, args.poll_interval, args.timeout)
    elif mode == "async":
        payload = build_async_payload(args, prompt, model)
        paths = run_async(base_url, api_key, payload, Path(args.output_dir),
                          args.format, args.poll_interval, args.timeout)
    else:
        payload = build_sync_payload(args, prompt, model)
        paths = run_sync(base_url, api_key, payload, Path(args.output_dir), args.format)

    print("生成完成：")
    for path in paths:
        print(path)


if __name__ == "__main__":
    main()
