#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中行外汇牌价 → 飞书机器人播报（美元现汇买入价）
增强版：
1. 标题时间使用北京时间
2. 修复“较上次”读取逻辑
3. 增加本周累计统计：首条、最新、累计变动、最高、最低、均值、记录数
4. 本周数据按 ISO 周自动重置
5. 通过 .last_price 和 .weekly_rates.json 持久化历史数据

注意：
如果在 GitHub Actions 中运行，并希望“较上次 / 本周累计”跨天保留，
workflow 需要在脚本运行后把 .last_price 和 .weekly_rates.json commit 回仓库。
"""

import os
import re
import sys
import time
import hmac
import json
import base64
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

import requests


# ============== 配置（建议用 GitHub Secrets / 环境变量注入，不要硬编码） ==============
LARK_WEBHOOK = os.getenv(
    "LARK_WEBHOOK",
    "https://open.feishu.cn/open-apis/bot/v2/hook/你的token",
)

# 没开「签名校验」就保持空字符串
LARK_SECRET = os.getenv("LARK_SECRET", "")

# 目标币种：现默认只播美元
TARGET_CURRENCY = os.getenv("TARGET_CURRENCY", "美元")

# 抓取来源
BOC_URL = "https://www.boc.cn/sourcedb/whpj/"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 报价值变动的最小阈值（避免重复播报同值）
PRICE_CHANGE_THRESHOLD = float(os.getenv("PRICE_CHANGE_THRESHOLD", "0.01"))

# true 时：如果较上次变动小于阈值，则不推送
SKIP_IF_UNCHANGED = os.getenv("SKIP_IF_UNCHANGED", "false").lower() == "true"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("boc-feishu")


# ============== 本地持久化文件 ==============
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 飞书消息中历史价格（用于显示“较上次”）
LAST_PRICE_FILE = os.path.join(BASE_DIR, ".last_price")

# 本周累计数据
WEEKLY_RATES_FILE = os.path.join(BASE_DIR, ".weekly_rates.json")


# ============== 时间工具 ==============
BEIJING_TZ = timezone(timedelta(hours=8))


def now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def beijing_time_str(fmt: str = "%Y-%m-%d %H:%M") -> str:
    return now_beijing().strftime(fmt)


def current_week_key() -> str:
    """
    ISO 周：
    例：2026-W23
    周一作为一周开始。
    """
    d = now_beijing().isocalendar()
    return f"{d.year}-W{d.week:02d}"


def weekday_cn(dt: datetime) -> str:
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return names[dt.weekday()]


# ============== 抓取 ==============
def fetch_boc_html(url: str = BOC_URL, retries: int = 3, timeout: int = 15) -> str:
    """带 UA、Referer 和重试的抓取。"""
    last_err: Optional[Exception] = None

    for i in range(retries):
        try:
            r = requests.get(
                url,
                headers={
                    "User-Agent": UA,
                    "Referer": "https://www.boc.cn/",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                },
                timeout=timeout,
            )
            r.raise_for_status()

            # 中行页面编码偶尔不稳定，优先用 apparent_encoding 兜底
            if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
                r.encoding = r.apparent_encoding or "utf-8"

            text = r.text
            if "美元" not in text or "现汇买入价" not in text:
                raise ValueError("返回内容异常，未识别到目标字段")

            return text

        except Exception as e:  # noqa: BLE001
            last_err = e
            log.warning("第 %d 次抓取失败: %s", i + 1, e)
            time.sleep(2 * (i + 1))

    raise RuntimeError(f"抓取中行页面失败：{last_err}")


def clean_cell(value: str) -> str:
    value = re.sub(r"<.*?>", "", value)
    value = value.replace("&nbsp;", " ")
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def parse_currency_row(html: str, currency: str = "美元") -> Dict[str, str]:
    """从 HTML 中解析指定币种那一行。"""
    pattern = rf"<tr\s+data-currency=['\"]{re.escape(currency)}['\"]>(.*?)</tr>"
    m = re.search(pattern, html, re.S)

    if not m:
        # 兜底：有些页面结构变化时，不一定保留 data-currency
        pattern_fallback = rf"<tr[^>]*>\s*<td[^>]*>\s*{re.escape(currency)}\s*</td>(.*?)</tr>"
        m = re.search(pattern_fallback, html, re.S)

    if not m:
        raise ValueError(f"未在页面找到币种「{currency}」")

    row_html = m.group(0)
    cells = re.findall(r"<td[^>]*>(.*?)</td>", row_html, re.S)
    cells = [clean_cell(c) for c in cells]

    labels = [
        "货币名称",
        "现汇买入价",
        "现钞买入价",
        "现汇卖出价",
        "现钞卖出价",
        "中行折算价",
        "发布日期",
        "发布时间",
    ]

    if len(cells) < len(labels):
        raise ValueError(f"字段数量异常: got {len(cells)}, expect {len(labels)}，cells={cells}")

    return dict(zip(labels, cells))


# ============== 历史数据 ==============
def read_last_price() -> Optional[float]:
    try:
        with open(LAST_PRICE_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()

        if not content:
            return None

        return float(content)

    except (FileNotFoundError, ValueError):
        return None


def write_last_price(price: float) -> None:
    with open(LAST_PRICE_FILE, "w", encoding="utf-8") as f:
        f.write(f"{price:.4f}")


def read_weekly_data() -> Dict[str, Any]:
    try:
        with open(WEEKLY_RATES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return {}

        return data

    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def write_weekly_data(data: Dict[str, Any]) -> None:
    with open(WEEKLY_RATES_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_weekly_data(price: float, source_time: str) -> Dict[str, Any]:
    """
    更新本周累计数据。
    文件结构：
    {
      "week_key": "2026-W23",
      "currency": "美元",
      "records": [
        {
          "created_at": "2026-06-03 13:39",
          "weekday": "周三",
          "source_time": "13:35:45",
          "price": 675.6
        }
      ]
    }
    """
    week_key = current_week_key()
    now = now_beijing()

    data = read_weekly_data()

    if data.get("week_key") != week_key or data.get("currency") != TARGET_CURRENCY:
        data = {
            "week_key": week_key,
            "currency": TARGET_CURRENCY,
            "records": [],
        }

    records: List[Dict[str, Any]] = data.setdefault("records", [])

    # 避免同一分钟重复运行时把完全相同价格刷多次
    current_record = {
        "created_at": now.strftime("%Y-%m-%d %H:%M"),
        "weekday": weekday_cn(now),
        "source_time": source_time or "-",
        "price": round(price, 4),
    }

    if records:
        last = records[-1]
        same_minute = last.get("created_at") == current_record["created_at"]
        same_price = abs(float(last.get("price", 0)) - price) < 1e-6
        if same_minute and same_price:
            records[-1] = current_record
        else:
            records.append(current_record)
    else:
        records.append(current_record)

    write_weekly_data(data)
    return data


def build_weekly_summary(weekly_data: Dict[str, Any]) -> str:
    records = weekly_data.get("records") or []

    if not records:
        return "暂无本周累计数据"

    prices = [float(r["price"]) for r in records]
    first = prices[0]
    latest = prices[-1]
    diff = latest - first

    if abs(diff) < 1e-6:
        diff_text = "— 持平"
    elif diff > 0:
        diff_text = f"📈 累计上涨 {diff:.2f}"
    else:
        diff_text = f"📉 累计下跌 {abs(diff):.2f}"

    max_price = max(prices)
    min_price = min(prices)
    avg_price = sum(prices) / len(prices)

    first_record = records[0]
    latest_record = records[-1]

    return (
        f"**本周记录数**：{len(records)} 次\n"
        f"**本周首条**：{first:.2f}（{first_record.get('weekday', '-')}"
        f" {first_record.get('created_at', '-')[-5:]}）\n"
        f"**本周最新**：{latest:.2f}（{latest_record.get('weekday', '-')}"
        f" {latest_record.get('created_at', '-')[-5:]}）\n"
        f"**本周累计**：{diff_text}\n"
        f"**本周最高**：{max_price:.2f}\n"
        f"**本周最低**：{min_price:.2f}\n"
        f"**本周均值**：{avg_price:.2f}"
    )


# ============== 飞书推送 ==============
def gen_sign(secret: str, ts: int) -> str:
    string_to_sign = f"{ts}\n{secret}"
    h = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(h).decode("utf-8")


def build_card(
    currency: str,
    data: Dict[str, str],
    prev_price: Optional[float],
    weekly_data: Dict[str, Any],
) -> dict:
    """构造飞书交互卡片。"""
    price_str = data["现汇买入价"]
    price = float(price_str)
    today = beijing_time_str("%Y-%m-%d %H:%M")

    # 较上次涨跌
    if prev_price is None:
        delta_text, template = "— 首次记录", "blue"
    elif abs(price - prev_price) < 1e-6:
        delta_text, template = "— 与上次持平", "blue"
    else:
        diff = price - prev_price
        sign = "📈 上涨" if diff > 0 else "📉 下跌"
        delta_text = f"{sign} {abs(diff):.2f}"
        template = "green" if diff > 0 else "red"

    weekly_summary = build_weekly_summary(weekly_data)

    markdown = (
        f"**币种**：{currency}\n"
        f"**现汇买入价**：**{price_str}** 元 / 100{currency}\n"
        f"**发布时间**：{data.get('发布时间', '-')}\n"
        f"**中行折算价**：{data.get('中行折算价', '-')}\n"
        f"**较上次**：{delta_text}\n\n"
        f"---\n"
        f"### 本周累计\n"
        f"{weekly_summary}"
    )

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"💱 中行 {currency} 现汇买入价 · {today}",
                },
                "template": template,
            },
            "elements": [
                {"tag": "markdown", "content": markdown},
                {"tag": "hr"},
                {
                    "tag": "note",
                    "elements": [
                        {
                            "tag": "plain_text",
                            "content": "数据来源：中国银行外汇牌价 · 仅供参考，实际以银行结售汇牌价为准",
                        }
                    ],
                },
            ],
        },
    }


def push_to_feishu(card: dict) -> None:
    ts = int(time.time())
    body = {"timestamp": ts, **card}

    if LARK_SECRET:
        body["sign"] = gen_sign(LARK_SECRET, ts)

    r = requests.post(LARK_WEBHOOK, json=body, timeout=10)
    r.raise_for_status()

    resp = r.json()
    if resp.get("code") not in (0, "0"):
        raise RuntimeError(f"飞书返回错误: {resp}")

    log.info("推送成功: %s", resp)


# ============== 主流程 ==============
def main() -> int:
    log.info("开始抓取中行牌价，目标币种: %s", TARGET_CURRENCY)

    html = fetch_boc_html()
    data = parse_currency_row(html, TARGET_CURRENCY)
    log.info("解析结果: %s", data)

    cur_price = float(data["现汇买入价"])
    prev = read_last_price()

    # 如果启用阈值过滤且变动太小，则跳过推送
    if (
        SKIP_IF_UNCHANGED
        and prev is not None
        and abs(cur_price - prev) < PRICE_CHANGE_THRESHOLD
    ):
        log.info("价格变动 < 阈值，放弃推送")
        return 0

    weekly_data = update_weekly_data(cur_price, data.get("发布时间", "-"))

    card = build_card(TARGET_CURRENCY, data, prev, weekly_data)
    push_to_feishu(card)

    write_last_price(cur_price)

    log.info("执行完成，北京时间: %s", beijing_time_str())
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        log.exception("执行失败: %s", e)
        sys.exit(1)
