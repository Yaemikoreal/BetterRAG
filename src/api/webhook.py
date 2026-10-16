# -*- coding: utf-8 -*-
"""
飞书/钉钉卡片消息推送与交互卡片渲染 (Webhook Integration)
严格实现 docs/BetterRAG生产级开发实施方案.md §4.5 与 §5.6
"""
from __future__ import annotations

import logging
from typing import Optional, Dict, Any
import httpx

from src.engine.models import Block, ConflictRecord

logger = logging.getLogger(__name__)


def build_feishu_conflict_card(
    conflict: ConflictRecord,
    defender: Block,
    challenger: Block,
    callback_base_url: str = "http://localhost:8000",
) -> Dict[str, Any]:
    """
    构造飞书富文本交互式审批卡片：
    - 展示新旧规版本与条款内容对照
    - 提供【一键接替】与【保持旧规】交互按钮
    """
    return {
        "msg_type": "interactive",
        "card": {
            "config": {
                "wide_screen_mode": True
            },
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"🚨 制度互斥红灯告警 (命中 {conflict.clash_count} 次)"
                },
                "template": "red"
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**检测到潜在制度冲突，已在问答中连续碰撞 {conflict.clash_count} 次，请管理员核实裁决。**"
                    }
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": False,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**【现有权威旧规】** (置信度: {defender.conf_stored:.2f} · {defender.context_prefix or '正文'})\n> {defender.content}"
                            }
                        },
                        {
                            "is_short": False,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**【提议挑战新规】** (置信度: {challenger.conf_stored:.2f} · {challenger.context_prefix or '正文'})\n> {challenger.content}"
                            }
                        }
                    ]
                },
                {"tag": "hr"},
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "✅ 一键接替 (新规转正/旧规归档)"
                            },
                            "type": "primary",
                            "value": {
                                "action": "replace",
                                "conflict_id": conflict.id,
                                "challenger_id": challenger.id,
                                "defender_id": defender.id
                            }
                        },
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "❌ 驳回新规 (维持权威旧规)"
                            },
                            "type": "danger",
                            "value": {
                                "action": "reject",
                                "conflict_id": conflict.id,
                                "challenger_id": challenger.id
                            }
                        }
                    ]
                }
            ]
        }
    }


async def send_feishu_conflict_card(
    webhook_url: str,
    conflict: ConflictRecord,
    defender: Block,
    challenger: Block,
    callback_base_url: str = "http://localhost:8000",
) -> bool:
    """通过飞书自定义机器人 Webhook 发送交互卡片"""
    if not webhook_url or webhook_url.startswith("https://open.feishu.cn/open-apis/bot/v2/hook/xxxxxx"):
        logger.debug("未配置有效的飞书 Webhook URL，跳过卡片发送")
        return False

    card_payload = build_feishu_conflict_card(conflict, defender, challenger, callback_base_url)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(webhook_url, json=card_payload)
            if resp.status_code == 200:
                logger.info("成功推送飞书冲突卡片: conflict_id=%s", conflict.id)
                return True
            else:
                logger.warning("飞书卡片推送失败: status=%s, body=%s", resp.status_code, resp.text)
                return False
    except Exception as e:
        logger.error("飞书 Webhook 发送异常: %s", e)
        return False
