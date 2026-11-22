"""订阅归属专属接口：消费回执领取/签收、地址变更、归属过滤消费明细与补发待办。

与通用资源路由（/api/{module}）并存：通用路由不承载归属逻辑，
归属判定只允许走这里的订阅身份维度接口。路径均为 /api/subscriber/{id}/... 三段以上，
与通用路由 /api/{module}/{entry_id}（两段）及 /api/{module}/{entry_id}/actions（字面 actions）不冲突。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services.subscriber import SubscriberService

router = APIRouter(prefix="/api/subscriber", tags=["订阅归属与补发"])


class ReceivePayload(BaseModel):
    版本编号: str = Field(min_length=1)


class ChangeAddressPayload(BaseModel):
    新地址: str = Field(min_length=1)


class SignPayload(BaseModel):
    签收地址: str | None = None


def _service() -> SubscriberService:
    return SubscriberService()


@router.get("/{entry_id}/consumption", response_model=dict)
def consumption_detail(entry_id: int) -> dict[str, Any]:
    """按订阅归属过滤的消费明细：只含本订阅实际领取记录对应的版本批次。"""
    detail = _service().consumption_detail(entry_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"订阅方 {entry_id} 不存在或已归档")
    return detail


@router.get("/{entry_id}/redelivery-todos", response_model=dict)
def redelivery_todos(entry_id: int) -> dict[str, Any]:
    """该订阅的补发待办：携带新地址与原回执的关系及归属判定。"""
    todos = _service().redelivery_todos(entry_id)
    if todos is None:
        raise HTTPException(status_code=404, detail=f"订阅方 {entry_id} 不存在或已归档")
    return todos


@router.post("/{entry_id}/receive", response_model=ActionResult)
def receive_version(entry_id: int, payload: ReceivePayload) -> ActionResult:
    """订阅方按自身身份领取主题下的数据版本，生成归属该订阅的消费回执。"""
    receipt, message = _service().receive_version(entry_id, payload.版本编号)
    return ActionResult(ok=receipt is not None, message=message, entry=receipt)


@router.post("/{entry_id}/change-address", response_model=ActionResult)
def change_address(entry_id: int, payload: ChangeAddressPayload) -> ActionResult:
    """更换接收地址：历史已确认消费仍归原订阅，回执接收地址保持签收时快照。"""
    entry, message = _service().change_address(entry_id, payload.新地址)
    return ActionResult(ok=entry is not None, message=message, entry=entry)


@router.post("/{entry_id}/receipts/{receipt_id}/sign", response_model=ActionResult)
def sign_receipt(entry_id: int, receipt_id: int, payload: SignPayload) -> ActionResult:
    """签收消费回执：归属校验只认订阅身份，旧地址迟到回执仍归原订阅。"""
    receipt, message = _service().sign_receipt(entry_id, receipt_id, payload.签收地址)
    return ActionResult(ok=receipt is not None, message=message, entry=receipt)


@router.post("/{entry_id}/receipts/{receipt_id}/redeliver", response_model=ActionResult)
def request_redelivery(entry_id: int, receipt_id: int) -> ActionResult:
    """用按归属过滤的消费凭据生成补发记录，归属判定同步到补发待办。"""
    task, message = _service().request_redelivery(entry_id, receipt_id)
    return ActionResult(ok=task is not None, message=message, entry=task)


@router.post("/{entry_id}/redelivery/{task_id}/confirm", response_model=ActionResult)
def confirm_redelivery(entry_id: int, task_id: int) -> ActionResult:
    """接收方确认补发：校验任务归属后执行，原回执标记为已补发。"""
    task, message = _service().confirm_redelivery(entry_id, task_id)
    return ActionResult(ok=task is not None, message=message, entry=task)
