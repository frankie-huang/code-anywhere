"""transcript.sh CLI 入口冒烟测试

钉住独立执行入口的加载、调用与三态退出码契约
（0 = 有内容 / 1 = 硬失败 / 2 = 无可提取内容）：
    - 相对路径调用：`cd src/lib && bash transcript.sh <file>` 时依赖
      加载不因 ${BASH_SOURCE%/*} 缺斜杠而拼错路径（回归：实测 exit 1）
    - 绝对路径调用：Python 侧 subprocess 的实际调用方式
    - 退出码语义：缺失文件 = 1、轮次未落盘 = 2、非法字节提取失败 = 1
    - Codex 持久化格式 + turn_id：第二参数透传到提取器切片指定轮次
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src', 'server'))

_TRANSCRIPT_SH = os.path.join(ROOT, 'src', 'lib', 'transcript.sh')


class TestTranscriptCli(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.mkdtemp()
        cls._fixture = os.path.join(cls._tmpdir, 'claude-session.jsonl')
        with open(cls._fixture, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'user', 'message': {'content': '问'}}) + '\n')
            f.write(json.dumps({'type': 'assistant', 'sessionId': 'sid',
                                'message': {'content': [
                                    {'type': 'text', 'text': '答复'}]}}) + '\n')

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls._tmpdir, ignore_errors=True)

    def test_absolute_path_invocation(self):
        """绝对路径调用（Python 侧 subprocess 的方式）正常提取"""
        result = subprocess.run(
            ['bash', _TRANSCRIPT_SH, self._fixture],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['texts'], ['答复'])

    def test_relative_path_invocation(self):
        """相对路径调用（cd src/lib && bash transcript.sh）不因依赖加载失败

        回归：${BASH_SOURCE%/*} 对不含斜杠的调用名会拼出
        "transcript.sh/core.sh" 的错误路径，实测 exit 1
        """
        lib_dir = os.path.dirname(_TRANSCRIPT_SH)
        result = subprocess.run(
            ['bash', os.path.basename(_TRANSCRIPT_SH), self._fixture],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=30, cwd=lib_dir)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['texts'], ['答复'])

    def test_missing_file_nonzero_exit(self):
        """文件缺失时退出码严格为 1（硬失败）"""
        result = subprocess.run(
            ['bash', _TRANSCRIPT_SH, os.path.join(self._tmpdir, 'nope.jsonl')],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=30)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout.strip(), '')

    def test_no_content_exits_two(self):
        """无可提取内容（如轮次未落盘）退出码严格为 2（非故障）"""
        fixture = os.path.join(self._tmpdir, 'running-session.jsonl')
        with open(fixture, 'w', encoding='utf-8') as f:
            f.write(json.dumps({'type': 'user', 'message': {'content': '新问题'}}) + '\n')
        result = subprocess.run(
            ['bash', _TRANSCRIPT_SH, fixture],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=30)
        self.assertEqual(result.returncode, 2)

    def test_extractor_failure_exits_one(self):
        """提取器执行失败（非法 UTF-8 字节令 jq/python 均失败）退出码严格为 1"""
        fixture = os.path.join(self._tmpdir, 'invalid-bytes.jsonl')
        with open(fixture, 'wb') as f:
            f.write(b'\xff\xfe{"type": "user"}\n')
        result = subprocess.run(
            ['bash', _TRANSCRIPT_SH, fixture],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=30)
        self.assertEqual(result.returncode, 1)

    def test_codex_turn_id_slicing(self):
        """Codex 持久化格式：turn_id 第二参数切片指定轮次"""
        fixture = os.path.join(self._tmpdir, 'codex-session.jsonl')
        records = [
            {'type': 'session_meta', 'payload': {'id': 'thread-1'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'T1'}},
            {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': '第一轮答复'}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete',
                                              'turn_id': 'T1', 'last_agent_message': '第一轮答复'}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'T2'}},
            {'type': 'event_msg', 'payload': {'type': 'agent_message', 'message': '第二轮答复'}},
            {'type': 'event_msg', 'payload': {'type': 'task_complete',
                                              'turn_id': 'T2', 'last_agent_message': '第二轮答复'}},
        ]
        with open(fixture, 'w', encoding='utf-8') as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')

        result = subprocess.run(
            ['bash', _TRANSCRIPT_SH, fixture, 'T1'],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIn('第一轮答复', data['texts'])
        self.assertNotIn('第二轮答复', ''.join(data['texts']))


if __name__ == '__main__':
    unittest.main()
