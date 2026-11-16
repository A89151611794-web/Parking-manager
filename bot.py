import os, csv, sqlite3, logging, calendar
from datetime import datetime, date
from zoneinfo import ZoneInfo
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TZ = ZoneInfo('Europe/Moscow')
DB_PATH = os.getenv('DB_PATH', 'parking.sqlite3')
ADMIN_ID = int(os.getenv('ADMIN_CHAT_ID', '0'))
TOKEN = os.environ['BOT_TOKEN']
IMPORT_FILE = os.getenv('IMPORT_FILE', 'tenants_import.csv')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

MENU = ReplyKeyboardMarkup([
    ['🚗 Места', '💳 Принять оплату'],
    ['⚠️ Должники', '📅 Сегодня оплатить'],
    ['📊 Отчёт за месяц', '➕ Добавить место'],
    ['❓ Помощь']
], resize_keyboard=True)

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute('''CREATE TABLE IF NOT EXISTS tenants (
        place TEXT PRIMARY KEY, tenant TEXT NOT NULL DEFAULT '', car TEXT NOT NULL DEFAULT '',
        monthly_fee INTEGER NOT NULL, due_day INTEGER, paid_through TEXT NOT NULL DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1)''')
    c.execute('''CREATE TABLE IF NOT EXISTS payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, place TEXT NOT NULL, amount INTEGER NOT NULL,
        months INTEGER NOT NULL, paid_at TEXT NOT NULL, period_from TEXT NOT NULL, period_to TEXT NOT NULL,
        note TEXT DEFAULT '')''')
    c.execute('''CREATE TABLE IF NOT EXISTS legacy (
        place TEXT NOT NULL, month TEXT NOT NULL, status TEXT NOT NULL,
        PRIMARY KEY(place, month))''')
    c.commit()
    return c

def add_month(ym: str, n: int) -> str:
    y, m = map(int, ym.split('-'))
    idx = y * 12 + (m - 1) + n
    return f'{idx // 12:04d}-{idx % 12 + 1:02d}'

def current_month(): return datetime.now(TZ).strftime('%Y-%m')
def money(n): return f'{n:,.0f}'.replace(',', ' ') + ' ₽'

def import_initial():
    c = db()
    count = c.execute('SELECT COUNT(*) n FROM tenants').fetchone()['n']
    if count or not os.path.exists(IMPORT_FILE):
        c.close(); return
    with open(IMPORT_FILE, newline='', encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            place = (r.get('place') or '').strip()
            if not place: continue
            fee = int(float(r.get('monthly_fee') or 0))
            day = int(r['due_day']) if (r.get('due_day') or '').isdigit() else None
            c.execute('INSERT OR IGNORE INTO tenants(place,tenant,car,monthly_fee,due_day,paid_through) VALUES(?,?,?,?,?,?)',
                      (place, r.get('tenant','').strip(), r.get('car','').strip(), fee, day, r.get('paid_through','').strip()))
            for item in (r.get('legacy_statuses') or '').split(';'):
                if ':' in item:
                    m, st = item.split(':', 1)
                    c.execute('INSERT OR IGNORE INTO legacy(place,month,status) VALUES(?,?,?)', (place,m,st))
    c.commit(); c.close()

def is_admin(update):
    return ADMIN_ID and update.effective_user and update.effective_user.id == ADMIN_ID

async def deny(update):
    await update.effective_message.reply_text('Доступ закрыт. Это личный бот владельца паркинга.')

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text(f'Ваш Telegram ID: {update.effective_user.id}\nПередайте этот ID администратору для настройки доступа.')
        return
    await update.message.reply_text('Parking Manager запущен. Выбери действие в меню.', reply_markup=MENU)

async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f'Ваш Telegram ID: {update.effective_user.id}')

async def help_cmd(update, context):
    if not is_admin(update): return await deny(update)
    await update.message.reply_text(
        'Команды:\n'
        '/places — все активные места\n'
        '/pay НОМЕР_МЕСТА МЕСЯЦЫ [СУММА] — принять оплату и продлить срок\n'
        'Пример: /pay 1211 3 90000\n'
        '/debts — долги и ближайшие даты\n'
        '/today — кому платить сегодня\n'
        '/report YYYY-MM — фактические поступления за месяц\n'
        '/add НОМЕР | ИМЯ | АВТО | СУММА | ДЕНЬ — добавить место\n'
        '/edit НОМЕР СУММА — изменить месячную стоимость\n'
        '/history НОМЕР — история платежей\n'
        '/id — показать ваш Telegram ID', reply_markup=MENU)

async def places(update, context):
    if not is_admin(update): return await deny(update)
    c=db(); rows=c.execute('SELECT * FROM tenants WHERE active=1 ORDER BY place').fetchall(); c.close()
    if not rows: return await update.message.reply_text('Мест пока нет.')
    lines=[f"{r['place']} — {r['tenant'] or 'без имени'} — {money(r['monthly_fee'])}/мес. — оплачено по: {r['paid_through'] or 'не указано'}" for r in rows]
    await update.message.reply_text('\n'.join(lines))

