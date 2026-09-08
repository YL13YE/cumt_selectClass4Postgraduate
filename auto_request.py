from __future__ import annotations

import argparse
import base64
import getpass
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import requests


BASE_URL = "https://gms.cumt.edu.cn"
PATH_PREFIX = "MyHeartWillGoOnpygl/xswsxk"
BJID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


@dataclass(frozen=True)
class Config:
    gms_token: str
    jsessionid: str
    bjids: tuple[str, ...]
    pdid: str | None
    interval: float
    timeout: float
    max_network_errors: int
    workers: int
    base_url: str


def encode_route(route: str, gms_token: str) -> str:
    """生成 GMS 使用的无填充 URL-safe Base64 路径。"""
    payload = f"{PATH_PREFIX}/{route}#{gms_token}"
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")


def build_url(base_url: str, route: str, gms_token: str) -> str:
    return f"{base_url.rstrip('/')}/stu/{encode_route(route, gms_token)}"


def parse_cookie(cookie_header: str) -> tuple[str, str]:
    """从 Cookie 请求头中提取两个必需的会话 Cookie。"""
    cookie_header = cookie_header.strip()
    if cookie_header.lower().startswith("cookie:"):
        cookie_header = cookie_header.split(":", 1)[1].strip()

    cookies: dict[str, str] = {}
    for item in cookie_header.split(";"):
        item = item.strip()
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        cookies[name.strip()] = value.strip().strip('"')

    gms_token = cookies.get("cn_com_southsoft_gms", "").strip()
    jsessionid = cookies.get("JSESSIONID", "").strip()
    if not gms_token or not jsessionid:
        raise ValueError(
            "Cookie 中必须包含 cn_com_southsoft_gms 和 JSESSIONID，"
            "格式示例：cn_com_southsoft_gms=...; JSESSIONID=..."
        )
    return gms_token, jsessionid


def build_session(config: Config) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/143.0.0.0 Safari/537.36 Edg/143.0.0.0"
            ),
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": config.base_url.rstrip("/"),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    )
    session.cookies.set("cn_com_southsoft_gms", config.gms_token)
    session.cookies.set("JSESSIONID", config.jsessionid)
    return session


def parse_response(response: requests.Response) -> dict[str, Any] | None:
    try:
        result = response.json()
    except (requests.exceptions.JSONDecodeError, ValueError):
        preview = response.text.replace("\r", " ").replace("\n", " ")[:160]
        print(f"响应不是 JSON，HTTP {response.status_code}: {preview}")
        return None

    if not isinstance(result, dict):
        print(f"响应 JSON 类型异常: {type(result).__name__}")
        return None
    return result


def log(message: str, output_lock: threading.Lock) -> None:
    with output_lock:
        print(message, flush=True)


def run_for_bjid(
    config: Config,
    bjid: str,
    stop_event: threading.Event,
    output_lock: threading.Lock,
) -> tuple[str, int]:
    """为一个 bjid 持续请求，直到成功、异常或用户停止。"""
    request_url = build_url(config.base_url, "xk", config.gms_token)
    request_headers: dict[str, str] = {}
    if config.pdid:
        referer_url = build_url(config.base_url, "tcxk", config.gms_token)
        request_headers["Referer"] = f"{referer_url}?{urlencode({'code': 4, 'pdid': config.pdid})}"

    data = {"lb": "1", "bjid": bjid}
    session = build_session(config)
    consecutive_network_errors = 0
    attempt = 0

    try:
        while not stop_event.is_set():
            attempt += 1
            now = datetime.now().strftime("%H:%M:%S")
            log(f"[{now}] bjid={bjid} 第 {attempt} 次请求...", output_lock)

            try:
                response = session.post(
                    request_url,
                    headers=request_headers,
                    data=data,
                    timeout=config.timeout,
                )
                consecutive_network_errors = 0
            except requests.RequestException as exc:
                consecutive_network_errors += 1
                wait_seconds = min(config.interval * (2 ** (consecutive_network_errors - 1)), 10.0)
                log(f"bjid={bjid} 网络请求失败: {exc}", output_lock)

                if (
                    config.max_network_errors > 0
                    and consecutive_network_errors >= config.max_network_errors
                ):
                    log(f"bjid={bjid} 连续网络错误达到上限，停止。", output_lock)
                    return bjid, 2

                if stop_event.wait(wait_seconds):
                    return bjid, 130
                continue

            result = parse_response(response)
            if result is None:
                if stop_event.wait(config.interval):
                    return bjid, 130
                continue

            code = result.get("code")
            code_text = str(code)
            message = result.get("msg", result.get("message", ""))
            log(f"bjid={bjid} 服务器响应: code={code}, msg={message}", output_lock)

            if code_text == "200":
                log(f"bjid={bjid} 选课成功。", output_lock)
                return bjid, 0

            if code_text == "300":
                log(f"bjid={bjid} 当前满员，{config.interval:g} 秒后重试。", output_lock)
                if stop_event.wait(config.interval):
                    return bjid, 130
                continue

            if response.status_code in (401, 403) or code_text in {"401", "403"}:
                log(f"bjid={bjid} 登录状态可能已失效，请更新 Cookie。", output_lock)
                return bjid, 3

            log(f"bjid={bjid} 收到未识别的业务状态，停止。", output_lock)
            return bjid, 4
    finally:
        session.close()

    return bjid, 130


