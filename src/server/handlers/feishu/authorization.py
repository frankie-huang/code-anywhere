"""飞书注册授权（卡片渲染与发送、授权结果处理）

register.py 的平台实现层：注册/换绑/解绑需要用户授权确认，飞书形态是
发送授权卡片、用户点卡片按钮、回写卡片状态。注册编排（HTTP 注册流程、
binding 写入决策）在 handlers/register.py，经函数调用进入本模块。

授权通过后通知 Callback 后端走 services/callback_client.py 的
notify_register_callback（与 register.py 共用）。

函数：
    - 卡片构建: _build_authorization_card / _build_register_status_card
    - 卡片发送: _send_authorization_card（HTTP 路径）/ _send_ws_authorization_card（WS 路径）
      / _notify_admin_of_registration（管理员通知）
    - 身份查询: get_bot_open_id
    - HTTP 授权结果（卡片回调入口）: handle_authorization_decision / handle_register_unbind
    - WS 授权发起（ws_handler 调用）: handle_ws_registration / handle_ws_rebind_registration
    - WS 授权结果（卡片回调入口）: handle_ws_authorization_approved /
      handle_ws_authorization_denied / handle_ws_register_unbind
"""

import json
import logging
from typing import Any, Dict, Optional

from utils.ws_protocol import ws_send_text

logger = logging.getLogger(__name__)

# 授权卡片中的安全提示文案（HTTP 和 WS 注册流程共用）
_PERMISSION_DESCRIPTION = (
    "**授权后该后端将获得以下权限：**\n"
    "• 接收你发送给机器人的所有消息\n"
    "• 向你发送消息通知（如权限请求、任务状态等）"
)
_SECURITY_WARNING = (
    "**安全风险提示：**\n"
    "• 后端可读取你的对话内容\n"
    "• 后端可主动向你推送消息\n"
    "• 请确认该后端来源可信后再授权"
)
_SECURITY_WARNING_REBIND = (
    "**安全风险提示：**\n"
    "• 旧终端将被断开连接\n"
    "• 新终端可读取你的对话内容\n"
    "• 请确认该请求来源可信后再授权"
)

def get_bot_open_id() -> Optional[str]:
    """从 FeishuAPIService 获取机器人 open_id"""
    from services.feishu_api import FeishuAPIService
    service = FeishuAPIService.get_instance()
    if service:
        return service.bot_open_id
    return None


def _build_authorization_card(title: str, content: str, approve_value: dict, deny_value: dict) -> dict:
    """构建授权卡片（允许/拒绝按钮）

    HTTP 和 WS 注册流程共用卡片骨架，仅按钮回调 value 不同。

    Args:
        title: 卡片标题
        content: 卡片正文（lark_md 格式）
        approve_value: 允许按钮的 callback value
        deny_value: 拒绝按钮的 callback value

    Returns:
        飞书卡片 JSON 字典
    """
    return {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "blue"
        },
        "body": {
            "direction": "vertical",
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": content}
                },
                {"tag": "hr"},
                {
                    "tag": "column_set",
                    "flex_mode": "none",
                    "horizontal_spacing": "8px",
                    "background_style": "default",
                    "columns": [
                        {
                            "tag": "column",
                            "width": "weighted",
                            "vertical_align": "top",
                            "elements": [{
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "允许"},
                                "type": "primary",
                                "behaviors": [{"type": "callback", "value": approve_value}]
                            }]
                        },
                        {
                            "tag": "column",
                            "width": "weighted",
                            "vertical_align": "top",
                            "elements": [{
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "拒绝"},
                                "type": "danger",
                                "behaviors": [{"type": "callback", "value": deny_value}]
                            }]
                        }
                    ]
                }
            ]
        }
    }


