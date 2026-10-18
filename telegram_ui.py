"""aiogram handlers. All data/actions go through the SQLite store, never Modbus."""
import asyncio
import logging
from datetime import datetime, timezone

from aiogram import BaseMiddleware, Dispatcher, Router
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
                               TelegramNetworkError, TelegramRetryAfter, TelegramUnauthorizedError)
from aiogram.filters import Command
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           LinkPreviewOptions, Message)
from sqlalchemy.exc import SQLAlchemyError

from telegram_store import TelegramAccess

log = logging.getLogger('ingecon.telegram')
HELP = ('Команди: /start, /status, /inverters, /groups, /history, /help.\n'
        '/notifications — налаштування сповіщень. /control — вибір цілей.\n'
        'Прив’язка: у вебпанелі Налаштування → Telegram → Створити код, потім /start КОД у приватному чаті.\n'
        'Дані тільки з SQLite. Start/stop заблоковані до окремого дозволу; Modbus-записи не виконуються.')


def clean(value):
    return ''.join(char if char.isprintable() else ' ' for char in str(value))[:100]


def utc(value):
    return datetime.fromtimestamp(value,timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label,callback_data=data) for label,data in row] for row in rows])


def menu(role):
    rows = [[('Статус','menu:status'),('Інвертори','list:i:0')],
            [('Групи','list:g:0'),('Історія','menu:history')],
            [('Сповіщення','menu:notifications'),('Допомога','menu:help')]]
    if role in ('Administrator','Operator'):
        rows.append([('Керування 🔒','menu:control')])
    return keyboard(rows)


async def send(bot, telegram_id, text, markup=None):
    return await bot.send_message(chat_id=telegram_id,text=text,reply_markup=markup,parse_mode=None,
        link_preview_options=LinkPreviewOptions(is_disabled=True),protect_content=True)


class UpdateGate(BaseMiddleware):
    def __init__(self, store):
        self.store = store

    async def __call__(self, handler, event, data):
        item = event.message or event.callback_query
        message = item.message if isinstance(item,CallbackQuery) else item
        user = item.from_user if item else None
        if (not user or user.is_bot or not isinstance(message,Message) or
            message.chat.type!='private' or message.chat.id!=user.id):
            return None  # No data or even help is sent to group/inline/forwarded identities.
        if not self.store.accept_update(data['bot'].id,event.update_id,user.id):
            return None
        return await handler(event,data)


