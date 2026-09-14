#!/usr/bin/env python3
"""
Agent Callback Server

功能：
    为 Agent Hook 与 IM 平台之间提供回调服务：权限审批决策回传、会话管理、
    Agent 启停、目录与通知配置等（路由见 handlers/http_handler.py）

权限审批流程（Unix Socket 部分）：
    1. hooks/permission.sh 发送交互卡片（带按钮），并通过 Socket 注册请求
    2. 用户在 IM 侧点击按钮，回调到本服务的 HTTP 端点
    3. 本服务通过 Socket 把决策回传给等待中的 hook
    4. hooks/permission.sh 将决策返回给触发 hook 的 Agent

WebSocket 隧道模式：
    当 GATEWAY_URL 配置为 ws:// 或 wss:// 时启用。
    Callback 后端主动连接网关建立 WS 隧道，无需公网可达。
    适用于本地开发环境或内网部署场景。

通信协议详见: shared/protocol.md
"""

import base64
import http.server
import json
import logging
import os
import signal
import socket
import socketserver
import sys
import threading
import time
from typing import Dict, List

# 将 shared 目录加入模块搜索路径（供本进程所有模块使用）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'shared'))
from logging_config import setup_logging

from config import (
    PERMISSION_REQUEST_TIMEOUT, DEFAULT_PERMISSION_REQUEST_TIMEOUT,
    get_config, get_config_positive_int,
    DEFAULT_SOCKET_PATH, DEFAULT_HTTP_PORT,
    CALLBACK_SERVER_URL, GATEWAY_URL,
    GATEWAY_MODE, DEFAULT_CHAT_DIR,
    IS_CALLBACK_BACKEND
)
from platforms import get_im_adapter
from services.card_cache import CardCache
from services.request_manager import RequestManager
from services.ws_registry import WebSocketRegistry

from stores.auth_token_store import AuthTokenStore
from stores.binding_store import BindingStore
from stores.directory_store import DirectoryStore
from stores.group_chat_store import GroupChatStore
from stores.group_session_store import GroupSessionStore
from stores.message_session_store import MessageSessionStore
from stores.notify_config_store import NotifyConfigStore
from stores.session_chat_store import SessionChatStore

from handlers.http_handler import HttpRequestHandler

# =============================================================================
# 配置 (优先级: .env > 环境变量 > 默认值)
# =============================================================================

SOCKET_PATH = get_config('PERMISSION_SOCKET_PATH', DEFAULT_SOCKET_PATH)
HTTP_PORT = get_config_positive_int('CALLBACK_SERVER_PORT', int(DEFAULT_HTTP_PORT))

# 项目根目录 (src/server -> src -> project_root)
project_root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))

# 日志配置
logger = setup_logging('server', configure_root=True)
logger.info("Logging to: %s (daily rotating)", logger.handlers[0].baseFilename)


# =============================================================================
# Socket 服务器处理
# =============================================================================

