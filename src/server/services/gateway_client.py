"""Callback → 网关的出站调用（平台无关）

Callback 侧不持有 IM 平台凭据时，出站动作统一 POST 给网关代发。
本模块只管传输：网关地址、auth_token、HTTP 调用与异常兜底；
端点路径与请求体由各平台 adapter 构造——那是平台自己的协议。

与 services/callback_client.py 方向相反：那边是网关调用 Callback。
"""

import logging
from typing import Any, Dict, Tuple

from utils.http_client import post_json

logger = logging.getLogger(__name__)


def post_to_gateway(endpoint: str, payload: Dict[str, Any]) -> Tuple[bool, Any]:
    """POST 到网关端点

    Args:
        endpoint: 网关路径，如 '/gw/feishu/send'
        payload: 请求体

    Returns:
        (True, 响应 dict) 或 (False, 错误串)
    """
    from config import GATEWAY_URL
    from stores.auth_token_store import AuthTokenStore

    if not GATEWAY_URL:
        return False, 'no gateway configured'

    store = AuthTokenStore.get_instance()
    auth_token = store.get() if store else ''
    if not auth_token:
        return False, 'no auth_token available'

    # resp 消费一并入 try：body 为非 dict JSON（null/数组）时 post_json 返回
    # None/list，resp.get 抛 AttributeError 逃逸
    try:
        resp = post_json(GATEWAY_URL.rstrip('/') + endpoint, payload,
                         headers={'X-Auth-Token': auth_token})
        if resp.get('success'):
            return True, resp
        return False, resp.get('error', 'unknown')
    except Exception as e:
        return False, str(e)