def build_dispatcher(store):
    dispatcher = Dispatcher(disable_fsm=True)
    dispatcher.update.outer_middleware(UpdateGate(store))
    router = Router()
    dispatcher.include_router(router)

    def listing(telegram_id, kind, page=0):
        rows, more = store.inventory(telegram_id,'g' if kind=='ng' else kind,page)
        buttons = []
        text = 'Інвертори' if kind=='i' else 'Групи сповіщень' if kind=='ng' else 'Групи'
        snapshot = {row['id']:row for row in store.snapshot(telegram_id)['inverters']} if kind=='i' else {}
        for row in rows:
            target_id,label = row[:2]
            caption = clean(label)
            if kind=='i':
                state = snapshot.get(target_id,{}).get('state','Unknown')
                caption = f'#{target_id} {caption} — {state}'
                text += f'\n#{target_id} {clean(label)} · {clean(row[2])} · {state}'
            else:
                text += '\n'+caption
            callback = f'pref:group:{target_id}' if kind=='ng' else f'target:{kind}:{target_id}'
            buttons.append([(caption,callback)])
        if not rows:
            text += '\nСписок порожній.'
        nav = []
        if page: nav.append(('←',f'list:{kind}:{page-1}'))
        if more: nav.append(('→',f'list:{kind}:{page+1}'))
        if nav: buttons.append(nav)
        if kind=='ng': buttons.append([('Усі групи','pref:all')])
        buttons.append([('Меню','menu:start')])
        return text,keyboard(buttons)

    def screen(telegram_id, section):
        username,role = store.identity(telegram_id)
        if section=='status':
            snapshot = store.snapshot(telegram_id)
            counts = {state:sum(row['state']==state for row in snapshot['inverters'])
                      for state in ('Online','Offline','Degraded','Unknown')}
            text = f"Усього: {len(snapshot['inverters'])}\n"+' · '.join(f'{key}: {value}' for key,value in counts.items())
            text += '\nМонітор: '+('активний' if snapshot['monitor']['alive'] else 'зупинений або дані застарілі')
            return text,menu(role)
        if section=='history':
            events = store.history(telegram_id)
            text = 'Останні події (UTC):'+('\nПодій немає.' if not events else '')
            for event_id,name,previous,state,occurred in events:
                text += f'\n#{event_id} {clean(name)}: {previous} → {state}\n{utc(occurred)}'
            return text,menu(role)
        if section=='control':
            if role=='Viewer':
                raise TelegramAccess('Viewer не має права керувати.')
            return 'Виберіть інвертор або групу. Усі start/stop заблоковані.',keyboard([
                [('Інвертор','list:i:0'),('Група','list:g:0')],[('Меню','menu:start')]])
        if section=='notifications':
            settings = store.preferences(telegram_id)
            text = 'Сповіщення: '+('увімкнено' if settings['notifications'] else 'вимкнено')
            text += '\nГрупа: '+clean(settings['group_filter'] or 'Усі')
            buttons = [[('Увімкнути / вимкнути','pref:notifications')]]
            for name in ('online','offline','degraded'):
                buttons.append([(name.title()+(' ✓' if settings[name] else ' —'),'pref:'+name)])
            buttons += [[('Вибрати групу','list:ng:0')],[('Меню','menu:start')]]
            return text,keyboard(buttons)
        if section=='help':
            return HELP,menu(role)
        return f'Прив’язано: {clean(username)} · {role}\nСтатуси лише з SQLite; керування заблоковано.',menu(role)

    async def message_handler(message, bot):
        telegram_id = message.from_user.id
        pieces = (message.text or '').split(maxsplit=1)
        command = pieces[0].split('@')[0].lstrip('/') if pieces else 'help'
        try:
            if command=='help':
                await send(bot,telegram_id,HELP)
                return
            if command=='start' and len(pieces)==2:
                store.redeem(telegram_id,pieces[1].strip())
            if command=='inverters':
                text,markup = listing(telegram_id,'i')
            elif command=='groups':
                text,markup = listing(telegram_id,'g')
            else:
                text,markup = screen(telegram_id,command)
            await send(bot,telegram_id,text,markup)
        except TelegramAccess as error:
            await send(bot,telegram_id,str(error))

    async def callback_handler(call, bot):
        telegram_id = call.from_user.id
        await call.answer()
        parts = (call.data or '').split(':')
        try:
            _,role = store.identity(telegram_id)
            if len(call.data or '')>64:
                raise TelegramAccess('Некоректна кнопка.')
            if parts[0]=='menu' and len(parts)==2 and parts[1] in ('start','status','history','help','control','notifications'):
                text,markup = screen(telegram_id,parts[1])
            elif parts[0]=='list' and len(parts)==3 and parts[1] in ('i','g','ng'):
                text,markup = listing(telegram_id,parts[1],int(parts[2]))
            elif parts[0]=='target' and len(parts)==3:
                with store.sessions() as session:
                    store.user(session,telegram_id)
                    targets,_ = store.targets(session,parts[1],int(parts[2]))
                    text = '\n'.join(f'#{inv.id} {clean(inv.name)} · {clean(inv.ip_address)}' for inv in targets)
                rows = [[('Меню','menu:start')]]
                if role in ('Administrator','Operator'):
                    rows.insert(0,[('Start 🔒',f'act:on:{parts[1]}:{parts[2]}'),('Stop 🔒',f'act:off:{parts[1]}:{parts[2]}')])
                markup = keyboard(rows)
            elif parts[0]=='act' and len(parts)==4:
                intent,payload = store.prepare(telegram_id,parts[1],parts[2],int(parts[3]))
                text = ('Підтвердьте '+('START' if parts[1]=='on' else 'STOP')+' для точного списку:\n'+
                    '\n'.join(f'#{row[0]} {clean(row[1])} · {clean(row[3])}' for row in payload['targets'])+
                    '\nДійсне 120 секунд, одноразове. Записи заблоковані; виконання не буде.')
                markup = keyboard([[('Підтвердити',f'confirm:{intent}'),('Скасувати',f'cancel:{intent}')]])
            elif parts[0] in ('confirm','cancel') and len(parts)==2:
                result,details = store.confirm(telegram_id,parts[1],cancel=parts[0]=='cancel')
                labels = {'blocked':'Заблоковано. Modbus-запис не виконувався.', 'expired':'Підтвердження застаріло.',
                          'cancelled':'Скасовано.', 'invalidated':'Права або цілі змінено. Підготуйте нове підтвердження.'}
                text = labels[result]+'\n'+'\n'.join(f"#{row['id']} {clean(row['name'])}: заблоковано" for row in details)
                markup = menu(role)
            elif parts[0]=='pref' and len(parts) in (2,3):
                if parts[1]=='all' and len(parts)==2:
                    store.preferences(telegram_id,'group')
                elif parts[1]=='group' and len(parts)==3:
                    store.preferences(telegram_id,'group',int(parts[2]))
                elif len(parts)==2:
                    store.preferences(telegram_id,parts[1])
                else:
                    raise TelegramAccess('Некоректне налаштування.')
                text,markup = screen(telegram_id,'notifications')
            else:
                raise TelegramAccess('Невідома кнопка. Відкрийте /start.')
            await send(bot,telegram_id,text,markup)
        except (ValueError,OverflowError) as error:
            if parts[0] in ('act','confirm','cancel'):
                store.audit_rejected(telegram_id,call.id)
            text = str(error) if isinstance(error,TelegramAccess) else 'Некоректні дані кнопки.'
            await send(bot,telegram_id,text)

    router.message.register(message_handler,Command('start','status','inverters','groups','history','help','notifications','control'))
    router.callback_query.register(callback_handler)

    @router.errors()
    async def safe_error(event,bot):
        # Never log update text, pairing codes, API exception strings or tokens.
        log.error('Telegram handler failed: %s',type(event.exception).__name__)
        item = event.update.message or event.update.callback_query
        message = item.message if isinstance(item,CallbackQuery) else item
        if (isinstance(event.exception,SQLAlchemyError) and isinstance(message,Message) and
            message.chat.type=='private' and item.from_user and message.chat.id==item.from_user.id):
            try:
                await send(bot,item.from_user.id,'База даних тимчасово недоступна. Повторіть запит пізніше; команди не виконуються.')
            except TelegramAPIError:
                pass
        return True

    return dispatcher


