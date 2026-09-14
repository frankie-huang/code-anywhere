"""飞书入站事件解析测试

钉住「原始飞书事件 JSON → 纯文本 / @提及 / 命令 / IMEvent」这条解析链的行为，
防止 adapter.parse_event 与 content.py 的后续改动引入回归；其中
TestParseEventEquivalence 保留了收口前的内联提取逻辑作对照（等价性基线）。

事件样本为合成数据，结构照抄真实日志（log/feishu_message/），ID 与内容均为占位值。
"""

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'src', 'server'))
sys.path.insert(0, os.path.join(ROOT, 'src', 'shared'))

from handlers.feishu import _parse_command  # noqa: E402
from handlers.feishu.content import (build_mention_resolution,  # noqa: E402
                                     extract_message_text)
from platforms.feishu_adapter import FeishuAdapter  # noqa: E402
from platforms.models import IMEventKind  # noqa: E402

BOT_OPEN_ID = 'ou_test_bot_0000000000000000000000'
SENDER_OPEN_ID = 'ou_test_sender_00000000000000000000'


# =========================================================================
# 事件样本（合成，结构与真实日志一致）
# =========================================================================

def _envelope(message, sender_id=None):
    """包一层飞书 im.message.receive_v1 事件信封"""
    return {
        'schema': '2.0',
        'header': {
            'event_id': 'evt_test_0001',
            'token': '',
            'create_time': '1787426748251',
            'event_type': 'im.message.receive_v1',
            'tenant_key': 'tk_test',
            'app_id': 'cli_test',
        },
        'event': {
            'message': message,
            'sender': {
                'sender_id': sender_id if sender_id is not None else {
                    'open_id': SENDER_OPEN_ID,
                    'union_id': 'on_test_0000000000000000000000000',
                    'user_id': 'u_test01',
                },
                'sender_type': 'user',
                'tenant_key': 'tk_test',
            },
        },
    }


# 单聊纯文本
TEXT_P2P = _envelope({
    'message_id': 'om_test_p2p_0001',
    'chat_id': 'oc_test_p2p_0001',
    'chat_type': 'p2p',
    'message_type': 'text',
    'parent_id': '',
    'content': '{"text":"/new"}',
})

# 群聊 @bot（含 @_user_1 占位符）
TEXT_GROUP_AT_BOT = _envelope({
    'message_id': 'om_test_group_0001',
    'chat_id': 'oc_test_group_0001',
    'chat_type': 'group',
    'message_type': 'text',
    'parent_id': '',
    'content': '{"text":"@_user_1 帮我看下"}',
    'mentions': [{
        'id': {'open_id': BOT_OPEN_ID, 'union_id': 'on_test_bot', 'user_id': ''},
        'key': '@_user_1',
        'name': 'Claude Code 助手',
        'tenant_key': 'tk_test',
    }],
})

# 话题内回复（parent_id 非空）
TEXT_GROUP_THREAD = _envelope({
    'message_id': 'om_test_group_0002',
    'chat_id': 'oc_test_group_0001',
    'chat_type': 'group',
    'message_type': 'text',
    'parent_id': 'om_test_group_0001',
    'content': '{"text":"继续"}',
})

# 群聊富文本
POST_GROUP = _envelope({
    'message_id': 'om_test_group_0003',
    'chat_id': 'oc_test_group_0001',
    'chat_type': 'group',
    'message_type': 'post',
    'parent_id': '',
    'content': ('{"title":"标题","content":[[{"tag":"text","text":"第一段"}],'
                '[{"tag":"text","text":"第二段"}]]}'),
})

# 卡片按钮回调
CARD_ACTION = {
    'schema': '2.0',
    'header': {
        'event_id': 'evt_test_0002',
        'token': '',
        'create_time': '1787426748251',
        'event_type': 'card.action.trigger',
        'app_id': 'cli_test',
    },
    'event': {
        # 生产报文的 operator 含 4 键（tenant_key/user_id/open_id/union_id），
        # id_values 遍历全部值——fixture 保持同形态，防「只测两键」漏掉回归
        'operator': {'tenant_key': 'tk_test', 'user_id': 'u_test01',
                     'open_id': SENDER_OPEN_ID, 'union_id': 'on_test01'},
        'action': {
            'value': {'action': 'allow', 'request_id': 'req_test_0001',
                      'owner_id': SENDER_OPEN_ID},
            'tag': 'button',
        },
        'context': {'open_message_id': 'om_test_card_0001'},
    },
}


