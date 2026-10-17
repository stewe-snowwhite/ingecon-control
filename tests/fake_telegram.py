"""aiogram BaseSession double: API methods stay in memory, never HTTP."""
from datetime import datetime, timezone

from aiogram.client.session.base import BaseSession
from aiogram.methods import (AnswerCallbackQuery, GetMe, GetWebhookInfo, SendMessage,
                             SetMyCommands)
from aiogram.types import Chat, Message, User, WebhookInfo


class FakeTelegramSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls, self.failures = [], []
        self.closed = False

    async def close(self):
        self.closed = True

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method,SendMessage):
            if self.failures:
                raise self.failures.pop(0)
            return Message(message_id=len(self.calls),date=datetime.now(timezone.utc),
                           chat=Chat(id=method.chat_id,type='private'),text=method.text)
        if isinstance(method,GetMe):
            return User(id=123456,is_bot=True,first_name='Mock',username='MockIngeconBot')
        if isinstance(method,GetWebhookInfo):
            return WebhookInfo(url='',has_custom_certificate=False,pending_update_count=0)
        if isinstance(method,(AnswerCallbackQuery,SetMyCommands)):
            return True
        raise AssertionError('Unexpected Telegram API method: '+type(method).__name__)

    async def stream_content(self, *args, **kwargs):
        if False:
            yield b''
        raise AssertionError('File/network streaming is forbidden in tests')

    @property
    def messages(self):
        return [call for call in self.calls if isinstance(call,SendMessage)]