async def notification_batch(store, bot):
    store.fanout()
    seen = set()
    for _ in range(20):
        item = store.claim_delivery(seen)
        if item is None:
            break
        if item is False:
            continue
        delivery_id,telegram_id,event = item
        seen.add(telegram_id)  # At most one message per recipient in this 5-second batch.
        event_id,name,previous,state,occurred = event
        text = f'Подія #{event_id}: {clean(name)}\n{previous} → {state}\n{utc(occurred)}\nДжерело: журнал станів SQLite.'
        try:
            reply = await send(bot,telegram_id,text)
        except TelegramRetryAfter as error:
            store.finish_delivery(delivery_id,'pending',error='rate_limited',retry_after=error.retry_after)
        except TelegramForbiddenError:
            store.finish_delivery(delivery_id,'failed',error='forbidden')
            store.disable_notifications(telegram_id)
        except TelegramBadRequest:
            store.finish_delivery(delivery_id,'failed',error='bad_request')
        except TelegramUnauthorizedError:
            store.finish_delivery(delivery_id,'unknown',error='unauthorized')
            raise
        except (TelegramNetworkError,TelegramAPIError,TimeoutError):
            store.finish_delivery(delivery_id,'unknown',error='ambiguous_transport')
        except asyncio.CancelledError:
            store.finish_delivery(delivery_id,'unknown',error='interrupted')
            raise
        else:
            store.finish_delivery(delivery_id,'sent',message_id=reply.message_id)


async def notification_loop(store, bot):
    store.recover()
    cleaned = 0
    while True:
        try:
            if store.clock()-cleaned>=3600:
                store.cleanup()
                cleaned = store.clock()
            await notification_batch(store,bot)
        except TelegramUnauthorizedError:
            log.error('Telegram token rejected; notifications stopped.')
            return
        except (SQLAlchemyError,TelegramAPIError):
            log.error('Telegram notification cycle failed; retrying later.')
        await asyncio.sleep(5)
