"""注册两阶段提交测试

钉住「notify → 确认 → 落盘」这条链的行为：
    - HTTP 续期分支（register._process_registration 同 callback_url）：
      notify 确认后才落盘新 token、notify 失败保持两侧旧 token、
      upsert 失败仅记日志不抛
    - 授权卡片批准分支（authorization.handle_authorization_decision）：
      store 不可用在 notify 之前拦截、notify 失败渲染黄卡不落盘、
      upsert 失败渲染红卡、配置缺失渲染红卡

背景：notify 走「网关 → callback」方向，与所有 /cb/* 转发同向——该方向
持续不通时 HTTP 模式整体不可用；notify 明确失败即 callback 必然没存上
新 token，跳过落盘即两侧保持一致。残留错位在 callback 下次重启重注册时
自愈，故不引入幂等重放或串行锁等额外机制。
"""

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src', 'server'))
sys.path.insert(0, os.path.join(ROOT, 'src', 'shared'))

from handlers import register as reg  # noqa: E402
from handlers.feishu import authorization as az  # noqa: E402

OWNER = 'u_test_owner'
URL = 'http://cb.test/callback'


def _fake_binding_store(binding=None, upsert_ok=True):
    """BindingStore 替身：get 返回 binding，upsert 返回 upsert_ok"""
    store = mock.MagicMock()
    store.get.return_value = binding
    store.upsert.return_value = upsert_ok
    return store


class TestRenewalBranch(unittest.TestCase):
    """同 callback_url 续期分支（register._process_registration）"""

    def test_notify_precedes_upsert_with_new_token(self):
        """轮换出新 token，notify 确认后才落盘（两者用的是同一个 token）"""
        store = _fake_binding_store({'callback_url': URL, 'auth_token': 'TOK_OLD'})
        with mock.patch('stores.binding_store.BindingStore.get_instance',
                        mock.MagicMock(return_value=store)), \
             mock.patch('services.auth_token.generate_auth_token',
                        mock.MagicMock(return_value='TOK_NEW')), \
             mock.patch('services.callback_client.notify_register_callback',
                        mock.MagicMock(return_value=True)) as notify, \
             mock.patch('platforms.get_im_adapter') as fa:
            fa.return_value.get_auth_secret.return_value = 'secret'
            reg._process_registration(URL, OWNER, '1.2.3.4', {})
        notify.assert_called_once_with(URL, OWNER, 'TOK_NEW')
        self.assertEqual(store.upsert.call_args[0][2], 'TOK_NEW')

    def test_notify_failure_keeps_binding(self):
        """notify 失败不动 binding：upsert 不执行，两侧保持旧 token"""
        store = _fake_binding_store({'callback_url': URL, 'auth_token': 'TOK_OLD'})
        with mock.patch('stores.binding_store.BindingStore.get_instance',
                        mock.MagicMock(return_value=store)), \
             mock.patch('services.callback_client.notify_register_callback',
                        mock.MagicMock(return_value=False)), \
             mock.patch('platforms.get_im_adapter') as fa:
            fa.return_value.get_auth_secret.return_value = 'secret'
            reg._process_registration(URL, OWNER, '1.2.3.4', {})
        store.upsert.assert_not_called()

    def test_upsert_failure_does_not_raise(self):
        """upsert 失败记日志、不抛异常（后台线程，抛出会中断注册流程）"""
        store = _fake_binding_store({'callback_url': URL, 'auth_token': 'TOK_OLD'},
                                    upsert_ok=False)
        with mock.patch('stores.binding_store.BindingStore.get_instance',
                        mock.MagicMock(return_value=store)), \
             mock.patch('services.callback_client.notify_register_callback',
                        mock.MagicMock(return_value=True)), \
             mock.patch('platforms.get_im_adapter') as fa:
            fa.return_value.get_auth_secret.return_value = 'secret'
            reg._process_registration(URL, OWNER, '1.2.3.4', {})  # 不应抛
        store.upsert.assert_called_once()


class TestAuthorizationDecision(unittest.TestCase):
    """授权卡片批准分支（authorization.handle_authorization_decision）"""

    def _run(self, store, notify_ok=True):
        with mock.patch('stores.binding_store.BindingStore.get_instance',
                        mock.MagicMock(return_value=store)), \
             mock.patch('services.auth_token.generate_auth_token',
                        mock.MagicMock(return_value='NEW_TOKEN')), \
             mock.patch('services.callback_client.notify_register_callback',
                        mock.MagicMock(return_value=notify_ok)) as notify, \
             mock.patch('config.FEISHU_APP_SECRET', 'test_secret'):
            resp = az.handle_authorization_decision(
                URL, OWNER, '1.2.3.4', True, {})
        return resp, notify

    def _card(self, resp):
        return resp['card']['data']['header']

    def test_success_creates_binding(self):
        store = _fake_binding_store(None)
        resp, _ = self._run(store)
        store.upsert.assert_called_once()
        self.assertEqual(resp['toast']['type'], 'success')

    def test_notify_failure_returns_yellow_card(self):
        """notify 失败：不落盘 + 黄卡引导重试"""
        store = _fake_binding_store(None)
        resp, _ = self._run(store, notify_ok=False)
        store.upsert.assert_not_called()
        header = self._card(resp)
        self.assertEqual(header['template'], 'yellow')
        self.assertIn('授权失败', header['title']['content'])

    def test_store_missing_aborts_before_notify(self):
        """store 不可用：notify 都不发起（避免 Callback 单侧持 token）+ 红卡"""
        resp, notify = self._run(None)
        notify.assert_not_called()
        self.assertEqual(resp['toast']['type'], 'error')
        self.assertEqual(self._card(resp)['template'], 'red')

    def test_upsert_failure_returns_red_card(self):
        """Callback 已确认但本地写盘失败：红卡引导重新注册"""
        store = _fake_binding_store(None, upsert_ok=False)
        resp, _ = self._run(store)
        store.upsert.assert_called_once()
        self.assertEqual(resp['toast']['type'], 'error')
        self.assertEqual(self._card(resp)['template'], 'red')

    def test_secret_missing_returns_red_card(self):
        """FEISHU_APP_SECRET 未配置：红卡（配置错误需管理员，重试无用）"""
        store = _fake_binding_store(None)
        with mock.patch('stores.binding_store.BindingStore.get_instance',
                        mock.MagicMock(return_value=store)), \
             mock.patch('services.callback_client.notify_register_callback') as notify, \
             mock.patch('config.FEISHU_APP_SECRET', ''):
            resp = az.handle_authorization_decision(
                URL, OWNER, '1.2.3.4', True, {})
        notify.assert_not_called()
        self.assertEqual(resp['toast']['type'], 'error')
        header = self._card(resp)
        self.assertEqual(header['template'], 'red')
        self.assertIn('授权失败', header['title']['content'])


if __name__ == '__main__':
    unittest.main()
