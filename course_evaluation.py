"""研究生课程评价：查询列表、查询模板和手动提交。"""
from __future__ import annotations

import argparse
import base64
import json
import random
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlencode

import requests

from auto_request import BASE_URL, BJID_PATTERN, parse_cookie, positive_float

EVALUATION_PREFIX = "MyHeartWillGoOnpygl/kcjxpjnew"
FIELDS = ("czyc", "mbid", "bjid", "jsbh", "xh", "qtyj", "xxnrlist", "zt", "pjjg")
IDENTIFIER = re.compile(r"^[A-Za-z0-9_-]+$")
FIXED_QUESTION = "有无违背党和国家大政方针、违背宪法法律、危害国家安全、破坏民族团结等言行"


def prepare_payload(template: object, *, mbid: str, bjid: str, jsbh: str,
                    xh: str) -> tuple[dict, list[dict]]:
    """按已观察到的题目权重生成分数；返回请求内容和供用户检查的明细。"""
    if not isinstance(template, list) or not template:
        raise ValueError("模板响应必须是非空数组")
    if not IDENTIFIER.fullmatch(mbid) or not BJID_PATTERN.fullmatch(bjid):
        raise ValueError("mbid 格式无效或 bjid 不是标准 UUID")
    if not jsbh.strip() or not xh.strip():
        raise ValueError("教师编号和学号不能为空")
    answers, details = [], []
    seen = set()
    total_weight = Decimal(0)
    for item in template:
        if not isinstance(item, dict) or item.get("MBID") != mbid:
            raise ValueError("模板中的 MBID 缺失或与指定模板不一致")
        gcdid = item.get("GCDID")
        if not isinstance(gcdid, str) or not gcdid.strip() or gcdid in seen:
            raise ValueError("题目 GCDID 为空或重复")
        seen.add(gcdid)
        try:
            weight = Decimal(str(item["GCDQZ"]))
        except (KeyError, InvalidOperation):
            raise ValueError(f"题目 {gcdid} 的 GCDQZ 无效") from None
        if weight not in (Decimal("0.05"), Decimal("0.10")):
            raise ValueError(f"题目 {gcdid} 的权重 {weight} 不在已知的 0.05、0.10 范围内")
        total_weight += weight
        maximum = int(weight * 100)
        score = random.SystemRandom().choice((maximum - 1, maximum))
        answers.append({"gcdid": gcdid, "xxnrid": str(score)})
        details.append({"题目": item.get("GCDMC", gcdid), "题目ID": gcdid,
                        "满分": maximum, "分数": score})
    if total_weight != Decimal(1):
        raise ValueError(f"题目权重之和为 {total_weight}，预期为 1")
    payload = {"czyc": "0", "mbid": mbid, "bjid": bjid, "jsbh": jsbh,
               "xh": xh, "qtyj": "", "xxnrlist": answers, "zt": "1",
               "pjjg": str(sum(row["分数"] for row in details))}
    return payload, details


def route_url(base_url: str, route: str, token: str) -> str:
    # 评价系统将 Base64 的填充字符 = 替换为 _，而不是直接去掉。
    raw = f"{EVALUATION_PREFIX}/{route}#{token}".encode("utf-8")
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").replace("=", "_")
    return f"{base_url.rstrip('/')}/stu/{encoded}"


def evaluation_referer(base_url: str, token: str, *, bjid: str,
                       jsbh: str, mbid: str = "", pjid: str = "") -> str:
    query = urlencode({"pjid": pjid, "bjid": bjid, "jsbh": jsbh, "mbid": mbid})
    return f"{route_url(base_url, 'wsjxpj_pj', token)}?{query}"


def validate_payload(value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("评价文件必须是 JSON 对象")
    missing = [key for key in FIELDS if key not in value]
    extra = sorted(set(value) - set(FIELDS))
    if missing or extra:
        raise ValueError(f"评价字段不匹配，缺少: {missing}；多余: {extra}")
    for key in FIELDS:
        if key == "xxnrlist":
            continue
        if not isinstance(value[key], (str, int, float)) or isinstance(value[key], bool):
            raise ValueError(f"{key} 必须是字符串或数字")
        if key != "qtyj" and not str(value[key]).strip():
            raise ValueError(f"{key} 不能为空")
    if not BJID_PATTERN.fullmatch(str(value["bjid"])):
        raise ValueError("bjid 必须是标准 UUID")
    if not IDENTIFIER.fullmatch(str(value["mbid"])):
        raise ValueError("mbid 格式无效")
    if str(value["czyc"]) != "0":
        raise ValueError("固定问题必须选择否（czyc=0）")
    answers = value["xxnrlist"]
    if not isinstance(answers, list) or not answers:
        raise ValueError("xxnrlist 必须是非空数组")
    for item in answers:
        if (not isinstance(item, dict) or set(item) != {"gcdid", "xxnrid"}
                or any(not isinstance(item[k], str) or not item[k].strip()
                       for k in ("gcdid", "xxnrid"))):
            raise ValueError("xxnrlist 每项必须包含非空字符串 gcdid 和 xxnrid")
    if len({item["gcdid"] for item in answers}) != len(answers):
        raise ValueError("xxnrlist 中 gcdid 不能重复")
    return value


def make_session(token: str, jsessionid: str, base_url: str) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/143.0.0.0 Safari/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": base_url.rstrip("/"),
    })
    session.cookies.set("cn_com_southsoft_gms", token)
    session.cookies.set("JSESSIONID", jsessionid)
    return session


