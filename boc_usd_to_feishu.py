#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中行外汇牌价 → 飞书机器人播报（美元现汇买入价）
- 数据源：https://www.boc.cn/sourcedb/whpj/
- 字段：现汇买入价（每 100 美元兑人民币）
- 推送：飞书自定义机器人（interactive 卡片）
"""
import os
import sys
import time
import hmac
import base64
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict
import requests
from bs4 import BeautifulSoup

# ============== 配置（用 GitHub Secrets 注入） ==============
LARK_WEBHOOK = os.getenv("LARK_WEBHOOK", "")
LARK_SECRET = os.getenv("LARK_SECRET", "")
TARGET_CURRENCY = os.getenv("TARGET_CURRENCY", "美元")

BOC_URL = "https://www.boc.cn/sourcedb/whpj/"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("boc-feishu")

# BOC 表格列顺序（按实际页面）
LABELS = [
    "货币名称", "现汇买入价", "现钞买入价", "现汇卖出价",
    "现钞卖出价", "中行折算价", "发布日期", "发布时间",
]

_last_price_file = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".last_price"
)


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
            r.encoding = "utf-8"
            if "现汇买入价" not in r.text:
                raise ValueError("返回内容异常，未识别到目标字段")
            return r.text
        except Exception as e:
            last_err = e
            log.warning("第 %d 次抓取失败: %s", i + 1, e)
            time.sleep(2 * (i + 1))
    raise RuntimeError(f"抓取中行页面失败：{last_err}")


def parse_currency_row(html: str, currency: str = "美元") -> Dict[str, str]:
    """用 BeautifulSoup 解析指定币种那一行。"""
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table")

    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if not cells:
                continue
            first = cells[0].get_text(strip=True)
            if first == currency:
                cell_texts = [c.get_text(strip=True) for c in cells]
                if len(cell_texts) < len(LABELS):
                    raise ValueError(
                        f"字段数量异常: got {len(cell_texts)}, expect {len(LABELS)}"
                    )
                return dict(zip(LABELS, cell_texts))

    raise ValueError(f"未在页面找到币种「{currency}」")


# ============== 飞书推送 ==============
def gen_sign(secret: str, ts: int) -> str:
    string_to_sign = f"{ts}\n{secret}"
    h = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(h).decode("utf-8")


def build_card(currency: str, data: Dict[str, str], prev_price: Optional[float]) -> dict:
    price_str = data["现汇买入价"]
    price = float(price_str)
    # 强制用北京时间（UTC+8），避免 GitHub Actions runner 的 UTC 时区干扰
    BJ_TZ = timezone(timedelta(hours=8))
    today = datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M")

    if prev_price is None or abs(price - prev_price) < 1e-6:
        delta_text, template = "— 与上次持平", "blue"
    else:
        diff = price - prev_price
        sign = "📈 上涨" if diff > 0 else "📉 下跌"
        delta_text = f"{sign} {abs(diff):.2f}"
        template = "green" if diff > 0 else "red"

    markdown = (
        f"**币种**：{currency}\n"
        f"**现汇买入价**：**{price_str}** 元 / 100{currency}\n"
        f"**发布时间**：{data.get('发布时间', '-')}\n"
        f"**中行折算价**：{data.get('中行折算价', '-')}\n"
        f"**较上次**：{delta_text}"
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
def read_last_price() -> Optional[float]:
    try:
        with open(_last_price_file, "r", encoding="utf-8") as f:
            content = f.read().strip()
            return float(content) if content else None
    except (FileNotFoundError, ValueError):
        return None


def write_last_price(price: float) -> None:
    with open(_last_price_file, "w", encoding="utf-8") as f:
        f.write(f"{price:.4f}")


def main() -> int:
    log.info("开始抓取中行牌价，目标币种: %s", TARGET_CURRENCY)
    html = fetch_boc_html()
    data = parse_currency_row(html, TARGET_CURRENCY)
    log.info("解析结果: %s", data)

    cur_price = float(data["现汇买入价"])
    prev = read_last_price()

    card = build_card(TARGET_CURRENCY, data, prev)
    push_to_feishu(card)
    write_last_price(cur_price)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log.exception("执行失败: %s", e)
        sys.exit(1)
