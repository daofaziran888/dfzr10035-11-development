"""订阅管理领域服务：管理数据订阅，与 webhook_router、data_version、call_log、data_cache 联动。

新增「主题消费回执 → 订阅版本批次明细」归属能力：
- 订阅方按自身身份领取主题下的数据版本，生成归属该订阅的消费回执（接收记录）；
- 归属键是订阅身份，主题相同不能替代订阅身份；改地址不改历史回执归属；
- 一笔交付只关联一条接收记录，其他订阅只有主题访问权限也不能改动归属；
- 按归属过滤的消费凭据可生成补发记录，同步到重放队列（补发待办），
  并携带新地址与原回执的关系，供接收方确认。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

TODAY = "2026-11-22"
RECEIPT_MODULE = "consume_receipt"
REDELIVERY_MODULE = "retry_queue"
# 数据版本处于这些状态时才允许订阅方领取
CLAIMABLE_VERSION_STATUS = {"可用", "标注中"}


class SubscriberService(BaseModuleService):
    MODULE = "subscriber"
    ENTITY = "订阅方"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["订阅编号", "订阅方", "数据主题", "推送地址", "推送频率", "消费状态", "通知方式", "最后推送"]
    REQUIRED_FIELDS = ["订阅编号", "订阅方", "数据主题"]
    STATUS_ORDER = ["待激活", "推送中", "消费确认", "已停订"]
    ACTION_RULES = {"激活订阅": "推送中", "确认消费": "消费确认", "停订服务": "已停订"}
    NEGATIVE_ACTIONS = []

    def run_action(self, entry_id: int, action: str):
        """激活订阅时创建webhook规则和数据版本；确认消费时更新调用日志；停订时清理缓存。"""
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        if action == "激活订阅":
            self._create_webhook_rule(entry)
            self._create_data_version(entry)
            message += "，已创建Webhook路由规则和数据版本"
        elif action == "确认消费":
            self._log_consume_event(entry)
            message += "，已记录消费日志"
        elif action == "停订服务":
            self._cleanup_cache_refs(entry)
            message += "，已清理关联缓存引用"
        return entry, message

    # ---------- 消费回执：领取 / 签收 / 地址变更 ----------

    def receive_version(self, entry_id: int, version_no: str) -> tuple[dict[str, Any] | None, str]:
        """订阅方按自身身份领取主题下的数据版本，生成归属该订阅的消费回执。

        归属规则：一笔交付（一个版本）只关联一条接收记录；归属键是订阅身份，
        其他订阅即使订阅了同一主题，也不能认领已被领取的交付。
        """
        sub = store.find(self.MODULE, entry_id)
        if sub is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        if sub.get("status") == "已停订":
            return None, f"订阅 {sub.get('订阅编号')} 已停订，不能领取新版本"
        topic = sub.get("数据主题", "")
        version_no = (version_no or "").strip()
        version = self._find_version(version_no)
        if version is None:
            return None, f"数据版本 {version_no} 不存在或已归档"
        if version.get("数据来源") != topic:
            return None, (
                f"版本 {version_no} 属于来源「{version.get('数据来源')}」，"
                f"与本订阅主题「{topic}」不符，不能跨来源领取"
            )
        if version.get("status") not in CLAIMABLE_VERSION_STATUS:
            return None, f"版本 {version_no} 当前状态「{version.get('status')}」，不可领取"
        sub_code = sub.get("订阅编号", "")
        for receipt in store.rows(RECEIPT_MODULE):
            if receipt.get("版本编号") != version_no:
                continue
            owner = receipt.get("归属订阅编号", "")
            if owner == sub_code:
                return None, (
                    f"订阅 {sub_code} 已领取过版本 {version_no}"
                    f"（回执 {receipt.get('回执编号')}），不能重复领取"
                )
            return None, (
                f"版本 {version_no} 的交付已归属订阅 {owner}"
                f"（回执 {receipt.get('回执编号')}）：一笔交付只能关联一个接收记录，"
                f"订阅 {sub_code} 仅拥有主题访问权限，不能改动本份归属"
            )
        receipt = self._new_receipt(sub, version)
        store.rows(RECEIPT_MODULE).append(receipt)
        sub["消费状态"] = "待签收"
        sub["最后推送"] = TODAY
        self._log_consume_with_version(sub, receipt, "receive")
        return receipt, (
            f"订阅 {sub_code} 已领取版本 {version_no}，"
            f"生成消费回执 {receipt['回执编号']}（归属订阅身份，与主题其他订阅方隔离）"
        )

    def sign_receipt(
        self,
        entry_id: int,
        receipt_id: int,
        sign_address: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """签收消费回执。归属校验只认订阅身份：旧地址的迟到回执仍归原订阅。"""
        sub = store.find(self.MODULE, entry_id)
        if sub is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        receipt = store.find(RECEIPT_MODULE, receipt_id)
        if receipt is None:
            return None, f"消费回执 {receipt_id} 不存在或已归档"
        denied = self._check_receipt_owner(sub, receipt, "签收")
        if denied:
            return None, denied
        if receipt.get("签收状态") != "待签收":
            return None, f"回执 {receipt.get('回执编号')} 当前状态「{receipt.get('签收状态')}」，不能重复签收"
        address = (sign_address or "").strip() or sub.get("推送地址", "")
        receipt["签收状态"] = "已签收"
        receipt["status"] = "已签收"
        receipt["pending"] = False
        receipt["签收地址"] = address
        receipt["签收时间"] = TODAY
        extra = ""
        current = sub.get("推送地址", "")
        if address != current:
            receipt["地址备注"] = (
                f"签收地址 {address} 与当前推送地址 {current} 不一致："
                "改地址不影响归属，历史已确认消费仍归原订阅"
            )
            extra = "；旧地址回执迟到抵达，归属仍归原订阅"
        sub["消费状态"] = "已确认"
        self._log_consume_with_version(sub, receipt, "sign")
        return receipt, f"回执 {receipt.get('回执编号')} 已签收（归属 {receipt.get('归属订阅编号')}）{extra}"

    def change_address(self, entry_id: int, new_address: str) -> tuple[dict[str, Any] | None, str]:
        """更换接收地址：记录地址历史并同步Webhook规则；历史回执归属与接收地址快照不变。"""
        sub = store.find(self.MODULE, entry_id)
        if sub is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        new_address = (new_address or "").strip()
        if not new_address:
            return None, "新接收地址不能为空"
        old_address = sub.get("推送地址", "")
        if new_address == old_address:
            return None, "新地址与当前推送地址相同，无需变更"
        history = sub.setdefault("地址历史", [])
        history.append({"旧地址": old_address, "新地址": new_address, "变更时间": TODAY})
        sub["推送地址"] = new_address
        # 同步该订阅的Webhook路由规则目标地址
        sid = str(sub["id"]).zfill(4)
        synced = 0
        for rule in store.rows("webhook_router"):
            if rule.get("规则编号") == f"WEBH-SUB-{sid}":
                rule["目标URL"] = new_address
                synced += 1
        kept = sum(
            1 for r in store.rows(RECEIPT_MODULE)
            if r.get("归属订阅编号") == sub.get("订阅编号")
        )
        message = (
            f"订阅 {sub.get('订阅编号')} 接收地址已变更：{old_address} → {new_address}"
            f"；历史 {kept} 条消费回执仍归原订阅，接收地址保持签收时快照"
        )
        if synced:
            message += f"；已同步 {synced} 条Webhook规则目标地址"
        return sub, message

    # ---------- 归属过滤的消费明细与补发待办 ----------

    def consumption_detail(self, entry_id: int) -> dict[str, Any] | None:
        """按订阅归属过滤的消费明细：只含本订阅实际领取记录对应的版本批次。

        同一主题下其他订阅方的回执只计数、不并入明细，保证乙的消费明细保持独立。
        """
        sub = store.find(self.MODULE, entry_id)
        if sub is None:
            return None
        topic = sub.get("数据主题", "")
        sub_code = sub.get("订阅编号", "")
        details = []
        for receipt in store.rows(RECEIPT_MODULE):
            if receipt.get("归属订阅编号") != sub_code:
                continue
            details.append({
                "回执编号": receipt.get("回执编号"),
                "版本编号": receipt.get("版本编号"),
                "接收地址": receipt.get("接收地址"),
                "签收地址": receipt.get("签收地址", ""),
                "签收状态": receipt.get("签收状态"),
                "归属依据": receipt.get("归属依据"),
                "地址备注": receipt.get("地址备注", ""),
                "补发任务编号": receipt.get("补发任务编号", ""),
                "领取时间": receipt.get("领取时间"),
                "签收时间": receipt.get("签收时间", ""),
                "版本批次": self._version_brief(receipt.get("版本编号", "")),
            })
        peers = []
        for other in store.rows(self.MODULE):
            if other.get("数据主题") != topic or other.get("订阅编号") == sub_code:
                continue
            peer_code = other.get("订阅编号", "")
            peers.append({
                "订阅编号": peer_code,
                "订阅方": other.get("订阅方", ""),
                "回执数": sum(
                    1 for r in store.rows(RECEIPT_MODULE)
                    if r.get("归属订阅编号") == peer_code
                ),
            })
        return {
            "订阅编号": sub_code,
            "订阅方": sub.get("订阅方", ""),
            "数据主题": topic,
            "当前推送地址": sub.get("推送地址", ""),
            "地址历史": sub.get("地址历史", []),
            "归属口径": (
                "消费明细按订阅身份归属过滤：仅含本订阅实际领取记录对应的版本批次；"
                "主题相同不能替代订阅身份，改地址不影响历史已确认消费的归属"
            ),
            "明细": details,
            "同主题其他订阅": peers,
        }

    def request_redelivery(self, entry_id: int, receipt_id: int) -> tuple[dict[str, Any] | None, str]:
        """用按归属过滤的消费凭据生成可执行的补发记录，同步到重放队列（补发待办）。

        补发记录携带新地址与原回执的关系，归属判定结果一并写入待办，供接收方确认。
        """
        sub = store.find(self.MODULE, entry_id)
        if sub is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        receipt = store.find(RECEIPT_MODULE, receipt_id)
        if receipt is None:
            return None, f"消费回执 {receipt_id} 不存在或已归档"
        denied = self._check_receipt_owner(sub, receipt, "发起补发")
        if denied:
            return None, denied
        if receipt.get("签收状态") == "已补发":
            return None, f"回执 {receipt.get('回执编号')} 已完成补发，不能重复发起"
        for task in store.rows(REDELIVERY_MODULE):
            if task.get("原回执编号") == receipt.get("回执编号") and task.get("status") in ("待重试", "重试中"):
                return None, (
                    f"回执 {receipt.get('回执编号')} 已存在补发待办 {task.get('任务编号')}"
                    f"（状态 {task.get('status')}），不能重复发起"
                )
        sub_code = sub.get("订阅编号", "")
        new_address = sub.get("推送地址", "")
        old_address = receipt.get("接收地址", "")
        ownership = (
            f"归属判定：回执 {receipt.get('回执编号')} 归订阅 {sub_code} 所有"
            f"（按订阅身份判定，主题相同不替代订阅身份）；"
            f"原接收地址 {old_address} → 新接收地址 {new_address}"
        )
        tasks = store.rows(REDELIVERY_MODULE)
        rid = str(receipt["id"]).zfill(4)
        task = {
            "id": max((int(t.get("id", 0)) for t in tasks), default=0) + 1,
            "任务编号": f"RETR-REDELIV-{rid}",
            "失败端点": receipt.get("数据主题", ""),
            "失败原因": f"地址变更补发：{ownership}",
            "重试次数": 0,
            "最大重试": 3,
            "退避策略": "立即补发",
            "最后重试": "",
            "队列状态": "待补发",
            # 归属判定扩展字段：补发待办携带新地址与原回执的关系
            "原回执编号": receipt.get("回执编号"),
            "归属订阅编号": sub_code,
            "版本编号": receipt.get("版本编号"),
            "原接收地址": old_address,
            "新接收地址": new_address,
            "归属判定": ownership,
            "创建时间": TODAY,
            "status": "待重试", "pending": True, "abnormal": False,
        }
        tasks.append(task)
        receipt["补发任务编号"] = task["任务编号"]
        return task, (
            f"已凭回执 {receipt.get('回执编号')}（归属 {sub_code}）生成补发待办 "
            f"{task['任务编号']}：版本 {receipt.get('版本编号')} 将补发至新地址 {new_address}，"
            "归属判定已同步待办，等待接收方确认"
        )

    def confirm_redelivery(self, entry_id: int, task_id: int) -> tuple[dict[str, Any] | None, str]:
        """接收方确认补发：校验任务归属后执行补发，原回执标记为已补发。"""
        sub = store.find(self.MODULE, entry_id)
        if sub is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        task = store.find(REDELIVERY_MODULE, task_id)
        if task is None:
            return None, f"补发任务 {task_id} 不存在或已归档"
        sub_code = sub.get("订阅编号", "")
        owner = task.get("归属订阅编号")
        if owner != sub_code:
            return None, (
                f"补发任务 {task.get('任务编号')} 归属订阅 {owner}，"
                f"订阅 {sub_code} 仅拥有主题访问权限，不能确认本份归属"
            )
        if task.get("status") not in ("待重试", "重试中"):
            return None, f"补发任务 {task.get('任务编号')} 当前状态「{task.get('status')}」，不能确认"
        task["status"] = "已成功"
        task["pending"] = False
        task["队列状态"] = "已补发确认"
        task["最后重试"] = TODAY
        task["重试次数"] = int(task.get("重试次数", 0) or 0) + 1
        receipt = self._find_receipt_by_code(task.get("原回执编号", ""))
        if receipt is not None:
            receipt["签收状态"] = "已补发"
            receipt["status"] = "已补发"
            receipt["pending"] = False
            receipt["补发完成时间"] = TODAY
        sub["消费状态"] = "已确认"
        if receipt is not None:
            self._log_consume_with_version(sub, receipt, "redeliver")
        return task, (
            f"补发任务 {task.get('任务编号')} 已确认执行：版本 {task.get('版本编号')} "
            f"已按归属依据补发至新地址 {task.get('新接收地址')}，"
            f"原回执 {task.get('原回执编号')} 标记为已补发"
        )

    def redelivery_todos(self, entry_id: int) -> dict[str, Any] | None:
        """该订阅的补发待办列表：按归属过滤，携带新地址与原回执的关系。"""
        sub = store.find(self.MODULE, entry_id)
        if sub is None:
            return None
        sub_code = sub.get("订阅编号", "")
        todos = [
            {
                "任务编号": t.get("任务编号"),
                "版本编号": t.get("版本编号"),
                "原回执编号": t.get("原回执编号"),
                "原接收地址": t.get("原接收地址"),
                "新接收地址": t.get("新接收地址"),
                "归属判定": t.get("归属判定"),
                "状态": t.get("status"),
                "创建时间": t.get("创建时间", ""),
            }
            for t in store.rows(REDELIVERY_MODULE)
            if t.get("归属订阅编号") == sub_code
        ]
        return {"订阅编号": sub_code, "订阅方": sub.get("订阅方", ""), "待办": todos}

    # ---------- 内部工具 ----------

    def _find_version(self, version_no: str) -> dict[str, Any] | None:
        for version in store.rows("data_version"):
            if version.get("版本编号") == version_no:
                return version
        return None

    def _find_receipt_by_code(self, receipt_code: str) -> dict[str, Any] | None:
        for receipt in store.rows(RECEIPT_MODULE):
            if receipt.get("回执编号") == receipt_code:
                return receipt
        return None

    def _check_receipt_owner(self, sub: dict, receipt: dict, operation: str) -> str | None:
        """归属校验：回执只认领取时的订阅身份，其他订阅不能改动本份归属。"""
        sub_code = sub.get("订阅编号", "")
        owner = receipt.get("归属订阅编号", "")
        if owner != sub_code:
            return (
                f"回执 {receipt.get('回执编号')} 归属订阅 {owner}，"
                f"订阅 {sub_code} 仅拥有主题访问权限，不能{operation}本份归属"
            )
        return None

    def _new_receipt(self, sub: dict, version: dict) -> dict[str, Any]:
        receipts = store.rows(RECEIPT_MODULE)
        next_id = max((int(r.get("id", 0)) for r in receipts), default=0) + 1
        sub_code = sub.get("订阅编号", "")
        return {
            "id": next_id,
            "回执编号": f"RCPT-{sub_code}-{str(next_id).zfill(4)}",
            "归属订阅": f"{sub.get('订阅方', '')}({sub_code})",
            "归属订阅编号": sub_code,
            "数据主题": sub.get("数据主题", ""),
            "版本编号": version.get("版本编号", ""),
            "接收地址": sub.get("推送地址", ""),
            "签收状态": "待签收",
            "归属依据": (
                f"订阅 {sub_code} 于 {TODAY} 实际领取版本 {version.get('版本编号', '')}；"
                "归属键为订阅身份，主题相同不替代订阅身份"
            ),
            "领取时间": TODAY,
            "签收时间": "",
            "status": "待签收", "pending": True, "abnormal": False,
        }

    def _version_brief(self, version_no: str) -> dict[str, Any] | None:
        version = self._find_version(version_no)
        if version is None:
            return None
        return {
            "版本标签": version.get("版本标签", ""),
            "行数统计": version.get("行数统计", ""),
            "Schema摘要": version.get("Schema摘要", ""),
            "创建时间": version.get("创建时间", ""),
            "版本状态": version.get("status", ""),
        }

    def _log_consume_with_version(self, sub: dict, receipt: dict, event: str):
        """消费日志关联订阅身份与版本批次：消费日志不再只有主题，可回溯到回执。"""
        log_rows = store.rows("call_log")
        sid = str(sub["id"]).zfill(4)
        log_rows.append({
            "id": max((int(r.get("id", 0)) for r in log_rows), default=0) + 1,
            "记录编号": f"CALL-SUB-{sid}-{event.upper()}",
            "接口端点": receipt.get("数据主题", ""),
            "请求参数": (
                f"consume:sub={sub.get('订阅编号', '')},"
                f"version={receipt.get('版本编号', '')},"
                f"receipt={receipt.get('回执编号', '')}"
            ),
            "响应状态": "200",
            "响应耗时(ms)": 50,
            "返回行数": 1,
            "错误信息": "",
            "调用时间": TODAY,
            "status": "成功", "pending": False, "abnormal": False,
        })

    # ---------- 既有联动逻辑 ----------

    def _create_webhook_rule(self, sub: dict):
        """订阅激活时创建对应Webhook路由规则。"""
        wh_rows = store.rows("webhook_router")
        sid = str(sub["id"]).zfill(4)
        wh_rows.append({
            "id": max((int(r.get("id", 0)) for r in wh_rows), default=0) + 1,
            "规则编号": f"WEBH-SUB-{sid}",
            "触发事件": sub.get("数据主题", ""),
            "目标URL": sub.get("推送地址", ""),
            "签名方式": "HMAC-SHA256",
            "重试上限": 3,
            "超时秒数": 30,
            "投递状态": "就绪",
            "最后触发": "",
            "status": "草稿", "pending": True, "abnormal": False,
        })

    def _create_data_version(self, sub: dict):
        """订阅激活时创建数据版本追溯。"""
        ver_rows = store.rows("data_version")
        sid = str(sub["id"]).zfill(4)
        ver_rows.append({
            "id": max((int(r.get("id", 0)) for r in ver_rows), default=0) + 1,
            "版本编号": f"DATA-SUB-{sid}",
            "数据来源": sub.get("数据主题", ""),
            "行数统计": 0,
            "版本标签": "sub_init",
            "Schema摘要": sub.get("推送频率", ""),
            "创建原因": f"订阅{sid}激活",
            "依赖版本": "v0",
            "创建时间": "2026-11-17",
            "status": "创建中", "pending": True, "abnormal": False,
        })

    def _log_consume_event(self, sub: dict):
        """消费确认时写调用日志。"""
        log_rows = store.rows("call_log")
        sid = str(sub["id"]).zfill(4)
        log_rows.append({
            "id": max((int(r.get("id", 0)) for r in log_rows), default=0) + 1,
            "记录编号": f"CALL-SUB-{sid}",
            "接口端点": sub.get("数据主题", ""),
            "请求参数": sub.get("订阅方", ""),
            "响应状态": "200",
            "响应耗时(ms)": 50,
            "返回行数": 1,
            "错误信息": "",
            "调用时间": "2026-11-17",
            "status": "成功", "pending": False, "abnormal": False,
        })

    def _cleanup_cache_refs(self, sub: dict):
        """停订时清理关联缓存策略引用标记。"""
        topic = sub.get("数据主题", "")
        for cache in store.rows("data_cache"):
            if cache.get("目标端点") == topic:
                cache["命中次数"] = str(max(0, int(cache.get("命中次数", 0)) - 1))

    def check_subscription_chain(self, topic: str) -> dict:
        """跨模块订阅链路检查：订阅方→Webhook路由→数据版本→缓存→投递管道。"""
        subs = [s for s in store.rows("subscriber") if s.get("数据主题") == topic]
        whs = [w for w in store.rows("webhook_router") if w.get("触发事件") == topic]
        vers = [v for v in store.rows("data_version") if v.get("数据来源") == topic]
        caches = [c for c in store.rows("data_cache") if c.get("目标端点") == topic]
        deliveries = [d for d in store.rows("data_delivery") if d.get("数据来源") == topic]
        receipts = [r for r in store.rows(RECEIPT_MODULE) if r.get("数据主题") == topic]
        return {
            "topic": topic,
            "subscribers": len(subs),
            "webhook_rules": len(whs),
            "data_versions": len(vers),
            "cache_policies": len(caches),
            "delivery_pipelines": len(deliveries),
            "consume_receipts": len(receipts),
            "chain_complete": len(whs) > 0 and len(vers) > 0,
        }
