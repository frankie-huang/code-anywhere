"""/copy 指令核心逻辑测试

钉住事后提取链路的纯逻辑行为：
    - 围栏自适应：_wrap_in_code_block 的外层围栏必须比内容中
      最长连续反引号串更长，否则嵌套代码块当场断裂
    - 最终答复选取：_pick_final_reply 只取分段结果的最后一条
      （stop message），中间过程叙述不返回
    - CLI 提取：_extract_last_response 经 lib/transcript.sh 真实执行
      （Claude/Codex 双格式 fixture，与 Stop hook 共用同一提取器）
    - 畸形输出判故障：exit 0 但 texts 缺失/非列表/元素非字符串时
      判 invalid extraction output，不误报成"轮次未完成"
    - 锚点语义：发送成功也不更新 last_message_id（工具消息非会话锚点）

背景：/copy 与 Stop hook 共用 lib/transcript.sh 的唯一一份解析实现，
Python 侧不重写解析器；transcript_path 由 hook 每次触发上报并落库
（report_session_meta，字段语义测试见 test_session_transcript_path.py）。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src', 'server'))

from handlers.transcript import (  # noqa: E402
    _wrap_in_code_block, _extract_last_response, _pick_final_reply)
from stores.session_chat_store import SessionChatStore  # noqa: E402


class TestWrapInCodeBlock(unittest.TestCase):

    def test_plain_text_uses_triple_fence(self):
        """无反引号的内容用标准 ``` 围栏，开围栏带 markdown 标注"""
        result = _wrap_in_code_block('hello')
        self.assertTrue(result.startswith('```markdown\n'))
        self.assertTrue(result.endswith('\n```'))

    def test_inline_backtick_stays_triple(self):
        """行内代码（单个反引号）不抬高围栏"""
        result = _wrap_in_code_block('使用 `code` 标记')
        self.assertTrue(result.startswith('```markdown\n'))

    def test_nested_triple_fence_escalates(self):
        """内容含 ``` 围栏时外层必须用 ````（3+1）"""
        content = '说明：\n```python\nprint(1)\n```'
        result = _wrap_in_code_block(content)
        self.assertTrue(result.startswith('````markdown\n'))
        self.assertTrue(result.endswith('\n````'))
        # 内容中的 ``` 围栏原样保留
        self.assertIn('```python', result)

    def test_longest_run_wins(self):
        """内容中更长连续反引号串（4 个）把外层抬到 5 个"""
        content = 'a````b'
        result = _wrap_in_code_block(content)
        self.assertTrue(result.startswith('`````markdown\n'))
        self.assertTrue(result.endswith('\n`````'))


class TestPickFinalReply(unittest.TestCase):

    def test_multi_segment_returns_last_only(self):
        """多段输出（边做边说）只取最终一条 stop message，中间叙述不返回"""
        texts = ['先查一下', '改好了', '最终答复：完成']
        self.assertEqual(_pick_final_reply(texts), '最终答复：完成')

    def test_single_segment_and_empty(self):
        self.assertEqual(_pick_final_reply(['唯一答复']), '唯一答复')
        self.assertEqual(_pick_final_reply([]), '')

    def test_strips_whitespace(self):
        self.assertEqual(_pick_final_reply(['  \n答复\n']), '答复')