def _notify_admin_of_registration(
    service: Any,
    requester_id: str,
    client_ip: str,
    callback_url: str,
    old_callback_url: str = ''
):
    """发送注册通知给网关管理员

    如果未配置 FEISHU_OWNER_ID 或管理员就是请求者自己，则不发送通知。

    Args:
        service: 飞书 API 服务实例
        requester_id: 请求注册的用户 ID
        client_ip: 客户端 IP
        callback_url: Callback 后端 URL
        old_callback_url: 旧的 callback_url（有值则表示换绑场景）
    """
    from config import FEISHU_OWNER_ID as gateway_owner_id

    # 未配置管理员或管理员就是请求者自己，不需要通知
    if not gateway_owner_id or gateway_owner_id == requester_id:
        return

    # 构建通知内容（有 old_callback_url 则是换绑场景）
    is_rebind = bool(old_callback_url)
    if is_rebind:
        action_text = "换绑"
        detail = f"**旧设备**: `{old_callback_url}`\n**新设备**: `{callback_url}`"
    else:
        action_text = "注册"
        detail = f"**Callback URL**: `{callback_url}`"

    # 使用 lark_md 格式，@ 注册用户让管理员知道是谁
    content = (
        f"<at id=\"{requester_id}\"></at> 正在请求{action_text}\n\n"
        f"**来源 IP**: `{client_ip}`\n"
        f"{detail}"
    )

    # 构建简单的通知卡片
    card = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"用户{action_text}通知"},
            "template": "blue"
        },
        "body": {
            "direction": "vertical",
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": content}
                }
            ]
        }
    }

    success, result = service.send_card(json.dumps(card, ensure_ascii=False), receive_id=gateway_owner_id)

    if success:
        logger.info(f"[register] Admin notification sent to {gateway_owner_id}")
    else:
        logger.error(f"[register] Failed to send admin notification: {result}")


def _send_authorization_card(
    owner_id: str,
    client_ip: str,
    callback_url: str,
    binding_params: Optional[Dict[str, Any]] = None,
    old_callback_url: str = ''
):
    """发送飞书授权卡片（HTTP 注册模式）

    注意：不在卡片中嵌入 auth_token，而是等用户授权后再实时生成。
    用户点击允许时会验证 operator_id == owner_id，确保本人操作。

    Args:
        owner_id: 飞书用户 ID
        client_ip: 客户端 IP
        callback_url: Callback 后端 URL
        binding_params: per-user 配置参数
        old_callback_url: 旧的 callback_url（如果有，表示更换设备场景）
    """
    if binding_params is None:
        binding_params = {}
    from services.feishu_api import FeishuAPIService

    service = FeishuAPIService.get_instance()
    if not service or not service.enabled:
        logger.warning("[register] FeishuAPIService not enabled, cannot send authorization card")
        return

    # 构建卡片内容
    if old_callback_url:
        # 更换设备场景
        title = "Callback 后端更换设备请求"
        content = (
            f"**旧设备**: `{old_callback_url}`\n"
            f"**新设备**: `{callback_url}`\n"
            f"**来源 IP**: `{client_ip}`\n\n"
            f"{_PERMISSION_DESCRIPTION}\n\n"
            f"{_SECURITY_WARNING}\n\n"
            f"是否允许更换到新设备？"
        )
    else:
        # 新设备注册场景
        title = "新的 Callback 后端注册请求"
        content = (
            f"**Callback URL**: `{callback_url}`\n"
            f"**来源 IP**: `{client_ip}`\n\n"
            f"{_PERMISSION_DESCRIPTION}\n\n"
            f"{_SECURITY_WARNING}\n\n"
            f"是否允许该后端绑定？"
        )

    approve_value = {
        "action": "approve_register",
        "callback_url": callback_url,
        "owner_id": owner_id,
        "request_ip": client_ip,
        "old_callback_url": old_callback_url,
    }
    approve_value.update(binding_params)

    card = _build_authorization_card(
        title, content,
        approve_value=approve_value,
        deny_value={
            "action": "deny_register",
            "callback_url": callback_url,
            "owner_id": owner_id
        }
    )

    success, result = service.send_card(json.dumps(card, ensure_ascii=False), receive_id=owner_id)

    if success:
        logger.info(f"[register] Authorization card sent to {owner_id}")
    else:
        logger.error(f"[register] Failed to send authorization card: {result}")

    # 通知网关管理员
    _notify_admin_of_registration(
        service=service,
        requester_id=owner_id,
        client_ip=client_ip,
        callback_url=callback_url,
        old_callback_url=old_callback_url
    )


