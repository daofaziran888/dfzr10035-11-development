"""订阅归属场景验证：甲乙各收库存主题一次（甲v1、乙v2），交叉核对甲换地址前后的记录。

覆盖的归属规则：
1. 消费回执按订阅身份归属，主题相同不能替代订阅身份；
2. 一笔交付（一个版本）只关联一条接收记录，其他订阅不能改动本份归属；
3. 改地址后历史已确认消费仍归原订阅，旧地址迟到回执照常签收；
4. 补发记录由归属过滤的消费凭据生成，归属判定同步补发待办并携带新旧地址关系；
5. 乙的消费明细保持独立。

运行：cd backend && .venv/bin/python tests/test_subscriber_ownership.py
也可被 pytest 直接收集（test_* 函数）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.subscriber import SubscriberService  # noqa: E402
from app.store import store  # noqa: E402

SVC = SubscriberService()
JIA_ID = 4   # 订阅方甲 SUBS-0004，库存主题，旧地址 old-a
YI_ID = 5    # 订阅方乙 SUBS-0005，库存主题
ADDR_A_OLD = "https://old-a.example.com/hook"
ADDR_A_NEW = "https://new-a.example.com/hook"
ADDR_B = "https://b.example.com/hook"


def _sub(entry_id: int) -> dict:
    sub = store.find("subscriber", entry_id)
    assert sub is not None, f"订阅 {entry_id} 应存在"
    return sub


def test_seed_ready() -> None:
    jia, yi = _sub(JIA_ID), _sub(YI_ID)
    assert jia["数据主题"] == yi["数据主题"] == "库存主题"
    assert jia["推送地址"] == ADDR_A_OLD and yi["推送地址"] == ADDR_B
    versions = {v["版本编号"]: v for v in store.rows("data_version") if v["数据来源"] == "库存主题"}
    assert versions["DATA-STK-V1"]["版本标签"] == "v1"
    assert versions["DATA-STK-V2"]["版本标签"] == "v2"
    print("✓ 种子就绪：甲乙同订阅库存主题，v1/v2 快照可用")


def test_receive_creates_owned_receipt() -> None:
    receipt, msg = SVC.receive_version(JIA_ID, "DATA-STK-V1")
    assert receipt is not None, msg
    assert receipt["归属订阅编号"] == "SUBS-0004"
    assert receipt["版本编号"] == "DATA-STK-V1"
    assert receipt["接收地址"] == ADDR_A_OLD
    assert receipt["签收状态"] == "待签收"
    receipt_yi, msg = SVC.receive_version(YI_ID, "DATA-STK-V2")
    assert receipt_yi is not None, msg
    assert receipt_yi["归属订阅编号"] == "SUBS-0005"
    print(f"✓ 甲领取 v1（{receipt['回执编号']}）、乙领取 v2（{receipt_yi['回执编号']}），各归各的订阅身份")


def test_one_delivery_one_receipt() -> None:
    dup, msg = SVC.receive_version(JIA_ID, "DATA-STK-V1")
    assert dup is None and "已领取过" in msg
    robbed, msg = SVC.receive_version(YI_ID, "DATA-STK-V1")
    assert robbed is None
    assert "一笔交付只能关联一个接收记录" in msg and "SUBS-0004" in msg
    print(f"✓ 一笔交付只关联一条接收记录：乙抢领 v1 被拒（{msg}）")


def test_change_address_keeps_history() -> None:
    entry, msg = SVC.change_address(JIA_ID, ADDR_A_NEW)
    assert entry is not None, msg
    assert entry["推送地址"] == ADDR_A_NEW
    assert entry["地址历史"] == [{"旧地址": ADDR_A_OLD, "新地址": ADDR_A_NEW, "变更时间": "2026-11-22"}]
    receipt = store.rows("consume_receipt")[0]
    assert receipt["接收地址"] == ADDR_A_OLD, "历史回执的接收地址必须保持签收时快照"
    print(f"✓ 甲换地址 old→new，地址历史留痕；回执接收地址仍为旧地址（{msg}）")


def test_late_receipt_from_old_address_still_owned() -> None:
    receipt_id = store.rows("consume_receipt")[0]["id"]
    receipt, msg = SVC.sign_receipt(JIA_ID, receipt_id, sign_address=ADDR_A_OLD)
    assert receipt is not None, msg
    assert receipt["签收状态"] == "已签收"
    assert receipt["归属订阅编号"] == "SUBS-0004"
    assert "旧地址" in receipt["地址备注"] or "不一致" in receipt["地址备注"]
    print(f"✓ 旧地址迟到回执签收成功，归属仍归甲（{receipt['地址备注']}）")


def test_peer_cannot_sign_or_confirm() -> None:
    receipt_id = store.rows("consume_receipt")[0]["id"]
    denied, msg = SVC.sign_receipt(YI_ID, receipt_id, sign_address=ADDR_B)
    assert denied is None and "仅拥有主题访问权限" in msg
    print(f"✓ 乙不能签收甲的回执（{msg}）")


def test_consumption_detail_isolated_by_owner() -> None:
    detail_jia = SVC.consumption_detail(JIA_ID)
    assert detail_jia is not None
    assert [d["版本编号"] for d in detail_jia["明细"]] == ["DATA-STK-V1"]
    assert detail_jia["明细"][0]["版本批次"]["版本标签"] == "v1"
    assert detail_jia["当前推送地址"] == ADDR_A_NEW
    assert detail_jia["明细"][0]["接收地址"] == ADDR_A_OLD, "换地址前的领取记录保持原地址"
    peers = detail_jia["同主题其他订阅"]
    assert peers == [{"订阅编号": "SUBS-0005", "订阅方": "订阅方乙", "回执数": 1}]
    detail_yi = SVC.consumption_detail(YI_ID)
    assert detail_yi is not None
    assert [d["版本编号"] for d in detail_yi["明细"]] == ["DATA-STK-V2"]
    assert detail_yi["明细"][0]["版本批次"]["版本标签"] == "v2"
    print("✓ 交叉核对：甲的明细只有 v1（换地址前后归属不变），乙的明细只有 v2，互相独立")


def test_redelivery_flow_with_ownership() -> None:
    receipt = store.rows("consume_receipt")[0]
    task, msg = SVC.request_redelivery(JIA_ID, receipt["id"])
    assert task is not None, msg
    assert task["status"] == "待重试"
    assert task["原回执编号"] == receipt["回执编号"]
    assert task["归属订阅编号"] == "SUBS-0004"
    assert task["版本编号"] == "DATA-STK-V1"
    assert task["原接收地址"] == ADDR_A_OLD and task["新接收地址"] == ADDR_A_NEW
    assert "SUBS-0004" in task["归属判定"]
    print(f"✓ 补发待办 {task['任务编号']} 已生成，携带新地址与原回执关系及归属判定")

    dup, msg = SVC.request_redelivery(JIA_ID, receipt["id"])
    assert dup is None and "不能重复发起" in msg
    print("✓ 同一回执的补发待办不能重复发起")

    todos = SVC.redelivery_todos(JIA_ID)
    assert todos is not None and len(todos["待办"]) == 1
    assert todos["待办"][0]["新接收地址"] == ADDR_A_NEW
    todos_yi = SVC.redelivery_todos(YI_ID)
    assert todos_yi is not None and todos_yi["待办"] == [], "乙没有补发待办"

    denied, msg = SVC.confirm_redelivery(YI_ID, task["id"])
    assert denied is None and "仅拥有主题访问权限" in msg
    print(f"✓ 乙不能确认甲的补发任务（{msg}）")

    done, msg = SVC.confirm_redelivery(JIA_ID, task["id"])
    assert done is not None, msg
    assert done["status"] == "已成功" and done["队列状态"] == "已补发确认"
    assert receipt["签收状态"] == "已补发"
    print(f"✓ 甲确认补发：任务已成功，原回执标记已补发（{msg}）")


def test_call_log_links_version_batch() -> None:
    logs = [
        l for l in store.rows("call_log")
        if "sub=SUBS-0004" in str(l.get("请求参数", ""))
        and "version=DATA-STK-V1" in str(l.get("请求参数", ""))
    ]
    events = {str(l["记录编号"]) for l in logs}
    assert any("RECEIVE" in e for e in events), "领取应写消费日志"
    assert any("SIGN" in e for e in events), "签收应写消费日志"
    assert any("REDELIVER" in e for e in events), "补发确认应写消费日志"
    assert all("receipt=RCPT-SUBS-0004" in l["请求参数"] for l in logs)
    print(f"✓ 消费日志已关联订阅身份+版本批次+回执编号（{len(logs)} 条）")


def run_all() -> None:
    steps = [
        test_seed_ready,
        test_receive_creates_owned_receipt,
        test_one_delivery_one_receipt,
        test_change_address_keeps_history,
        test_late_receipt_from_old_address_still_owned,
        test_peer_cannot_sign_or_confirm,
        test_consumption_detail_isolated_by_owner,
        test_redelivery_flow_with_ownership,
        test_call_log_links_version_batch,
    ]
    for step in steps:
        step()
    print(f"\n全部 {len(steps)} 个场景断言通过：归属判定、地址变更、补发待办、明细隔离均符合预期")


if __name__ == "__main__":
    run_all()