def run_all(config: Config) -> int:
    stop_event = threading.Event()
    output_lock = threading.Lock()
    results: dict[str, int] = {}

    log(
        f"已准备 {len(config.bjids)} 个 bjid，使用 {config.workers} 个线程并发请求。",
        output_lock,
    )
    log("按 Ctrl+C 可停止全部请求。", output_lock)

    executor = ThreadPoolExecutor(max_workers=config.workers)
    futures = [
        executor.submit(run_for_bjid, config, bjid, stop_event, output_lock)
        for bjid in config.bjids
    ]

    try:
        for future in as_completed(futures):
            bjid, status = future.result()
            results[bjid] = status
    except KeyboardInterrupt:
        log("收到停止信号，正在停止全部线程。", output_lock)
        stop_event.set()
        for future in futures:
            future.cancel()
        return 130
    finally:
        stop_event.set()
        executor.shutdown(wait=True, cancel_futures=True)

    success_count = sum(status == 0 for status in results.values())
    log(f"全部任务结束，成功 {success_count}/{len(config.bjids)} 个。", output_lock)
    return 0 if success_count > 0 else 1


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须为大于 0 的数字")
    return number


def positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须为大于 0 的整数")
    return number


def normalize_bjids(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        result.extend(item for item in re.split(r"[\s,，]+", value.strip()) if item)

    unique: list[str] = []
    seen: set[str] = set()
    for bjid in result:
        if bjid not in seen:
            seen.add(bjid)
            unique.append(bjid)
    return tuple(unique)


def parse_args() -> Config:
    parser = argparse.ArgumentParser(
        add_help=False,
        formatter_class=argparse.RawTextHelpFormatter,
        description=(
            "多教学班并发选课请求脚本。\n"
            "请求地址根据 Cookie 中的 cn_com_southsoft_gms 动态生成。"
        ),
        epilog=(
            "使用示例：\n"
            "  1. 交互输入 bjid 和 Cookie：\n"
            "     py -3 auto_request.py\n\n"
            "  2. 指定多个 bjid，Cookie 仍由程序提示输入：\n"
            "     py -3 auto_request.py --bjid ID1 --bjid ID2\n\n"
            "  3. 在一个参数中输入多个 bjid：\n"
            "     py -3 auto_request.py --bjid \"ID1,ID2,ID3\"\n\n"
            "  4. 指定线程数和重试间隔：\n"
            "     py -3 auto_request.py --bjid ID1,ID2 --workers 2 --interval 1.5\n\n"
            "  5. 需要 Referer 时提供 pdid：\n"
            "     py -3 auto_request.py --bjid ID1 --pdid COURSE_ID\n\n"
            "Cookie 输入格式：\n"
            "  cn_com_southsoft_gms=你的值; JSESSIONID=你的值\n"
            "也可粘贴以 Cookie: 开头的完整请求头。\n\n"
            "说明：\n"
            "  bjid                 教学班 ID，至少填写一个。\n"
            "  pdid                 可选，仅用于生成 Referer。\n"
            "  workers              并发线程数，默认值为 bjid 数量，最多 5。\n"
            "  interval             满员后的重试间隔，默认 1 秒。\n"
            "  timeout              单次请求超时，默认 8 秒。\n"
            "  max-network-errors   每个 bjid 的连续网络错误上限，默认 5；0 表示不限制。\n\n"
            "运行期间按 Ctrl+C 可停止全部线程。"
        ),
    )
    parser.add_argument(
        "-h",
        "--help",
        "-help",
        action="help",
        help="显示帮助信息并退出",
    )
    parser.add_argument(
        "--cookie",
        help="完整 Cookie，例如 cn_com_southsoft_gms=...; JSESSIONID=...；不填写时交互输入",
    )
    parser.add_argument(
        "--bjid",
        action="append",
        default=[],
        help="教学班 ID，可重复传入，也可使用逗号或空格分隔多个值",
    )
    parser.add_argument(
        "--pdid",
        default=None,
        help="可选课程号；填写后才会附加 Referer 请求头",
    )
    parser.add_argument("--workers", type=positive_int, help="并发线程数，默认不超过 5 个")
    parser.add_argument("--interval", type=positive_float, default=1.0, help="满员后的重试间隔，默认 1 秒")
    parser.add_argument("--timeout", type=positive_float, default=8.0, help="单次请求超时，默认 8 秒")
    parser.add_argument(
        "--max-network-errors",
        type=int,
        default=5,
        help="单个 bjid 的连续网络错误上限；设为 0 表示不限制，默认 5",
    )
    parser.add_argument("--base-url", default=BASE_URL, help=argparse.SUPPRESS)
    args = parser.parse_args()

    raw_bjids = normalize_bjids(args.bjid)
    if not raw_bjids:
        entered = input("请输入一个或多个 bjid，用逗号或空格分隔：").strip()
        raw_bjids = normalize_bjids([entered])

    if not raw_bjids:
        parser.error("至少需要输入一个 bjid")
    invalid_bjids = [bjid for bjid in raw_bjids if not BJID_PATTERN.fullmatch(bjid)]
    if invalid_bjids:
        parser.error(f"以下 bjid 不是标准 UUID 格式: {', '.join(invalid_bjids)}")

    cookie_header = args.cookie
    if not cookie_header:
        cookie_header = input(
            "请输入 Cookie（cn_com_southsoft_gms=...; JSESSIONID=...）："
        )
    try:
        gms_token, jsessionid = parse_cookie(cookie_header)
    except ValueError as exc:
        parser.error(str(exc))

    if args.max_network_errors < 0:
        parser.error("--max-network-errors 不能小于 0")

    workers = args.workers or min(len(raw_bjids), 5)
    workers = min(workers, len(raw_bjids))

    return Config(
        gms_token=gms_token,
        jsessionid=jsessionid,
        bjids=raw_bjids,
        pdid=args.pdid.strip() if args.pdid else None,
        interval=args.interval,
        timeout=args.timeout,
        max_network_errors=args.max_network_errors,
        workers=workers,
        base_url=args.base_url.strip(),
    )


if __name__ == "__main__":
    sys.exit(run_all(parse_args()))


