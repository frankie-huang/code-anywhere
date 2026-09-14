"""IM 平台适配器抽象基类

定义 Hook/Server 与具体 IM 平台之间的平台无关接口。业务层（main.py /
handlers / services）只调用本模块的抽象方法，各 IM 平台在各自 adapter 中
提供具体实现（见 feishu_adapter.py）。

接口分四档（段内方法按此排序，强制力从强到弱）：
  1. @abstractmethod —— 必备：平台必须实现，否则无法实例化
  2. 默认 raise —— 条件必备：需要该能力的平台必须实现（漏覆盖在首次
     调用即炸），但特定类平台可合法不实现（如免授权平台之于 start_*）
  3. 默认实现（无装饰器）—— 可选能力，未覆盖时走安全默认值（no-op / 空值）
  4. 能力接口（GroupCapable 等混入类）—— 平台按需混入，混入即须全部覆盖

生命周期方法（main.py 调用）：
  - initialize_runtime：启动时建立/校验平台连接
  - shutdown_runtime：优雅关闭
  - cleanup_expired_data：清理本平台自有存储中的过期数据

新增平台：实现本基类，在 platforms/__init__.py 工厂注册；支持群聊的平台
一并混入 GroupCapable。
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, Optional, Tuple

from .models import IMEvent

logger = logging.getLogger(__name__)


class IMAdapter(ABC):
    """IM 平台适配器抽象基类"""

    @abstractmethod
    def platform_name(self) -> str:
        """平台标识（如 'feishu'），用于日志与绑定记录"""
        raise NotImplementedError

    @abstractmethod
    def get_owner_id(self) -> str:
        """当前平台配置的 owner id（main.py 启动时读取，用于网关注册等）"""
        raise NotImplementedError

    def initialize_runtime(self, runtime_dir: str, has_gateway: bool) -> bool:
        """启动时初始化平台运行时（main.py 调用）

        典型工作：初始化平台 API 服务、建立长连接、校验本端凭据。

        Args:
            runtime_dir: 运行时数据目录（平台自有 store 在此落盘）
            has_gateway: 是否配置了网关地址（单机部署也为 True，指向本地网关）；
                与 config.IS_CALLBACK_BACKEND（仅分离部署为 True）不同——本参数
                为 True 时，平台连接可能由网关持有，本端应跳过建连

        Returns:
            False 表示必要配置缺失、应终止启动；True 表示就绪或可降级运行。
            未覆盖时返回 True（该平台无需启动期初始化）。

        异常由 main.py 兜底（终止启动），实现内不需要自行捕获。
        """
        return True

    def shutdown_runtime(self) -> None:
        """优雅关闭平台运行时（main.py 退出时调用）

        典型工作：停止长连接客户端。未覆盖时为 no-op。

        异常由调用方兜底（记录失败并继续后续关闭步骤），实现内不需要自行
        捕获——在此吞掉会让关闭日志把失败记成成功。
        """
        return None

    def cleanup_expired_data(self) -> int:
        """清理本平台自有存储中的过期数据（周期性维护线程调用）

        平台无关的公共 store（MessageSessionStore 等）由 main.py 直接清理，
        本方法仅负责平台自有的存储（如飞书的卡片缓存）。未覆盖时返回 0。

        异常由清理线程兜底（记录失败并继续下一轮），实现内不需要自行捕获。
        """
        return 0

    # --- Callback 侧出站（cb_ 前缀）---
    #
    # 由 handlers/outbound.py 调用。单机部署直连平台 API，分离部署经网关代发，
    # 两条路径都由 adapter 内部决定——业务层只表达「发什么」，不关心怎么发。

    @abstractmethod
    def cb_reply_text(self, text: str, receive_id: str,
                      message_id: str = '') -> Tuple[bool, str]:
        """回复/发送文本，返回 (是否成功, 消息 ID 或错误串)

        message_id 为空时降级为向 receive_id 直接发送。
        """
        raise NotImplementedError

    @abstractmethod
    def cb_reply_markdown(self, markdown: str, receive_id: str,
                          message_id: str = '') -> Tuple[bool, str]:
        """回复/发送 markdown，返回 (是否成功, 消息 ID 或错误串)

        不支持富文本的平台可降级为纯文本发送。
        """
        raise NotImplementedError

    def cb_add_typing(self, receive_id: str = '',
                      message_id: str = '') -> None:
        """标记「处理中」。无此概念的平台不覆盖即可（默认 no-op）

        必需参数按平台不同：飞书在消息上打表情，只认 message_id；
        微信向接收者发「正在输入」状态，只认 receive_id。两者都传即可。
        """
        return None

    def cb_remove_typing(self, receive_id: str = '',
                         message_id: str = '') -> None:
        """清除「处理中」标记。无此概念的平台不覆盖即可（默认 no-op）"""
        return None

    # --- 注册授权 ---
    #
    # 注册流程相关接口，按档位分组（必备 → 条件必备 → 可选）：
    #   get_auth_secret — token 密钥来源（必备，各平台必然生成 token）
    #   start_authorization / start_ws_authorization — 发起授权（需要授权的
    #     平台实现；授权结果由各平台自己的事件入口处理，不经接口）
    #   get_gateway_metadata — 下发消息的平台附加字段（可选，默认空）
    #   default_binding_params — per-user 配置的全局默认值（可选，默认空）

    @abstractmethod
    def get_auth_secret(self, binding_params: Dict[str, Any]) -> str:
        """auth_token 的 HMAC 密钥（网关侧生成 token 用，如 register.py）

        密钥来源按平台：飞书用网关静态配置 FEISHU_APP_SECRET（binding_params
        未用）；微信等无静态应用密钥的平台用注册请求携带的会话凭据
        （binding_params['im_token']）。返回空串表示无法生成。
        """
        raise NotImplementedError

    def start_authorization(self, owner_id: str, client_ip: str,
                            callback_url: str, binding_params: Dict[str, Any],
                            old_callback_url: str = '') -> None:
        """发起授权 — HTTP 注册路径（register.py 调用）

        飞书：发送授权卡片，等用户点「允许」后由卡片回调完成注册。

        默认 raise 而非 no-op：授权是注册的必经环节，漏覆盖若静默跳过，
        用户将永远等不到授权（注册请求却已返回成功）。免授权平台接入时
        再引入跳过授权环节的判定（如扫码已验证身份的平台），而非依赖
        本方法的默认值。
        """
        raise NotImplementedError('platform requires authorization flow')

    def start_ws_authorization(self, owner_id: str, client_ip: str,
                               request_id: str, binding_params: Dict[str, Any],
                               old_ip: Optional[str] = None) -> bool:
        """发起授权 — WS 隧道路径（ws_handler.py 调用）

        与 start_authorization 的差别：WS 模式下 callback 未必公网可达，
        授权结果经隧道回写，故以 request_id 标识本次 pending 连接。

        Args:
            old_ip: 换绑场景下的旧终端 IP。None 表示首次注册；'' 表示
                换绑但旧 IP 未知（binding 未记录 registered_ip）——两种
                换绑形态都要按换绑处理，不能按 IP 真值分流

        Returns:
            授权请求是否已送达用户（用于调用方决定是否设卡片冷却）

        默认 raise，理由同 start_authorization。
        """
        raise NotImplementedError('platform requires authorization flow')

    def get_gateway_metadata(self) -> Dict[str, Any]:
        """下发给 Callback 的平台附加字段（注册通知 / auth_ok 消息携带）

        飞书：bot_open_id（Callback 端存储后用于 @ 机器人判定）。
        无附加字段的平台不覆盖即可。
        """
        return {}

    def default_binding_params(self) -> Dict[str, Any]:
        """本平台 per-user 配置的全局默认值（注册时随 binding 落盘）

        Callback 侧注册（WS 隧道 / HTTP）组装 binding_params 时调用，
        网关经 extract_binding_params 提取后存入 BindingStore。读取时
        各消费方从 binding 取值、缺失用自身默认值兜底，故平台无
        per-user 配置时不覆盖即可（返回空 dict）。
        """
        return {}

    # --- 网关侧入站（parse_ / handle_ 前缀）---
    #
    # 平台事件与 Callback 端调用的入口。http_handler 只做协议层（读 body、
    # 统一 owner 鉴权、写响应），路径声明与事件解析全部下沉到适配器：
    #   gateway_routes — Callback 经 HTTP 调用的平台端点表（可选，默认空）
    #   handle_inbound_event — 平台事件回调入口（可选，默认不处理）
    #   parse_event — 平台原始事件 → IMEvent

    def gateway_routes(self) -> Dict[str, Callable[..., Tuple[bool, dict]]]:
        """本平台在网关侧暴露的 HTTP 端点表：path → handler(binding, data)

        由 http_handler 统一做 owner 鉴权后分发，故 handler 只需处理业务。
        无自有端点的平台不覆盖即可（默认空表）。
        """
        return {}

    def handle_inbound_event(self, raw: Dict[str, Any],
                             verify_token: bool = True) -> Tuple[bool, dict]:
        """平台事件回调入口，返回 (是否已处理, 响应体)

        Args:
            raw: 平台原始事件数据
            verify_token: 是否校验平台签名/verification token。长连接等
                已在连接层完成认证的通道传 False

        未覆盖时返回 (False, {})，由调用方按未知请求处理。
        """
        return False, {}

    def parse_event(self, raw: Dict[str, Any]) -> Optional[IMEvent]:
        """解析平台原始事件为 IMEvent；非本平台事件返回 None（交还调用方兜底）

        平台的连接验证请求（如飞书 URL 验证）解析为 kind=VERIFICATION 的
        IMEvent，challenge 等应答字段放 raw，由调用方按平台约定回应。
        未覆盖时返回 None（该平台的事件不经此抽象，走原生入口）。
        """
        return None


class GroupCapable(ABC):
    """群聊能力（仅支持群聊的平台混入）

    业务层用 isinstance(adapter, GroupCapable) 判断平台是否支持群聊，不支持的
    平台跳过群聊相关维护。混入即表示支持，故两个方法都用 @abstractmethod——
    漏实现在实例化（启动时 get_im_adapter）就失败，而非等到调用时。

    只覆盖平台动作本身（后端可用性、解散单个群）；归属校验、预通知 callback、
    空闲判定等编排逻辑与平台无关，见 services/group_maintenance.py。
    """

    @abstractmethod
    def group_backend_ready(self) -> Tuple[bool, str]:
        """群聊后端是否可用，不可用时第二个返回值为原因（写入解散结果的 error）"""
        raise NotImplementedError

    @abstractmethod
    def dissolve_group(self, chat_id: str) -> Tuple[bool, str]:
        """解散单个群聊，返回 (是否成功, 失败原因)"""
        raise NotImplementedError

    @abstractmethod
    def cb_create_group(self, session_id: str,
                        project_dir: str) -> Tuple[bool, str]:
        """Callback 侧建群，返回 (是否成功, 群 ID 或错误串)

        群名由平台侧按 prefix + 目录名 + 日期统一构造，调用方不关心。
        """
        raise NotImplementedError
