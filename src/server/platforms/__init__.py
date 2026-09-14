"""IM 平台适配器工厂

按 IM_PLATFORM 配置选择并实例化对应的 IMAdapter。业务层通过 get_im_adapter()
获取单例，不感知具体平台。

新增平台：
    1. 实现 platforms.base.IMAdapter（可选 platforms.base.GroupCapable）
    2. 在下方 _create_adapter 注册一个分支
"""

from typing import Optional

from .base import IMAdapter

_adapter: Optional[IMAdapter] = None


def _create_adapter(platform_name: str) -> IMAdapter:
    """按平台名创建适配器实例（新增平台在此注册）"""
    if platform_name == 'feishu':
        from .feishu_adapter import FeishuAdapter
        return FeishuAdapter()
    raise ValueError(
        "Unknown IM platform: %s (expected: feishu). Check IM_PLATFORM in .env" % platform_name
    )


def get_im_adapter() -> IMAdapter:
    """获取当前 IM 平台适配器单例

    首次调用时按 IM_PLATFORM 配置（默认 feishu）创建。未知平台抛 ValueError。
    """
    global _adapter
    if _adapter is None:
        from config import get_config
        platform = get_config('IM_PLATFORM', 'feishu') or 'feishu'
        _adapter = _create_adapter(platform)
    return _adapter


def reset_adapter() -> None:
    """重置单例（供测试切换平台用）"""
    global _adapter
    _adapter = None
