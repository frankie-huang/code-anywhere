"""Auto Register - Callback 后端启动时自动向飞书网关注册

Callback 后端使用，启动时自动向飞书网关注册获取 auth_token。

OpenAPI 模式下：
  - 分离部署（配置 GATEWAY_URL=http(s)://）：HTTP 回调模式，向远端网关注册
  - 分离部署（配置 GATEWAY_URL=ws(s)://）：WS 隧道模式，无需 HTTP 注册
  - 单机部署（未配置 GATEWAY_URL）：使用 WS 隧道连接本地网关

需配置：CALLBACK_SERVER_URL 与平台 owner 配置（feishu 为 FEISHU_OWNER_ID）
注册接口：{GATEWAY_URL}/gw/register
"""

import json
import logging
import threading
import urllib.request
import urllib.error
from typing import Optional, Tuple, Dict, Any

from utils.http_client import DEFAULT_HTTP_TIMEOUT

logger = logging.getLogger(__name__)


def build_binding_params() -> Dict[str, Any]:
    """组装注册时的 per-user 配置默认值（WS 隧道与 HTTP 注册共用）

    平台自有默认值经 adapter 提供（default_binding_params）；Agent 命令
    表与默认聊天目录是平台无关项，在此合并。
    """
    from config import DEFAULT_CHAT_DIR, DEFAULT_CHAT_FOLLOW_THREAD, get_default_agent
    from agents import get_all_agent_commands
    from platforms import get_im_adapter

    all_cmds = get_all_agent_commands()
    binding_params = get_im_adapter().default_binding_params()
    binding_params.update({
        'default_agent': get_default_agent(),
        'claude_commands': all_cmds.get('claude'),
        'codex_commands': all_cmds.get('codex'),
        'default_chat_dir': DEFAULT_CHAT_DIR,
        'default_chat_follow_thread': DEFAULT_CHAT_FOLLOW_THREAD,
    })
    return binding_params


class AutoRegister:
    """自动注册服务（单例模式）

    Callback 后端启动时自动向飞书网关注册获取 auth_token。
    """

    _instance = None  # type: Optional[AutoRegister]
    _lock = threading.Lock()

    def __init__(self, callback_url: str, owner_id: str, gateway_url: str):
        """初始化自动注册服务

        Args:
            callback_url: Callback 后端 URL
            owner_id: 飞书用户 ID
            gateway_url: 飞书网关地址
        """
        self._callback_url = callback_url
        self._owner_id = owner_id
        self._gateway_url = gateway_url

        # 启用条件：三个参数都存在
        self._enabled = bool(callback_url and owner_id and gateway_url)

        if self._enabled:
            logger.info(
                f"[auto-register] Initialized: owner_id={owner_id}, "
                f"callback_url={callback_url}, gateway={gateway_url}"
            )
        else:
            logger.warning("[auto-register] Disabled (missing configuration)")

    @classmethod
    def initialize(cls, callback_url: str, owner_id: str, gateway_url: str) -> 'AutoRegister':
        """初始化单例实例

        Args:
            callback_url: Callback 后端 URL
            owner_id: 飞书用户 ID
            gateway_url: 飞书网关地址

        Returns:
            AutoRegister 实例
        """
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls(callback_url, owner_id, gateway_url)
            return cls._instance

    @classmethod
    def get_instance(cls) -> Optional['AutoRegister']:
        """获取单例实例

        Returns:
            AutoRegister 实例，未初始化返回 None
        """
        return cls._instance

    @property
    def enabled(self) -> bool:
        """服务是否启用"""
        return self._enabled

    def register_in_background(self):
        """在后台线程中执行注册"""
        if not self._enabled:
            logger.warning("[auto-register] Cannot register: service not enabled")
            return

        thread = threading.Thread(target=self._register, daemon=True)
        thread.start()

    def _register(self):
        """执行注册"""
        logger.info(
            f"[auto-register] Starting registration in background: "
            f"owner_id={self._owner_id}, callback_url={self._callback_url}, gateway={self._gateway_url}"
        )

        register_url = self._gateway_url.rstrip('/') + '/gw/register'
        binding_params = build_binding_params()
        success, message = self._do_register(
            self._callback_url, self._owner_id, register_url,
            binding_params=binding_params
        )

        if success:
            logger.info(f"[auto-register] Registration successful: {message}")
        else:
            logger.warning(f"[auto-register] Registration failed: {message}")

    def _do_register(
        self,
        callback_url: str,
        owner_id: str,
        register_url: str,
        binding_params: Optional[Dict[str, Any]] = None
    ) -> Tuple[bool, str]:
        """向飞书网关注册

        Args:
            callback_url: Callback 后端 URL
            owner_id: 飞书用户 ID
            register_url: 飞书网关注册接口完整 URL（已包含 /gw/register）
            binding_params: per-user 配置参数

        Returns:
            (success, message): success 表示是否成功，message 是响应消息或错误信息
        """
        if binding_params is None:
            binding_params = {}
        api_url = register_url.rstrip('/')

        request_data = {
            'callback_url': callback_url,
            'owner_id': owner_id,
        }
        request_data.update(binding_params)

        logger.info(f"[auto-register] Registering to gateway: {api_url}")

        try:
            req = urllib.request.Request(
                api_url,
                data=json.dumps(request_data).encode('utf-8'),
                headers={'Content-Type': 'application/json'},
                method='POST'
            )

            # 创建无代理的 opener
            no_proxy_handler = urllib.request.ProxyHandler({})
            opener = urllib.request.build_opener(no_proxy_handler)
            with opener.open(req, timeout=DEFAULT_HTTP_TIMEOUT) as response:
                response_data = json.loads(response.read().decode('utf-8'))
                if response_data.get('success'):
                    logger.info("[auto-register] Registration accepted")
                    return True, response_data.get('message', 'Accepted')
                else:
                    error = response_data.get('error', 'Unknown error')
                    logger.warning(f"[auto-register] Registration failed: {error}")
                    return False, error

        except urllib.error.HTTPError as e:
            error_msg = f"HTTP {e.code}: {e.reason}"
            logger.error(f"[auto-register] HTTP error: {error_msg}")
            return False, error_msg
        except urllib.error.URLError as e:
            error_msg = f"URL error: {e.reason}"
            logger.error(f"[auto-register] URL error: {error_msg}")
            return False, error_msg
        except Exception as e:
            error_msg = str(e)
            logger.error(f"[auto-register] Error: {error_msg}")
            return False, error_msg
