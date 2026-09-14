"""Callback 侧 IM 出站发送（平台无关）

Callback 侧主动向 IM 平台发消息/卡片、增删「处理中」标记、创建群聊。
单机直发还是经网关代发、消息如何构造，全部由 IMAdapter.cb_* 屏蔽——
业务层只表达「发什么」。
"""

from typing import Tuple

from platforms import get_im_adapter


def reply_text(text: str, receive_id: str, message_id: str = '') -> Tuple[bool, str]:
    """回复/发送文本消息，返回 (是否成功, 消息 ID 或错误串)"""
    return get_im_adapter().cb_reply_text(text, receive_id, message_id)


def reply_markdown(markdown: str, receive_id: str, message_id: str = '') -> Tuple[bool, str]:
    """回复/发送 markdown 消息，返回 (是否成功, 消息 ID 或错误串)"""
    return get_im_adapter().cb_reply_markdown(markdown, receive_id, message_id)


def add_typing(receive_id: str = '', message_id: str = '') -> None:
    """加「处理中」标记（飞书为消息表情，微信为向接收者发输入状态）

    主要用于从队列 drain 出来执行的指令：让用户知道排队中的指令已开始处理。
    """
    get_im_adapter().cb_add_typing(receive_id, message_id)


def remove_typing(receive_id: str = '', message_id: str = '') -> None:
    """移除「处理中」标记"""
    get_im_adapter().cb_remove_typing(receive_id, message_id)


def create_group(session_id: str, project_dir: str) -> Tuple[bool, str]:
    """创建群聊，返回 (是否成功, 群 ID 或错误串)

    不支持群聊的平台返回失败，由调用方降级为单聊。
    """
    from platforms.base import GroupCapable

    adapter = get_im_adapter()
    if not isinstance(adapter, GroupCapable):
        return False, 'platform does not support group chat'
    return adapter.cb_create_group(session_id, project_dir)
