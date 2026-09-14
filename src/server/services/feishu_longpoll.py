"""飞书 WebSocket 长连接服务

使用 lark-oapi SDK 的 ws.Client 连接飞书长连接，接收事件推送。
事件格式转换后调用现有的 handle_feishu_request() 处理。

功能:
    - 使用 lark_oapi.ws.Client 建立 WebSocket 长连接
    - SDK 内置自动重连和心跳维护
    - 消息事件 (im.message.receive_v1): 使用 register_p2_customized_event 接收原始 dict
    - 卡片回调 (card.action.trigger): 使用 register_p2_card_action_trigger 接收并返回响应
    - Patch SDK 以支持 CARD 类型消息（SDK 1.5.x 未实现此分支）

限制:
    - 需要 Python >= 3.8 和 lark-oapi SDK
    - 仅支持企业自建应用
    - 每应用最多 50 个长连接

使用方式:
    from services.feishu_longpoll import start_feishu_longpoll
    start_feishu_longpoll(app_id, app_secret, event_handler)

    from services.feishu_longpoll import stop_feishu_longpoll
    stop_feishu_longpoll()
"""

import json
import logging
import threading
from typing import Any, Callable, Dict, Optional

from logging_config import setup_logging

logger = setup_logging('feishu_longpoll', console=False, propagate=False)

_sdk_loggers_redirected = False


def _redirect_sdk_loggers() -> None:
    """将 SDK 相关 logger 重定向到 feishu_longpoll 日志文件，避免写入 callback 日志

    仅在首次调用时执行，后续调用直接返回。
    """
    global _sdk_loggers_redirected
    if _sdk_loggers_redirected:
        return
    for name in ('Lark', 'lark_oapi', 'lark_oapi.ws', 'websockets'):
        sdk_logger = logging.getLogger(name)
        sdk_logger.propagate = False
        for h in logger.handlers:
            sdk_logger.addHandler(h)
    _sdk_loggers_redirected = True


# 尝试导入 lark-oapi SDK（需要 Python >= 3.8）
import sys
if sys.version_info >= (3, 8):
    try:
        import lark_oapi as lark
        from lark_oapi.ws import Client as WSClient
        from lark_oapi.event.dispatcher_handler import EventDispatcherHandler
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )
        HAS_LARK_SDK = True
    except ImportError:
        HAS_LARK_SDK = False
        lark = None  # type: ignore
        WSClient = None
        EventDispatcherHandler = None
        P2CardActionTriggerResponse = None
else:
    HAS_LARK_SDK = False
    lark = None  # type: ignore
    WSClient = None
    EventDispatcherHandler = None
    P2CardActionTriggerResponse = None


_ws_client_patched = False


def _patch_ws_client_card_handling() -> None:
    """Patch SDK ws.Client 以支持 CARD 类型消息

    lark-oapi 1.5.x 的 _handle_data_frame 对 MessageType.CARD 直接 return，
    导致卡片回调在长连接模式下无法处理。

    此 patch 将 CARD 消息也路由到 event_handler.do_without_validation()，
    与 EVENT 消息使用相同的处理管道（通过 _callback_processor_map 分发）。

    仅在首次调用时执行 patch，后续调用直接返回。
    """
    global _ws_client_patched
    if _ws_client_patched or not HAS_LARK_SDK:
        return

    import base64
    import http
    import time
    from lark_oapi.ws.const import (
        HEADER_BIZ_RT,
        HEADER_MESSAGE_ID,
        HEADER_SEQ,
        HEADER_SUM,
        HEADER_TRACE_ID,
        HEADER_TYPE,
    )
    from lark_oapi.ws.enum import MessageType
    from lark_oapi.ws.model import Response

    ws_logger = logging.getLogger('lark_oapi.ws')

    async def _handle_data_frame_patched(self, frame):
        """替换原始 _handle_data_frame，增加 CARD 类型支持"""
        hs = frame.headers

        def _get(key):
            for h in hs:
                if h.key == key:
                    return h.value
            return ""

        msg_id = _get(HEADER_MESSAGE_ID)
        trace_id = _get(HEADER_TRACE_ID)
        sum_ = _get(HEADER_SUM)
        seq = _get(HEADER_SEQ)

        pl = frame.payload
        if int(sum_) > 1:
            pl = self._combine(msg_id, int(sum_), int(seq), pl)
            if pl is None:
                return

        message_type = MessageType(_get(HEADER_TYPE))
        ws_logger.debug("[patched] receive message, type: %s, id: %s, trace: %s",
                        message_type.value, msg_id, trace_id)

        resp = Response(code=http.HTTPStatus.OK)
        try:
            start_ms = int(round(time.time() * 1000))

            if message_type in (MessageType.EVENT, MessageType.CARD):
                result = self._event_handler.do_without_validation(pl)
            else:
                ws_logger.debug("[patched] skipping unhandled message type: %s", message_type.value)
                return

            end_ms = int(round(time.time() * 1000))
            header = hs.add()
            header.key = HEADER_BIZ_RT
            header.value = str(end_ms - start_ms)
            if result is not None:
                resp.data = base64.b64encode(
                    lark.JSON.marshal(result).encode('utf-8')
                )
        except Exception as e:
            ws_logger.error("[patched] handle failed, type: %s, id: %s, err: %s",
                            message_type.value, msg_id, e)
            resp = Response(code=http.HTTPStatus.INTERNAL_SERVER_ERROR)

        frame.payload = lark.JSON.marshal(resp).encode('utf-8')
        await self._write_message(frame.SerializeToString())

    WSClient._handle_data_frame = _handle_data_frame_patched
    _ws_client_patched = True
    logger.info("[feishu-lp] Patched WSClient._handle_data_frame for CARD support")