def handle_socket_client(conn: socket.socket, addr):
    """处理来自 permission.sh 的 Socket 连接

    流程：
        1. 接收请求数据（JSON 格式）
        2. 注册请求到 RequestManager（保持连接打开）
        3. 发送确认响应
        4. 等待 resolve() 通过此连接返回用户决策
        5. 连接在 resolve() 中关闭
    """
    fileno = conn.fileno()
    logger.info(f"[socket] New connection, fileno={fileno}")

    try:
        # 设置接收超时，避免永久阻塞
        recv_timeout = 5.0
        conn.settimeout(recv_timeout)
        logger.debug(f"[socket] Set receive timeout to {recv_timeout}s")

        data = b''
        start_time = time.time()

        while True:
            try:
                chunk = conn.recv(4096)
            except socket.timeout:
                logger.warning(f"[socket] Receive timeout after {recv_timeout}s (received {len(data)} bytes so far)")
                conn.close()
                return
            except OSError as e:
                logger.warning(f"[socket] Receive error: {e} (received {len(data)} bytes so far)")
                conn.close()
                return

            elapsed = time.time() - start_time
            logger.debug(f"[socket] Received {len(chunk) if chunk else 0} bytes (elapsed: {elapsed:.1f}s)")

            if not chunk:
                logger.warning(f"[socket] Connection closed by client (received {len(data)} bytes total)")
                conn.close()
                return

            data += chunk

            # 检查是否收到完整的 JSON
            try:
                json.loads(data.decode('utf-8'))
                logger.debug(f"[socket] Complete JSON received ({len(data)} bytes)")
                break
            except json.JSONDecodeError as e:
                logger.debug(f"[socket] Incomplete JSON, continuing... ({str(e)[:50]})")
                continue

        # 恢复阻塞模式（后续操作不需要超时）
        conn.settimeout(None)
        logger.debug(f"[socket] Removed timeout, connection ready for response")

        logger.info(f"[socket] Received {len(data)} bytes of JSON data")

        request = json.loads(data.decode('utf-8'))

        # 健康检查探测：收到 ping 消息，回复 pong 后关闭
        if request.get('type') == 'ping':
            conn.sendall(json.dumps({'type': 'pong'}).encode())
            conn.close()
            logger.debug("[socket] Health check ping received, pong sent")
            return

        request_id = request.get('request_id')
        hook_pid = request.get('hook_pid')  # 新增：hook 脚本的进程 ID

        if not request_id:
            conn.sendall(json.dumps({'success': False, 'error': 'missing request_id'}).encode())
            conn.close()
            return

        # 保存 hook_pid 到 request 中，供后续使用
        request['hook_pid'] = hook_pid

        # 解码 raw_input_encoded 提取 session_id、tool_name、tool_input 等上下文
        raw_input_encoded = request.get('raw_input_encoded')
        if raw_input_encoded:
            try:
                raw_input = json.loads(base64.b64decode(raw_input_encoded).decode('utf-8'))
                request['session_id'] = raw_input.get('session_id', 'unknown')
                request['tool_name'] = raw_input.get('tool_name')
                request['tool_input'] = raw_input.get('tool_input', {})
                request['permission_suggestions'] = raw_input.get('permission_suggestions', [])
                logger.debug(f"[socket] Decoded session_id: {request['session_id']}, tool_name: {request['tool_name']}")
            except Exception as e:
                logger.warning(f"[socket] Failed to decode raw_input_encoded: {e}")
                request['session_id'] = 'unknown'
        else:
            # 兜底：无 raw_input_encoded 的请求（理论上不应到达这里）
            request['session_id'] = 'unknown'

        request['agent_type'] = request.get('agent_type') or 'claude'

        session_id = request['session_id']
        logger.info(f"[socket] Request ID: {request_id}, Session: {session_id}, Hook PID: {hook_pid}")

        # 注册请求（保存 socket 连接供后续 resolve 使用）
        RequestManager.get_instance().register(request_id, conn, request)

        # 发送确认响应
        conn.sendall(json.dumps({
            'success': True,
            'message': 'Request registered',
            'session_id': session_id
        }).encode())

        # 重要：不关闭连接！等待用户响应后由 resolve() 关闭
        logger.info(f"[socket] Request {request_id} registered (Session: {session_id}), waiting for user response...")

    except Exception as e:
        logger.error(f"Socket handler error: {e}")
        try:
            conn.sendall(json.dumps({'success': False, 'error': str(e)}).encode())
            conn.close()
        except Exception:
            pass


def run_socket_server():
    """运行 Unix Domain Socket 服务器

    功能：
        监听 Unix Socket，接受来自 permission.sh 的连接
    """
    # 删除已存在的 socket 文件（避免 TOCTOU 竞态条件）
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    os.chmod(SOCKET_PATH, 0o600)  # 仅所有者可读写，防止其他用户访问
    server.listen(10)

    logger.info(f"Socket server listening on {SOCKET_PATH}")

    while True:
        conn, addr = server.accept()
        thread = threading.Thread(target=handle_socket_client, args=(conn, addr))
        thread.daemon = True
        thread.start()


