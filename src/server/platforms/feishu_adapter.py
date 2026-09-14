"""飞书 IM 平台适配器（Server 端）

把 IMAdapter 定义的平台无关行为映射到飞书具体实现：
    - OpenAPI 服务 → services/feishu_api.py 的 FeishuAPIService
    - 长连接 → services/feishu_longpoll.py

当前实现生命周期方法（initialize_runtime / shutdown_runtime / get_owner_id /
platform_name）、Callback 侧出站（cb_*）、注册授权、入站事件解析（parse_event）
与 GroupCapable 的群聊动作；卡片构建随对应通路收口逐步补齐。
"""

import json
import logging
from typing import Any, Callable, Dict, Optional, Tuple

from services.gateway_client import post_to_gateway

from .base import GroupCapable, IMAdapter
from .models import IMEvent, IMEventKind

logger = logging.getLogger(__name__)


def _determine_event_mode() -> str:
    """确定飞书事件接收模式

    auto 模式下自动检测：
    - lark-oapi 已安装（需 Python >= 3.8）→ longpoll
    - lark-oapi 未安装 → http

    Returns:
        事件接收模式: http / longpoll
    """
    from config import FEISHU_EVENT_MODE
    from services.feishu_longpoll import is_longpoll_available
    available = is_longpoll_available()

    if FEISHU_EVENT_MODE == 'longpoll':
        if not available:
            logger.warning("FEISHU_EVENT_MODE=longpoll but prerequisites not met "
                           "(need Python >= 3.8 and lark-oapi SDK), falling back to HTTP")
            return 'http'
        return 'longpoll'

    if FEISHU_EVENT_MODE == 'http':
        return 'http'

    if FEISHU_EVENT_MODE != 'auto':
        logger.warning("Unknown FEISHU_EVENT_MODE=%s, falling back to auto", FEISHU_EVENT_MODE)

    # auto: 有 SDK 则 longpoll，否则 http
    mode = 'longpoll' if available else 'http'
    logger.info("Event mode auto-detected: %s", mode)
    return mode


