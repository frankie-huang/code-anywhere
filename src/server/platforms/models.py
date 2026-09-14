"""IM 平台事件中立模型

入站事件经 IMAdapter.parse_event 从平台原始 JSON 解析为本模块的中立结构，
业务层（handlers/services）只消费 IMEvent，不再接触平台原始字段。

设计约束：
- IMEvent 携带路由所需的全量键（message_id / chat_id / parent_id / sender 等），
  消息事件与卡片回调共用同一结构——卡片分支的专属字段仅在该 kind 下有意义
- raw 是原始事件的引用（非拷贝），语义只读：供日志脱敏与平台特定分支（如飞书
  卡片回写）使用，但不得从中重新提取路由键（绕过本抽象），也不得写入以传递
  处理状态（隐式通道读写方分散、难追踪）——跨函数状态走显式参数或具名字段
"""

from typing import Any, Dict, List


class IMEventKind(object):
    """入站事件类型（str 字面量，非枚举——与 RouteSource 风格一致，便于序列化）"""

    # 消息事件（用户在 IM 中发消息）
    MESSAGE = 'message'
    # 卡片交互（按钮点击 / 表单提交）
    CARD_ACTION = 'card_action'
    # 平台连接验证（如飞书 URL 验证），由 adapter 内部处理，通常不进入业务层
    VERIFICATION = 'verification'


class IMEvent(object):
    """平台无关的入站事件

    消息事件（kind=MESSAGE）字段：message_id / chat_id / chat_type / parent_id /
    message_type / sender_open_id / sender_user_id / text / is_at_bot

    卡片回调（kind=CARD_ACTION）字段：action_value / action_name / form_value /
    card_message_id / operator_open_id / operator_user_id

    所有字段均有安全默认值（空串/空 dict/False），未携带的键取默认值而非 KeyError。
    """

    def __init__(self, kind: str) -> None:
        self.kind = kind                        # IMEventKind.*
        self.raw: Dict[str, Any] = {}           # 平台原始事件（日志/平台分支用）

        # --- 通用标识 ---
        self.event_id: str = ''                 # 平台事件 ID（幂等去重/日志）

        # --- 消息事件 ---
        self.message_id: str = ''
        self.chat_id: str = ''
        self.chat_type: str = ''                # 'p2p' / 'group'（平台语义统一到这两个词）
        self.parent_id: str = ''                # 非空表示回复/话题消息
        self.message_type: str = ''             # 平台原始消息类型（text/post/...）
        self.sender_open_id: str = ''           # 发送者平台内标识（binding 键）
        self.sender_user_id: str = ''           # 发送者登录标识（协作者前缀对齐用）
        self.sender_id_values: List[str] = []   # 发送者全部标识值（查 binding 逐个尝试）
        self.text: str = ''                     # 解析后的纯文本（@占位符已替换）
        self.is_at_bot: bool = False            # 是否 @ 本服务 bot

        # --- 卡片回调 ---
        self.action_value: Dict[str, Any] = {}  # 按钮/表单携带的业务数据
        self.action_name: str = ''              # 触发组件名（区分按钮/表单提交）
        self.form_value: Dict[str, Any] = {}    # 表单字段值
        self.card_message_id: str = ''          # 卡片所在消息 ID（回写更新用）
        self.operator_open_id: str = ''         # 操作者平台内标识
        self.operator_user_id: str = ''         # 操作者登录标识
        self.operator_id_values: List[str] = [] # 操作者全部标识值

    def __repr__(self) -> str:
        preview = self.text[:30] if self.text else ''
        return (f"IMEvent(kind={self.kind}, chat_id={self.chat_id}, "
                f"message_id={self.message_id}, text={preview!r})")