def run_cleanup_thread():
    """运行清理线程 - 使用独立的定时器进行不同频率的清理

    清理任务：
        - 清理断开连接（每 5 秒）
        - 清理 pending 连接（每 30 秒）
        - 清理过期数据（每 1 小时）

    单次清理抛异常只记日志，线程继续下一轮——否则该类清理会静默停止
    （小时级那个一死，所有 store 的过期数据都不再清理）。
    """
    def _cleanup_loop(interval, name, fn):
        while True:
            time.sleep(interval)
            try:
                fn()
            except Exception as e:
                logger.error("[cleanup] %s failed: %s", name, e, exc_info=True)

    # 高频清理（5秒间隔）：断开连接
    def cleanup_high_freq():
        RequestManager.get_instance().cleanup_disconnected()

    # 中频清理（30秒间隔）：过期的 pending 连接
    def cleanup_mid_freq():
        ws_registry = WebSocketRegistry.get_instance()
        if ws_registry:
            ws_registry.cleanup_expired_pending()

    # 低频清理（1小时间隔）：过期数据 + 群聊自动解散
    def cleanup_low_freq():
        _cleanup_expired_data()
        # 群聊自动解散数据源在 gateway 侧（GroupChatStore + GroupSessionStore），
        # 仅 gateway / 单机部署执行；分离部署的 callback 端没有这两个 store，跳过
        if not IS_CALLBACK_BACKEND:
            _cleanup_group_chats()

    # 启动三个独立的清理线程
    for interval, name, fn in (
        (5, 'high-freq', cleanup_high_freq),     # 断开连接
        (30, 'mid-freq', cleanup_mid_freq),      # 过期 pending 连接
        (3600, 'low-freq', cleanup_low_freq),    # 过期数据 + 群聊自动解散
    ):
        thread = threading.Thread(target=_cleanup_loop, args=(interval, name, fn), daemon=True)
        thread.start()


def _cleanup_expired_data():
    """清理过期的数据

    数据存储过期与清理机制说明：

    | Store                | 文件名                  | 过期时间 | Lazy | 定期 | 说明                     |
    |----------------------|-------------------------|----------|------|------|--------------------------|
    | MessageSessionStore  | message_sessions.json   | 7 天     | ✅   | ✅   | 本函数清理                |
    | SessionChatStore     | session_chats.json      | 30 天    | ✅   | ✅   | 本函数清理                |
    | DirectoryStore       | directories.json        | 30 天    | ✅   | ✅   | 本函数清理                |
    | BindingStore         | bindings.json           | 无       | ❌   | ❌   | 需先实现自动续期机制       |
    | AuthTokenStore       | auth_token.json         | 无       | ❌   | ❌   | 单条记录，每次注册覆盖     |
    | WebSocketRegistry    | pending_connections     | 90s/10min| ✅   | ✅   | 中频清理（run_cleanup_thread）|

    本函数清理范围（低频，每 1 小时）：
        - message_sessions.json (7天过期)
        - session_chats.json (30天过期)
        - directories.json (30天过期 + 不存在目录)
    """
    # 清理 message_sessions
    store = MessageSessionStore.get_instance()
    if store:
        expired_count = store.cleanup_expired()
        if expired_count > 0:
            logger.info(f"[cleanup] Cleaned {expired_count} expired message mappings")

    # 清理 session_chats
    session_store = SessionChatStore.get_instance()
    if session_store:
        expired_count = session_store.cleanup_expired()
        if expired_count > 0:
            logger.info(f"[cleanup] Cleaned {expired_count} expired session mappings")

    # 清理 directories（过期 + 不存在目录）
    dir_store = DirectoryStore.get_instance()
    if dir_store:
        expired_count = dir_store.cleanup_expired()
        if expired_count > 0:
            logger.info(f"[cleanup] Cleaned {expired_count} expired directory entries")

    # 清理各 IM 平台自有存储中的过期数据
    platform_cleaned = get_im_adapter().cleanup_expired_data()
    if platform_cleaned > 0:
        logger.info(f"[cleanup] Cleaned {platform_cleaned} expired IM platform entries")


