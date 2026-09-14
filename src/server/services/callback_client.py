"""网关侧调用 Callback 后端的客户端（平台无关）

网关侧收到 IM 侧请求后，把请求送达 Callback 后端的传输层：
按 binding 的 callback_url 协议选择通道（WS 隧道 / HTTP），带上鉴权。
通用转发（forward_via_ws_or_http）只管「送过去、拿回来」，端点语义与
响应的编排处理（发通知、存映射等）由调用方负责；仅当某个端点被多个
调用方共用时（如注册通知、决策转发），才在此提供语义化封装以免各自
重复实现。

与 services/gateway_client.py 方向相反：那边是 Callback 侧调用网关。
"""

import logging
import socket
import urllib.error
from typing import Any, Dict, Optional, Tuple

from utils.http_client import DEFAULT_HTTP_TIMEOUT, post_json

logger = logging.getLogger(__name__)


def forward_via_ws_or_http(binding: Dict[str, Any], endpoint: str,
                           payload: Dict[str, Any],
                           timeout: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """通过 WS 或 HTTP 转发请求到 Callback

    根据 callback_url 协议决定转发方式：
    - ws:// 或 wss:// → 通过 WebSocket 隧道转发
    - http:// 或 https:// → 通过 HTTP 请求转发

    从 binding 字典中提取路由信息（owner_id、callback_url、auth_token）。

    Args:
        binding: 绑定信息字典（包含 _owner_id、callback_url、auth_token）
        endpoint: API 端点（如 /cb/decision, /cb/agent/new）
        payload: 请求数据
        timeout: 请求超时（秒），默认使用各通道的默认超时

    Returns:
        响应数据，失败返回 None
    """
    from services.ws_registry import WebSocketRegistry

    owner_id = binding.get('_owner_id', '')
    callback_url = binding.get('callback_url', '')
    auth_token = binding.get('auth_token', '')

    # 根据 callback_url 协议决定转发方式
    is_ws_mode = callback_url.startswith(('ws://', 'wss://'))

    if is_ws_mode:
        # 尝试通过 WS 转发
        registry = WebSocketRegistry.get_instance()
        if owner_id and registry and registry.is_authenticated(owner_id):
            # 获取该连接的 auth_token 用于本地 handler 验证
            ws_auth_token = registry.get_auth_token(owner_id)
            headers = {'X-Auth-Token': ws_auth_token} if ws_auth_token else {}
            response = registry.send_request(owner_id, endpoint, payload, headers, timeout=timeout)
            if response is not None:
                # WS 隧道返回格式: {status: HTTP码, body: 业务响应}
                # 提取 body 作为真正的业务响应
                return response.get('body', response)
        logger.warning("[callback-client] WS tunnel not available for %s", owner_id)
        return None

    # HTTP 模式（ws:// 或 wss:// 是 WS 隧道地址，不能用于 HTTP 请求）
    if callback_url:
        api_url = f"{callback_url.rstrip('/')}{endpoint}"
        http_timeout = int(timeout) if timeout else DEFAULT_HTTP_TIMEOUT
        logger.debug("[callback-client] Using HTTP for %s: %s", owner_id, api_url)
        try:
            return post_json(api_url, payload, headers={'X-Auth-Token': auth_token}, timeout=http_timeout)
        except Exception as e:
            logger.error("[callback-client] HTTP request failed: %s", e)
            return None

    logger.warning("[callback-client] No callback_url configured for %s", owner_id)
    return None


def forward_decision(binding: Dict[str, Any], payload: Dict[str, Any],
                     timeout: Optional[float] = 2.0
                     ) -> Optional[Tuple[bool, Optional[str], str]]:
    """转发决策到 Callback 的 /cb/decision（权限审批、问卷回答等共用）

    只收口传输与响应解析（success/decision/message 三元组）；决策的
    呈现（toast 文案、卡片更新、Typing）是调用方语义，不在此层。

    Args:
        binding: 绑定信息（含 callback_url / auth_token / _owner_id）
        payload: 决策请求体（action / request_id 及各自附加字段，由调用方组装）
        timeout: 转发超时。默认 2s——飞书卡片回调要求 3s 内响应，
            为后续呈现处理预留余量

    Returns:
        (success, decision, message) 三元组；None 表示无可用路由或
        传输失败，调用方按「回调服务不可达」处理
    """
    response_data = forward_via_ws_or_http(
        binding, '/cb/decision', payload, timeout=timeout)
    if response_data is None:
        return None
    return (response_data.get('success', False),
            response_data.get('decision'),
            response_data.get('message', ''))


def notify_register_callback(callback_url: str, owner_id: str,
                             auth_token: str) -> bool:
    """通知 Callback 后端 auth_token

    Args:
        callback_url: Callback 后端 URL
        owner_id: IM 用户 ID
        auth_token: 认证令牌

    Returns:
        True 表示 Callback 确认接收；False 表示送达失败或 Callback 拒绝
        （业务拒绝如 owner_id mismatch / 存储失败走 400，与网络故障一样
        落到异常分支）。调用方据此决定是否落盘新 token——失败时跳过
        upsert 可保持两侧 token 一致，链路在旧 token 下继续可用。
    """
    api_url = f"{callback_url.rstrip('/')}/cb/register"

    request_data = {
        'owner_id': owner_id,
        'auth_token': auth_token,
        'gateway_version': '1.0.0'
    }

    # 附带平台附加字段（飞书为 bot_open_id，供 Callback 端存储）
    from platforms import get_im_adapter
    request_data.update(get_im_adapter().get_gateway_metadata())

    logger.info(f"[register] Calling {api_url}")

    try:
        response_data = post_json(api_url, request_data, headers={'X-Auth-Token': auth_token}, timeout=DEFAULT_HTTP_TIMEOUT)
        logger.info(f"[register] Callback response: {response_data}")
        return True

    except urllib.error.HTTPError as e:
        logger.error(f"[register] Callback HTTP error: {e.code} {e.reason}")
    except urllib.error.URLError as e:
        logger.error(f"[register] Callback URL error: {e.reason}")
    except socket.timeout:
        logger.error("[register] Callback timeout")
    except Exception as e:
        logger.error(f"[register] Callback error: {e}")
    return False