class FeishuAdapter(IMAdapter, GroupCapable):
    """飞书平台适配器"""

    def __init__(self) -> None:
        # 网关端点表首次调用后缓存（adapter 为进程内单例）
        self._gateway_routes: Optional[Dict[str, Callable[..., Tuple[bool, dict]]]] = None

    def platform_name(self) -> str:
        return 'feishu'

    def get_owner_id(self) -> str:
        from config import FEISHU_OWNER_ID
        return FEISHU_OWNER_ID

    def initialize_runtime(self, runtime_dir: str, has_gateway: bool) -> bool:
        """启动飞书运行时：OpenAPI 服务 + 长连接（仅 openapi 模式需要）

        webhook 模式无需建立连接，通知直接 POST 到 webhook URL。
        openapi 模式下：
            - 有 APP 凭据（单机 / 纯网关）→ 初始化 API 服务，按需起长连接
            - 无凭据但配置了网关（分离部署 callback 侧）→ 跳过，IM 连接由网关持有

        飞书无自有 store，runtime_dir 未使用。

        Returns:
            恒为 True（飞书缺凭据时降级告警而非阻断启动，与改造前行为一致）
        """
        from config import (FEISHU_APP_ID, FEISHU_APP_SECRET, FEISHU_SEND_MODE,
                            get_config)
        from services.feishu_api import FeishuAPIService

        if FEISHU_SEND_MODE != 'openapi':
            logger.info("Feishu send mode: %s", FEISHU_SEND_MODE)
            if not get_config('FEISHU_WEBHOOK_URL', ''):
                logger.warning("FEISHU_WEBHOOK_URL not set - webhook notifications will be skipped")
            return True

        if FEISHU_APP_ID and FEISHU_APP_SECRET:
            FeishuAPIService.initialize()
            logger.info("Feishu OpenAPI service initialized (mode: %s)", FEISHU_SEND_MODE)
        elif has_gateway:
            # 分离部署 callback 侧：凭据在网关服务上
            logger.info("Feishu OpenAPI mode: using gateway (credentials not required)")
        else:
            logger.warning("FEISHU_SEND_MODE requires FEISHU_APP_ID and FEISHU_APP_SECRET")

        # 长连接（事件接收）：同样只在持有凭据时启动
        if _determine_event_mode() == 'longpoll':
            if FEISHU_APP_ID and FEISHU_APP_SECRET:
                from services.feishu_longpoll import start_feishu_longpoll
                client = start_feishu_longpoll(
                    FEISHU_APP_ID, FEISHU_APP_SECRET, self._on_longpoll_event)
                if client:
                    logger.info("Feishu longpoll mode enabled")
                else:
                    logger.warning("Feishu longpoll mode failed to start (SDK not available?)")
            elif has_gateway:
                logger.info("Feishu longpoll: skipped (callback backend, gateway handles connection)")
            else:
                logger.warning("longpoll mode requires FEISHU_APP_ID and FEISHU_APP_SECRET")

        return True

    def shutdown_runtime(self) -> None:
        """停止飞书长连接客户端（未启动时为 no-op）"""
        from services.feishu_longpoll import stop_feishu_longpoll
        stop_feishu_longpoll()
        logger.info("[shutdown] Feishu longpoll client stopped")

    # --- Callback 侧出站 ---

    @staticmethod
    def _standalone_service():
        """单机部署下可直发的 OpenAPI 服务；分离部署或服务不可用时返回 None

        返回 None 表示「本端发不出去」，调用方转走网关代发——这也覆盖了单机部署
        但 FeishuAPIService 未就绪的情况（网关地址此时指向本机）。
        """
        from config import IS_CALLBACK_BACKEND
        if IS_CALLBACK_BACKEND:
            return None
        try:
            from services.feishu_api import FeishuAPIService
            service = FeishuAPIService.get_instance()
            return service if service and service.enabled else None
        except Exception as e:
            logger.warning("[outbound] FeishuAPIService unavailable: %s", e)
            return None

    def cb_reply_text(self, text: str, receive_id: str,
                      message_id: str = '') -> Tuple[bool, str]:
        service = self._standalone_service()
        if service:
            # 永不抛异常（旧 outbound 契约）：调用方在裸后台线程执行回调，
            # 抛出会跳过 running_pid 清理与队列 drain
            try:
                return service.reply_or_send_text(text, receive_id, 'chat_id', message_id)
            except Exception as e:
                return False, str(e)

        data = {'msg_type': 'text', 'content': text,
                'owner_id': self.get_owner_id(), 'chat_id': receive_id}
        if message_id:
            data['reply_to_message_id'] = message_id
        ok, resp = post_to_gateway('/gw/feishu/send', data)
        if not ok:
            return False, resp
        return True, resp.get('message_id', '')

    def cb_reply_markdown(self, markdown: str, receive_id: str,
                          message_id: str = '') -> Tuple[bool, str]:
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "body": {
                "direction": "vertical",
                "elements": [{"tag": "markdown", "content": markdown}]
            }
        }

        service = self._standalone_service()
        if service:
            try:
                return service.reply_or_send_card(
                    json.dumps(card, ensure_ascii=False), receive_id, 'chat_id', message_id)
            except Exception as e:
                return False, str(e)

        data = {'msg_type': 'interactive', 'content': card,
                'owner_id': self.get_owner_id(), 'chat_id': receive_id}
        if message_id:
            data['reply_to_message_id'] = message_id
        ok, resp = post_to_gateway('/gw/feishu/send', data)
        if not ok:
            return False, resp
        return True, resp.get('message_id', '')

    def cb_add_typing(self, receive_id: str = '',
                      message_id: str = '') -> None:
        self._cb_reaction('/gw/feishu/add-reaction', message_id,
                          lambda service: service.add_reaction(message_id, 'Typing'))

    def cb_remove_typing(self, receive_id: str = '',
                         message_id: str = '') -> None:
        self._cb_reaction('/gw/feishu/remove-reaction', message_id,
                          lambda service: service.remove_reaction(message_id, 'Typing'))

    def _cb_reaction(self, endpoint: str, message_id: str, direct_call) -> None:
        """Typing 表情的增删：单机直调 API，分离部署经网关；失败只记日志"""
        if not message_id:
            return

        service = self._standalone_service()
        if service:
            try:
                direct_call(service)
            except Exception as e:
                logger.warning("[cb-reaction] failed: %s", e)
            return

        ok, err = post_to_gateway(endpoint, {
            'owner_id': self.get_owner_id(),
            'message_id': message_id,
            'emoji_type': 'Typing',
        })
        if not ok:
            logger.warning("[cb-reaction] gateway fallback failed: %s", err)

    # --- 注册授权 ---

    def get_auth_secret(self, binding_params: Dict[str, Any]) -> str:
        from config import FEISHU_APP_SECRET
        return FEISHU_APP_SECRET

    def start_authorization(self, owner_id: str, client_ip: str,
                            callback_url: str, binding_params: Dict[str, Any],
                            old_callback_url: str = '') -> None:
        from handlers.feishu.authorization import _send_authorization_card
        _send_authorization_card(owner_id, client_ip, callback_url,
                                 binding_params, old_callback_url)

    def start_ws_authorization(self, owner_id: str, client_ip: str,
                               request_id: str, binding_params: Dict[str, Any],
                               old_ip: Optional[str] = None) -> bool:
        from handlers.feishu.authorization import (handle_ws_registration,
                                                   handle_ws_rebind_registration)
        # old_ip is not None 即换绑（含旧 IP 未知的 ''），与 ws_handler
        # 原 token 不匹配分支的分流一致，不能按 IP 真值判断
        if old_ip is not None:
            return handle_ws_rebind_registration(owner_id, client_ip, request_id,
                                                 old_ip, binding_params)
        return handle_ws_registration(owner_id, client_ip, request_id,
                                      binding_params)

    def get_gateway_metadata(self) -> Dict[str, Any]:
        from handlers.feishu.authorization import get_bot_open_id
        bot_open_id = get_bot_open_id()
        return {'bot_open_id': bot_open_id} if bot_open_id else {}

    def default_binding_params(self) -> Dict[str, Any]:
        from config import (FEISHU_REPLY_IN_THREAD, FEISHU_SESSION_MODE,
                            FEISHU_GROUP_NAME_PREFIX, FEISHU_GROUP_DISSOLVE_DAYS,
                            FEISHU_GROUP_PREFIX_CHAT_ID, FEISHU_GROUP_ALLOW_COWORK)
        return {
            'reply_in_thread': FEISHU_REPLY_IN_THREAD,
            'session_mode': FEISHU_SESSION_MODE,
            'group_name_prefix': FEISHU_GROUP_NAME_PREFIX,
            'group_dissolve_days': FEISHU_GROUP_DISSOLVE_DAYS,
            'group_prefix_chat_id': FEISHU_GROUP_PREFIX_CHAT_ID,
            'group_allow_cowork': FEISHU_GROUP_ALLOW_COWORK,
        }

    # --- 网关侧入站 ---

    def gateway_routes(self) -> Dict[str, Callable[..., Tuple[bool, dict]]]:
        """飞书网关端点（Callback 侧经 HTTP 调用，与 cb_* 的网关代发目标一致）

        表内容固定，首次调用后缓存（adapter 为进程内单例）。
        """
        if self._gateway_routes is None:
            from handlers.feishu import (handle_add_reaction, handle_create_group,
                                         handle_remove_reaction, handle_send_message)
            self._gateway_routes = {
                '/gw/feishu/send': handle_send_message,
                '/gw/feishu/create-group': handle_create_group,
                '/gw/feishu/add-reaction': handle_add_reaction,
                '/gw/feishu/remove-reaction': handle_remove_reaction,
            }
        return self._gateway_routes

    def handle_inbound_event(self, raw: Dict[str, Any],
                             verify_token: bool = True) -> Tuple[bool, dict]:
        """飞书事件回调入口（URL 验证 / 消息事件 / 卡片回传交互）"""
        from handlers.feishu import handle_feishu_request
        return handle_feishu_request(raw, skip_token_validation=not verify_token)

    def _on_longpoll_event(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """长连接事件入口：与 HTTP 回调共用 handle_inbound_event

        连接已通过 App ID/Secret 认证，故跳过事件 token 校验。纯透传、
        不在本层捕获异常——两个调用方（feishu_longpoll 的消息/卡片包装器）
        各自兜底，并用该模块 logger 记录（落 log/feishu_longpoll/）。

        Returns:
            response dict（卡片回调时包含 toast 等字段）
        """
        _, response = self.handle_inbound_event(data, verify_token=False)
        return response

    def parse_event(self, raw: Dict[str, Any]) -> Optional[IMEvent]:
        """解析飞书事件（url_verification / 消息事件 / 卡片回调）为 IMEvent

        字段提取逻辑与 handlers/feishu/__init__.py 原实现逐字段一致，
        迁移历史见该文件 _handle_message_event。
        """
        from handlers.feishu.content import (build_mention_resolution,
                                             extract_message_text)

        # URL 验证请求（飞书配置事件订阅时下发，应答 challenge）
        if raw.get('type') == 'url_verification':
            event = IMEvent(IMEventKind.VERIFICATION)
            event.raw = raw
            return event

        header = raw.get('header', {})
        event_type = header.get('event_type', '')
        event_id = header.get('event_id', '')

        if event_type == 'im.message.receive_v1':
            message = raw.get('event', {}).get('message', {})
            sender_id_obj = raw.get('event', {}).get('sender', {}).get('sender_id', {})

            # 构建 @提及 替换表（bot→删除，人员→@name(user_id)）并判断是否 @bot
            mention_resolution, is_at_bot = build_mention_resolution(message.get('mentions'))
            # 解析消息纯文本内容（@ 占位符在解析过程中按 resolution 表替换）
            text = extract_message_text(
                message.get('message_type', ''), message.get('content', '{}'),
                mention_resolution)

            event = IMEvent(IMEventKind.MESSAGE)
            event.raw = raw
            event.event_id = event_id
            event.message_id = message.get('message_id', '')
            event.chat_id = message.get('chat_id', '')
            event.chat_type = message.get('chat_type', '')  # p2p / group
            event.message_type = message.get('message_type', '')  # text / image / ...
            event.parent_id = message.get('parent_id', '')  # 非空表示回复消息
            event.sender_open_id = sender_id_obj.get('open_id', '')
            event.sender_user_id = sender_id_obj.get('user_id', event.sender_open_id)
            event.sender_id_values = [v for v in sender_id_obj.values() if v]
            event.text = text
            event.is_at_bot = is_at_bot
            return event

        if event_type == 'card.action.trigger':
            inner = raw.get('event', {})
            operator = inner.get('operator', {})
            action = inner.get('action', {})

            event = IMEvent(IMEventKind.CARD_ACTION)
            event.raw = raw
            event.event_id = event_id
            event.action_value = action.get('value', {})
            event.action_name = action.get('name', '')
            event.form_value = action.get('form_value', {})
            event.card_message_id = inner.get('context', {}).get('open_message_id', '')
            event.operator_open_id = operator.get('open_id', '')
            event.operator_user_id = operator.get('user_id', event.operator_open_id)
            event.operator_id_values = [v for v in operator.values() if v]
            return event

        # 其他飞书事件类型：不认识，交还调用方兜底
        return None

    # --- GroupCapable ---

    @staticmethod
    def _group_service():
        """可用的 OpenAPI 服务实例，不可用时返回 None"""
        from services.feishu_api import FeishuAPIService
        service = FeishuAPIService.get_instance()
        return service if service and service.enabled else None

    def group_backend_ready(self) -> Tuple[bool, str]:
        service = self._group_service()
        if not service:
            return False, 'Feishu API service not available'
        return True, ''

    def dissolve_group(self, chat_id: str) -> Tuple[bool, str]:
        service = self._group_service()
        if not service:
            return False, 'Feishu API service not available'
        return service.dissolve_group_chat(chat_id)

    def cb_create_group(self, session_id: str, project_dir: str) -> Tuple[bool, str]:
        from config import FEISHU_GROUP_NAME_PREFIX

        owner_id = self.get_owner_id()
        if self._standalone_service():
            # 单机模式：调网关侧统一入口，建群 + 落账一次完成，
            # 幂等与归属校验都在该入口内部
            try:
                from handlers.feishu import create_group_chat_and_record
                return create_group_chat_and_record(
                    owner_id, session_id, project_dir, FEISHU_GROUP_NAME_PREFIX)
            except Exception as e:
                # 不回落网关：单机下网关即本进程，重试必然同样失败
                logger.warning("[cb_create_group] create_group_chat_and_record unavailable: %s", e)
                return False, str(e)

        ok, resp = post_to_gateway('/gw/feishu/create-group', {
            'owner_id': owner_id,
            'session_id': session_id,
            'project_dir': project_dir,
        })
        if not ok:
            return False, resp
        return True, resp.get('chat_id', '')