def _cleanup_group_chats():
    """群聊空闲自动解散维护（cleanup_expired_loop 每小时一次）。

    网关主导，数据源完全来自 gateway 侧 GroupChatStore + GroupSessionStore。
    解散阈值从每个 owner 的 BindingStore 记录读取（per-binding `group_dissolve_days`）：
      - binding 不存在 → 跳过（GroupChatStore 里出现未注册 owner 是脏数据）
      - days <= 0 → 该 owner 显式禁用自动解散，跳过
      - 否则按该值判断空闲
    """
    try:
        from platforms.base import GroupCapable
        from services.group_maintenance import batch_dissolve_groups, find_idle_group_chats

        if not isinstance(get_im_adapter(), GroupCapable):
            return  # 当前平台不支持群聊

        group_store = GroupChatStore.get_instance()
        gs_store = GroupSessionStore.get_instance()
        binding_store = BindingStore.get_instance()
        if not group_store or not gs_store or not binding_store:
            return

        now = int(time.time())

        all_groups = group_store.get_all()
        if not all_groups:
            return

        # 复用 get_all 已加载的 bucket，避免 find_idle_group_chats 内部重复读 group_chat 文件
        idle_by_owner: Dict[str, List[str]] = {}
        for owner_id, owner_bucket in all_groups.items():
            binding = binding_store.get(owner_id)
            if not binding:
                continue
            dissolve_days = binding.get('group_dissolve_days', 0) or 0
            if dissolve_days <= 0:
                continue
            idle_chats = find_idle_group_chats(
                owner_id, owner_chats=list(owner_bucket.values()),
                now=now, idle_days=dissolve_days)
            if idle_chats:
                idle_by_owner[owner_id] = idle_chats

        if not idle_by_owner:
            return

        for owner_id, chat_ids in idle_by_owner.items():
            binding = binding_store.get(owner_id)
            if not binding:
                continue
            result = batch_dissolve_groups(binding, chat_ids)
            dissolved_items = result.get('dissolved_items', [])
            failed = result.get('failed', [])
            skipped = result.get('skipped_items', [])
            if failed:
                logger.warning("[cleanup] Failed to dissolve %d group chats for owner=%s: %s",
                               len(failed), owner_id, failed)
            if skipped:
                logger.info("[cleanup] Skipped %d group chats for owner=%s",
                            len(skipped), owner_id)
            if dissolved_items:
                for cid in dissolved_items:
                    gs_store.remove(owner_id, cid)
                logger.info("[cleanup] Auto-dissolved %d idle group chats for owner=%s",
                            len(dissolved_items), owner_id)
    except Exception:
        logger.exception("[cleanup] Error in group chat cleanup")


# =============================================================================
# 多线程 HTTP 服务器
# =============================================================================

class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """支持多线程的 HTTP Server

    允许多个请求并发处理，避免飞书回调转发到 /cb/decision 时死锁。
    """
    allow_reuse_address = True
    daemon_threads = True


# =============================================================================
# 主函数
# =============================================================================

def main():
    """进程入口：注册信号、启动服务、退出时统一清理"""
    logger.info("Starting Agent Callback Server")

    # setup.sh stop/restart 发的是 SIGTERM，转成 KeyboardInterrupt 复用同一退出路径
    signal.signal(signal.SIGTERM, _raise_keyboard_interrupt)

    server = None
    try:
        server = _run_server()
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        _graceful_shutdown(server)


