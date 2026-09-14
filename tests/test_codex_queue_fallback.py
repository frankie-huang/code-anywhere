"""Codex 撞锁降级（codex queue）测试

钉住降级链路的纯逻辑行为：
    - 特征匹配：真实撞锁错误（含 "already has an active writer"）命中；
      宽前缀 "thread-store conflict" 单独出现不命中（防未来非 writer 的
      thread-store 冲突误判致消息滞留）；普通错误不命中
    - 能力声明：codex supports_queue 为 True；Claude 声明 False、特征表空、
      无 queue 命令
    - 命令构建：prompt 经 shlex.quote（含引号/换行安全）、debug 版脱敏、
      CODEX_ARGS_TEMPLATE 组装（含 wrapper 打包形态）、env overrides 前缀注入
    - try_queue_fallback 四种回落分支 + 投递成功分支 + cwd 对齐（mock
      subprocess.run，不真调 codex CLI）

背景：降级只在会话有活跃 writer（终端 TUI / 桌面端）时投递队列——锁冲突
⟺ 队列有人消费；误判会把消息投进无消费者的队列静默滞留。
"""

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src', 'server'))
sys.path.insert(0, os.path.join(ROOT, 'src', 'shared'))

from agents import (get_agent_adapter, is_concurrent_session_error,  # noqa: E402
                    try_queue_fallback)

# 2026-09-07 codex 进程真实撞锁错误（~/.codex/logs_2.sqlite 实录）
ERR_LOCK = ('ERROR codex_core::session::session: failed to initialize thread '
            'persistence: thread-store conflict: thread 01a079dc already has '
            'an active writer')
ERR_GENERIC_CONFLICT = 'thread-store conflict: store corrupted'
ERR_OTHER = 'API Error: 500 internal server error'

SID = '01a079dc-c6bf-7481-a654-c0dbc897b2ac'


class TestConcurrentSessionError(unittest.TestCase):

    def setUp(self):
        self.adapter = get_agent_adapter('codex')

    def test_real_lock_error_matches(self):
        """真实撞锁错误（两特征同现）命中"""
        self.assertTrue(self.adapter.is_concurrent_session_error(ERR_LOCK))

    def test_generic_conflict_alone_not_matched(self):
        """宽前缀单独出现不命中——防未来非 writer 的 thread-store 冲突误判"""
        self.assertFalse(self.adapter.is_concurrent_session_error(ERR_GENERIC_CONFLICT))

    def test_ordinary_error_not_matched(self):
        self.assertFalse(self.adapter.is_concurrent_session_error(ERR_OTHER))

    def test_shared_predicate_unknown_agent_safe(self):
        """共享判定入口对未知 agent_type 返回 False 而非抛异常"""
        self.assertFalse(is_concurrent_session_error('bogus_agent', ERR_LOCK))

    def test_claude_adapter_has_no_capability(self):
        """Claude 无此概念：能力声明 False、特征表空、无 queue 命令"""
        adapter = get_agent_adapter('claude')
        self.assertFalse(adapter.supports_queue)
        self.assertFalse(adapter.is_concurrent_session_error(ERR_LOCK))
        self.assertIsNone(adapter.build_queue_command_string('claude', SID, 'hi'))

    def test_codex_supports_queue(self):
        self.assertTrue(get_agent_adapter('codex').supports_queue)


