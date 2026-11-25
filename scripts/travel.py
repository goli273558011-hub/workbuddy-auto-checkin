#!/usr/bin/env python3
"""WorkBuddy 猫猫旅行（派出/领取），复用 checkin.py 的鉴权与请求封装。

只调用 status/config/depart/claim 四个接口；不触碰兑换、抽奖、补签。
接口地址按两个上游仓库原文列出的组合依次探测，404 自动换下一个。
"""
from __future__ import annotations

import json
import random
import sys
import time
from typing import Any

from checkin import (
    EXIT_BUSINESS,
    EXIT_NETWORK,
    EXIT_NO_SESSION,
    EXIT_OK,
    GROWTH_PREFIX,
    CheckinError,
    _api_ok,
    activity_user_id,
    api_bases,
    dig,
    load_creds,
    logged_message,
    refresh_access_token,
    request_json,
)

HELPER_BASE = "https://www.workbuddy.cn"
HELPER_PREFIX = "/activity/growth"


def emit(result: str, report: str, **extra: Any) -> None:
    payload = {"result": result, "report": report, **extra}
    print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def as_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return default


def uid_creds(creds: dict[str, str]) -> dict[str, str]:
    """按 helper 的 user_id 规则补 X-User-Id（JWT sub 兜底）。"""
    out = dict(creds)
    uid = activity_user_id(creds)
    if uid:
        out["uid"] = uid
    return out


def build_targets(creds: dict[str, str]) -> list[dict[str, Any]]:
    """两个仓库原文给出的接口组合，按 goli -> helper 顺序。"""
    targets: list[dict[str, Any]] = []
    with_uid = uid_creds(creds)
    for base in api_bases():
        targets.append({
            "label": base + GROWTH_PREFIX + "/buddy/travel",
            "base": base,
            "prefix": GROWTH_PREFIX + "/buddy/travel",
            "creds": with_uid,
        })
    helper_creds = dict(with_uid)
    helper_creds["domain"] = "www.workbuddy.cn"
    targets.append({
        "label": HELPER_BASE + HELPER_PREFIX + "/buddy/travel",
        "base": HELPER_BASE,
        "prefix": HELPER_PREFIX + "/buddy/travel",
        "creds": helper_creds,
    })
    return targets


