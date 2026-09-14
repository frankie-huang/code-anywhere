"""SessionChatStore.transcript_path 字段与会话元数据上报接口测试

钉住 hook 上报链路（report_session_meta → /cb/session/set-meta → store）的语义：
    - Store：不为不存在的 session 创建占位记录（与 set_env_overrides 同语义，
      避免干扰 do_ensure_chat 的存在性判断）、幂等覆盖、空串清空、
      get_transcript_path 三态（None/空串/路径，dissolved 不过滤——
      群解散≠transcript 失效，与 continue 路径读取口径一致）
    - Handler：合并语义按「字段是否存在」判断（缺失 = 不动，显式空串 =
      清空）、transcript_path 非字符串拒绝

背景：transcript 在 agent 本机，后端无法自行定位，路径由 hook 每次
触发上报、存入 session 记录，事后提取会话答复时读取。
"""

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src', 'server'))

from stores.session_chat_store import SessionChatStore  # noqa: E402


class TestSetTranscriptPath(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.store = SessionChatStore(self._tmpdir)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_requires_existing_session(self):
        """session 不存在时不创建占位记录（与 set_env_overrides 同语义）"""
        self.assertFalse(self.store.set_transcript_path('sid-x', '/tmp/a.jsonl'))
        self.assertIsNone(self.store.get_session('sid-x', include_dissolved=True))

    def test_set_and_overwrite(self):
        """写入后幂等覆盖，值不变时跳过写盘"""
        self.store.save('sid-1', 'oc_1')
        self.assertTrue(self.store.set_transcript_path('sid-1', '/tmp/a.jsonl'))
        self.assertEqual(self.store.get_session('sid-1').get('transcript_path'),
                         '/tmp/a.jsonl')
        # 覆盖为新路径（transcript 文件切换后自动跟上）
        self.assertTrue(self.store.set_transcript_path('sid-1', '/tmp/b.jsonl'))
        self.assertEqual(self.store.get_session('sid-1').get('transcript_path'),
                         '/tmp/b.jsonl')

    def test_clear_with_empty_path(self):
        """空串清空字段；已无字段时幂等成功"""
        self.store.save('sid-1', 'oc_1')
        self.store.set_transcript_path('sid-1', '/tmp/a.jsonl')
        self.assertTrue(self.store.set_transcript_path('sid-1', ''))
        self.assertNotIn('transcript_path', self.store.get_session('sid-1'))
        self.assertTrue(self.store.set_transcript_path('sid-1', ''))


class TestGetTranscriptPath(unittest.TestCase):

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.store = SessionChatStore(self._tmpdir)

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_tri_state(self):
        """三态：不存在 → None；存在未记录 → ''；已记录 → 路径"""
        self.assertIsNone(self.store.get_transcript_path('sid-none'))
        self.store.save('sid-1', 'oc_1')
        self.assertEqual(self.store.get_transcript_path('sid-1'), '')
        self.store.set_transcript_path('sid-1', '/tmp/a.jsonl')
        self.assertEqual(self.store.get_transcript_path('sid-1'), '/tmp/a.jsonl')

    def test_dissolved_still_served(self):
        """dissolved 的 session 仍返回路径（群解散≠transcript 失效，只读照常服务）"""
        self.store.save('sid-1', 'oc_1')
        self.store.set_transcript_path('sid-1', '/tmp/a.jsonl')
        self.store.mark_dissolved('oc_1')
        self.assertEqual(self.store.get_transcript_path('sid-1'), '/tmp/a.jsonl')


class TestSetSessionMetaHandler(unittest.TestCase):
    """/cb/session/set-meta 的合并语义与类型校验（mock 鉴权，单例 store 指向临时目录）"""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp()
        # 保存旧实例并清空后 initialize——initialize 只在 _instance 为 None 时
        # 新建，直接调用会静默复用其他测试留下的旧实例（指向旧目录）
        cls._old_instance = SessionChatStore._instance
        SessionChatStore._instance = None
        SessionChatStore.initialize(cls._tmpdir)
        cls.store = SessionChatStore.get_instance()

    @classmethod
    def tearDownClass(cls):
        SessionChatStore._instance = cls._old_instance
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def setUp(self):
        # 删除重建：save 是 merge 语义，直接 save 会残留上一用例写入的字段
        self.store.delete('sid-meta')
        self.store.save('sid-meta', 'oc_1')

    def _call(self, data):
        from handlers.callback import handle_set_session_meta
        with mock.patch('handlers.callback.check_global_auth_token',
                        return_value=True):
            return handle_set_session_meta(data, {})

    def test_explicit_empty_string_clears_path(self):
        """显式空串清空 transcript_path（缺失字段与空串语义分离）"""
        self.store.set_transcript_path('sid-meta', '/tmp/old.jsonl')
        status, body = self._call({'session_id': 'sid-meta', 'transcript_path': ''})
        self.assertEqual(status, 200)
        self.assertTrue(body['success'])
        self.assertNotIn('transcript_path', self.store.get_session('sid-meta'))

    def test_missing_field_keeps_path(self):
        """payload 不含 transcript_path 时保留旧值"""
        self.store.set_transcript_path('sid-meta', '/tmp/old.jsonl')
        status, _ = self._call({'session_id': 'sid-meta'})
        self.assertEqual(status, 200)
        self.assertEqual(self.store.get_session('sid-meta').get('transcript_path'),
                         '/tmp/old.jsonl')

    def test_rejects_non_string_path(self):
        """transcript_path 非字符串返回 400，store 不写入"""
        self.store.set_transcript_path('sid-meta', '/tmp/old.jsonl')
        status, body = self._call({'session_id': 'sid-meta',
                                   'transcript_path': {'bad': 'type'}})
        self.assertEqual(status, 400)
        self.assertFalse(body['success'])
        self.assertEqual(self.store.get_session('sid-meta').get('transcript_path'),
                         '/tmp/old.jsonl')

    def test_rejects_env_null(self):
        """env 显式 null 返回 400（与旧 /set-env 行为一致），env_overrides 不变"""
        self.store.set_env_overrides('sid-meta', {'A': '1'})
        status, body = self._call({'session_id': 'sid-meta', 'env': None})
        self.assertEqual(status, 400)
        self.assertFalse(body['success'])
        self.assertEqual(self.store.get_env_overrides('sid-meta'), {'A': '1'})

    def test_env_and_path_merge(self):
        """env 与 transcript_path 同报时各自更新"""
        status, _ = self._call({'session_id': 'sid-meta',
                                'env': {'ANTHROPIC_BASE_URL': 'https://x.com'},
                                'transcript_path': '/tmp/new.jsonl'})
        self.assertEqual(status, 200)
        session = self.store.get_session('sid-meta')
        self.assertEqual(session.get('transcript_path'), '/tmp/new.jsonl')
        self.assertEqual(session.get('env_overrides'),
                         {'ANTHROPIC_BASE_URL': 'https://x.com'})


if __name__ == '__main__':
    unittest.main()
