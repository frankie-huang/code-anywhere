"""会话 transcript 提取与 /copy 处理（Callback 侧）

/copy 指令的执行体：读取 session store 中 hook 上报的 transcript_path
定位 transcript 文件，复用 lib/transcript.sh 提取最后答复，包成 markdown
代码块回发。

提取实现只有 Shell 一份（Stop hook 与 /copy 共用 lib/transcript.sh），
Python 侧不维护第二份解析。
"""

import json
import logging
import os
import subprocess
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# transcript.sh 路径（src/lib/transcript.sh；本文件位于 src/server/handlers/）
_TRANSCRIPT_SH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'lib', 'transcript.sh')

# 提取子进程超时（秒）——长会话 jsonl 几十 MB 时整文件解析需数秒
_EXTRACT_TIMEOUT_SECONDS = 30


# =============================================
# 公开接口
# =============================================


def handle_copy_response(data: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
    """处理 /copy 请求：提取会话最后答复并以 markdown 代码块回发（网关调用）

    结果消息（代码块 / 错误提示）由本侧直接发送，网关只处理传输失败。
    鉴权由 callback.py 的路由薄封装统一完成（与其他 /cb/agent/* 一致）。

    Args:
        data: 请求数据
            - session_id: 会话 ID (必需)
            - chat_id: 回发目标群聊 ID (必需)
            - message_id: 用户的 /copy 消息 ID (可选，回复锚点)

    Returns:
        (status, body)：body 为 {'ok': bool, 'error': str}
    """
    session_id = data.get('session_id', '')
    chat_id = data.get('chat_id', '') or ''
    message_id = data.get('message_id', '') or ''
    if not session_id:
        return 400, {'error': 'Missing session_id'}
    if not chat_id:
        return 400, {'error': 'Missing chat_id'}

    from stores.session_chat_store import SessionChatStore
    store = SessionChatStore.get_instance()
    if not store:
        return 500, {'error': 'Session store not initialized'}

    # 定位 transcript：读 hook 上报落库的路径；缺路径的会话给一条
    # 新消息触发 hook 上报即可恢复
    transcript_path = store.get_transcript_path(session_id)
    if transcript_path is None:
        _send_copy_notice(chat_id, message_id, '会话已过期或不存在，请 /new 重新发起。')
        return 200, {'ok': False, 'error': 'session not found'}
    if not transcript_path:
        logger.info("[copy] No transcript_path in session: %s", session_id)
        _send_copy_notice(chat_id, message_id,
                          '该会话缺少 transcript 路径记录，发送一条新消息后即可恢复 /copy。')
        return 200, {'ok': False, 'error': 'transcript_path not recorded'}
    if not os.path.isfile(transcript_path):
        logger.warning("[copy] Transcript file missing: %s", transcript_path)
        _send_copy_notice(chat_id, message_id,
                          '找不到会话记录文件（transcript），可能已被清理。')
        return 200, {'ok': False, 'error': 'transcript file missing'}

    texts, error = _extract_last_response(transcript_path)
    if error:
        # 提取器故障（超时/脚本缺失/输出畸形）≠ 无内容，提示区分避免误导
        logger.warning("[copy] Extraction failed: session=%s, error=%s", session_id, error)
        _send_copy_notice(chat_id, message_id, '读取会话记录失败，请查看服务日志。')
        return 200, {'ok': False, 'error': error}
    if not texts:
        _send_copy_notice(chat_id, message_id, '当前轮次尚未完成或没有可复制的答复内容。')
        return 200, {'ok': False, 'error': 'no response content'}

    # 不做截断：/copy 的价值在完整原文，截断版复制没有意义；内容超长
    # 导致发送失败时直接报错，引导用户在终端用本地 /copy 获取
    content = _pick_final_reply(texts)
    block = _wrap_in_code_block(content)

    from handlers.outbound import reply_markdown, reply_text
    success, sent_id = reply_markdown(block, chat_id, message_id)
    if not success:
        # 卡片发送失败降级纯文本（代码块语法不渲染，但内容仍可复制）
        logger.warning("[copy] Markdown card failed, fallback to text: %s", sent_id)
        success, sent_id = reply_text(block, chat_id, message_id)
    if not success:
        logger.error("[copy] Failed to send copy result: %s", sent_id)
        _send_copy_notice(chat_id, message_id,
                          '发送失败（内容可能超出飞书消息限制），请在终端会话中使用 /copy 获取完整答复。')
        return 200, {'ok': False, 'error': sent_id}

    # 不更新 last_message_id、不写 MessageSessionStore：/copy 结果是只读工具
    # 消息而非对话内容，链式锚点保持在真正的会话消息（stop 卡片）上
    logger.info("[copy] Sent copy result: session=%s, chars=%d", session_id, len(content))
    return 200, {'ok': True}


# =============================================
# 内部实现
# =============================================


def _pick_final_reply(texts: List[str]) -> str:
    """取答复列表的最终一条（/copy 语义：只要 stop message）

    提取器按 assistant 消息分段：Claude 最后元素 = 最后一条 assistant
    消息（即 Stop 事件的 last_assistant_message，事后读已落盘）；
    Codex 最后元素 = task_complete.last_agent_message（提取器显式
    追加到尾部）。中间过程叙述（边做边说的多段输出）不返回。
    """
    return texts[-1].strip() if texts else ''


def _send_copy_notice(chat_id: str, message_id: str, text: str) -> None:
    """发送 /copy 流程的轻量提示（错误 / 无内容场景）"""
    from handlers.outbound import reply_text
    success, err = reply_text(text, chat_id, message_id)
    if not success:
        logger.error("[copy] Failed to send notice: %s", err)


def _extract_last_response(transcript_path: str) -> Tuple[List[str], str]:
    """调用 lib/transcript.sh 提取会话最后答复

    CLI 单次提取（不重试、不回退 subagents）——事后调用主文件必有内容，
    重试与子代理回退均为 Stop hook 落盘竞态的兜底，对 /copy 无意义。

    Returns:
        (texts, error)：texts 为答复文本列表（多段 assistant 输出）；
        失败时 texts 为空列表、error 为错误描述
    """
    try:
        result = subprocess.run(
            ['bash', _TRANSCRIPT_SH, transcript_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=_EXTRACT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return [], 'extraction timeout'
    except Exception as e:
        return [], str(e)
    if result.returncode == 2:
        # CLI 约定退出码 2 = 无可提取内容（非故障，如目标轮次尚未落盘）
        return [], ''
    if result.returncode != 0:
        stderr = (result.stderr or '').strip()
        return [], (stderr[:200] if stderr else 'extraction failed')
    try:
        data = json.loads(result.stdout.strip())
    except (ValueError, TypeError):
        return [], 'invalid extraction output'
    if not isinstance(data, dict):
        # 合法但非对象的 JSON（如 "[]"）同样视为输出畸形
        return [], 'invalid extraction output'
    # exit 0 的契约是「有内容」：texts 必须存在、为列表且元素均为字符串，
    # 否则判为输出畸形而非空内容（避免把提取器异常误报成"轮次未完成"）
    texts = data.get('texts')
    if not isinstance(texts, list) or not all(isinstance(t, str) for t in texts):
        return [], 'invalid extraction output'
    filtered = [t for t in texts if t.strip()]
    # exit 0 的契约是「有内容」：过滤后为空（[] 或全空白）同样视为输出畸形
    if not filtered:
        return [], 'invalid extraction output'
    return filtered, ''


def _wrap_in_code_block(text: str) -> str:
    """把文本包进 markdown 围栏代码块，开围栏带 markdown 语言标注

    答复本身几乎必然含 ``` 围栏，外层围栏必须比内容中最长的连续
    反引号串更长，否则代码块当场断裂。info string（markdown）写在
    开围栏尾部，闭围栏保持裸围栏。

    Args:
        text: 要包裹的文本（不含围栏）

    Returns:
        完整的 fenced code block
    """
    longest = 0
    run = 0
    for ch in text:
        if ch == '`':
            run += 1
            if run > longest:
                longest = run
        else:
            run = 0
    fence = '`' * max(3, longest + 1)
    return fence + 'markdown\n' + text + '\n' + fence
