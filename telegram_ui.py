"""aiogram handlers. All data/actions go through the SQLite store, never Modbus."""
import asyncio
import logging
import math
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
        'Статуси тільки з SQLite. Start/Stop — для Administrator/Operator після підтвердження '
        'і явного дозволу. /limit ID ВІДСОТОК — ліміт 1–100 для інвертора.')


def clean(value):
    return ''.join(char if char.isprintable() else ' ' for char in str(value))[:100]


def utc(value):
    return datetime.fromtimestamp(value,timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')


def power_value(row):
    value = row.get('active_power_w')
    return value if not row.get('stale', True) and type(value) in (int,float) and math.isfinite(value) else None


def power_text(watts):
    return f'{watts/1000:.2f}'.replace('.', ',')+' кВт' if watts is not None else '—'


def generation_text(snapshot):
    rows = snapshot['inverters']
    values = [(row,power_value(row)) for row in rows]
    known = [(row,value) for row,value in values if value is not None]
    if not known:
        return 'Поточна генерація: — ('+('немає обладнання' if not rows else 'немає актуальних даних')+')'
    text = 'Поточна генерація: '+power_text(sum(value for _,value in known))
    if len(known) != len(rows):
        text += f' (часткові дані {len(known)}/{len(rows)})'
    times = [row['checked_at'] for row,_ in known if type(row.get('checked_at')) in (int,float) and math.isfinite(row['checked_at'])]
    if times:
        first,last=min(times),max(times)
        text += '\nВиміри: '+(utc(first)+' — ' if first!=last else '')+utc(last)
    return text


def command_result_message(row):
    # Telegram presentation only; keep full transport details in the shared audit.
    return 'Запис команди підтверджено.' if row.get('outcome')=='register_confirmed' else clean(row['message'])


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label,callback_data=data) for label,data in row] for row in rows])