async def pay(update, context):
    if not is_admin(update): return await deny(update)
    if len(context.args) < 2:
        return await update.message.reply_text('Формат: /pay НОМЕР_МЕСТА МЕСЯЦЫ [СУММА]\nНапример: /pay 1211 3')
    place=context.args[0]
    try: months=int(context.args[1])
    except ValueError: return await update.message.reply_text('Количество месяцев должно быть числом.')
    if months < 1 or months > 36: return await update.message.reply_text('Укажи от 1 до 36 месяцев.')
    c=db(); r=c.execute('SELECT * FROM tenants WHERE place=? AND active=1',(place,)).fetchone()
    if not r: c.close(); return await update.message.reply_text('Такое активное место не найдено. Проверь номер через /places.')
    amount=int(context.args[2]) if len(context.args)>2 else int(r['monthly_fee'])*months
    if amount <= 0: c.close(); return await update.message.reply_text('Сумма должна быть положительной.')
    base=r['paid_through'] or add_month(current_month(),-1)
    period_from=add_month(base,1); period_to=add_month(base,months)
    paid_at=datetime.now(TZ).isoformat(timespec='seconds')
    c.execute('INSERT INTO payments(place,amount,months,paid_at,period_from,period_to) VALUES(?,?,?,?,?,?)',
              (place,amount,months,paid_at,period_from,period_to))
    c.execute('UPDATE tenants SET paid_through=? WHERE place=?',(period_to,place)); c.commit(); c.close()
    await update.message.reply_text(f'✅ Оплата записана\nМесто: {place}\nСумма: {money(amount)}\nПериод: {period_from} — {period_to}\nСледующая оплата: {add_month(period_to,1)} (день {r["due_day"] or "не задан"})', reply_markup=MENU)

async def debts(update, context):
    if not is_admin(update): return await deny(update)
    today=datetime.now(TZ).date(); month=today.strftime('%Y-%m'); c=db()
    rows=c.execute('SELECT * FROM tenants WHERE active=1 ORDER BY due_day,place').fetchall(); c.close()
    out=[]; total=0
    for r in rows:
        paid=r['paid_through'] or ''
        if paid < month:
            due_day=r['due_day'] or 1
            due=date(today.year,today.month,min(due_day,calendar.monthrange(today.year,today.month)[1]))
            status='просрочено' if today > due else 'ожидается'
            out.append(f"{r['place']} — {r['tenant'] or 'без имени'} — {money(r['monthly_fee'])} — {status} (дата {due.strftime('%d.%m')})")
            total += r['monthly_fee']
    await update.message.reply_text(('\n'.join(out)+f'\n\nИтого к оплате: {money(total)}') if out else 'Задолженностей за текущий месяц нет.')

async def today_due(update, context):
    if not is_admin(update): return await deny(update)
    today=datetime.now(TZ).date(); month=today.strftime('%Y-%m'); c=db()
    rows=c.execute('SELECT * FROM tenants WHERE active=1 AND due_day=? ORDER BY place',(today.day,)).fetchall(); c.close()
    rows=[r for r in rows if (r['paid_through'] or '') < month]
    if not rows: return await update.message.reply_text('Сегодня нет ожидаемых оплат. 👍')
    await update.message.reply_text('Сегодня ожидаются оплаты:\n'+'\n'.join(f"{r['place']} — {r['tenant']} — {money(r['monthly_fee'])}" for r in rows)+f'\nИтого: {money(sum(r["monthly_fee"] for r in rows))}')

async def report(update, context):
    if not is_admin(update): return await deny(update)
    month=context.args[0] if context.args else current_month()
    try: datetime.strptime(month,'%Y-%m')
    except ValueError: return await update.message.reply_text('Формат месяца: /report 2026-11')
    c=db(); rows=c.execute("SELECT place, SUM(amount) amount, COUNT(*) cnt FROM payments WHERE substr(paid_at,1,7)=? GROUP BY place ORDER BY place",(month,)).fetchall()
    total=sum(r['amount'] for r in rows); n=sum(r['cnt'] for r in rows); c.close()
    details='\n'.join(f"{r['place']}: {money(r['amount'])}" for r in rows) or 'Платежей, зарегистрированных ботом, нет.'
    await update.message.reply_text(f'📊 Фактические поступления за {month}\n\n{details}\n\nИТОГО: {money(total)}\nОпераций: {n}\n\nВажно: старые отметки «Оплачено» из таблицы импортированы как исходные статусы, но без даты и суммы фактического поступления. Они не включаются в этот отчёт.')

async def history(update, context):
    if not is_admin(update): return await deny(update)
    if not context.args: return await update.message.reply_text('Формат: /history НОМЕР')
    c=db(); rows=c.execute('SELECT * FROM payments WHERE place=? ORDER BY paid_at DESC LIMIT 30',(context.args[0],)).fetchall(); c.close()
    if not rows: return await update.message.reply_text('Записанных ботом платежей пока нет.')
    await update.message.reply_text('\n'.join(f"{r['paid_at'][:10]} — {money(r['amount'])}, {r['months']} мес. ({r['period_from']}—{r['period_to']})" for r in rows))