def _run_server():
    """初始化所有服务组件，返回已就绪的 HTTP server（不进入 serve_forever）

    服务组件：
        1. Unix Socket 服务器 - 接收 permission.sh 的连接
        2. 清理线程 - 定期清理断开连接和过期 session
        3. HTTP 服务器 - 接收飞书按钮的回调请求
        4. RequestManager - 管理待处理的权限请求
        5. MessageSessionStore - 维护 message_id 到 session 的映射
        6. SessionChatStore - 维护 session_id 到 chat_id 的映射
        7. DirectoryStore - 记录目录使用历史和目录级静音状态
        8. BindingStore - 维护 owner_id 到 callback_url 的绑定（网关专用）
        9. AuthTokenStore - 存储网关注册的 auth_token（Callback 后端专用）
        10. IMAdapter - IM 平台运行时（按 IM_PLATFORM 选择，负责建连与凭据校验）
        11. AutoRegister - 启动时自动向网关注册（可选）
    """
    logger.info(f"HTTP Port: {HTTP_PORT}")
    logger.info(f"Socket Path: {SOCKET_PATH}")
    env_timeout = os.environ.get('PERMISSION_REQUEST_TIMEOUT', '')
    if env_timeout:
        logger.info(f"Request Timeout: {PERMISSION_REQUEST_TIMEOUT}s (from env: PERMISSION_REQUEST_TIMEOUT={env_timeout})")
    else:
        logger.info(f"Request Timeout: {PERMISSION_REQUEST_TIMEOUT}s (default: {DEFAULT_PERMISSION_REQUEST_TIMEOUT}s)")

    # 检测/创建默认聊天目录
    if DEFAULT_CHAT_DIR:
        try:
            # 使用 exist_ok=True 消除 TOCTOU 竞态条件
            os.makedirs(DEFAULT_CHAT_DIR, exist_ok=True)
            # 创建后再次检查是否为可写目录
            if not os.path.isdir(DEFAULT_CHAT_DIR):
                logger.error(f"DEFAULT_CHAT_DIR '{DEFAULT_CHAT_DIR}' exists but is not a directory")
            elif not os.access(DEFAULT_CHAT_DIR, os.W_OK):
                logger.error(f"DEFAULT_CHAT_DIR '{DEFAULT_CHAT_DIR}' is not writable")
            else:
                logger.info(f"Default chat directory: {DEFAULT_CHAT_DIR}")
        except OSError as e:
            logger.warning(f"Failed to create default chat directory '{DEFAULT_CHAT_DIR}': {e}")

    # 初始化 RequestManager（权限请求管理）
    RequestManager.initialize()

    # 初始化 CardCache（用于卡片回调后更新状态）
    CardCache.initialize()

    # 运行时状态目录（以下所有 store 共用）；支持 RUNTIME_DIR 外置，默认项目根 runtime/
    runtime_dir = get_config('RUNTIME_DIR', os.path.join(project_root, 'runtime'))

    # 初始化 MessageSessionStore（用于继续会话功能）
    MessageSessionStore.initialize(runtime_dir)
    logger.info(f"MessageSessionStore initialized with runtime_dir={runtime_dir}")

    # 初始化 GroupSessionStore（群聊 chat_id → 活跃 session 的本地路由表）
    GroupSessionStore.initialize(runtime_dir)
    logger.info(f"GroupSessionStore initialized with runtime_dir={runtime_dir}")

    # 初始化 DirectoryStore（目录使用历史 + 目录级静音状态）
    DirectoryStore.initialize(runtime_dir)
    logger.info(f"DirectoryStore initialized with runtime_dir={runtime_dir}")

    # 初始化 NotifyConfigStore（运行时通知配置覆盖）
    NotifyConfigStore.initialize(runtime_dir)
    logger.info(f"NotifyConfigStore initialized with runtime_dir={runtime_dir}")

    # 初始化 BindingStore（用于网关注册功能）
    BindingStore.initialize(runtime_dir)
    logger.info(f"BindingStore initialized with runtime_dir={runtime_dir}")

    # 初始化 AuthTokenStore（用于存储网关注册的 auth_token）
    AuthTokenStore.initialize(runtime_dir)
    logger.info(f"AuthTokenStore initialized with runtime_dir={runtime_dir}")

    # 初始化 SessionChatStore（callback 后端存储 session_id -> chat_id 映射）
    from config import SESSION_EXPIRE_DAYS
    session_store = SessionChatStore.initialize(runtime_dir, expire_seconds=SESSION_EXPIRE_DAYS * 86400)
    logger.info(f"SessionChatStore initialized with runtime_dir={runtime_dir}, expire={SESSION_EXPIRE_DAYS}d")
    # 旧 session 都是 Claude 创建的（Codex 支持是新增的），固定补 'claude'
    session_store.backfill_agent_type('claude')
    session_store.migrate_claude_command()

    GroupChatStore.initialize(runtime_dir)
    logger.info(f"GroupChatStore initialized with runtime_dir={runtime_dir}")

    # 初始化 WebSocketRegistry（用于 WS 隧道连接管理）
    WebSocketRegistry.initialize()
    logger.info("WebSocketRegistry initialized")

    # 初始化 IM 平台适配器（按 IM_PLATFORM 选择，默认飞书）
    try:
        adapter = get_im_adapter()
    except (ValueError, ImportError) as e:
        logger.error("Failed to initialize IM adapter (check IM_PLATFORM in .env): %s", e)
        raise
    logger.info("IM adapter initialized: %s", adapter.platform_name())

    # 初始化平台运行时（网关侧建连接 / Callback 侧校验凭据，由 adapter 自行决定）
    # has_gateway: 配置了网关地址（单机部署也为 True，指向本地网关）；
    # 与 config.IS_CALLBACK_BACKEND（仅分离部署为 True）不是一回事
    has_gateway = bool(GATEWAY_URL)
    if not adapter.initialize_runtime(runtime_dir, has_gateway):
        raise RuntimeError("IM platform runtime not ready")

    # 平台端点预加载：启动期即加载 handlers/<platform> 模块，语法错误
    # 启动就暴露（fail-fast），而非等到首条事件才 500
    adapter.gateway_routes()

    # 启动 Socket 服务器线程
    socket_thread = threading.Thread(target=run_socket_server)
    socket_thread.daemon = True
    socket_thread.start()

    # 启动清理线程（内部启动独立的 daemon 线程）
    run_cleanup_thread()

    # 启动 HTTP 服务器（使用 ThreadedHTTPServer 支持并发请求）
    # 这样飞书回调请求和转发到 /cb/decision 的请求可以并发处理
    server = ThreadedHTTPServer(('0.0.0.0', HTTP_PORT), HttpRequestHandler)
    logger.info(f"HTTP server listening on port {HTTP_PORT} (threading enabled)")

    # 连接网关服务
    # - 分离部署：连接远程网关（GATEWAY_URL 由用户配置）
    # - 单机部署：连接本地网关（GATEWAY_URL 默认为 CALLBACK_SERVER_URL）
    # ws(s):// → WS 隧道模式；http(s):// → HTTP 回调模式
    owner_id = adapter.get_owner_id()
    if GATEWAY_URL and owner_id:
        logger.info("[main] Connecting to gateway at %s (mode=%s)", GATEWAY_URL, "separated" if IS_CALLBACK_BACKEND else "standalone")
        # 根据网关模式选择连接方式
        if GATEWAY_MODE == 'ws':
            # WS 隧道模式：客户端主动连接网关，适用于本地开发（callback 不可公网访问）
            from services.ws_tunnel_client import start_ws_tunnel_client
            from services.auto_register import build_binding_params
            binding_params = build_binding_params()
            start_ws_tunnel_client(
                GATEWAY_URL, owner_id,
                binding_params=binding_params
            )
            logger.info("WebSocket tunnel client started, gateway: %s", GATEWAY_URL)
        elif CALLBACK_SERVER_URL:
            # HTTP 回调模式：需要 Callback 后端公网可达
            from services.auto_register import AutoRegister
            AutoRegister.initialize(CALLBACK_SERVER_URL, owner_id, GATEWAY_URL)
            auto_register = AutoRegister.get_instance()
            if auto_register and auto_register.enabled:
                auto_register.register_in_background()
            else:
                logger.info("Auto-registration disabled")
    else:
        # owner id 未配置：当前服务作为纯粹的网关，不携带用户凭据，无需注册
        logger.info("[main] Running as pure gateway without user credentials")

    # 启动遥测服务（后台线程定期上报心跳）
    from telemetry.client import TelemetryService
    from telemetry.client_id import is_first_run
    # 注意：is_first_run() 必须在 get_client_id() 之前调用（后者会创建 client_id 文件）
    first_run = is_first_run()
    telemetry = TelemetryService.initialize()
    if telemetry.enabled:
        if first_run:
            logger.info("=" * 60)
            logger.info("NOTICE: Telemetry is enabled by default.")
            logger.info("Data collected: anonymous client ID, version, OS, repo URL.")
            logger.info("To opt out, set TELEMETRY_ENABLED=false in .env")
            logger.info("=" * 60)
        telemetry.start_in_background()
        logger.info("Telemetry service started")
    else:
        logger.info("Telemetry service disabled")

    return server