def menu(role, controls_enabled=False):
    rows = [[('Статус','menu:status'),('Інвертори','list:i:0')],
            [('Групи','list:g:0'),('Історія','menu:history')],
            [('Сповіщення','menu:notifications'),('Допомога','menu:help')]]
    if role in ('Administrator','Operator'):
        rows.append([('Керування' if controls_enabled else 'Керування 🔒','menu:control')])
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

    def control_note():
        return ('Start/Stop і ліміт дозволені після підтвердження. Сумісність прошивки не підтверджена.'
                if store.controls_enabled else 'Start/Stop заблоковані конфігурацією.')

    def target_lines(payload):
        lines=[]
        for row in payload['targets']:
            text=f'#{row[0]} {clean(row[1])} · {clean(row[3])}'
            transport=payload.get('transports',{}).get(str(row[0]))
            if transport: text+=f" · TCP {transport['port']}, Unit ID {transport['device_id']}"
            lines.append(text)
        return '\n'.join(lines)

    def listing(telegram_id, kind, page=0):
        rows, more = store.inventory(telegram_id,'g' if kind=='ng' else 'i' if kind=='a' else kind,page)
        buttons = []
        text = 'Група «Усі»' if kind=='a' else 'Інвертори' if kind=='i' else 'Групи сповіщень' if kind=='ng' else 'Групи'
        data = store.snapshot(telegram_id) if kind in ('i','g','a') else None
        snapshot = {row['id']:row for row in data['inverters']} if data else {}
        if kind=='g' and page==0:
            text += f'\nУсі · обладнання: {len(snapshot)}'
            buttons.append([('Усі','list:a:0')])
        if kind=='a':
            text += f'\nОбладнання: {len(snapshot)}\n'+generation_text(data)
        for row in rows:
            target_id,label = row[:2]
            caption = clean(label)
            if kind in ('i','a'):
                state = snapshot.get(target_id,{}).get('state','Unknown')
                caption = f'#{target_id} {caption} — {state}'
                text += f'\n#{target_id} {clean(label)} · {clean(row[2])} · {state} · '+power_text(power_value(snapshot.get(target_id,{})))
            else:
                if caption.casefold()=='усі': caption += ' (збережена група)'
                text += '\n'+caption
            callback = f'pref:group:{target_id}' if kind=='ng' else f'target:{"i" if kind=="a" else kind}:{target_id}'
            buttons.append([(caption,callback)])
        if not rows:
            text += '\nСписок порожній.'
        nav = []
        if page: nav.append(('←',f'list:{kind}:{page-1}'))
        if more: nav.append(('→',f'list:{kind}:{page+1}'))
        if nav: buttons.append(nav)
        if kind=='a':
            _,role=store.identity(telegram_id)
            if len(snapshot)>10 and role in ('Administrator','Operator'):
                text += '\nДля групової команди максимум 10 цілей; виберіть інвертор або меншу групу.'
            elif snapshot and role in ('Administrator','Operator'):
                text += '\nКоманди нижче стосуються всієї групи «Усі», включно з іншими сторінками.'
                suffix = '' if store.controls_enabled else ' 🔒'
                buttons.append([('Start: Усі'+suffix,'act:on:a:0'),('Stop: Усі'+suffix,'act:off:a:0')])
                if store.controls_enabled:
                    buttons.append([(f'{percent}%',f'act:limit:a:0:{percent}') for percent in (25,50,75,100)])
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
            text += '\nСтаном на '+utc(snapshot['generated_at'])+'\n'+generation_text(snapshot)
            return text,menu(role, store.controls_enabled)
        if section=='history':
            events = store.history(telegram_id)
            text = 'Останні події (UTC):'+('\nПодій немає.' if not events else '')
            for event_id,name,previous,state,occurred in events:
                text += f'\n#{event_id} {clean(name)}: {previous} → {state}\n{utc(occurred)}'
            return text,menu(role, store.controls_enabled)
        if section=='control':
            if role=='Viewer':
                raise TelegramAccess('Viewer не має права керувати.')
            return 'Виберіть інвертор або групу. '+control_note(),keyboard([
                [('Інвертор','list:i:0'),('Група','list:g:0'),('Усі','list:a:0')],[('Меню','menu:start')]])
        if section=='notifications':
            settings = store.preferences(telegram_id)
            text = 'Сповіщення: '+('увімкнено' if settings['notifications'] else 'вимкнено')
            group = clean(settings['group_filter'] or 'Усі')
            if settings['group_filter'] is not None and group.casefold()=='усі': group += ' (збережена група)'
            text += '\nГрупа: '+group
            buttons = [[('Увімкнути / вимкнути','pref:notifications')]]
            for name in ('online','offline','degraded'):
                buttons.append([(name.title()+(' ✓' if settings[name] else ' —'),'pref:'+name)])
            buttons += [[('Вибрати групу','list:ng:0')],[('Меню','menu:start')]]
            return text,keyboard(buttons)
        if section=='help':
            return HELP+'\n'+control_note(),menu(role, store.controls_enabled)
        return f'Прив’язано: {clean(username)} · {role}\nСтатуси лише з SQLite. '+control_note(),menu(role, store.controls_enabled)

    async def message_handler(message, bot):
        telegram_id = message.from_user.id
        pieces = (message.text or '').split(maxsplit=2)
        command = pieces[0].split('@')[0].lstrip('/') if pieces else 'help'
        try:
            if command=='help':
                await send(bot,telegram_id,HELP)
                return
            if command=='limit':
                if len(pieces) != 3:
                    raise TelegramAccess('Формат: /limit ID ВІДСОТОК (1–100). Потрібне окреме підтвердження.')
                intent, payload = store.prepare(telegram_id, 'limit', 'i', int(pieces[1]), int(pieces[2]))
                text = f"Підтвердьте ліміт {payload['value']}%:\n"+target_lines(payload)
                text += '\nДійсне 120 секунд, одноразове. '+control_note()
                await send(bot, telegram_id, text, keyboard([[('Підтвердити',f'confirm:{intent}'),('Скасувати',f'cancel:{intent}')]]))
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
        except ValueError as error:
            await send(bot,telegram_id,str(error) if isinstance(error,TelegramAccess) else 'Некоректна команда. Відкрийте /help.')

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
            elif parts[0]=='list' and len(parts)==3 and parts[1] in ('i','g','ng','a'):
                text,markup = listing(telegram_id,parts[1],int(parts[2]))
            elif parts[0]=='target' and len(parts)==3:
                with store.sessions() as session:
                    store.user(session,telegram_id)
                    targets,_ = store.targets(session,parts[1],int(parts[2]))
                    text = '\n'.join(f'#{inv.id} {clean(inv.name)} · {clean(inv.ip_address)}' for inv in targets)
                rows = [[('Меню','menu:start')]]
                if role in ('Administrator','Operator'):
                    suffix = '' if store.controls_enabled else ' 🔒'
                    rows.insert(0,[('Start'+suffix,f'act:on:{parts[1]}:{parts[2]}'),('Stop'+suffix,f'act:off:{parts[1]}:{parts[2]}')])
                    if store.controls_enabled:
                        rows.insert(1,[(f'{percent}%', f'act:limit:{parts[1]}:{parts[2]}:{percent}') for percent in (25,50,75,100)])
                markup = keyboard(rows)
            elif parts[0]=='act' and len(parts) in (4,5):
                value = int(parts[4]) if len(parts)==5 else 100
                intent,payload = store.prepare(telegram_id,parts[1],parts[2],int(parts[3]),value)
                label = {'on':'START', 'off':'STOP', 'limit':f'ЛІМІТ {value}%', 'limit_max':'МАКСИМУМ 100%'}[parts[1]]
                text = ('Підтвердьте '+label+' для точного списку:\n'+
                    target_lines(payload)+
                    '\nДійсне 120 секунд, одноразове. '+control_note()+
                    (' Дозволені команди виконують реальний Modbus-запис.' if store.controls_enabled else ''))
                markup = keyboard([[('Підтвердити',f'confirm:{intent}'),('Скасувати',f'cancel:{intent}')]])
            elif parts[0] in ('confirm','cancel') and len(parts)==2:
                # Bounded device I/O must not pause notification delivery.
                result,details = await asyncio.to_thread(store.confirm, telegram_id, parts[1], cancel=parts[0]=='cancel')
                labels = {'blocked':'Заблоковано. Modbus-запис не виконувався.', 'expired':'Підтвердження застаріло.',
                          'cancelled':'Скасовано.', 'invalidated':'Права, конфігурація або цілі змінено. Підготуйте нове підтвердження.',
                          'confirmed':'Записи команд підтверджено.',
                          'partial':'Частину записів підтверджено; перевірте результати кожного інвертора.',
                          'unconfirmed':'Записи не підтверджено. Автоматичного повторення немає.',
                          'unknown':'Результат невідомий. Не повторюйте команду без перевірки стану.'}
                text = labels[result]+'\n'+'\n'.join(f"#{row['id']} {clean(row['name'])}: {command_result_message(row)}" for row in details)
                markup = menu(role, store.controls_enabled)
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

    router.message.register(message_handler,Command('start','status','inverters','groups','history','help','notifications','control','limit'))
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
                await send(bot,item.from_user.id,'База даних тимчасово недоступна. Результат команди може бути невідомим; не повторюйте її без перевірки стану.')
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