async def add_tenant(update, context):
    if not is_admin(update): return await deny(update)
    raw=' '.join(context.args)
    parts=[x.strip() for x in raw.split('|')]
    if len(parts)!=5: return await update.message.reply_text('Формат:\n/add НОМЕР | ИМЯ | АВТО | СУММА | ДЕНЬ\nПример:\n/add 3001 | Иван | BMW | 30000 | 15')
    place,name,car,fee,day=parts
    try: fee=int(fee); day=int(day)
    except ValueError: return await update.message.reply_text('Сумма и день должны быть числами.')
    if fee<=0 or not 1<=day<=31: return await update.message.reply_text('Проверь сумму и день оплаты (1–31).')
    c=db()
    try: c.execute('INSERT INTO tenants(place,tenant,car,monthly_fee,due_day,paid_through) VALUES(?,?,?,?,?,?)',(place,name,car,fee,day,add_month(current_month(),-1))); c.commit()
    except sqlite3.IntegrityError: c.close(); return await update.message.reply_text('Такой номер места уже существует.')
    c.close(); await update.message.reply_text(f'Добавлено место {place}: {name}, {money(fee)}/мес., оплата {day}-го числа.')

async def edit_fee(update, context):
    if not is_admin(update): return await deny(update)
    if len(context.args)!=2: return await update.message.reply_text('Формат: /edit НОМЕР СУММА')
    try: fee=int(context.args[1])
    except ValueError: return await update.message.reply_text('Сумма должна быть числом.')
    c=db(); cur=c.execute('UPDATE tenants SET monthly_fee=? WHERE place=? AND active=1',(fee,context.args[0])); c.commit(); c.close()
    await update.message.reply_text('Стоимость обновлена.' if cur.rowcount else 'Место не найдено.')

async def text_menu(update, context):
    if not is_admin(update): return
    t=update.message.text
    if t=='🚗 Места': return await places(update,context)
    if t=='⚠️ Должники': return await debts(update,context)
    if t=='📅 Сегодня оплатить': return await today_due(update,context)
    if t=='📊 Отчёт за месяц': return await report(update,context)
    if t=='❓ Помощь': return await help_cmd(update,context)
    if t=='💳 Принять оплату': return await update.message.reply_text('Введи команду, например: /pay 1211 3 — принять оплату за 3 месяца.')
    if t=='➕ Добавить место': return await update.message.reply_text('Введи /add НОМЕР | ИМЯ | АВТО | СУММА | ДЕНЬ')

async def daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID: return
    today=datetime.now(TZ).date(); month=today.strftime('%Y-%m'); c=db()
    rows=c.execute('SELECT * FROM tenants WHERE active=1 AND due_day=? ORDER BY place',(today.day,)).fetchall(); c.close()
    rows=[r for r in rows if (r['paid_through'] or '') < month]
    if rows:
        msg='🔔 Сегодня срок оплаты паркинга:\n'+'\n'.join(f"{r['place']} — {r['tenant'] or 'без имени'} — {money(r['monthly_fee'])}" for r in rows)+f'\n\nИтого ожидается: {money(sum(r["monthly_fee"] for r in rows))}'
        await context.bot.send_message(chat_id=ADMIN_ID,text=msg)

async def monthly_report_job(context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID: return
    month=add_month(current_month(),-1); c=db(); rows=c.execute("SELECT place, SUM(amount) amount FROM payments WHERE substr(paid_at,1,7)=? GROUP BY place ORDER BY place",(month,)).fetchall(); c.close()
    total=sum(r['amount'] for r in rows)
    await context.bot.send_message(chat_id=ADMIN_ID,text=f'📊 Отчёт за {month}\nФактически поступило: {money(total)}\nОпераций: {len(rows)}')

async def post_init(app: Application):
    app.job_queue.run_daily(daily_reminder, time=datetime.strptime('12:00','%H:%M').time().replace(tzinfo=TZ), name='daily_payment_reminder')
    app.job_queue.run_monthly(monthly_report_job, when=datetime.strptime('12:05','%H:%M').time().replace(tzinfo=TZ), day=1, name='monthly_report')

def main():
    import_initial()
    app=Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler('start',start)); app.add_handler(CommandHandler('id',whoami)); app.add_handler(CommandHandler('help',help_cmd))
    app.add_handler(CommandHandler('places',places)); app.add_handler(CommandHandler('pay',pay)); app.add_handler(CommandHandler('debts',debts)); app.add_handler(CommandHandler('today',today_due))
    app.add_handler(CommandHandler('report',report)); app.add_handler(CommandHandler('history',history)); app.add_handler(CommandHandler('add',add_tenant)); app.add_handler(CommandHandler('edit',edit_fee))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_menu))
    app.run_polling(drop_pending_updates=True)

if __name__=='__main__': main()
