"""Telegram presentation for the shared budget service; no device I/O."""
from aiogram.types import ForceReply
from sqlalchemy import select, update

from models import CommandAudit, TelegramCommand
import power_budget as budget


class PowerTelegram:
    def __init__(self,store):
        self.store=store
        self.service=budget.BudgetService(store.engine,clock=store.clock)

    def identity(self,telegram_id):
        with self.store.sessions() as session:
            _,user=self.store.user(session,telegram_id)
            return user.id,user.role

    def screen(self,telegram_id):
        from telegram_ui import keyboard
        _,role=self.identity(telegram_id);state=budget.status()
        text='Загальний ліміт, кВт: '+('увімкнено' if state['enabled'] else 'вимкнено')
        if state.get('target_w') is not None:text+='\nЗадано: '+budget.kw(state['target_w'])+' кВт'
        if state.get('actual_w') is not None:text+='\nПоточна генерація: '+budget.kw(state['actual_w'])+f" кВт ({state['known']}/{state['total']})"
        if state.get('missing_ids'):
            text+=f"\nБез зв'язку/свіжих даних: {len(state['missing_ids'])}. Оцінка резерву: {budget.kw(state.get('offline_reserved_w',0))} кВт."
            text+='\nСумарна генерація підтверджена лише частково.'
        text+='\n'+state['reason']+'\nРозрахунок кожні 30 с. Діє до ручного вимкнення.'
        rows=[]
        if role=='Administrator':
            rows.append([('Ввести загальний ліміт','budget:input')])
            if state['enabled']:rows.append([('Вимкнути автоматизацію','budget:disable')])
        rows.append([('Меню','menu:start')])
        return text,keyboard(rows)

    def preview(self,intent,payload):
        from telegram_ui import clean,keyboard
        if payload['enabled']:
            text=(f"Підтвердьте загальний ліміт {budget.kw(payload['target_w'])} кВт.\n"
                  'Регулювання за фактичною генерацією кожні 30 с, до ручного вимкнення.\n'
                  'При втраті зв’язку залишок розподіляється між доступними з резервом для недоступних.\n'
                  'Після активації контролера виконуються реальні записи ліміту.\n')
        else:text='Підтвердьте вимкнення автоматизації. Останні ліміти залишаться; 100% автоматично не встановлюється.\n'
        text+='\n'.join(f"#{row['id']} {clean(row['name'])} · {row['ip']}:{row['port']} · Unit ID {row['device_id']}" for row in payload['targets'])
        text+='\nОдноразове підтвердження, 120 секунд. Налаштування саме по собі не підтверджує фактичну генерацію.'
        return text,keyboard([[('Підтвердити','budget:confirm:'+intent),('Скасувати','budget:cancel:'+intent)]])

    def prepare(self,telegram_id,value=None):
        uid,_=self.identity(telegram_id)
        intent,payload=self.service.preview(uid,value,telegram_id=telegram_id)
        return self.preview(intent,payload)

    async def input(self,telegram_id,bot):
        from telegram_ui import send
        with self.store.sessions.begin() as session:
            uid,_=self.identity(telegram_id)
            user=self.service.actor(session,uid,telegram_id)
            intent,_=self.store._new_intent(session,user,telegram_id,
                dict(action='budget_enable',targets=[],kind='budget_input',
                     version=user.session_version,prompt_id=None),None,input_request=True)
            session.flush();session.get(TelegramCommand,intent).state='budget_input'
        prompt=await send(bot,telegram_id,
            'Введіть загальний ліміт у кВт, наприклад 400 або 400,5.\n'
            'Номінали вводити не потрібно. Введення діє 5 хв; скасувати /cancel.',
            ForceReply(selective=True,input_field_placeholder='Загальний ліміт, кВт'))
        with self.store.sessions.begin() as session:
            self.service.actor(session,uid,telegram_id)
            row=session.get(TelegramCommand,intent)
            if row.state!='budget_input':raise ValueError('Запит введення вже скасовано.')
            row.payload=dict(row.payload,prompt_id=prompt.message_id)

    def has_input(self,telegram_id):
        with self.store.sessions() as session:
            return session.scalar(select(TelegramCommand.id).where(TelegramCommand.telegram_id==telegram_id,
                TelegramCommand.state=='budget_input').limit(1)) is not None

    def submit(self,telegram_id,text,reply_to=None):
        uid,_=self.identity(telegram_id)
        value=budget.watts(text)
        with self.store.sessions.begin() as session:
            user=self.service.actor(session,uid,telegram_id)
            row=session.scalar(select(TelegramCommand).where(TelegramCommand.telegram_id==telegram_id,
                TelegramCommand.user_id==uid,TelegramCommand.state=='budget_input').order_by(TelegramCommand.expires_at.desc()).limit(1))
            if (not row or row.expires_at<=self.store.clock() or row.payload['version']!=user.session_version or
                row.payload['prompt_id'] is None):raise ValueError('Запит введення застарів. Виберіть загальний ліміт заново.')
            if reply_to is not None and reply_to!=row.payload['prompt_id']:raise ValueError('Відповідь на інший запит введення.')
            row.state='entered'
            session.execute(update(CommandAudit).where(CommandAudit.request_id=='tg:'+row.id).values(result='entered'))
        return self.prepare(telegram_id,value)

    def confirm(self,telegram_id,intent,cancel=False):
        uid,_=self.identity(telegram_id)
        result=self.service.confirm(uid,intent,telegram_id=telegram_id,cancel=cancel)
        labels={'configured':'Налаштування збережено. Фактичне виконання перевіряється контролером.',
                'cancelled':'Скасовано.','invalidated':'Підтвердження застаріло або дані змінилися. Створіть нове.'}
        text,markup=self.screen(telegram_id)
        return labels[result]+'\n'+text,markup