def show_response(response: requests.Response) -> int:
    print(f"HTTP {response.status_code}")
    try:
        result = response.json()
    except ValueError:
        print(response.text)
        print("响应不是 JSON，请检查登录状态和服务器返回内容。", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not 200 <= response.status_code < 300:
        return 1
    # 不假定评价系统的业务状态字段；原样展示以供核对。
    return 0


def save_prepared(args: argparse.Namespace, template_data: object) -> int:
    payload, details = prepare_payload(template_data, mbid=args.mbid, bjid=args.bjid,
                                       jsbh=args.jsbh, xh=args.xh)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, indent=2)
        output.write("\n")
    for index, row in enumerate(details, 1):
        print(f"{index}. {row['题目']}：{row['分数']}/{row['满分']}（{row['题目ID']}）")
    print(f"合计：{payload['pjjg']}/100；已生成 {args.output}，请检查后再决定是否提交。")
    return 0


def review_items(payload: dict, template: object) -> list[tuple[str, str, str]]:
    """按题目 ID 匹配名称和实际将发送的分数。"""
    if not isinstance(template, list) or not template:
        raise ValueError("模板响应必须是非空数组")
    names = {}
    for item in template:
        if not isinstance(item, dict) or item.get("MBID") != str(payload["mbid"]):
            raise ValueError("模板 ID 与评价文件不一致")
        identifier, name = item.get("GCDID"), item.get("GCDMC")
        if (not isinstance(identifier, str) or not identifier.strip()
                or not isinstance(name, str) or not name.strip() or identifier in names):
            raise ValueError("模板中存在缺失或重复的题目 ID，或缺少题目名称")
        names[identifier] = name
    answers = payload["xxnrlist"]
    if set(names) != {item["gcdid"] for item in answers}:
        raise ValueError("评价文件与模板的题目不一致，请重新生成或检查文件")
    return [(names[item["gcdid"]], item["xxnrid"], item["gcdid"]) for item in answers]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="课程评价：查询列表、查询模板或手动提交")
    parser.add_argument("--cookie", help="包含 cn_com_southsoft_gms 和 JSESSIONID 的 Cookie；省略时交互输入")
    parser.add_argument("--timeout", type=positive_float, default=8.0, help="请求超时秒数（默认 8）")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list", help="查询课程评价列表")
    template = sub.add_parser("template", help="查询指定评价模板")
    template.add_argument("--mbid", required=True, help="模板 ID")
    template.add_argument("--bjid", required=True, help="教学班 ID")
    template.add_argument("--jsbh", required=True, help="教师编号")
    template.add_argument("--pjid", default="", help="评价 ID，默认为空")
    template.add_argument("--referer-mbid", default="", help="评价页面 Referer 中的 mbid，默认为空")
    prepare = sub.add_parser("prepare", help="根据模板随机生成评价文件，不提交")
    prepare.add_argument("--mbid", required=True, help="模板 ID")
    prepare.add_argument("--bjid", required=True, help="教学班 ID")
    prepare.add_argument("--jsbh", required=True, help="教师编号")
    prepare.add_argument("--xh", required=True, help="学号")
    prepare.add_argument("--output", required=True, type=Path, help="新建的评价 JSON 文件")
    prepare.add_argument("--template-file", type=Path, help="使用本地模板 JSON，省略时从系统查询")
    prepare.add_argument("--pjid", default="", help="评价页面 Referer 中的 pjid，默认为空")
    prepare.add_argument("--referer-mbid", default="", help="评价页面 Referer 中的 mbid，默认为空")
    submit = sub.add_parser("submit", help="根据 JSON 文件提交评价（需要交互确认）")
    submit.add_argument("--file", type=Path, required=True, help="包含评价字段的 UTF-8 JSON 文件")
    submit.add_argument("--template-file", type=Path, help="用于确认题目名称的本地模板 JSON，省略时从系统查询")
    submit.add_argument("--pjid", default="", help="评价页面 Referer 中的 pjid，默认为空")
    submit.add_argument("--referer-mbid", default="", help="评价页面 Referer 中的 mbid，默认为空")
    args = parser.parse_args(argv)

    payload = None
    if args.action == "submit":
        try:
            payload = validate_payload(json.loads(args.file.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError) as exc:
            parser.error(f"无法读取评价文件: {exc}")
    elif args.action in ("template", "prepare"):
        if not IDENTIFIER.fullmatch(args.mbid) or not BJID_PATTERN.fullmatch(args.bjid):
            parser.error("mbid 格式无效或 bjid 不是标准 UUID")

    if args.action == "prepare" and args.template_file:
        try:
            template_data = json.loads(args.template_file.read_text(encoding="utf-8-sig"))
            return save_prepared(args, template_data)
        except (OSError, ValueError) as exc:
            print(f"生成评价文件失败: {exc}", file=sys.stderr)
            return 1

    if args.cookie:
        try:
            token, jsessionid = parse_cookie(args.cookie)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        token = input("请输入 cn_com_southsoft_gms：").strip().strip('"')
        jsessionid = input("请输入 JSESSIONID：").strip().strip('"')
        if not token or not jsessionid:
            parser.error("两个 Cookie 均不能为空")

    with make_session(token, jsessionid, BASE_URL) as session:
        headers = {}
        files = None
        if args.action == "list":
            route = "getJxpjByXh"
            headers["Referer"] = route_url(BASE_URL, "index", token)
        elif args.action in ("template", "prepare"):
            route = f"mbylData>mbid={args.mbid}"
            headers["Referer"] = evaluation_referer(BASE_URL, token, bjid=args.bjid,
                                                     jsbh=args.jsbh, pjid=args.pjid,
                                                     mbid=args.referer_mbid)
        else:
            route = "savePj"
            headers["Referer"] = evaluation_referer(BASE_URL, token, bjid=str(payload["bjid"]),
                                                     jsbh=str(payload["jsbh"]), pjid=args.pjid,
                                                     mbid=args.referer_mbid)
            try:
                if args.template_file:
                    template_data = json.loads(args.template_file.read_text(encoding="utf-8-sig"))
                else:
                    template_url = route_url(BASE_URL, f"mbylData>mbid={payload['mbid']}", token)
                    template_response = session.post(template_url, headers=headers,
                                                     timeout=args.timeout, allow_redirects=False)
                    if not 200 <= template_response.status_code < 300:
                        raise ValueError(f"模板请求返回 HTTP {template_response.status_code}")
                    template_data = template_response.json()
                items = review_items(payload, template_data)
            except (OSError, ValueError, requests.RequestException) as exc:
                print(f"无法核对评价项目，未提交评价: {exc}", file=sys.stderr)
                return 1
            print("即将发送的评价项目及分数：")
            for index, (name, score, identifier) in enumerate(items, 1):
                print(f"{index}. {name}：{score} 分（题目 ID：{identifier}）")
            print(f"{FIXED_QUESTION}：否（czyc=0）")
            print(f"评价总分 pjjg：{payload['pjjg']}")
            print("完整提交内容：")
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            try:
                confirmation = input("输入 提交评价 确认发送，其他输入取消：").strip()
            except EOFError:
                confirmation = ""
            if confirmation != "提交评价":
                print("已取消，未发送评价。")
                return 1
            files = [(key, (None, json.dumps(payload[key], ensure_ascii=False, separators=(",", ":"))
                            if key == "xxnrlist" else str(payload[key]))) for key in FIELDS]
        try:
            response = session.post(route_url(BASE_URL, route, token), headers=headers,
                                    files=files, timeout=args.timeout, allow_redirects=False)
        except requests.RequestException as exc:
            print(f"请求失败: {exc}", file=sys.stderr)
            if payload is not None:
                print("提交状态未知，请先到系统核对，勿直接重试。", file=sys.stderr)
            return 1
        if args.action == "prepare":
            if not 200 <= response.status_code < 300:
                return show_response(response) or 1
            try:
                return save_prepared(args, response.json())
            except (OSError, ValueError) as exc:
                print(f"生成评价文件失败: {exc}", file=sys.stderr)
                return 1
        status = show_response(response)
        if payload is not None:
            print("请根据服务器响应和系统页面核对评价是否提交成功。")
        return status


if __name__ == "__main__":
    sys.exit(main())