def _build_register_status_card(
    title: str,
    content: str,
    template: str,
    button: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """构建注册授权状态卡片（用于卡片回调响应）

    飞书卡片回调响应格式要求：
    {
      "type": "raw",
      "data": { ... schema 2.0 卡片数据 ... }
    }

    Args:
        title: 卡片标题
        content: 卡片内容（支持 Markdown）
        template: 标题模板颜色（green/red/blue等）
        button: 可选按钮配置（包含 behaviors 格式）

    Returns:
        卡片字典（包含 type 和 data）
    """
    elements = [
        {
            'tag': 'div',
            'text': {
                'tag': 'lark_md',
                'content': content
            }
        }
    ]

    if button:
        elements.append({'tag': 'hr'})
        elements.append({
            'tag': 'column_set',
            'flex_mode': 'none',
            'horizontal_spacing': '8px',
            'background_style': 'default',
            'columns': [
                {
                    'tag': 'column',
                    'width': 'weighted',
                    'vertical_align': 'top',
                    'elements': [
                        {
                            'tag': button.get('tag', 'button'),
                            'text': button.get('text'),
                            'type': button.get('type'),
                            'behaviors': [
                                {
                                    'type': 'callback',
                                    'value': button.get('value')
                                }
                            ]
                        }
                    ]
                }
            ]
        })

    return {
        'type': 'raw',
        'data': {
            'schema': '2.0',
            'config': {'wide_screen_mode': True},
            'header': {
                'title': {'tag': 'plain_text', 'content': title},
                'template': template
            },
            'body': {
                'direction': 'vertical',
                'elements': elements
            }
        }
    }


def handle_authorization_decision(
    callback_url: str,
    owner_id: str,
    client_ip: str,
    approved: bool,
    binding_params: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """处理用户授权决策

    Args:
        callback_url: Callback 后端 URL
        owner_id: 飞书用户 ID
        client_ip: 客户端 IP
        approved: 用户是否批准
        binding_params: per-user 配置参数

    Returns:
        飞书响应（包含 toast 和更新的卡片）
    """
    if binding_params is None:
        binding_params = {}
    from stores.binding_store import BindingStore
    from services.auth_token import generate_auth_token
    from config import FEISHU_APP_SECRET

    def _fail(toast: str, detail: str, template: str) -> Dict[str, Any]:
        """授权失败响应：toast + 状态卡片替换原授权卡（否则「允许」按钮
        还挂着，用户无从得知这次点击的结果）。卡片统一带终端标识。

        template 语义：yellow = 操作性失败、用户可重试（后端未就绪等）；
        red = 服务端故障、重试无用（配置/存储问题，需管理员介入）。
        """
        return {
            'toast': {'type': 'error', 'content': toast},
            'card': _build_register_status_card(
                title='⚠️ 授权失败',
                content=(
                    f"**Callback URL**: `{callback_url}`\n"
                    f"**来源 IP**: `{client_ip}`\n\n{detail}"
                ),
                template=template
            )
        }

    if approved:
        # 验证配置
        if not FEISHU_APP_SECRET:
            logger.error("[register] FEISHU_APP_SECRET not configured")
            return _fail('服务配置错误',
                         '服务配置错误，请联系管理员检查配置。', 'red')

        # 实时生成 auth_token
        auth_token = generate_auth_token(FEISHU_APP_SECRET, owner_id)
        logger.info("[register] Authorization approved, generated auth_token")

        # 网关侧落盘能力先行确认：store 不可用就不发起 notify，否则
        # Callback 单侧持有新 token、与网关错位
        store = BindingStore.get_instance()
        if not store:
            logger.error("[register] BindingStore not initialized, abort authorization")
            return _fail('服务内部错误', '服务内部错误，请稍后重试。', 'red')

        # 通知 Callback 接收 token，确认后才落盘 binding（与 WS 路径的
        # auth_ok_ack 两阶段一致）：失败时跳过 upsert、渲染失败卡片，
        # 两侧保持旧状态、用户可重试；若先落盘，Callback 未收到新 token
        # 会与网关错位，此后所有 /cb/* 转发 401
        from services.callback_client import notify_register_callback
        if not notify_register_callback(callback_url, owner_id, auth_token):
            logger.error("[register] Notify callback failed for %s, binding not created", owner_id)
            return _fail('通知后端服务失败',
                         '未能在该后端上完成授权。\n\n请确认后端服务已启动后重试。', 'yellow')

        # Callback 已确认：落盘 binding；写盘失败时 Callback 已持有新
        # token 而网关无记录，渲染失败卡片引导重新发起注册
        if not store.upsert(owner_id, callback_url, auth_token, client_ip,
                            binding_params=binding_params):
            logger.error("[register] BindingStore upsert failed for %s", owner_id)
            return _fail('服务内部错误',
                         '授权信息保存失败。\n\n请重新发起注册以恢复。', 'red')

        # 返回已授权卡片（带解绑按钮）
        content = (
            f"**Callback URL**: `{callback_url}`\n"
            f"**来源 IP**: `{client_ip}`\n\n"
            f"**已授权权限：**\n"
            f"• 接收你发送给机器人的所有消息\n"
            f"• 向你发送消息通知\n\n"
            f"已成功授权该后端"
        )
        return {
            'toast': {
                'type': 'success',
                'content': '已授权绑定'
            },
            'card': _build_register_status_card(
                title='✓ 已授权',
                content=content,
                template='green',
                button={
                    'tag': 'button',
                    'text': {'tag': 'plain_text', 'content': '解绑'},
                    'type': 'danger',
                    'value': {
                        'action': 'unbind_register',
                        'callback_url': callback_url,
                        'owner_id': owner_id
                    }
                }
            )
        }
    else:
        # 用户拒绝：仅删除 owner_id + callback_url 精确匹配的绑定
        toast_content = '已拒绝注册请求'
        store = BindingStore.get_instance()
        if store:
            existing = store.get(owner_id)
            if existing and existing.get('callback_url') == callback_url:
                store.delete(owner_id)
                toast_content = '已拒绝并解除绑定'
                logger.info(f"[register] User denied, deleted binding for {callback_url}: owner_id={owner_id}")

        logger.info(f"[register] User denied registration: owner_id={owner_id}")
        return {
            'toast': {
                'type': 'success' if '解除' in toast_content else 'info',
                'content': toast_content
            },
            'card': _build_register_status_card(
                title='✗ 已拒绝',
                content=f"**Callback URL**: `{callback_url}`\n\n已拒绝该后端的注册请求",
                template='red'
            )
        }


def handle_register_unbind(callback_url: str, owner_id: str) -> Dict[str, Any]:
    """处理用户解绑操作（HTTP 模式）

    Args:
        callback_url: Callback 后端 URL
        owner_id: 飞书用户 ID

    Returns:
        飞书响应（包含 toast 和更新的卡片）
    """
    from stores.binding_store import BindingStore

    # 删除绑定记录
    store = BindingStore.get_instance()
    if store:
        existing = store.get(owner_id)
        if existing and existing.get('callback_url') == callback_url:
            store.delete(owner_id)
            logger.info("[register] User unbound: owner_id=%s, callback_url=%s", owner_id, callback_url)
        else:
            logger.info("[register] User unbind (no existing binding): owner_id=%s, callback_url=%s", owner_id, callback_url)

    # 返回已解绑卡片（无按钮）
    return {
        'toast': {
            'type': 'info',
            'content': '已解绑'
        },
        'card': _build_register_status_card(
            title='✗ 已解绑',
            content=(
                f"**Callback URL**: `{callback_url}`\n\n"
                f"已解除绑定，该后端将：\n"
                f"• 无法再接收你的消息\n"
                f"• 无法再向你发送通知"
            ),
            template='grey'
        )
    }


def _send_ws_authorization_card(owner_id: str, request_id: str,
                                client_ip: str, title: str, content: str,
                                old_ip: str = '',
                                binding_params: Optional[Dict[str, Any]] = None) -> bool:
    """发送 WS 模式授权卡片（注册/换绑共用）

    每个终端独立发送自己的卡片，包含允许/拒绝按钮。
    当用户允许其中一个终端时，其他 pending 连接会被自动关闭。

    Args:
        owner_id: 飞书用户 ID
        request_id: 本次注册请求的唯一标识（UUID），用于卡片-连接匹配验证
        client_ip: 客户端 IP
        title: 卡片标题
        content: 卡片正文（lark_md 格式）
        old_ip: 旧终端 IP（有值则表示换绑场景）
        binding_params: per-user 配置参数

    Returns:
        True 表示卡片发送成功
    """
    if binding_params is None:
        binding_params = {}
    from services.feishu_api import FeishuAPIService

    service = FeishuAPIService.get_instance()
    if not service or not service.enabled:
        logger.warning("[ws_register] FeishuAPIService not enabled, cannot send authorization card")
        return False

    approve_value = {
        "action": "approve_register",
        "mode": "ws",
        "owner_id": owner_id,
        "request_id": request_id,
        "request_ip": client_ip,
    }
    approve_value.update(binding_params)

    card = _build_authorization_card(
        title, content,
        approve_value=approve_value,
        deny_value={
            "action": "deny_register",
            "mode": "ws",
            "owner_id": owner_id,
            "request_id": request_id
        }
    )

    success, result = service.send_card(json.dumps(card, ensure_ascii=False), receive_id=owner_id)

    if success:
        logger.info("[ws_register] Authorization card sent to %s: %s", owner_id, title)
    else:
        logger.error("[ws_register] Failed to send authorization card: %s", result)

    # 通知网关管理员
    _notify_admin_of_registration(
        service=service,
        requester_id=owner_id,
        client_ip=client_ip,
        callback_url=f'WS 隧道 ({client_ip})',
        old_callback_url=f'WS 隧道 ({old_ip})' if old_ip else ''
    )

    return success


def handle_ws_registration(owner_id: str, client_ip: str,
                           request_id: str,
                           binding_params: Optional[Dict[str, Any]] = None) -> bool:
    """处理 WebSocket 隧道的注册请求

    发送飞书授权卡片，用户授权后通过 WS 通道下发 auth_token。
    每个终端独立发送自己的卡片，允许其中一个时自动关闭其他 pending 连接。

    Args:
        owner_id: 飞书用户 ID
        client_ip: 客户端 IP
        request_id: 本次注册请求的唯一标识（UUID），用于卡片-连接匹配验证
        binding_params: per-user 配置参数

    Returns:
        True 表示卡片发送成功
    """
    title = "WebSocket 隧道连接请求"
    content = (
        f"**来源 IP**: `{client_ip}`\n\n"
        f"有本地 Callback 后端尝试通过 WebSocket 隧道连接。\n\n"
        f"{_PERMISSION_DESCRIPTION}\n\n"
        f"{_SECURITY_WARNING}\n\n"
        f"是否允许该连接？"
    )
    return _send_ws_authorization_card(owner_id, request_id, client_ip, title, content,
                                       binding_params=binding_params)


def handle_ws_rebind_registration(owner_id: str, client_ip: str,
                                  request_id: str, old_ip: str,
                                  binding_params: Optional[Dict[str, Any]] = None) -> bool:
    """处理 WS 模式的换绑注册请求（新终端替换旧终端）

    发送飞书授权卡片，展示旧终端 IP 和新终端 IP，用户授权后走现有
    handle_ws_authorization_approved() 逻辑。
    每个终端独立发送自己的卡片，允许其中一个时自动关闭其他 pending 连接。

    Args:
        owner_id: 飞书用户 ID
        client_ip: 新终端 IP
        request_id: 本次注册请求的唯一标识（UUID），用于卡片-连接匹配验证
        old_ip: 旧终端 IP
        binding_params: per-user 配置参数

    Returns:
        True 表示卡片发送成功
    """
    title = "WebSocket 隧道换绑请求"
    content = (
        f"**旧终端 IP**: `{old_ip or '未知'}`\n"
        f"**新终端 IP**: `{client_ip}`\n\n"
        f"有新终端尝试通过 WebSocket 隧道连接，将替换当前已绑定的终端。\n\n"
        f"{_PERMISSION_DESCRIPTION}\n\n"
        f"{_SECURITY_WARNING_REBIND}\n\n"
        f"是否允许换绑到新终端？"
    )
    return _send_ws_authorization_card(owner_id, request_id, client_ip, title, content,
                                       old_ip=old_ip,
                                       binding_params=binding_params)


def handle_ws_authorization_approved(owner_id: str, client_ip: str,
                                     request_id: str,
                                     binding_params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """处理 WS 模式授权通过

    生成 auth_token，存入 BindingStore，通过 WS 下发给客户端。

    Args:
        owner_id: 飞书用户 ID
        client_ip: 客户端 IP
        request_id: 本次注册请求的唯一标识（UUID），用于卡片-连接匹配验证
        binding_params: per-user 配置参数

    Returns:
        飞书响应（包含 toast 和更新的卡片）
    """
    if not request_id:
        logger.warning("[ws_register] Authorization approved but request_id is empty for %s", owner_id)
        return {
            'toast': {'type': 'error', 'content': '请求参数异常'},
        }

    from services.auth_token import generate_auth_token
    from services.ws_registry import WebSocketRegistry
    from config import FEISHU_APP_SECRET

    if not FEISHU_APP_SECRET:
        logger.error("[ws_register] FEISHU_APP_SECRET not configured")
        return {
            'toast': {
                'type': 'error',
                'content': '服务配置错误'
            }
        }

    # 生成 auth_token
    auth_token = generate_auth_token(FEISHU_APP_SECRET, owner_id)
    logger.info("[ws_register] Authorization approved, generated auth_token for %s", owner_id)

    # 通过 WS 下发 auth_token
    # BindingStore 更新延迟到消息循环收到 auth_ok_ack 后执行，
    # 确保客户端已确认收到新 token 后再持久化（与续期路径一致）
    registry = WebSocketRegistry.get_instance()
    if not registry:
        logger.error("[ws_register] WebSocketRegistry not initialized")
        return {
            'toast': {'type': 'error', 'content': '服务内部错误'},
            'card': _build_register_status_card(
                title='⚠️ 授权失败',
                content='服务内部错误，请稍后重试。',
                template='red'
            )
        }

    pending_conn = registry.get_pending(owner_id, request_id)
    if not pending_conn:
        logger.warning("[ws_register] No pending connection for %s, request_id=%s", owner_id, request_id)
        return {
            'toast': {'type': 'error', 'content': '连接已断开或不存在'},
            'card': _build_register_status_card(
                title='⚠️ 授权失败',
                content=(
                    f"**来源 IP**: `{client_ip}`\n\n"
                    f"该终端的连接已断开或不存在。\n\n"
                    f"可能已被其他操作处理，请刷新后重试。"
                ),
                template='yellow'
            )
        }

    try:
        # 原子地设置 auth_token、绑定参数，并关闭其他 pending 连接
        # 防止客户端极快回复 auth_ok_ack 时消息循环找不到 token
        # 同时验证 pending 连接仍然存在（防止 get_pending 与此处之间的竞态)
        # 注意：session_mode 等参数已在注册时存入 pending_binding_params
        # 此处传入的参数会与已有参数合并（不覆盖已有值）
        card_binding_params = dict(binding_params or {})
        card_binding_params['client_ip'] = client_ip
        if not registry.prepare_authorization(owner_id, request_id, auth_token, card_binding_params):
            logger.warning("[ws_register] prepare_authorization failed: connection gone for %s, request_id=%s", owner_id, request_id)
            return {
                'toast': {'type': 'error', 'content': '连接已断开或不存在'},
                'card': _build_register_status_card(
                    title='⚠️ 授权失败',
                    content=(
                        f"**来源 IP**: `{client_ip}`\n\n"
                        f"该终端的连接已断开或不存在。\n\n"
                        f"可能已被其他操作处理，请刷新后重试。"
                    ),
                    template='yellow'
                )
            }

        from handlers.ws_handler import ws_send_auth_ok
        ws_send_auth_ok(pending_conn, auth_token)
        logger.info("[ws_register] Sent auth_ok to %s via WS, request_id=%s, waiting for auth_ok_ack", owner_id, request_id)
    except Exception as e:
        logger.error("[ws_register] Failed to send auth_ok: %s", e)
        return {
            'toast': {'type': 'error', 'content': '发送授权信息失败'},
            'card': _build_register_status_card(
                title='⚠️ 授权失败',
                content=(
                    f"**来源 IP**: `{client_ip}`\n\n"
                    f"向客户端发送授权信息失败。\n\n"
                    f"请重新启动后端服务后重试。"
                ),
                template='yellow'
            )
        }

    # 返回已授权卡片（带解绑按钮）
    content = (
        f"**来源 IP**: `{client_ip}`\n\n"
        f"**已授权权限：**\n"
        f"• 接收你发送给机器人的所有消息\n"
        f"• 向你发送消息通知\n\n"
        f"已成功授权 WebSocket 隧道连接"
    )
    return {
        'toast': {
            'type': 'success',
            'content': '已授权绑定'
        },
        'card': _build_register_status_card(
            title='✓ 已授权',
            content=content,
            template='green',
            button={
                'tag': 'button',
                'text': {'tag': 'plain_text', 'content': '解绑'},
                'type': 'danger',
                'value': {
                    'action': 'unbind_register',
                    'mode': 'ws',
                    'callback_url': 'ws://tunnel',
                    'owner_id': owner_id
                }
            }
        )
    }


def handle_ws_authorization_denied(owner_id: str, request_id: str) -> Dict[str, Any]:
    """处理 WS 模式授权拒绝（单个终端）

    通过 WS 发送 auth_error 消息，关闭指定的 pending 连接。

    注意：即使 pending 连接已断开（如超时），仍返回"已拒绝"而非"连接已断开"。
    原因：用户点击拒绝表示不想绑定该终端，无论连接状态如何，"不绑定"这个
    结果都达成了，无需区分内部状态。这与授权通过场景不同——授权通过时
    如果连接断开，用户的期望（绑定成功）无法达成，需要告知失败。

    Args:
        owner_id: 飞书用户 ID
        request_id: 请求 ID（用于指定拒绝哪个终端）

    Returns:
        飞书响应（包含 toast 和更新的卡片）
    """
    if not request_id:
        logger.warning("[ws_register] Authorization denied but request_id is empty for %s", owner_id)
        return {
            'toast': {'type': 'error', 'content': '请求参数异常'},
        }

    from services.ws_registry import WebSocketRegistry

    # 通过 WS 发送 auth_error，然后直接关闭 socket
    # 注意：不能调用 ws_close()，因为消息循环线程正在同一个 socket 上 recv()，
    # 跨线程操作 socket 不安全。直接 close() 会让消息循环收到异常后退出。
    registry = WebSocketRegistry.get_instance()
    if registry:
        pending_conn = registry.get_pending(owner_id, request_id)
        if pending_conn:
            try:
                msg = json.dumps({
                    'type': 'auth_error',
                    'action': 'stop',
                    'message': 'authorization denied'
                })
                ws_send_text(pending_conn, msg)
            except Exception as e:
                logger.debug("[ws_register] Error sending auth_error: %s", e)
            # 直接关闭 socket，消息循环会因 ConnectionError 退出并清理
            try:
                pending_conn.close()
            except Exception:
                pass

        registry.remove_pending(owner_id, request_id)

    logger.info("[ws_register] Authorization denied for %s, request_id=%s", owner_id, request_id)

    return {
        'toast': {
            'type': 'info',
            'content': '已拒绝该终端的连接请求'
        },
        'card': _build_register_status_card(
            title='✗ 已拒绝',
            content="已拒绝该终端的 WebSocket 隧道连接请求",
            template='red'
        )
    }


def handle_ws_register_unbind(owner_id: str) -> Dict[str, Any]:
    """处理 WS 模式用户解绑操作

    关闭 WebSocket 连接并删除绑定记录。

    Args:
        owner_id: 飞书用户 ID

    Returns:
        飞书响应（包含 toast 和更新的卡片）
    """
    from stores.binding_store import BindingStore
    from services.ws_registry import WebSocketRegistry

    # 关闭 WebSocket 连接
    # 注意：不能调用 ws_close()，因为消息循环线程正在同一个 socket 上 recv()，
    # ws_close() 会发送 close frame 后阻塞等待响应（最多 2 秒），跨线程不安全。
    # 直接 close() 会让消息循环收到异常后退出并清理。
    registry = WebSocketRegistry.get_instance()
    if registry:
        ws_conn = registry.get(owner_id)
        if ws_conn:
            try:
                msg = json.dumps({'type': 'unbind', 'action': 'stop', 'message': 'user unbind'})
                ws_send_text(ws_conn, msg)
            except Exception as e:
                logger.debug("[ws_register] Error sending unbind: %s", e)
            try:
                ws_conn.close()
            except Exception:
                pass
        registry.unregister(owner_id)

    # 删除绑定记录
    store = BindingStore.get_instance()
    if store:
        existing = store.get(owner_id)
        if existing and existing.get('callback_url', '').startswith('ws'):
            store.delete(owner_id)
            logger.info("[ws_register] WS user unbound: owner_id=%s", owner_id)
        else:
            logger.info("[ws_register] WS user unbind (no existing binding): owner_id=%s", owner_id)

    return {
        'toast': {
            'type': 'info',
            'content': '已解绑'
        },
        'card': _build_register_status_card(
            title='✗ 已解绑',
            content=(
                "已解除 WebSocket 隧道绑定，该后端将：\n"
                "• 无法再接收你的消息\n"
                "• 无法再向你发送通知"
            ),
            template='grey'
        )
    }