class TestExtractLastResponse(unittest.TestCase):
    """经 lib/transcript.sh 真实执行（与 Stop hook 共用同一提取器）"""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    @staticmethod
    def _write_jsonl(path, records):
        with open(path, 'w', encoding='utf-8') as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + '\n')

    def test_claude_format(self):
        """Claude 格式：取最后一条 user 之后的 assistant text"""
        path = os.path.join(self._tmpdir, 'claude-session.jsonl')
        self._write_jsonl(path, [
            {'type': 'user', 'message': {'content': '第一问'}},
            {'type': 'assistant', 'sessionId': 'sid-1',
             'message': {'content': [{'type': 'text', 'text': '第一答'}]}},
            {'type': 'user', 'message': {'content': '第二问'}},
            {'type': 'assistant', 'sessionId': 'sid-1',
             'message': {'content': [
                 {'type': 'thinking', 'thinking': '思考中'},
                 {'type': 'text', 'text': '第二答 **含 markdown**\n```code```'},
             ]}},
        ])
        texts, error = _extract_last_response(path)
        self.assertEqual(error, '')
        self.assertEqual(texts, ['第二答 **含 markdown**\n```code```'])

    def test_codex_format(self):
        """Codex 持久化格式：无 turn_id 时取最后一个 task_complete 所在 turn"""
        path = os.path.join(self._tmpdir, 'codex-session.jsonl')
        self._write_jsonl(path, [
            {'type': 'session_meta', 'payload': {'id': 'thread-1'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'T1'}},
            {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': '第一答'}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete',
                                              'turn_id': 'T1', 'last_agent_message': '第一答'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'T2'}},
            {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': '第二答'}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete',
                                              'turn_id': 'T2', 'last_agent_message': '第二答'}},
        ])
        texts, error = _extract_last_response(path)
        self.assertEqual(error, '')
        self.assertTrue(texts)
        self.assertIn('第二答', texts[-1])
        self.assertNotIn('第一答', texts[-1])

    def test_running_turn_no_complete(self):
        """Claude 运行中轮次：答复尚未落盘时提取为空，且不判为故障

        CLI 退出码 2 = 无可提取内容（非 error）——判错会把这个最常见
        场景误报成「读取会话记录失败」
        """
        path = os.path.join(self._tmpdir, 'running-session.jsonl')
        self._write_jsonl(path, [
            {'type': 'user', 'message': {'content': '新问题'}},
        ])
        texts, error = _extract_last_response(path)
        self.assertEqual(texts, [])
        self.assertEqual(error, '')

    def test_missing_file(self):
        texts, error = _extract_last_response(os.path.join(self._tmpdir, 'nope.jsonl'))
        self.assertEqual(texts, [])
        self.assertTrue(error)

    @unittest.skipUnless(shutil.which('jq'), 'jq-only 行为：无 jq 时 python3 路径跳过坏行判无内容（exit 2）')
    def test_truncated_jsonl_is_hard_failure(self):
        """末行截断令 jq 解析失败 → 提取器异常退出判硬失败（exit 1 链路），
        不当"无内容"误报成"轮次尚未完成"（Claude 提取器 jq 无 python 回退
        是既有行为，修复另批）"""
        path = os.path.join(self._tmpdir, 'broken.jsonl')
        with open(path, 'w', encoding='utf-8') as f:
            f.write('{"type":"user","message":{"content":"q"}}\n')
            f.write('{"type":"assistant","message":{"conte')  # 末行截断
        texts, error = _extract_last_response(path)
        self.assertEqual(texts, [])
        self.assertEqual(error, 'extraction failed')


class TestExtractMalformedOutput(unittest.TestCase):
    """exit 0 但输出畸形时判为故障而非空内容（mock subprocess，不真调 CLI）"""

    @staticmethod
    def _run_with_stdout(stdout):
        fake = mock.Mock(returncode=0, stdout=stdout, stderr='')
        with mock.patch('subprocess.run', return_value=fake):
            return _extract_last_response('/tmp/whatever.jsonl')

    def test_missing_texts_key(self):
        """'{}' 缺 texts：exit 0 却无内容载体，判输出畸形"""
        texts, error = self._run_with_stdout('{}')
        self.assertEqual(texts, [])
        self.assertEqual(error, 'invalid extraction output')

    def test_non_list_texts(self):
        """texts 非列表（如 0）判输出畸形，不当空内容"""
        _, error = self._run_with_stdout('{"texts": 0}')
        self.assertEqual(error, 'invalid extraction output')

    def test_non_string_elements(self):
        """texts 元素非字符串判输出畸形"""
        _, error = self._run_with_stdout('{"texts": [123]}')
        self.assertEqual(error, 'invalid extraction output')


class TestCopyNotSessionAnchor(unittest.TestCase):
    """/copy 结果是只读工具消息：发送成功也不更新 last_message_id（锚点保持在 stop 卡片）"""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp()
        # 单例隔离：保存-清空-initialize，tearDownClass 恢复
        cls._old_instance = SessionChatStore._instance
        SessionChatStore._instance = None
        SessionChatStore.initialize(cls._tmpdir)
        cls.store = SessionChatStore.get_instance()

    @classmethod
    def tearDownClass(cls):
        SessionChatStore._instance = cls._old_instance
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def test_success_keeps_last_message_id(self):
        fixture = os.path.join(self._tmpdir, 'anchor-session.jsonl')
        with open(fixture, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'user', 'message': {'content': '问'}}) + '\n')
            f.write(json.dumps({'type': 'assistant', 'sessionId': 'sid-anchor',
                                'message': {'content': [
                                    {'type': 'text', 'text': '答复'}]}}) + '\n')
        self.store.save('sid-anchor', 'oc_1')
        self.store.set_transcript_path('sid-anchor', fixture)
        # 预置既有锚点（stop 卡片）：/copy 不得挪动也不得清空
        self.store.set_last_message_id('sid-anchor', 'om_stop_card')

        from handlers.transcript import handle_copy_response
        with mock.patch('handlers.outbound.reply_markdown',
                        return_value=(True, 'om_copy_result')) as mocked:
            status, body = handle_copy_response(
                {'session_id': 'sid-anchor', 'chat_id': 'oc_1', 'message_id': 'om_cmd'})

        self.assertEqual(status, 200)
        self.assertTrue(body['ok'])
        mocked.assert_called_once()
        # 发送成功也不更新链式锚点（设计选择：工具消息非会话内容）
        self.assertEqual(self.store.get_last_message_id('sid-anchor'), 'om_stop_card')


if __name__ == '__main__':
    unittest.main()