class _FakeService(object):
    """FeishuAPIService 替身，只提供 bot_open_id"""

    def __init__(self, bot_open_id=''):
        self.bot_open_id = bot_open_id


def _patch_bot(bot_open_id=BOT_OPEN_ID, app_id=''):
    """让 build_mention_resolution 看到指定的 bot 身份"""
    return mock.patch.multiple(
        'services.feishu_api.FeishuAPIService',
        get_instance=mock.MagicMock(return_value=_FakeService(bot_open_id)),
    ), mock.patch('config.FEISHU_APP_ID', app_id)


class TestBuildMentionResolution(unittest.TestCase):
    """@提及 替换表构建 + 是否 @bot 判定"""

    def _resolve(self, mentions, bot_open_id=BOT_OPEN_ID, app_id=''):
        p_service, p_app = _patch_bot(bot_open_id, app_id)
        with p_service, p_app:
            return build_mention_resolution(mentions)

    def test_none_mentions(self):
        self.assertEqual(self._resolve(None), ({}, False))

    def test_empty_mentions(self):
        self.assertEqual(self._resolve([]), ({}, False))

    def test_at_bot_by_open_id(self):
        mentions = TEXT_GROUP_AT_BOT['event']['message']['mentions']
        resolution, is_at_bot = self._resolve(mentions)
        # bot 被替换为空串（从正文里删掉）
        self.assertEqual(resolution, {'@_user_1': ''})
        self.assertTrue(is_at_bot)

    def test_at_bot_by_app_id(self):
        """bot_info.app_id 匹配分支（飞书历史下发格式）"""
        mentions = [{'key': '@_user_1', 'name': 'bot',
                     'bot_info': {'app_id': 'cli_test'},
                     'id': {'open_id': 'ou_other'}}]
        resolution, is_at_bot = self._resolve(mentions, bot_open_id='', app_id='cli_test')
        self.assertEqual(resolution, {'@_user_1': ''})
        self.assertTrue(is_at_bot)

    def test_at_person_with_name_and_uid(self):
        mentions = [{'key': '@_user_1', 'name': '张三',
                     'id': {'open_id': 'ou_person', 'user_id': 'u_zhangsan'}}]
        resolution, is_at_bot = self._resolve(mentions)
        self.assertEqual(resolution, {'@_user_1': '@张三(u_zhangsan)'})
        self.assertFalse(is_at_bot)

    def test_at_person_without_uid(self):
        mentions = [{'key': '@_user_1', 'name': '张三', 'id': {'open_id': 'ou_person'}}]
        resolution, _ = self._resolve(mentions)
        self.assertEqual(resolution, {'@_user_1': '@张三'})

    def test_at_person_without_name(self):
        """无姓名时保留原占位符（安全降级）"""
        mentions = [{'key': '@_user_1', 'id': {'open_id': 'ou_person'}}]
        resolution, _ = self._resolve(mentions)
        self.assertEqual(resolution, {'@_user_1': '@_user_1'})

    def test_mention_without_key_skipped(self):
        mentions = [{'name': '张三', 'id': {'open_id': 'ou_person'}}]
        self.assertEqual(self._resolve(mentions), ({}, False))

    def test_non_dict_mention_skipped(self):
        self.assertEqual(self._resolve(['bad', None]), ({}, False))

    def test_bot_and_person_mixed(self):
        mentions = [
            {'key': '@_user_1', 'name': 'bot', 'id': {'open_id': BOT_OPEN_ID}},
            {'key': '@_user_2', 'name': '张三',
             'id': {'open_id': 'ou_person', 'user_id': 'u_zhangsan'}},
        ]
        resolution, is_at_bot = self._resolve(mentions)
        self.assertEqual(resolution,
                         {'@_user_1': '', '@_user_2': '@张三(u_zhangsan)'})
        self.assertTrue(is_at_bot)