def call(
    target: dict[str, Any],
    endpoint: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    url = target["base"] + target["prefix"] + "/" + endpoint
    return request_json(url, target["creds"], method=method, payload=payload)


def probe(targets: list[dict[str, Any]]) -> dict[str, Any]:
    """用 status 找出真正可用的接口组合；401 直接判定登录失效。"""
    errors: list[str] = []
    for target in targets:
        status, body = call(target, "status", "GET")
        if status in (401, 403):
            raise CheckinError(
                "登录态已失效（%s HTTP %d）。请更新 WORKBUDDY_ACCESS_TOKEN。"
                % (target["label"], status),
                EXIT_NO_SESSION,
            )
        if status in (404, 405):
            errors.append("%s HTTP %d" % (target["label"], status))
            continue
        if status >= 500:
            errors.append("%s HTTP %d" % (target["label"], status))
            continue
        if status >= 400:
            detail = logged_message(body, target["creds"]) or ("HTTP %d" % status)
            errors.append("%s %s" % (target["label"], detail))
            continue
        if _api_ok(status, body):
            return target
        errors.append("%s code=%s" % (target["label"], body.get("code")))
    raise CheckinError("旅行状态接口不可用：" + "；".join(errors), EXIT_NETWORK)


def api(
    target: dict[str, Any],
    endpoint: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status, body = call(target, endpoint, method=method, payload=payload)
    if status in (401, 403):
        raise CheckinError(
            "%s 登录态已失效（HTTP %d）。" % (endpoint, status), EXIT_NO_SESSION
        )
    if status in (404, 405) or status >= 500:
        raise CheckinError(
            "%s 请求失败 HTTP %d（%s）" % (endpoint, status, target["label"]),
            EXIT_NETWORK,
        )
    if status >= 400:
        detail = logged_message(body, target["creds"]) or ("HTTP %d" % status)
        raise CheckinError("%s 失败：%s" % (endpoint, detail), EXIT_BUSINESS)
    if body.get("code") not in (0, 200, None):
        detail = logged_message(body, target["creds"]) or ("code=%s" % body.get("code"))
        raise CheckinError("%s 返回 %s" % (endpoint, detail), EXIT_BUSINESS)
    return body


def run_travel(creds: dict[str, str]) -> str:
    target = probe(build_targets(creds))
    departed = 0
    claimed = 0

    for _round in range(1, 5):
        status_body = api(target, "status", "GET")
        state = str(dig(status_body, "state") or "")
        limited = bool(dig(status_body, "daily_limit_reached"))
        arrive_at = as_int(dig(status_body, "arrive_at"))
        server_now = as_int(dig(status_body, "server_now")) or int(time.time())
        reward = as_int(dig(status_body, "reward_credit"))
        location = dig(status_body, "location")
        loc_name = (
            str(location.get("name") or "") if isinstance(location, dict) else ""
        )

        if state == "idle":
            if limited:
                return "猫猫旅行：今日已达上限，无需操作"
            if departed >= 1:
                return "猫猫旅行：已派出（服务端状态未同步），本次结束"
            config = api(target, "config", "GET")
            locations = dig(config, "locations")
            if not isinstance(locations, list) or not locations:
                raise CheckinError("travel/config 未返回 locations", EXIT_BUSINESS)
            chosen = random.choice(locations)
            if not isinstance(chosen, dict):
                raise CheckinError("travel/config 的 location 格式无效", EXIT_BUSINESS)
            loc_id = as_int(chosen.get("id"), 0)
            if not loc_id:
                raise CheckinError("travel/config 的 location id 无效", EXIT_BUSINESS)

            d_status, depart = call(target, "depart", "POST", {"location_id": loc_id})
            if d_status in (401, 403):
                raise CheckinError("派出时登录态已失效（HTTP %d）。" % d_status, EXIT_NO_SESSION)
            if d_status >= 400 or not isinstance(depart, dict):
                raise CheckinError("派出请求失败 HTTP %d" % d_status, EXIT_NETWORK)
            d_code = depart.get("code")
            if d_code not in (0, 200, None):
                msg = logged_message(depart, target["creds"]) or ("code=%s" % d_code)
                if any(
                    word in msg
                    for word in ("上限", "限制", "次数", "已达", "已用", "limit", "already")
                ):
                    return "猫猫旅行：今日已无法再派出（%s），结束" % msg
                raise CheckinError("派出失败：%s" % msg, EXIT_BUSINESS)

            departed = 1
            continue

        if state == "traveling":
            left = max(0, (arrive_at - server_now) // 60)
            name = loc_name or "旅行中"
            return "猫猫旅行：「%s」采风进行中，约 %d 分钟后回家" % (name, left)

        if state == "arrived":
            if claimed >= 1:
                return "猫猫旅行：礼物已领取（服务端状态未同步），本次结束"
            c_status, claim = call(target, "claim", "POST", {})
            if c_status in (401, 403):
                raise CheckinError("领取时登录态已失效（HTTP %d）。" % c_status, EXIT_NO_SESSION)
            if c_status >= 400 or not isinstance(claim, dict):
                raise CheckinError("领取请求失败 HTTP %d" % c_status, EXIT_NETWORK)
            c_code = claim.get("code")
            if c_code not in (0, 200, None):
                msg = logged_message(claim, target["creds"]) or ("code=%s" % c_code)
                raise CheckinError("领取失败：%s" % msg, EXIT_BUSINESS)
            credit = as_int(dig(claim, "reward_credit"), reward)
            claimed = 1
            continue

        return "猫猫旅行：未知状态 %s，跳过" % state

    return "猫猫旅行：状态机已跑满 4 轮，本次结束"


def run() -> int:
    creds = load_creds()
    if not creds["access_token"]:
        creds["access_token"] = refresh_access_token(creds)
    try:
        report = run_travel(creds)
    except CheckinError as exc:
        if exc.exit_code != EXIT_NO_SESSION or creds.get("_did_refresh"):
            raise
        creds["access_token"] = refresh_access_token(creds)
        creds["_did_refresh"] = "1"
        report = run_travel(creds)
    emit("TRAVEL_OK", report)
    return EXIT_OK


def main() -> int:
    try:
        return run()
    except CheckinError as exc:
        emit("ERROR", str(exc), exit_code=exc.exit_code)
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
