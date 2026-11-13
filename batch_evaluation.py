"""批量准备和确认课程评价；不会自动提交。"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

import requests

from auto_request import BASE_URL, parse_cookie, positive_float
from course_evaluation import (FIXED_QUESTION, IDENTIFIER, evaluation_referer,
                               make_session, prepare_payload, review_items,
                               route_url, validate_payload, FIELDS)


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def fetch(session: requests.Session, route: str, token: str, referer: str, timeout: float) -> object:
    response = session.post(route_url(BASE_URL, route, token), headers={"Referer": referer},
                            timeout=timeout, allow_redirects=False)
    if not 200 <= response.status_code < 300:
        raise ValueError(f"查询失败：HTTP {response.status_code}")
    return response.json()


def evaluation_status(item: dict) -> str:
    """根据已观察到的列表响应区分已提交、未评价和未知状态。"""
    status = item.get("ZT")
    if str(status) == "1":
        return "已评价"
    if status is None and not any(item.get(key) not in (None, "") for key in ("TJID", "PJJG")):
        return "未评价"
    return "状态未知"


def pending_listing(result: object) -> list[dict]:
    if not isinstance(result, list):
        raise ValueError("课程列表必须是 JSON 数组")
    pending = []
    for item in result:
        if not isinstance(item, dict):
            raise ValueError("课程列表条目格式无效")
        status = evaluation_status(item)
        if status == "未评价":
            pending.append(item)
        else:
            print(f"跳过{status}：{item.get('KCMC', '未知课程')} / {item.get('JSXM', '未知教师')}")
    return pending


def filter_reviewed(reviewed: list[dict], listing: object) -> list[dict]:
    """提交前重新查询，只保留当前仍未评价的班级和教师。"""
    if not isinstance(listing, list):
        raise ValueError("课程列表必须是 JSON 数组")
    statuses = {}
    for item in listing:
        if not isinstance(item, dict) or not isinstance(item.get("BJID"), str) or not isinstance(item.get("JSBH"), str):
            raise ValueError("课程列表缺少班级或教师编号")
        pair = (item["BJID"], item["JSBH"])
        if pair in statuses:
            raise ValueError("课程列表中存在重复的班级和教师")
        statuses[pair] = evaluation_status(item)
    remaining = []
    for entry in reviewed:
        payload, course = entry["payload"], entry["course"]
        status = statuses.get((payload["bjid"], payload["jsbh"]), "列表中不存在")
        if status == "未评价":
            remaining.append(entry)
        else:
            print(f"跳过{status}：{course['course']} / {course['teacher']}")
    return remaining


def get_template(session: requests.Session, token: str, payload: dict, args: argparse.Namespace) -> object:
    if args.template_file:
        return read_json(args.template_file)
    referer = evaluation_referer(BASE_URL, token, bjid=str(payload["bjid"]),
                                 jsbh=str(payload["jsbh"]))
    return fetch(session, f"mbylData>mbid={payload['mbid']}", token, referer, args.timeout)


def courses_from_list(result: object, selections: list[int] | None) -> list[dict]:
    if not isinstance(result, list) or not result:
        raise ValueError("课程列表必须是非空 JSON 数组")
    if selections and (len(set(selections)) != len(selections)
                       or any(i < 1 or i > len(result) for i in selections)):
        raise ValueError("--select 包含重复或超出范围的序号")
    indices = [i - 1 for i in selections] if selections else range(len(result))
    courses, seen = [], set()
    for i in indices:
        item = result[i]
        if not isinstance(item, dict) or not all(isinstance(item.get(k), str) and item[k].strip()
                                                for k in ("BJID", "JSBH", "KCMC", "JSXM")):
            raise ValueError(f"列表第 {i+1} 项缺少课程、教师或标识")
        pair = (item["BJID"], item["JSBH"])
        if pair in seen:
            raise ValueError(f"列表中教学班和教师重复：{pair}")
        seen.add(pair)
        courses.append({"bjid": pair[0], "jsbh": pair[1], "course": item["KCMC"], "teacher": item["JSXM"]})
    return courses


def parse_selections(value: str) -> list[int]:
    try:
        result = [int(part.strip()) for part in value.split(",") if part.strip()]
    except ValueError:
        raise argparse.ArgumentTypeError("序号应为逗号分隔的整数") from None
    if not result:
        raise argparse.ArgumentTypeError("请选择至少一个序号")
    return result


def review_batch(batch: object, templates: list[object]) -> list[dict]:
    if not isinstance(batch, list) or not batch or len(batch) != len(templates):
        raise ValueError("批量文件必须是非空数组")
    result, seen = [], set()
    for entry, template in zip(batch, templates):
        if not isinstance(entry, dict) or not isinstance(entry.get("course"), dict):
            raise ValueError("批量文件条目格式无效")
        course = entry["course"]
        payload = validate_payload(entry.get("payload"))
        pair = (payload["bjid"], payload["jsbh"])
        if pair in seen or (course.get("bjid"), course.get("jsbh")) != pair:
            raise ValueError("课程与评价的班级/教师不一致或重复")
        seen.add(pair)
        if not all(isinstance(course.get(k), str) and course[k].strip() for k in ("course", "teacher")):
            raise ValueError("课程或教师名称缺失")
        rows = review_items(payload, template)
        result.append({"course": course, "payload": payload, "rows": rows})
    return result


def confirm_and_submit(session: requests.Session, token: str, timeout: float,
                       reviewed: list[dict]) -> int:
    print(f"即将提交 {len(reviewed)} 条评价，请逐项核对：")
    for i, item in enumerate(reviewed, 1):
        course, payload = item["course"], item["payload"]
        print(f"\n{i}. {course['course']} / {course['teacher']}（班级 {payload['bjid']}，教师 {payload['jsbh']}）")
        for name, score, identifier in item["rows"]:
            print(f"  {name}：{score} 分（题目 ID：{identifier}）")
        print(f"  {FIXED_QUESTION}：否（czyc=0）")
        print(f"  总分：{payload['pjjg']}；其他意见：{payload['qtyj']}")
    try:
        confirmation = input("输入 批量提交评价 确认发送，其他输入取消：").strip()
    except EOFError:
        confirmation = ""
    if confirmation != "批量提交评价":
        print("已取消，未提交评价。")
        return 1
    for i, item in enumerate(reviewed, 1):
        payload, course = item["payload"], item["course"]
        referer = evaluation_referer(BASE_URL, token, bjid=str(payload["bjid"]), jsbh=str(payload["jsbh"]))
        files = [(key, (None, json.dumps(payload[key], ensure_ascii=False, separators=(",", ":"))
                        if key == "xxnrlist" else str(payload[key]))) for key in FIELDS]
        try:
            response = session.post(route_url(BASE_URL, "savePj", token), headers={"Referer": referer},
                                    files=files, timeout=timeout, allow_redirects=False)
            result = response.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"第 {i} 条状态未知：{exc}；请在系统核对后再操作。", file=sys.stderr)
            return 1
        print(f"第 {i} 条 {course['course']} / {course['teacher']}：HTTP {response.status_code} {json.dumps(result, ensure_ascii=False)}")
        if not 200 <= response.status_code < 300 or not isinstance(result, dict) or str(result.get("code")) != "200":
            print("服务器未明确返回 code=200，已停止后续提交；请在系统核对。", file=sys.stderr)
            return 1
        if i < len(reviewed):
            delay = random.randint(30, 60)
            print(f"等待 {delay} 秒后提交下一条评价（按 Ctrl+C 可停止）。", flush=True)
            try:
                time.sleep(delay)
            except KeyboardInterrupt:
                print("\n已停止后续提交，请根据上方结果核对已提交的评价。")
                return 130
    return 0


def interactive() -> int:
    """直接启动时引导查询、选课、生成并确认批量评价。"""
    try:
        print("课程评价：查询并选择课程后，将在提交前展示所有评价明细。")
        token = input("请输入 cn_com_southsoft_gms：").strip().strip('"')
        jsessionid = input("请输入 JSESSIONID：").strip().strip('"')
        if not token or not jsessionid:
            raise ValueError("两个 Cookie 均不能为空")
        with make_session(token, jsessionid, BASE_URL) as session:
            listing = fetch(session, "getJxpjByXh", token, route_url(BASE_URL, "index", token), 8.0)
            listing = pending_listing(listing)
            if not listing:
                print("没有可确认的未评价课程。")
                return 0
            courses = courses_from_list(listing, None)
            print("\n未评价课程列表：")
            for i, course in enumerate(courses, 1):
                print(f"{i}. {course['course']} / {course['teacher']}（班级 {course['bjid']}，教师 {course['jsbh']}）")
            selected = input("输入要评价的序号，用逗号分隔；直接回车选择全部：").strip()
            if selected:
                courses = courses_from_list(listing, parse_selections(selected.replace("，", ",")))
            xh = input("请输入学号：").strip()
            if not xh:
                raise ValueError("学号不能为空")
            mbid = input("请输入模板 ID（回车使用抓包中的默认模板）：").strip() or "23B8965E784CB2111798A9F65F2BDB7F"
            if not IDENTIFIER.fullmatch(mbid):
                raise ValueError("模板 ID 格式无效")
            batch, templates = [], []
            for course in courses:
                referer = evaluation_referer(BASE_URL, token, bjid=course["bjid"], jsbh=course["jsbh"])
                template = fetch(session, f"mbylData>mbid={mbid}", token, referer, 8.0)
                payload, _ = prepare_payload(template, mbid=mbid, bjid=course["bjid"],
                                             jsbh=course["jsbh"], xh=xh)
                batch.append({"course": course, "payload": payload})
                templates.append(template)
            reviewed = review_batch(batch, templates)
            current = fetch(session, "getJxpjByXh", token, route_url(BASE_URL, "index", token), 8.0)
            reviewed = filter_reviewed(reviewed, current)
            if not reviewed:
                print("没有可确认的未评价课程，未提交评价。")
                return 0
            return confirm_and_submit(session, token, 8.0, reviewed)
    except (EOFError, KeyboardInterrupt):
        print("\n已停止；请根据已显示的提交结果核对系统状态。")
        return 1
    except (OSError, ValueError, argparse.ArgumentTypeError, requests.RequestException) as exc:
        print(f"操作失败：{exc}；未继续提交。", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    if argv is None and len(sys.argv) == 1:
        return interactive()
    parser = argparse.ArgumentParser(description="批量准备及提交课程评价")
    parser.add_argument("--cookie", help="包含两个会话 Cookie 的请求头")
    parser.add_argument("--timeout", type=positive_float, default=8.0)
    sub = parser.add_subparsers(dest="action", required=True)
    prepare = sub.add_parser("prepare", help="生成批量评价文件，不提交")
    prepare.add_argument("--mbid", required=True)
    prepare.add_argument("--xh", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    prepare.add_argument("--list-file", type=Path, help="本地课程列表 JSON，省略时从系统查询")
    prepare.add_argument("--select", type=parse_selections, help="按列表顺序选择序号，如 1,3,5；省略选择全部")
    submit = sub.add_parser("submit", help="显示全部明细，确认后逐项提交")
    submit.add_argument("--file", type=Path, required=True)
    for command in (prepare, submit):
        command.add_argument("--template-file", type=Path, help="本地模板 JSON，省略时逐项从系统查询")
    args = parser.parse_args(argv)
    try:
        if args.cookie:
            token, jsessionid = parse_cookie(args.cookie)
        elif args.action == "prepare" and args.list_file and args.template_file:
            token = jsessionid = ""
        else:
            token = input("请输入 cn_com_southsoft_gms：").strip().strip('"')
            jsessionid = input("请输入 JSESSIONID：").strip().strip('"')
            if not token or not jsessionid:
                raise ValueError("两个 Cookie 均不能为空")
        with make_session(token, jsessionid, BASE_URL) as session:
            if args.action == "prepare":
                if not IDENTIFIER.fullmatch(args.mbid):
                    raise ValueError("模板 ID 格式无效")
                listing = (read_json(args.list_file) if args.list_file else
                           fetch(session, "getJxpjByXh", token, route_url(BASE_URL, "index", token), args.timeout))
                listing = pending_listing(listing)
                if not listing:
                    print("没有可确认的未评价课程。")
                    return 0
                courses = courses_from_list(listing, args.select)
                batch = []
                for course in courses:
                    target = {"mbid": args.mbid, **course}
                    template = get_template(session, token, target, args)
                    payload, _ = prepare_payload(template, mbid=args.mbid, bjid=course["bjid"],
                                                       jsbh=course["jsbh"], xh=args.xh)
                    batch.append({"course": course, "payload": payload})
                    print(f"已准备：{course['course']} / {course['teacher']}，总分 {payload['pjjg']}")
                with args.output.open("x", encoding="utf-8") as out:
                    json.dump(batch, out, ensure_ascii=False, indent=2)
                    out.write("\n")
                print(f"已生成 {len(batch)} 条评价：{args.output}；尚未提交。")
                return 0
            batch = read_json(args.file)
            if not isinstance(batch, list) or not batch:
                raise ValueError("批量文件必须是非空数组")
            templates = []
            for entry in batch:
                if not isinstance(entry, dict):
                    raise ValueError("批量文件条目格式无效")
                payload = validate_payload(entry.get("payload"))
                templates.append(get_template(session, token, payload, args))
            reviewed = review_batch(batch, templates)
            current = fetch(session, "getJxpjByXh", token, route_url(BASE_URL, "index", token), args.timeout)
            reviewed = filter_reviewed(reviewed, current)
            if not reviewed:
                print("没有可确认的未评价课程，未提交评价。")
                return 0
            return confirm_and_submit(session, token, args.timeout, reviewed)
    except (OSError, ValueError, requests.RequestException) as exc:
        print(f"操作失败：{exc}；未继续提交。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