class TestExtractMessageText(unittest.TestCase):
    """消息 content → 纯文本"""

    # === text ===

    def test_text_plain(self):
        self.assertEqual(extract_message_text('text', '{"text":"hello"}'), 'hello')

    def test_text_at_bot_removed(self):
        """@bot 占位符连同尾随空格一起删除"""
        self.assertEqual(
            extract_message_text('text', '{"text":"@_user_1 帮我看下"}',
                                 {'@_user_1': ''}),
            '帮我看下')

    def test_text_at_person_replaced(self):
        self.assertEqual(
            extract_message_text('text', '{"text":"@_user_1 你好"}',
                                 {'@_user_1': '@张三(u1)'}),
            '@张三(u1) 你好')

    def test_text_at_placeholder_not_in_table_kept(self):
        """替换表里没有的占位符保留原样"""
        self.assertEqual(
            extract_message_text('text', '{"text":"@_user_9 hi"}', {'@_user_1': ''}),
            '@_user_9 hi')

    def test_text_without_resolution_keeps_placeholder(self):
        self.assertEqual(
            extract_message_text('text', '{"text":"@_user_1 hi"}'),
            '@_user_1 hi')

    def test_text_at_without_trailing_space(self):
        self.assertEqual(
            extract_message_text('text', '{"text":"@_user_1你好"}',
                                 {'@_user_1': '@张三'}),
            '@张三你好')

    # === post ===

    def test_post_title_and_paragraphs(self):
        content = POST_GROUP['event']['message']['content']
        self.assertEqual(extract_message_text('post', content), '标题\n第一段\n第二段')

    def test_post_without_title(self):
        content = '{"content":[[{"tag":"text","text":"正文"}]]}'
        self.assertEqual(extract_message_text('post', content), '正文')

    def test_post_link(self):
        content = '{"content":[[{"tag":"a","text":"链接","href":"https://e.com"}]]}'
        self.assertEqual(extract_message_text('post', content),
                         '[链接](https://e.com)')

    def test_post_link_without_text(self):
        content = '{"content":[[{"tag":"a","href":"https://e.com"}]]}'
        self.assertEqual(extract_message_text('post', content),
                         '[https://e.com](https://e.com)')

    def test_post_code_block(self):
        content = ('{"content":[[{"tag":"code_block","language":"python",'
                   '"text":"print(1)\\n"}]]}')
        self.assertEqual(extract_message_text('post', content),
                         '```python\nprint(1)\n```')

    def test_post_at_via_resolution(self):
        content = '{"content":[[{"tag":"at","user_id":"@_user_1"}]]}'
        self.assertEqual(
            extract_message_text('post', content, {'@_user_1': '@张三(u1)'}),
            '@张三(u1)')

    def test_post_at_fallback_user_name(self):
        content = '{"content":[[{"tag":"at","user_id":"@_user_9","user_name":"李四"}]]}'
        self.assertEqual(extract_message_text('post', content, {}), '@李四')

    def test_post_ignores_media_tags(self):
        content = ('{"content":[[{"tag":"img","image_key":"k"},'
                   '{"tag":"text","text":"文字"}]]}')
        self.assertEqual(extract_message_text('post', content), '文字')

    def test_post_empty_paragraph_dropped(self):
        content = '{"content":[[],[{"tag":"text","text":"正文"}]]}'
        self.assertEqual(extract_message_text('post', content), '正文')

    # === 其他类型与异常 ===

    def test_image_returns_empty(self):
        self.assertEqual(extract_message_text('image', '{"image_key":"img_v2_x"}'), '')

    def test_invalid_json_returned_as_is(self):
        self.assertEqual(extract_message_text('text', 'not-json'), 'not-json')

    def test_non_dict_json_returns_empty(self):
        self.assertEqual(extract_message_text('text', '[1,2]'), '')

    def test_empty_content_returns_empty(self):
        self.assertEqual(extract_message_text('text', '{}'), '')


class TestParseCommand(unittest.TestCase):
    """斜杠命令解析（纯字符串处理，与 IM 平台无关）"""

    def test_plain_text_not_command(self):
        self.assertEqual(_parse_command('hello'), (False, '', ''))

    def test_command_without_args(self):
        self.assertEqual(_parse_command('/new'), (True, 'new', ''))

    def test_command_with_args(self):
        self.assertEqual(_parse_command('/new --dir=/tmp hello'),
                         (True, 'new', '--dir=/tmp hello'))

    def test_command_leading_space(self):
        self.assertEqual(_parse_command('  /clear'), (True, 'clear', ''))

    def test_empty_text(self):
        self.assertEqual(_parse_command(''), (False, '', ''))


