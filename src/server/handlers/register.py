"""Gateway Register Handler - 网关注册接口处理器

两种注册模式：
    HTTP 模式：Callback 后端需公网可达（网关通过 HTTP 转发请求）
    WebSocket 模式：Callback 后端无需公网可达（主动连接网关建立隧道）

本模块只含注册编排（平台无关）；授权的飞书形态（授权卡片、授权结果
处理、WS 授权交互）在 handlers/feishu/authorization.py。

HTTP 注册流程（网关使用）：
    - handle_register_request(): 接收 Callback 后端的注册请求（本模块）
    - handle_authorization_decision(): 处理用户授权决策（authorization）
    - handle_register_unbind(): 处理用户解绑操作（authorization）

WebSocket 注册流程（网关使用）：
    - handle_ws_registration(): 处理 WS 隧道的首次注册（authorization）
    - handle_ws_rebind_registration(): 处理 WS 隧道的换绑请求（authorization）
    - handle_ws_authorization_approved(): 处理 WS 模式授权通过（authorization）
    - handle_ws_authorization_denied(): 处理 WS 模式授权拒绝（authorization）
    - handle_ws_register_unbind(): 处理 WS 模式用户解绑（authorization）

Callback 后端使用：
    - handle_register_callback(): 接收网关的 auth_token 通知
    - handle_check_owner_id(): 验证 owner_id 所属权

数据存储服务（从 services 导入）：
    - AuthTokenStore: 存储 auth_token
    - BindingStore: 存储绑定关系
    - WebSocketRegistry: WS 连接注册表
"""

import logging
import socket
import urllib.error
from typing import Tuple, Dict, Any, Optional

from stores.auth_token_store import AuthTokenStore
from utils.concurrency import run_in_background
from utils.http_client import DEFAULT_HTTP_TIMEOUT, post_json

logger = logging.getLogger(__name__)


def extract_binding_params(data: Dict[str, Any]) -> Dict[str, Any]:
    """从请求/卡片回调数据中提取 per-user 配置参数

    统一入口，供 handle_register_request（HTTP 注册）、ws_handler（WS 隧道注册）
    和 feishu.group（授权卡片回调）复用。
    新增/删除 per-user 配置字段时只需修改此处和 binding_store.upsert()。

    Args:
        data: 包含 per-user 配置的请求数据 dict

    Returns:
        per-user 配置参数 dict，字段说明：
            session_mode: 会话模式 message/thread/group（默认 message）
            default_agent: 默认 agent 标识，如 'claude'/'codex'
            claude_commands: 可用的 Claude 斜杠命令列表（None=未传）
            codex_commands: 可用的 Codex 斜杠命令列表（None=未传）
            default_chat_dir: 默认聊天工作目录
            default_chat_follow_thread: 默认聊天目录是否跟随全局话题模式
            group_name_prefix: 群聊名称前缀（None=未传，保留旧值）
            group_dissolve_days: 群聊自动解散天数（None=未传，保留旧值）
            group_prefix_chat_id: 群聊 prompt 前缀是否含群 ID（None=未传，保留旧值）
            group_allow_cowork: 群聊协作者模式（None=未传，保留旧值）
    """
    # 入口转换：优先使用 session_mode，旧客户端用 reply_in_thread 映射
    session_mode = data.get('session_mode', '')
    if not session_mode or session_mode not in ('message', 'thread', 'group'):
        reply_in_thread = data.get('reply_in_thread', False)
        session_mode = 'thread' if reply_in_thread else 'message'

    return {
        'session_mode': session_mode,
        'default_agent': data.get('default_agent', ''),
        'claude_commands': data.get('claude_commands'),
        'codex_commands': data.get('codex_commands'),
        'default_chat_dir': data.get('default_chat_dir', ''),
        'default_chat_follow_thread': data.get('default_chat_follow_thread', True),
        'group_name_prefix': data.get('group_name_prefix'),
        'group_dissolve_days': data.get('group_dissolve_days'),
        'group_prefix_chat_id': data.get('group_prefix_chat_id'),
        'group_allow_cowork': data.get('group_allow_cowork'),
    }