# 需要监听的飞书事件类型（通过 register_p2_customized_event 注册）
_SUBSCRIBED_EVENTS = [
    'im.message.receive_v1',
]


def _marshal_sdk_event(obj: Any, log_name: str) -> Optional[Dict[str, Any]]:
    """将 SDK 事件对象转换为 handle_feishu_request 兼容的 dict

    使用 lark.JSON.marshal() 序列化后 json.loads()，格式与 HTTP 回调一致。

    Args:
        obj: SDK 事件对象（CustomizedEvent / P2CardActionTrigger 等）
        log_name: 日志中显示的对象名称

    Returns:
        转换后的 dict，失败返回 None
    """
    try:
        json_str = lark.JSON.marshal(obj)
        data = json.loads(json_str)
        if not data:
            logger.warning("[feishu-lp] %s marshaled to empty dict", log_name)
            return None
        return data
    except Exception as e:
        logger.error("[feishu-lp] %s conversion error: %s", log_name, e, exc_info=True)
        return None


class FeishuLongPollClient:
    """飞书 WebSocket 长连接客户端

    封装 lark_oapi.ws.Client，在后台 daemon 线程中运行。
    SDK 内置自动重连 (auto_reconnect=True) 和心跳维护。
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        event_handler: Callable[[Dict[str, Any]], Dict[str, Any]],
    ):
        """初始化客户端

        Args:
            app_id: 飞书应用 App ID
            app_secret: 飞书应用 App Secret
            event_handler: 事件处理回调（必需），接收 dict 数据，返回 response dict。
                由平台适配层注入——本模块只做 SDK 封装，不认识 handlers
        """
        if not HAS_LARK_SDK:
            raise ImportError("lark-oapi SDK not installed. Run: pip install lark-oapi")

        self._app_id = app_id
        self._app_secret = app_secret
        self._event_handler = event_handler
        self._ws_client: Optional[Any] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        """在后台线程中启动长连接"""
        if self._thread and self._thread.is_alive():
            logger.warning("[feishu-lp] Already running")
            return

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name='FeishuLongPoll'
        )
        self._thread.start()
        logger.info("[feishu-lp] Background thread started")

    def stop(self) -> None:
        """停止客户端

        设置停止标志并尝试关闭 WebSocket 连接。
        由于 SDK 没有公开的 stop 方法，通过关闭底层连接来中断阻塞。
        """
        logger.info("[feishu-lp] Stop requested")
        self._stop_event.set()

        # 尝试关闭底层 WebSocket 连接以中断阻塞的 start()
        ws = self._ws_client
        if ws:
            try:
                # 尝试关闭底层连接（私有属性，SDK 升级后可能变更）
                inner_ws = getattr(ws, '_ws', None)
                if inner_ws is not None:
                    inner_ws.close()
                # 部分版本可能支持 close 方法
                close_fn = getattr(ws, 'close', None)
                if close_fn is not None:
                    close_fn()
            except Exception as e:
                logger.debug("[feishu-lp] Error closing WebSocket: %s", e)

        # 等待线程退出（最多 2 秒）
        # SDK 内部连接常无法被 close 打断，线程多半要等到超时；它是 daemon，
        # 进程退出时会被回收，等更久没有实际收益
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                logger.warning("[feishu-lp] Thread did not exit within 2s")
            else:
                logger.info("[feishu-lp] Thread exited cleanly")

    _RETRY_INTERVAL = 30  # 异常后重试间隔（秒）

    def _build_dispatcher(self) -> Any:
        """构建事件分发器（注册所有事件处理函数）"""
        builder = EventDispatcherHandler.builder("", "")

        for event_type in _SUBSCRIBED_EVENTS:
            handler_fn = self._make_event_handler(event_type)
            builder.register_p2_customized_event(event_type, handler_fn)

        builder.register_p2_card_action_trigger(self._handle_card_action)

        return builder.build()

    def _run(self) -> None:
        """运行 SDK WebSocket 客户端（阻塞调用，异常后自动重试）"""
        # 延迟执行 patch 和 logger 重定向，避免仅 import 模块就产生副作用
        _redirect_sdk_loggers()
        _patch_ws_client_card_handling()

        # dispatcher 只需构建一次，事件处理器是静态的
        dispatcher = self._build_dispatcher()

        while not self._stop_event.is_set():
            try:
                self._ws_client = WSClient(
                    app_id=self._app_id,
                    app_secret=self._app_secret,
                    event_handler=dispatcher,
                    log_level=lark.LogLevel.INFO,
                    auto_reconnect=True,
                )

                logger.info("[feishu-lp] Connecting to Feishu WebSocket...")
                self._ws_client.start()

            except Exception as e:
                if self._stop_event.is_set():
                    break
                logger.error("[feishu-lp] Client error: %s (retrying in %ds)",
                             e, self._RETRY_INTERVAL, exc_info=True)
                self._stop_event.wait(self._RETRY_INTERVAL)

        logger.info("[feishu-lp] Run loop exited")

    def _make_event_handler(self, event_type: str) -> Callable[[Any], None]:
        """为指定事件类型创建处理函数（fire-and-forget）"""
        def handler(event: Any) -> None:
            try:
                data = _marshal_sdk_event(event, event_type)
                if data:
                    logger.info("[feishu-lp] Received event: %s", event_type)
                    self._event_handler(data)
                else:
                    logger.warning("[feishu-lp] Failed to convert event: %s", event_type)
            except Exception as e:
                logger.error("[feishu-lp] Handler error for %s: %s",
                             event_type, e, exc_info=True)
        return handler

    def _handle_card_action(self, trigger: Any) -> Any:
        """处理卡片回调事件

        将 P2CardActionTrigger 转换为 dict，调用 event_handler 获取响应，
        再将响应转换为 P2CardActionTriggerResponse 返回给飞书。

        Args:
            trigger: P2CardActionTrigger 实例

        Returns:
            P2CardActionTriggerResponse 实例
        """
        try:
            data = _marshal_sdk_event(trigger, 'card.action.trigger')
            if not data:
                return P2CardActionTriggerResponse({})

            logger.info("[feishu-lp] Received card action")
            response = self._event_handler(data)

            if not response or not isinstance(response, dict):
                return P2CardActionTriggerResponse({})

            return P2CardActionTriggerResponse(response)

        except Exception as e:
            logger.error("[feishu-lp] Card action error: %s", e, exc_info=True)
            return P2CardActionTriggerResponse({})


# =============================================================================
# 全局客户端实例管理
# =============================================================================

_client_instance: Optional[FeishuLongPollClient] = None
_client_lock = threading.Lock()


def is_longpoll_available() -> bool:
    """检查长连接模式是否可用（需要 Python >= 3.8 且 lark-oapi 已安装）"""
    return HAS_LARK_SDK


def start_feishu_longpoll(
    app_id: str,
    app_secret: str,
    event_handler: Callable[[Dict[str, Any]], Dict[str, Any]],
) -> Optional[FeishuLongPollClient]:
    """启动飞书长连接客户端

    Args:
        app_id: 飞书应用 App ID
        app_secret: 飞书应用 App Secret
        event_handler: 事件处理回调（接收事件 dict，返回 response dict），
            由平台适配层传入——本模块只做 SDK 封装，不认识 handlers

    Returns:
        客户端实例，SDK 未安装时返回 None
    """
    global _client_instance

    if not HAS_LARK_SDK:
        logger.warning("[feishu-lp] lark-oapi SDK not installed, cannot start")
        return None

    with _client_lock:
        if _client_instance:
            logger.warning("[feishu-lp] Client already exists")
            return _client_instance

        _client_instance = FeishuLongPollClient(app_id, app_secret, event_handler)
        _client_instance.start()
        return _client_instance


def stop_feishu_longpoll() -> None:
    """停止飞书长连接客户端"""
    global _client_instance

    with _client_lock:
        if _client_instance:
            _client_instance.stop()
            _client_instance = None