class TestParseEvent(unittest.TestCase):
    """FeishuAdapter.parse_event：原始事件 → IMEvent"""

    def setUp(self):
        self.adapter = FeishuAdapter()

    def _parse(self, raw, bot_open_id=BOT_OPEN_ID, app_id=''):
        p_service, p_app = _patch_bot(bot_open_id, app_id)
        with p_service, p_app:
            return self.adapter.parse_event(raw)

    # === 消息事件 ===

    def test_p2p_text(self):
        event = self._parse(TEXT_P2P)
        self.assertEqual(event.kind, IMEventKind.MESSAGE)
        self.assertEqual(event.message_id, 'om_test_p2p_0001')
        self.assertEqual(event.chat_id, 'oc_test_p2p_0001')
        self.assertEqual(event.chat_type, 'p2p')
        self.assertEqual(event.message_type, 'text')
        self.assertEqual(event.parent_id, '')
        self.assertEqual(event.text, '/new')
        self.assertFalse(event.is_at_bot)

    def test_sender_ids(self):
        event = self._parse(TEXT_P2P)
        self.assertEqual(event.sender_open_id, SENDER_OPEN_ID)
        self.assertEqual(event.sender_user_id, 'u_test01')
        # 供 find_binding 逐个尝试查 binding，顺序与原 dict 一致
        self.assertEqual(event.sender_id_values,
                         [SENDER_OPEN_ID, 'on_test_0000000000000000000000000', 'u_test01'])

    def test_sender_user_id_falls_back_to_open_id(self):
        """sender_id 缺 user_id 键时回退 open_id（与原实现一致）"""
        raw = _envelope(TEXT_P2P['event']['message'],
                        sender_id={'open_id': SENDER_OPEN_ID})
        event = self._parse(raw)
        self.assertEqual(event.sender_user_id, SENDER_OPEN_ID)

    def test_sender_empty_user_id_not_replaced(self):
        """user_id 键存在但为空串时保持空串（get 有键即返回，不触发默认值）"""
        raw = _envelope(TEXT_P2P['event']['message'],
                        sender_id={'open_id': SENDER_OPEN_ID, 'user_id': ''})
        event = self._parse(raw)
        self.assertEqual(event.sender_user_id, '')

    def test_sender_id_values_drops_empty(self):
        """空标识不进候选列表——否则查 binding 时会用空串命中错误绑定"""
        raw = _envelope(TEXT_P2P['event']['message'],
                        sender_id={'open_id': SENDER_OPEN_ID, 'union_id': '',
                                   'user_id': 'u_test01'})
        event = self._parse(raw)
        self.assertEqual(event.sender_id_values, [SENDER_OPEN_ID, 'u_test01'])

    def test_operator_id_values_drops_empty(self):
        """卡片操作者标识同样过滤空值"""
        card = dict(CARD_ACTION)
        card['event'] = dict(CARD_ACTION['event'],
                             operator={'open_id': SENDER_OPEN_ID, 'user_id': ''})
        event = self._parse(card)
        self.assertEqual(event.operator_id_values, [SENDER_OPEN_ID])

    def test_group_at_bot(self):
        event = self._parse(TEXT_GROUP_AT_BOT)
        self.assertEqual(event.chat_type, 'group')
        self.assertTrue(event.is_at_bot)
        # @bot 占位符已从正文剔除
        self.assertEqual(event.text, '帮我看下')

    def test_group_at_bot_when_bot_unknown(self):
        """bot_open_id 未就绪时 @bot 降级为普通人员提及

        is_at_bot 不会误判为 True（群聊 @bot 过滤据此放行），但正文里的
        占位符会被替换成 '@机器人名' 而非删除——这是 content.py 的既有降级行为。
        """
        event = self._parse(TEXT_GROUP_AT_BOT, bot_open_id='')
        self.assertFalse(event.is_at_bot)
        self.assertEqual(event.text, '@Claude Code 助手 帮我看下')

    def test_thread_reply_parent_id(self):
        event = self._parse(TEXT_GROUP_THREAD)
        self.assertEqual(event.parent_id, 'om_test_group_0001')
        self.assertEqual(event.text, '继续')

    def test_post_message(self):
        event = self._parse(POST_GROUP)
        self.assertEqual(event.message_type, 'post')
        self.assertEqual(event.text, '标题\n第一段\n第二段')

    def test_raw_preserved(self):
        event = self._parse(TEXT_P2P)
        self.assertIs(event.raw, TEXT_P2P)
        self.assertEqual(event.event_id, 'evt_test_0001')

    # === 卡片回调 ===

    def test_card_action(self):
        event = self._parse(CARD_ACTION)
        self.assertEqual(event.kind, IMEventKind.CARD_ACTION)
        self.assertEqual(event.action_value,
                         {'action': 'allow', 'request_id': 'req_test_0001',
                          'owner_id': SENDER_OPEN_ID})
        self.assertEqual(event.card_message_id, 'om_test_card_0001')
        self.assertEqual(event.operator_open_id, SENDER_OPEN_ID)
        self.assertEqual(event.operator_user_id, 'u_test01')
        # values 遍历顺序 = fixture 键序（tenant_key/user_id/open_id/union_id）
        self.assertEqual(event.operator_id_values,
                         ['tk_test', 'u_test01', SENDER_OPEN_ID, 'on_test01'])

    def test_card_action_form_value_default(self):
        event = self._parse(CARD_ACTION)
        self.assertEqual(event.form_value, {})
        self.assertEqual(event.action_name, '')

    def test_card_action_form_value(self):
        """表单回传路径：action.form_value / action.name 提取"""
        card = dict(CARD_ACTION)
        card['event'] = dict(
            CARD_ACTION['event'],
            action=dict(CARD_ACTION['event']['action'],
                        form_value={'option': 'chat', 'input': 'hi'},
                        name='select_mode'))
        event = self._parse(card)
        self.assertEqual(event.form_value, {'option': 'chat', 'input': 'hi'})
        self.assertEqual(event.action_name, 'select_mode')

    def test_operator_user_id_falls_back_to_open_id(self):
        """operator 缺 user_id 时回退 open_id（与消息路径 sender 同语义）"""
        card = dict(CARD_ACTION)
        card['event'] = dict(
            CARD_ACTION['event'],
            operator={'open_id': SENDER_OPEN_ID, 'union_id': 'on_test01'})
        event = self._parse(card)
        self.assertEqual(event.operator_user_id, SENDER_OPEN_ID)
        self.assertEqual(event.operator_id_values, [SENDER_OPEN_ID, 'on_test01'])

    def test_message_missing_chat_type_defaults_empty(self):
        """message 缺 chat_type 键时默认空串（非 group 判定依赖此值）"""
        raw = {
            'header': {'event_type': 'im.message.receive_v1',
                       'event_id': 'evt_test_0003'},
            'event': {
                'message': {'message_id': 'om_test_0003', 'chat_id': 'oc_test',
                            'message_type': 'text', 'content': '{"text":"hi"}'},
                'sender': {'sender_id': {'open_id': SENDER_OPEN_ID}},
            },
        }
        event = self._parse(raw)
        self.assertEqual(event.chat_type, '')
        self.assertEqual(event.sender_user_id, SENDER_OPEN_ID)
        self.assertEqual(event.sender_id_values, [SENDER_OPEN_ID])

    # === 其他 ===

    def test_url_verification(self):
        event = self._parse({'type': 'url_verification', 'challenge': 'ch_test'})
        self.assertEqual(event.kind, IMEventKind.VERIFICATION)
        # challenge 留在 raw 里由调用方按平台约定应答
        self.assertEqual(event.raw['challenge'], 'ch_test')

    def test_unknown_event_type_returns_none(self):
        self.assertIsNone(self._parse({'header': {'event_type': 'im.chat.updated_v1'}}))

    def test_empty_payload_returns_none(self):
        self.assertIsNone(self._parse({}))

    def test_missing_message_fields_use_defaults(self):
        """缺字段不抛异常，取安全默认值"""
        raw = {'header': {'event_type': 'im.message.receive_v1'}, 'event': {}}
        event = self._parse(raw)
        self.assertEqual(event.kind, IMEventKind.MESSAGE)
        self.assertEqual(event.chat_id, '')
        self.assertEqual(event.text, '')
        self.assertEqual(event.sender_id_values, [])