def handle_register_request(data: dict, client_ip: str = '') -> Tuple[bool, dict]:
    """处理 Callback 后端的注册请求

    网关立即返回成功，后续异步处理：
        - 已绑定：直接调用 Callback 后端的 /cb/register
        - 未绑定：发起平台授权

    Args:
        data: 请求数据
            - callback_url: Callback 后端 URL（必需）
            - owner_id: 飞书用户 ID（必需）
            - session_mode: 会话模式，message/thread/group（可选，默认 message）
            - reply_in_thread: 已废弃，兼容旧客户端（True 映射为 session_mode='thread'）
            - default_agent: 默认 agent 类型（可选）
            - claude_commands: 可用的 Claude 命令列表（可选）
            - codex_commands: 可用的 Codex 命令列表（可选）
            - default_chat_dir: 默认聊天目录（可选）
            - default_chat_follow_thread: 默认聊天目录是否跟随全局话题模式（可选）
            - group_name_prefix: 群聊名称前缀（可选）
            - group_dissolve_days: 群聊自动解散天数（可选）
            - group_prefix_chat_id: 群聊 prompt 前缀是否含群 ID（可选）
            - group_allow_cowork: 群聊协作者模式（可选）
        client_ip: 客户端 IP 地址

    Returns:
        (success, response): success 表示是否处理成功，response 是响应数据
    """
    callback_url = data.get('callback_url', '')
    owner_id = data.get('owner_id', '')
    binding_params = extract_binding_params(data)

    # 验证参数
    if not callback_url or not owner_id:
        logger.warning("[register] Missing params: callback_url or owner_id")
        return True, {
            'success': False,
            'error': 'missing required fields: callback_url, owner_id'
        }

    logger.info("[register] Registration request: owner_id=%s, callback_url=%s, ip=%s, session_mode=%s, claude_commands=%s, default_chat_dir=%s",
                owner_id, callback_url, client_ip, binding_params.get('session_mode'), binding_params.get('claude_commands'), binding_params.get('default_chat_dir'))

    # 在后台线程中处理注册逻辑（异步）
    run_in_background(_process_registration, (callback_url, owner_id, client_ip, binding_params))

    # 立即返回成功
    return True, {
        'success': True,
        'message': 'Registration request received, processing in background'
    }


def handle_register_callback(data: dict) -> Tuple[bool, dict]:
    """处理网关注册回调（网关调用 Callback 后端）

    Callback 后端接收网关的 auth_token 通知并存储。

    Args:
        data: 请求数据
            - owner_id: 飞书用户 ID
            - auth_token: 认证令牌
            - bot_open_id: 机器人 open_id（可选）
            - gateway_version: 网关版本（可选）

    Returns:
        (success, response)
    """
    from platforms import get_im_adapter

    owner_id = data.get('owner_id', '')
    auth_token = data.get('auth_token', '')
    bot_open_id = data.get('bot_open_id', '')
    gateway_version = data.get('gateway_version', '')

    # 获取配置的 owner_id（经 adapter，平台无关编排不读平台键）
    config_owner_id = get_im_adapter().get_owner_id()

    if not owner_id or not auth_token:
        logger.warning("[cb/register] Missing params: owner_id or auth_token")
        return True, {
            'success': False,
            'error': 'missing required fields: owner_id, auth_token'
        }

    # 验证 owner_id 是否与配置一致
    if config_owner_id and owner_id != config_owner_id:
        logger.warning(
            f"[cb/register] owner_id mismatch: received={owner_id}, config={config_owner_id}"
        )
        return True, {
            'success': False,
            'error': 'owner_id mismatch'
        }

    logger.info(
        f"[cb/register] Storing auth_token for owner_id={owner_id}, "
        f"bot_open_id={bot_open_id}, gateway_version={gateway_version}"
    )

    # 存储 auth_token（及 bot_open_id）
    token_store = AuthTokenStore.get_instance()
    if token_store:
        if token_store.save(owner_id, auth_token, bot_open_id=bot_open_id):
            logger.info("[cb/register] Auth token stored successfully")
            return True, {
                'success': True,
                'message': 'Registration successful'
            }
        else:
            return True, {
                'success': False,
                'error': 'Failed to store token'
            }
    else:
        logger.warning("[cb/register] AuthTokenStore not initialized")
        return True, {
            'success': False,
            'error': 'AuthTokenStore not initialized'
        }


def handle_check_owner_id(data: dict) -> Tuple[bool, dict]:
    """处理 owner_id 验证请求（网关调用 Callback 后端）

    网关在发送授权卡片前，先验证 callback_url 是否属于该 owner_id。
    这防止了恶意注册请求。

    Args:
        data: 请求数据
            - owner_id: 飞书用户 ID

    Returns:
        (success, response): response 包含 is_owner 字段
    """
    from platforms import get_im_adapter

    request_owner_id = data.get('owner_id', '')
    config_owner_id = get_im_adapter().get_owner_id()

    logger.info(f"[cb/check-owner] Request: owner_id={request_owner_id}")

    # 验证 owner_id 是否与配置一致
    is_owner = bool(request_owner_id and request_owner_id == config_owner_id)

    if is_owner:
        logger.info("[cb/check-owner] Verification passed")
    else:
        logger.warning(
            f"[cb/check-owner] Verification failed: request={request_owner_id}, config={config_owner_id}"
        )

    return True, {
        'success': True,
        'is_owner': is_owner
    }


