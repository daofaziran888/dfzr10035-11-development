"""消费回执领域服务：主题消费回执台账，按订阅归属关联数据版本批次明细。

回执是归属判定的唯一凭据：
- 归属键是订阅身份（订阅编号），不是数据主题——同一主题下多个订阅方各自独立；
- 一笔交付（一个数据版本的投递）只关联一条接收记录，其他订阅即使拥有
  同一主题的访问权限，也不能签收、补发或改动本份归属；
- 回执只能由订阅管理模块的领取/签收/补发动作生成与流转，本模块只提供
  只读台账视图，不开放手工登记和直接动作，避免归属链路被旁路修改。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ConsumeReceiptService(BaseModuleService):
    MODULE = "consume_receipt"
    ENTITY = "消费回执"
    OPERATION_TERM = "核对"
    LIST_FIELDS = ["回执编号", "归属订阅", "数据主题", "版本编号", "接收地址", "签收状态", "归属依据", "领取时间"]
    REQUIRED_FIELDS = ["回执编号", "归属订阅", "数据主题", "版本编号"]
    STATUS_ORDER = ["待签收", "已签收", "已补发", "已作废"]
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []

    def create_entry(self, values: dict[str, Any]):
        """回执不允许手工登记：只能由订阅方领取数据版本时生成，保证归属凭据完整。"""
        return None, ["消费回执不能手工登记，请通过订阅管理的领取接口生成"]

    def run_action(self, entry_id: int, action: str):
        """回执状态只能经订阅归属校验接口流转（签收/补发/确认），不开放直接动作。"""
        return None, (
            f"消费回执不支持直接动作「{action}」，"
            "请通过订阅管理的签收/补发接口按归属校验后流转"
        )

    def receipts_of(self, sub_code: str) -> list[dict[str, Any]]:
        """按归属订阅过滤回执：归属键是订阅身份，不是数据主题。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("归属订阅编号") == sub_code
        ]

    def find_receipt(self, receipt_id: int) -> dict[str, Any] | None:
        return store.find(self.MODULE, receipt_id)
