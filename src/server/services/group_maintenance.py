"""群聊维护（平台无关）

批量解散与空闲筛选的编排逻辑：归属校验、预通知 callback、清理归属记录、
空闲判定——这些都与 IM 平台无关。真正「解散一个群」的动作由实现了
platforms.base.GroupCapable 的适配器执行。

调用方：
    - main.py _cleanup_group_chats(): 定时清理空闲群聊
    - handlers/feishu/command.py: /groups dissolve 命令

前置条件：当前 adapter 需实现 GroupCapable。main.py 显式 isinstance 判定后才调用；
handlers/feishu 下的调用方只在飞书平台生效，天然满足。
"""

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def batch_dissolve_groups(binding: Dict[str, Any],
                          chat_ids: List[str]) -> Dict[str, Any]:
    """批量解散群聊并清理归属记录（网关侧核心函数）

    只解散 GroupChatStore 中归属于 binding owner 的群聊。
    非服务创建的群聊（不在 store 中或归属其他 owner）直接跳过，不视为失败。

    执行顺序：先通知 callback 标记 dissolved，再调平台 API 解散。
    这样即使平台 API 失败，session 被标记 dissolved 但群仍存活——
    用户下次在群内交互时 continue/ensure-chat 会自动复活 session（自愈）。
    反之若先解散再通知，通知失败会导致 session 指向已解散群且无法自愈。

    Args:
        binding: 绑定信息（包含 _owner_id，用于归属校验和 callback 通知）
        chat_ids: 待解散的群聊 ID 列表

    Returns:
        {
            'dissolved_items': List[str],          # 实际解散的 chat_id
            'skipped_items': List[str],            # 非服务创建或不属于该 owner 的 chat_id
            'failed': List[{'chat_id', 'error'}],  # 真正的 API 错误
        }
    """
    owner_id = binding.get('_owner_id', '')
    if not owner_id:
        logger.warning("[batch-dissolve] owner_id is empty, refusing %d chat(s)", len(chat_ids))
        return {
            'dissolved_items': [],
            'skipped_items': [],
            'failed': [{'chat_id': cid, 'error': 'owner_id not configured'} for cid in chat_ids],
        }

    from platforms import get_im_adapter
    from stores.group_chat_store import GroupChatStore

    adapter = get_im_adapter()
    ready, ready_err = adapter.group_backend_ready()
    if not ready:
        return {
            'dissolved_items': [],
            'skipped_items': [],
            'failed': [{'chat_id': cid, 'error': ready_err} for cid in chat_ids],
        }

    group_store = GroupChatStore.get_instance()
    if not group_store:
        return {
            'dissolved_items': [],
            'skipped_items': [],
            'failed': [{'chat_id': cid, 'error': 'GroupChatStore not initialized'} for cid in chat_ids],
        }
    my_chats = {item['chat_id'] for item in group_store.get_chats_by_owner(owner_id)}

    # 1) 按归属过滤
    targets = []
    skipped_items = []
    for cid in chat_ids:
        if cid not in my_chats:
            skipped_items.append(cid)
        else:
            targets.append(cid)

    if not targets:
        return {'dissolved_items': [], 'skipped_items': skipped_items, 'failed': []}

    # 2) 先通知 callback 标记 dissolved；失败则中止，等下次清理重试
    from services.session_facade import SessionFacade
    resp = SessionFacade.invalidate_chats(binding, targets)
    if resp is None:
        return {
            'dissolved_items': [],
            'skipped_items': skipped_items,
            'failed': [{'chat_id': cid, 'error': 'pre-notify failed'} for cid in targets],
        }

    # 3) 再调平台 API 解散 + 清 GroupChatStore
    dissolved_items = []
    failed = []
    for cid in targets:
        ok, err = adapter.dissolve_group(cid)
        if ok:
            group_store.remove(cid)
            dissolved_items.append(cid)
        else:
            failed.append({'chat_id': cid, 'error': err})

    return {'dissolved_items': dissolved_items, 'skipped_items': skipped_items, 'failed': failed}


def find_idle_group_chats(owner_id: str, owner_chats: List[Dict[str, Any]],
                          now: int, idle_days: int) -> List[str]:
    """返回该 owner 下空闲（超过 idle_days 天未活跃）的群聊 chat_id 列表。

    空闲判定：now - last_active_at >= idle_days * 86400，无 last_active_at 时
    回退 created_at。自动解散（main.py）与 /groups dissolve idle 共用此函数。

    纯过滤器：owner_chats（群列表）与 now（判定时刻）均由调用方传入，函数不自取，
    避免内部重复读 group_chat 文件、并让批量调用共用同一时刻基准。仅 gs_data
    （session 活跃信息）按 owner 自取。
    """
    from stores.group_session_store import GroupSessionStore

    if idle_days <= 0 or not owner_id:
        return []
    gs_store = GroupSessionStore.get_instance()
    if not gs_store:
        return []

    gs_data = gs_store.get_by_owner(owner_id)
    threshold = idle_days * 86400

    idle_chat_ids: List[str] = []
    for item in owner_chats:
        cid = item.get('chat_id', '')
        if not cid:
            continue
        last_active = (gs_data.get(cid) or {}).get('last_active_at', item.get('created_at', 0))
        if now - last_active >= threshold:
            idle_chat_ids.append(cid)
    return idle_chat_ids