def _process_registration(
    callback_url: str,
    owner_id: str,
    client_ip: str,
    binding_params: Optional[Dict[str, Any]] = None
):
    """处理注册逻辑（后台线程）

    1. 查询绑定关系
    2. 已绑定且 callback_url 相同：直接调用 /cb/register 通知新 token
    3. 已绑定但 callback_url 不同：发起平台授权让用户确认是否更新
    4. 未绑定：先验证 callback_url 所属，再发起平台授权

    注意：auth_token 不再提前生成，而是在用户点击允许后实时生成。

    Args:
        callback_url: Callback 后端 URL
        owner_id: 飞书用户 ID
        client_ip: 客户端 IP
        binding_params: per-user 配置参数（由 extract_binding_params 提取）
    """
    if binding_params is None:
        binding_params = {}
    from stores.binding_store import BindingStore
    from services.auth_token import generate_auth_token

    # 验证配置（auth_token 密钥由平台提供）
    from platforms import get_im_adapter
    app_secret = get_im_adapter().get_auth_secret(binding_params)
    if not app_secret:
        logger.error("[register] platform auth secret not configured")
        return

    # 查询现有绑定
    store = BindingStore.get_instance()
    if not store:
        logger.error("[register] BindingStore not initialized")
        return

    binding = store.get(owner_id)

    if binding:
        # 已绑定，检查 callback_url 是否一致
        bound_callback_url = binding.get('callback_url', '')
        if bound_callback_url == callback_url:
            # callback_url 一致：直接更新 token（无需用户确认）
            logger.info("[register] Existing binding with same callback_url, updating token")
            auth_token = generate_auth_token(app_secret, owner_id)
            # notify 确认后才落盘：明确拒绝/未送达时跳过 upsert，两侧保持
            # 旧 token 一致（超时不保证，残留错位由下次重启重注册自愈）。
            # notify 与 /cb/* 转发同向，持续不通则 HTTP 模式整体不可用，
            # 无需为该场景的 token 状态做额外设计
            from services.callback_client import notify_register_callback
            if not notify_register_callback(callback_url, owner_id, auth_token):
                logger.error(
                    "[register] Notify callback failed, skip upsert to keep "
                    "both sides on the old token (owner=%s)", owner_id)
                return
            if not store.upsert(owner_id, callback_url, auth_token, client_ip,
                                binding_params=binding_params):
                logger.error("[register] BindingStore upsert failed for %s", owner_id)
        else:
            # callback_url 不同：发送授权卡片让用户确认是否更换设备
            # 不提前生成 auth_token，等用户批准后再生成
            logger.info(
                f"[register] Existing binding with different callback_url: "
                f"old={bound_callback_url}, new={callback_url}"
            )
            from platforms import get_im_adapter
            get_im_adapter().start_authorization(
                owner_id, client_ip, callback_url, binding_params,
                old_callback_url=bound_callback_url)
    else:
        # 未绑定：先验证 callback_url 是否属于该 owner_id
        logger.info("[register] No existing binding, verifying callback_url ownership")
        if not _check_owner_id(callback_url, owner_id):
            logger.warning(
                f"[register] Owner verification failed: callback_url={callback_url}, owner_id={owner_id}. "
                f"This may be a malicious registration request."
            )
            return
        # 验证通过，发起平台授权（如飞书授权卡片）
        # 不提前生成 auth_token，等用户批准后再生成
        from platforms import get_im_adapter
        get_im_adapter().start_authorization(
            owner_id, client_ip, callback_url, binding_params)


def _check_owner_id(callback_url: str, owner_id: str) -> bool:
    """验证 callback_url 是否属于该 owner_id

    调用 Callback 后端的 /cb/check-owner 接口进行验证。

    Args:
        callback_url: Callback 后端 URL
        owner_id: 飞书用户 ID

    Returns:
        True 表示验证通过，False 表示验证失败
    """
    api_url = f"{callback_url.rstrip('/')}/cb/check-owner"

    request_data = {
        'owner_id': owner_id
    }

    logger.info(f"[register] Checking owner_id: {api_url}")

    try:
        response_data = post_json(api_url, request_data, timeout=DEFAULT_HTTP_TIMEOUT)
        is_owner = response_data.get('is_owner', False)
        logger.info(f"[register] Owner check result: {is_owner}")
        return is_owner

    except urllib.error.HTTPError as e:
        logger.error(f"[register] Owner check HTTP error: {e.code} {e.reason}")
        return False
    except urllib.error.URLError as e:
        logger.error(f"[register] Owner check URL error: {e.reason}")
        return False
    except socket.timeout:
        logger.error("[register] Owner check timeout")
        return False
    except Exception as e:
        logger.error(f"[register] Owner check error: {e}")
        return False