class TestParseEventEquivalence(unittest.TestCase):
    """等价性基线：parse_event 的产出与收口前 _handle_message_event 的原地提取逐字段一致

    _original_extract 是收口前（b4f5d5a~1）handlers/feishu/__init__.py:163-192
    的逻辑复刻，不得改为调用 parse_event（那样就失去了对照意义）。
    """

    def setUp(self):
        self.adapter = FeishuAdapter()

    @staticmethod
    def _original_extract(data):
        """收口前的字段提取逻辑（逐行复刻，勿改为调用新实现）"""
        event = data.get('event', {})
        message = event.get('message', {})
        sender_id_obj = event.get('sender', {}).get('sender_id', {})

        mention_resolution, is_at_bot = build_mention_resolution(message.get('mentions'))
        text = extract_message_text(message.get('message_type', ''),
                                    message.get('content', '{}'), mention_resolution)
        sender_id = sender_id_obj.get('open_id', '')
        return {
            'message_id': message.get('message_id', ''),
            'chat_id': message.get('chat_id', ''),
            'chat_type': message.get('chat_type', ''),
            'message_type': message.get('message_type', ''),
            'parent_id': message.get('parent_id', ''),
            'sender_open_id': sender_id,
            'sender_user_id': sender_id_obj.get('user_id', sender_id),
            'text': text,
            'is_at_bot': is_at_bot,
        }

    def test_all_message_samples_identical(self):
        for name, sample in (('TEXT_P2P', TEXT_P2P),
                             ('TEXT_GROUP_AT_BOT', TEXT_GROUP_AT_BOT),
                             ('TEXT_GROUP_THREAD', TEXT_GROUP_THREAD),
                             ('POST_GROUP', POST_GROUP)):
            with self.subTest(sample=name):
                p_service, p_app = _patch_bot()
                with p_service, p_app:
                    event = self.adapter.parse_event(sample)
                    expected = self._original_extract(sample)
                actual = {
                    'message_id': event.message_id,
                    'chat_id': event.chat_id,
                    'chat_type': event.chat_type,
                    'message_type': event.message_type,
                    'parent_id': event.parent_id,
                    'sender_open_id': event.sender_open_id,
                    'sender_user_id': event.sender_user_id,
                    'text': event.text,
                    'is_at_bot': event.is_at_bot,
                }
                self.assertEqual(expected, actual)

    def test_card_action_identical(self):
        """卡片字段对照收口前（b4f5d5a~1）card_action.py:88-100 的提取

        operator_user_id 的 user_id→open_id fallback 不在那 13 行内——
        是与消息路径 sender_user_id 对齐的新语义（CHANGELOG 已知差异条目）。
        """
        inner = CARD_ACTION['event']
        operator = inner['operator']
        action = inner['action']
        p_service, p_app = _patch_bot()
        with p_service, p_app:
            event = self.adapter.parse_event(CARD_ACTION)
        self.assertEqual(event.operator_open_id, operator.get('open_id', ''))
        self.assertEqual(event.operator_user_id,
                         operator.get('user_id', operator.get('open_id', '')))
        self.assertEqual(event.action_value, action.get('value', {}))
        self.assertEqual(event.form_value, action.get('form_value', {}))
        self.assertEqual(event.card_message_id,
                         inner.get('context', {}).get('open_message_id', ''))


class TestEventSampleShape(unittest.TestCase):
    """样本形状自检：确保 fixture 与真实事件结构一致（样本被本文件各测试类共用）"""

    def test_message_samples_have_required_path(self):
        for name, sample in (('TEXT_P2P', TEXT_P2P),
                             ('TEXT_GROUP_AT_BOT', TEXT_GROUP_AT_BOT),
                             ('TEXT_GROUP_THREAD', TEXT_GROUP_THREAD),
                             ('POST_GROUP', POST_GROUP)):
            with self.subTest(sample=name):
                self.assertEqual(sample['header']['event_type'],
                                 'im.message.receive_v1')
                message = sample['event']['message']
                for field in ('message_id', 'chat_id', 'chat_type',
                              'message_type', 'parent_id', 'content'):
                    self.assertIn(field, message)
                self.assertIn('open_id', sample['event']['sender']['sender_id'])

    def test_card_action_sample_shape(self):
        self.assertEqual(CARD_ACTION['header']['event_type'], 'card.action.trigger')
        event = CARD_ACTION['event']
        self.assertIn('open_id', event['operator'])
        self.assertIn('value', event['action'])
        self.assertIn('open_message_id', event['context'])


if __name__ == '__main__':
    unittest.main()
