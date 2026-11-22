"""业务路由汇总：订阅归属专属接口先于通用资源路由注册，通用路由按模块 key 分派。"""
from __future__ import annotations

from app.routers import resource, subscriber_ops

ROUTERS = [subscriber_ops, resource]