class TestBuildQueueCommand(unittest.TestCase):

    def setUp(self):
        self.adapter = get_agent_adapter('codex')

    def test_plain_prompt(self):
        cmd = self.adapter.build_queue_command_string('codex', SID, 'hello world')
        self.assertEqual(cmd, "codex queue --thread %s --message 'hello world'" % SID)

    def test_prompt_with_quotes_and_newlines(self):
        """含单双引号与换行的 prompt 必须经 shlex.quote 安全转义"""
        prompt = "it's a \"test\"\nsecond line"
        cmd = self.adapter.build_queue_command_string('codex', SID, prompt)
        # round-trip：shlex.split 还原出的 --message 应等于原文
        import shlex
        argv = shlex.split(cmd)
        self.assertEqual(argv[argv.index('--message') + 1], prompt)

    def test_debug_redacts_prompt(self):
        cmd = self.adapter.build_queue_command_string(
            'codex', SID, 'secret prompt', debug=True)
        self.assertEqual(cmd, 'codex queue --thread %s --message PROMPT' % SID)

    def test_command_with_extra_flags(self):
        """CODEX_COMMAND 带附加 flag 时逐 token 展开（与 build_command_string 一致），
        全局 flag 位于 queue 子命令之前"""
        cmd = self.adapter.build_queue_command_string(
            'codex --sandbox danger-full-access', SID, 'hi')
        import shlex
        argv = shlex.split(cmd)
        self.assertEqual(argv[:3], ['codex', '--sandbox', 'danger-full-access'])
        self.assertEqual(argv[3], 'queue')
        self.assertEqual(argv[argv.index('--message') + 1], 'hi')

    def test_wrapper_template_respected(self):
        """CODEX_ARGS_TEMPLATE 为 wrapper 形态（-a "{args}"）时按模板组装，
        queue 降级与 exec 路径走同一模板"""
        with mock.patch('config.get_codex_args_template',
                        return_value='{cmd} exec-helper -a "{args}"'):
            cmd = self.adapter.build_queue_command_string('codex', SID, 'hi')
        import shlex
        argv = shlex.split(cmd)
        self.assertEqual(argv[:3], ['codex', 'exec-helper', '-a'])
        # "{args}" 引号占位符 → 全部 args 打包为单个 shell 参数
        self.assertEqual(argv[3], 'queue --thread %s --message hi' % SID)

    def test_env_overrides_prepended(self):
        """session env 快照与 exec 路径同样注入命令前缀"""
        fake_store = mock.Mock()
        fake_store.get_env_overrides.return_value = {'FOO_BAR': 'baz'}
        with mock.patch(
                'stores.session_chat_store.SessionChatStore.get_instance',
                return_value=fake_store):
            cmd = self.adapter.build_queue_command_string('codex', SID, 'hi')
        self.assertTrue(cmd.startswith('FOO_BAR=baz codex queue --thread'),
                        cmd)

    def test_env_redacted_in_debug(self):
        """debug 版同时隐藏 env 快照值与 prompt"""
        fake_store = mock.Mock()
        fake_store.get_env_overrides.return_value = {'FOO_BAR': 'baz'}
        with mock.patch(
                'stores.session_chat_store.SessionChatStore.get_instance',
                return_value=fake_store):
            cmd = self.adapter.build_queue_command_string(
                'codex', SID, 'secret', debug=True)
        self.assertTrue(cmd.startswith("FOO_BAR='***' codex queue --thread"), cmd)
        self.assertIn('--message PROMPT', cmd)


@mock.patch('agents.subprocess.run')
@mock.patch.dict(os.environ, {'SHELL': '/bin/bash'})
class TestTryQueueFallback(unittest.TestCase):

    def _ok_result(self):
        result = mock.Mock()
        result.returncode = 0
        result.stdout = 'Queued message x for thread %s.' % SID
        result.stderr = ''
        return result

    def test_dispatches_queue_on_lock_error(self, mock_run):
        """撞锁错误触发 codex queue 投递，cwd 对齐正常启动路径（project_dir）"""
        mock_run.return_value = self._ok_result()
        self.assertTrue(try_queue_fallback('codex', SID, '/proj/dir', 'codex',
                                           'hi', ERR_LOCK))
        argv = mock_run.call_args[0][0]
        self.assertIn('queue', argv[2])  # build_shell_cmd: [shell, -ic, cmd_str]
        self.assertEqual(mock_run.call_args[1].get('cwd'), '/proj/dir')

    def test_build_env_passed_to_subprocess(self, mock_run):
        """adapter.build_env 的返回值原样传给子进程 env（对齐 launch_agent）"""
        adapter = get_agent_adapter('codex')
        with mock.patch.object(adapter, 'build_env', return_value={'X_ENV': '1'}):
            mock_run.return_value = self._ok_result()
            self.assertTrue(try_queue_fallback('codex', SID, '', 'codex',
                                               'hi', ERR_LOCK))
        self.assertEqual(mock_run.call_args[1].get('env'), {'X_ENV': '1'})

    def test_non_lock_error_skipped(self, mock_run):
        """普通错误不投递（回落原错误通知）"""
        self.assertFalse(try_queue_fallback('codex', SID, '', 'codex', 'hi', ERR_OTHER))
        mock_run.assert_not_called()

    def test_unsupported_adapter_skipped(self, mock_run):
        """Claude 不支持队列投递，锁冲突特征也不触发"""
        self.assertFalse(try_queue_fallback('claude', SID, '', 'claude', 'hi', ERR_LOCK))
        mock_run.assert_not_called()

    def test_queue_failure_returns_false(self, mock_run):
        """queue 命令失败（如旧版 CLI 无子命令）回落原错误通知"""
        result = mock.Mock()
        result.returncode = 1
        result.stdout = ''
        result.stderr = 'error: unrecognized subcommand'
        mock_run.return_value = result
        self.assertFalse(try_queue_fallback('codex', SID, '', 'codex', 'hi', ERR_LOCK))

    def test_execution_exception_returns_false(self, mock_run):
        """queue 执行异常（超时等）不外抛"""
        mock_run.side_effect = Exception('timeout')
        self.assertFalse(try_queue_fallback('codex', SID, '', 'codex', 'hi', ERR_LOCK))


if __name__ == '__main__':
    unittest.main()