def _notify_ws_connections():
    """通知入站 WS 连接服务即将关闭（网关侧；无连接时为 no-op）"""
    from utils.ws_protocol import ws_send_text

    registry = WebSocketRegistry.get_instance()
    if not registry:
        return

    connections = registry.get_all_connections()
    if not connections:
        return

    logger.info("[shutdown] Notifying %d WebSocket connections...", len(connections))

    for owner_id, conn in connections.items():
        try:
            msg = json.dumps({'type': 'shutdown'})
            ws_send_text(conn, msg)
        except Exception as e:
            logger.debug("[shutdown] Failed to notify %s: %s", owner_id, e)

    # 等待 1 秒让消息发送
    time.sleep(1)

    logger.info("[shutdown] WebSocket connections notified")


def _stop_ws_tunnel_client():
    """停止出站 WS 隧道客户端（Callback 侧；未启动时为 no-op）

    与 _notify_ws_connections 相互独立：分离部署的 Callback 只有出站隧道、
    没有入站连接，不能依附于前者执行。
    """
    from services.ws_tunnel_client import stop_ws_tunnel_client
    stop_ws_tunnel_client()


def _graceful_shutdown(server=None):
    """依次执行各项关闭动作

    每步独立 try：单步失败不影响后续步骤。
    server 为 None 表示 HTTP 服务尚未创建（初始化途中收到信号）。
    """
    # 清理期间忽略后续 SIGTERM，避免中途被打断
    try:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    except Exception:
        pass

    steps = [
        ('notify-ws-connections', _notify_ws_connections),
        ('ws-tunnel-client', _stop_ws_tunnel_client),
        ('im-platform', lambda: get_im_adapter().shutdown_runtime()),
    ]
    if server is not None:
        # 用 server_close 而非 shutdown：进入本函数时 serve_forever 要么未启动、
        # 要么已退出，不需要再中断它；而 shutdown() 在 serve_forever 从未启动时
        # 会永久阻塞（等一个不会被置位的 Event）
        steps.append(('http-server', server.server_close))

    for name, fn in steps:
        try:
            fn()
        except Exception as e:
            logger.warning("[shutdown] %s failed: %s", name, e)


def _raise_keyboard_interrupt(signum, frame):
    """SIGTERM handler：转成 KeyboardInterrupt，走与 Ctrl-C 相同的优雅退出路径"""
    raise KeyboardInterrupt


if __name__ == '__main__':
    main()
