import os, csv, sqlite3, logging, calendar
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters

TZ = ZoneInfo('Europe/Moscow')
DB_PATH = os.getenv('DB_PATH', 'parking.sqlite3')
ADMIN_ID = int(os.getenv('ADMIN_CHAT_ID', '0'))
TOKEN = os.environ['BOT_TOKEN']
IMPORT_FILE = os.getenv('IMPORT_FILE', 'tenants_import.csv')
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

MENU = ReplyKeyboardMarkup([
    ['🚗 Места', '💳 Принять оплату'],
    ['↩️ Отменить оплату', '🔄 Изменить номер места'],
    ['✏️ Изменить данные арендатора', '🗑 Архивировать место'],
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
    # Safe migration for databases created by earlier bot versions.
    payment_columns = {row['name'] for row in c.execute('PRAGMA table_info(payments)').fetchall()}
    if 'cancelled' not in payment_columns:
        c.execute('ALTER TABLE payments ADD COLUMN cancelled INTEGER NOT NULL DEFAULT 0')
    if 'cancelled_at' not in payment_columns:
        c.execute('ALTER TABLE payments ADD COLUMN cancelled_at TEXT DEFAULT NULL')
    if 'previous_paid_through' not in payment_columns:
        c.execute('ALTER TABLE payments ADD COLUMN previous_paid_through TEXT DEFAULT NULL')
    c.commit()
    return c

def add_month(ym: str, n: int) -> str:
    y, m = map(int, ym.split('-'))
    idx = y * 12 + (m - 1) + n
    return f'{idx // 12:04d}-{idx % 12 + 1:02d}'

def current_month(): return datetime.now(TZ).strftime('%Y-%m')

MONTHS_RU = ('январь', 'февраль', 'март', 'апрель', 'май', 'июнь',
             'июль', 'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь')

def month_label(ym: str) -> str:
    y, m = map(int, ym.split('-'))
    return f'{MONTHS_RU[m - 1].capitalize()} {y}'

def month_due_date(ym: str, due_day: int | None) -> date:
    y, m = map(int, ym.split('-'))
    day = min(due_day or 1, calendar.monthrange(y, m)[1])
    return date(y, m, day)

def next_unpaid_month(paid_through: str, today: date | None = None) -> str:
    # An early payment is assigned to the current billing month if it is not
    # already covered. The schedule remains anchored to the tenant's due_day.
    today = today or datetime.now(TZ).date()
    this_month = today.strftime('%Y-%m')
    after_paid = add_month(paid_through, 1) if paid_through else this_month
    return max(this_month, after_paid)

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
        '/move СТАРЫЙ_НОМЕР НОВЫЙ_НОМЕР — изменить номер места арендатора\n'
        '/archive НОМЕР — архивировать место без удаления истории\n'
        '/editdata НОМЕР — изменить данные арендатора через меню\n'        '/history НОМЕР — история платежей\n'
        'Кнопка «↩️ Отменить оплату» — отменить последний платёж по месту с подтверждением\n'
        '/id — показать ваш Telegram ID', reply_markup=MENU)

async def send_long_message(message, text, limit=3500):
    # Telegram limits a message to 4096 characters; leave room for safety.
    parts=[]
    current=''
    for block in text.split('\n\n'):
        candidate = block if not current else current + '\n\n' + block
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            parts.append(current)
            current=''
        # Also split a single unusually long block without cutting the message limit.
        while len(block) > limit:
            cut=block.rfind('\n', 0, limit)
            if cut < 1: cut=limit
            parts.append(block[:cut])
            block=block[cut:].lstrip('\n')
        current=block
    if current:
        parts.append(current)
    for part in parts:
        await message.reply_text(part)

async def places(update, context):
    if not is_admin(update): return await deny(update)
    c=db(); rows=c.execute('SELECT * FROM tenants WHERE active=1 ORDER BY place').fetchall(); c.close()
    if not rows: return await update.message.reply_text('Мест пока нет.')
    today=datetime.now(TZ).date()
    lines=[]
    for r in rows:
        next_month=next_unpaid_month(r['paid_through'], today)
        next_date=month_due_date(next_month, r['due_day'])
        lines.append(
            f"{r['place']} — {r['tenant'] or 'без имени'} — {money(r['monthly_fee'])}/мес.\n"
            f"Оплачено по: {month_label(r['paid_through']) if r['paid_through'] else 'не указано'}\n"
            f"Следующий месяц оплаты: {month_label(next_month)}\n"
            f"Точная дата следующей оплаты: {next_date.strftime('%d.%m.%Y')}"
        )
    await send_long_message(update.message, '\n\n'.join(lines))

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
    today=datetime.now(TZ).date()
    period_from=next_unpaid_month(r['paid_through'], today)
    period_to=add_month(period_from, months-1)
    due_day=r['due_day'] or 1
    period_start=month_due_date(period_from, due_day)
    next_due=month_due_date(add_month(period_to,1), due_day)
    period_end=next_due-timedelta(days=1)
    paid_at=datetime.now(TZ).isoformat(timespec='seconds')
    c.execute('INSERT INTO payments(place,amount,months,paid_at,period_from,period_to,previous_paid_through) VALUES(?,?,?,?,?,?,?)',
              (place,amount,months,paid_at,period_from,period_to,r['paid_through'] or ''))
    c.execute('UPDATE tenants SET paid_through=? WHERE place=?',(period_to,place)); c.commit(); c.close()
    await update.message.reply_text(
        f'✅ Оплата записана\nМесто: {place}\nСумма: {money(amount)}\n'
        f'Оплачено за: {period_start.strftime("%d.%m.%Y")} — {period_end.strftime("%d.%m.%Y")}\n'
        f'Следующий месяц оплаты: {month_label(add_month(period_to,1))}\n'
        f'Точная дата следующей оплаты: {next_due.strftime("%d.%m.%Y")}', reply_markup=MENU)

async def debts(update, context):
    if not is_admin(update): return await deny(update)
    today=datetime.now(TZ).date(); month=today.strftime('%Y-%m'); c=db()
    rows=c.execute('SELECT * FROM tenants WHERE active=1 ORDER BY due_day,place').fetchall(); c.close()
    out=[]; total=0
    for r in rows:
        due_month=next_unpaid_month(r['paid_through'], today)
        due=month_due_date(due_month, r['due_day'])
        if due_month == month:
            status='просрочено' if today > due else 'ожидается'
            out.append(f"{r['place']} — {r['tenant'] or 'без имени'} — {money(r['monthly_fee'])} — {status} (дата {due.strftime('%d.%m.%Y')})")
            total += r['monthly_fee']
    await update.message.reply_text(('\n'.join(out)+f'\n\nИтого к оплате: {money(total)}') if out else 'Задолженностей за текущий месяц нет.')

async def today_due(update, context):
    if not is_admin(update): return await deny(update)
    today=datetime.now(TZ).date(); month=today.strftime('%Y-%m'); c=db()
    rows=c.execute('SELECT * FROM tenants WHERE active=1 AND due_day=? ORDER BY place',(today.day,)).fetchall(); c.close()
    rows=[r for r in rows if (r['paid_through'] or '') < month]
    if not rows: return await update.message.reply_text('Сегодня нет ожидаемых оплат. 👍')
    await update.message.reply_text('Сегодня ожидаются оплаты:\n'+'\n'.join(f"{r['place']} — {r['tenant']} — {money(r['monthly_fee'])} — дата {today.strftime('%d.%m.%Y')}" for r in rows)+f'\nИтого: {money(sum(r["monthly_fee"] for r in rows))}')

async def report(update, context):
    if not is_admin(update): return await deny(update)
    month=context.args[0] if context.args else current_month()
    try: datetime.strptime(month,'%Y-%m')
    except ValueError: return await update.message.reply_text('Формат месяца: /report 2026-11')
    c=db(); rows=c.execute("SELECT place, SUM(amount) amount, COUNT(*) cnt FROM payments WHERE cancelled=0 AND substr(paid_at,1,7)=? GROUP BY place ORDER BY place",(month,)).fetchall()
    total=sum(r['amount'] for r in rows); n=sum(r['cnt'] for r in rows); c.close()
    details='\n'.join(f"{r['place']}: {money(r['amount'])}" for r in rows) or 'Платежей, зарегистрированных ботом, нет.'
    await update.message.reply_text(f'📊 Фактические поступления за {month}\n\n{details}\n\nИТОГО: {money(total)}\nОпераций: {n}\n\nВажно: старые отметки «Оплачено» из таблицы импортированы как исходные статусы, но без даты и суммы фактического поступления. Они не включаются в этот отчёт.')

async def history(update, context):
    if not is_admin(update): return await deny(update)
    if not context.args: return await update.message.reply_text('Формат: /history НОМЕР')
    c=db(); rows=c.execute('SELECT * FROM payments WHERE place=? ORDER BY paid_at DESC LIMIT 30',(context.args[0],)).fetchall(); c.close()
    if not rows: return await update.message.reply_text('Записанных ботом платежей пока нет.')
    await update.message.reply_text('\n'.join(f"{r['paid_at'][:10]} — {money(r['amount'])}, {r['months']} мес. ({r['period_from']}—{r['period_to']})" + (' — ОТМЕНЁН' if r['cancelled'] else '') for r in rows))

async def cancel_payment_menu(update, context):
    if not is_admin(update): return await deny(update)
    c=db()
    rows=c.execute("""SELECT p.id, p.place, p.amount, p.months, p.paid_at, p.period_from, p.period_to,
                            COALESCE(t.tenant, '') AS tenant
                     FROM payments p LEFT JOIN tenants t ON t.place=p.place
                     WHERE p.cancelled=0 ORDER BY p.id DESC LIMIT 10""").fetchall()
    c.close()
    if not rows:
        return await update.message.reply_text('Действующих платежей для отмены нет.', reply_markup=MENU)
    buttons=[]
    for r in rows:
        label=f"#{r['id']} · {r['place']} · {money(r['amount'])}"
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"cancelpay:{r['id']}")])
    await update.message.reply_text(
        'Выбери платёж, который нужно отменить. Показываю последние 10 действующих платежей:',
        reply_markup=InlineKeyboardMarkup(buttons))

async def cancel_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query
    if not query: return
    if not (ADMIN_ID and query.from_user and query.from_user.id == ADMIN_ID):
        await query.answer('Доступ закрыт.', show_alert=True)
        return
    data=query.data or ''
    try:
        action, raw_id=data.split(':',1)
        payment_id=int(raw_id)
    except (ValueError, TypeError):
        await query.answer('Некорректный запрос.', show_alert=True)
        return

    if action == 'cancelpay' and payment_id == 0:
        await query.answer('Отмена не выполнена.')
        await query.edit_message_text('Хорошо, платёж не отменён.')
        return

    if action == 'cancelpay':
        c=db()
        r=c.execute("""SELECT p.*, COALESCE(t.tenant,'') AS tenant
                       FROM payments p LEFT JOIN tenants t ON t.place=p.place
                       WHERE p.id=? AND p.cancelled=0""",(payment_id,)).fetchone()
        c.close()
        if not r:
            await query.answer('Платёж не найден или уже отменён.', show_alert=True)
            return
        text=(f"Подтверди отмену платежа №{r['id']}\n\n"
              f"Место: {r['place']}\nАрендатор: {r['tenant'] or 'не указан'}\n"
              f"Сумма: {money(r['amount'])}\nДата принятия: {r['paid_at'][:10]}\n"
              f"Оплаченный период: {r['period_from']} — {r['period_to']}\n\n"
              'Платёж останется в истории с пометкой «ОТМЕНЁН» и не будет учитываться в отчётах.')
        keyboard=InlineKeyboardMarkup([
            [InlineKeyboardButton('✅ Да, отменить платёж', callback_data=f'confirmcancel:{payment_id}')],
            [InlineKeyboardButton('Не отменять', callback_data='cancelpay:0')]
        ])
        await query.answer()
        await query.edit_message_text(text, reply_markup=keyboard)
        return

    if action == 'confirmcancel':
        c=db()
        try:
            c.execute('BEGIN IMMEDIATE')
            r=c.execute('SELECT * FROM payments WHERE id=? AND cancelled=0',(payment_id,)).fetchone()
            if not r:
                c.rollback()
                await query.answer('Платёж уже отменён или не найден.', show_alert=True)
                return
            latest=c.execute('SELECT id FROM payments WHERE place=? AND cancelled=0 ORDER BY id DESC LIMIT 1',(r['place'],)).fetchone()
            if not latest or latest['id'] != payment_id:
                c.rollback()
                await query.answer('Нельзя отменить: по этому месту уже есть более поздняя действующая оплата. Сначала отмени её.', show_alert=True)
                return
            previous_paid_through=(r['previous_paid_through'] if r['previous_paid_through'] is not None
                                   else add_month(r['period_from'],-1))
            c.execute('UPDATE payments SET cancelled=1, cancelled_at=? WHERE id=?',
                      (datetime.now(TZ).isoformat(timespec='seconds'), payment_id))
            c.execute('UPDATE tenants SET paid_through=? WHERE place=?',(previous_paid_through,r['place']))
            c.commit()
        except Exception:
            c.rollback()
            logging.exception('Failed to cancel payment %s', payment_id)
            await query.answer('Не удалось отменить платёж. Данные не изменены.', show_alert=True)
            return
        finally:
            c.close()
        await query.answer('Платёж отменён.')
        await query.edit_message_text(
            f"↩️ Платёж №{payment_id} отменён.\nМесто: {r['place']}\nСумма: {money(r['amount'])}\n"
            f"Оплачено снова по: {month_label(previous_paid_through) if previous_paid_through else 'не указано'}\n"
            'Срок оплаты и отчёты пересчитаны. Запись об отмене сохранена в истории.')
        return
    await query.answer('Неизвестное действие.', show_alert=True)

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

async def move_place(update, context):
    if not is_admin(update): return await deny(update)
    if len(context.args) != 2:
        return await update.message.reply_text('Формат: /move СТАРЫЙ_НОМЕР НОВЫЙ_НОМЕР\nНапример: /move 1001 1045')
    old, new = (x.strip() for x in context.args)
    if not old or not new or old == new:
        return await update.message.reply_text('Укажи два разных номера места.')
    c=db()
    tenant=c.execute('SELECT * FROM tenants WHERE place=? AND active=1',(old,)).fetchone()
    occupied=c.execute('SELECT 1 FROM tenants WHERE place=?',(new,)).fetchone()
    c.close()
    if not tenant:
        return await update.message.reply_text(f'Активное место {old} не найдено.')
    if occupied:
        return await update.message.reply_text(f'Номер {new} уже существует в базе. Выбери другой свободный номер.')
    keyboard=InlineKeyboardMarkup([
        [InlineKeyboardButton('✅ Да, изменить номер', callback_data=f'moveconfirm:{old}:{new}')],
        [InlineKeyboardButton('Отмена', callback_data='movecancel:0')]
    ])
    await update.message.reply_text(
        f'Подтверди изменение номера места\n\n'
        f'Арендатор: {tenant["tenant"] or "не указан"}\n'
        f'Автомобиль: {tenant["car"] or "не указан"}\n'
        f'Старый номер: {old}\nНовый номер: {new}\n\n'
        'История платежей и данные арендатора сохранятся и будут привязаны к новому номеру.',
        reply_markup=keyboard)

async def move_place_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query=update.callback_query
    if not query: return
    if not (ADMIN_ID and query.from_user and query.from_user.id == ADMIN_ID):
        await query.answer('Доступ закрыт.', show_alert=True)
        return
    data=query.data or ''
    if data == 'movecancel:0':
        await query.answer('Изменение отменено.')
        await query.edit_message_text('Хорошо, номер места не изменён.')
        return
    try:
        action, old, new = data.split(':', 2)
    except ValueError:
        await query.answer('Некорректный запрос.', show_alert=True)
        return
    if action != 'moveconfirm':
        await query.answer('Неизвестное действие.', show_alert=True)
        return
    c=db()
    try:
        c.execute('BEGIN IMMEDIATE')
        tenant=c.execute('SELECT place FROM tenants WHERE place=? AND active=1',(old,)).fetchone()
        occupied=c.execute('SELECT 1 FROM tenants WHERE place=?',(new,)).fetchone()
        if not tenant:
            c.rollback()
            await query.answer('Старое место не найдено.', show_alert=True)
            return
        if occupied:
            c.rollback()
            await query.answer('Новый номер уже занят. Ничего не изменено.', show_alert=True)
            return
        c.execute('UPDATE tenants SET place=? WHERE place=?',(new,old))
        c.execute('UPDATE payments SET place=? WHERE place=?',(new,old))
        # Keep imported legacy payment statuses with the tenant under the new place number.
        old_legacy=c.execute('SELECT month,status FROM legacy WHERE place=?',(old,)).fetchall()
        for item in old_legacy:
            c.execute('INSERT OR IGNORE INTO legacy(place,month,status) VALUES(?,?,?)',(new,item['month'],item['status']))
        c.execute('DELETE FROM legacy WHERE place=?',(old,))
        c.commit()
    except Exception:
        c.rollback()
        logging.exception('Failed to change place %s to %s',old,new)
        await query.answer('Не удалось изменить номер. Данные не изменены.', show_alert=True)
        return
    finally:
        c.close()
    await query.answer('Номер места изменён.')
    await query.edit_message_text(
        f'✅ Номер места успешно изменён: {old} → {new}.\n'
        'Данные арендатора, график оплаты и история платежей сохранены.')

async def archive_menu(update, context):
    if not is_admin(update): return await deny(update)
    c=db(); rows=c.execute("SELECT place,tenant,car FROM tenants WHERE active=1 ORDER BY place").fetchall(); c.close()
    if not rows: return await update.message.reply_text('Нет активных мест для архивации.')
    buttons=[]
    for r in rows:
        label=f"{r['place']} · {r['tenant'] or 'без имени'}"
        buttons.append([InlineKeyboardButton(label[:60], callback_data=f"archiveselect:{r['place']}")])
    await update.message.reply_text('Выбери место, которое нужно убрать из активного списка. История платежей сохранится:', reply_markup=InlineKeyboardMarkup(buttons))

async def archive_command(update, context):
    if not is_admin(update): return await deny(update)
    if len(context.args)!=1: return await archive_menu(update,context)
    place=context.args[0]
    c=db(); r=c.execute('SELECT * FROM tenants WHERE place=? AND active=1',(place,)).fetchone(); c.close()
    if not r: return await update.message.reply_text('Активное место не найдено.')
    kb=InlineKeyboardMarkup([[InlineKeyboardButton('✅ Да, архивировать',callback_data=f'archiveconfirm:{place}')],[InlineKeyboardButton('Отмена',callback_data='archivecancel:0')]])
    await update.message.reply_text(f"Подтверди архивацию места {place}\nАрендатор: {r['tenant'] or 'не указан'}\nАвтомобиль: {r['car'] or 'не указан'}\n\nМесто исчезнет из активного списка и напоминаний. Платежи и история сохранятся.",reply_markup=kb)

async def archive_callback(update, context):
    q=update.callback_query
    if not q: return
    if not (ADMIN_ID and q.from_user and q.from_user.id==ADMIN_ID):
        await q.answer('Доступ закрыт.',show_alert=True); return
    try: action,place=q.data.split(':',1)
    except ValueError:
        await q.answer('Некорректный запрос.',show_alert=True); return
    if action=='archiveselect':
        c=db(); r=c.execute('SELECT * FROM tenants WHERE place=? AND active=1',(place,)).fetchone(); c.close()
        if not r: await q.answer('Активное место не найдено.',show_alert=True); return
        kb=InlineKeyboardMarkup([[InlineKeyboardButton('✅ Да, архивировать',callback_data=f'archiveconfirm:{place}')],[InlineKeyboardButton('Отмена',callback_data='archivecancel:0')]])
        await q.answer(); await q.edit_message_text(f"Подтверди архивацию места {place}\nАрендатор: {r['tenant'] or 'не указан'}\nАвтомобиль: {r['car'] or 'не указан'}\n\nИстория платежей сохранится.",reply_markup=kb); return
    if action=='archivecancel':
        await q.answer('Отменено.'); await q.edit_message_text('Место не архивировано.'); return
    if action=='archiveconfirm':
        c=db(); cur=c.execute('UPDATE tenants SET active=0 WHERE place=? AND active=1',(place,)); c.commit(); c.close()
        if not cur.rowcount: await q.answer('Место уже архивировано или не найдено.',show_alert=True); return
        await q.answer('Место архивировано.'); await q.edit_message_text(f'🗑 Место {place} архивировано. История платежей сохранена.'); return
    await q.answer('Неизвестное действие.',show_alert=True)

async def editdata_menu(update, context):
    if not is_admin(update): return await deny(update)
    if len(context.args)==1:
        place=context.args[0]
        c=db(); r=c.execute('SELECT * FROM tenants WHERE place=? AND active=1',(place,)).fetchone(); c.close()
        if not r: return await update.message.reply_text('Активное место не найдено.')
        return await show_edit_fields(update.message,place,r)
    c=db(); rows=c.execute('SELECT place,tenant,car FROM tenants WHERE active=1 ORDER BY place').fetchall(); c.close()
    if not rows: return await update.message.reply_text('Нет активных мест.')
    buttons=[]
    for r in rows:
        label=f"{r['place']} · {r['tenant'] or 'без имени'}"
        buttons.append([InlineKeyboardButton(label[:60],callback_data=f'editselect:{r["place"]}')])
    await update.message.reply_text('Выбери арендатора, данные которого нужно изменить:',reply_markup=InlineKeyboardMarkup(buttons))

async def show_edit_fields(message,place,r):
    kb=InlineKeyboardMarkup([
        [InlineKeyboardButton('Имя арендатора',callback_data=f'editfield:{place}:tenant')],
        [InlineKeyboardButton('Автомобиль',callback_data=f'editfield:{place}:car')],
        [InlineKeyboardButton('Стоимость в месяц',callback_data=f'editfield:{place}:fee')],
        [InlineKeyboardButton('День оплаты',callback_data=f'editfield:{place}:day')],
    ])
    await message.reply_text(f"Место {place}\nИмя: {r['tenant'] or 'не указано'}\nАвтомобиль: {r['car'] or 'не указан'}\nСтоимость: {money(r['monthly_fee'])}/мес.\nДень оплаты: {r['due_day'] or 'не указан'}\n\nЧто меняем?",reply_markup=kb)

async def editdata_callback(update,context):
    q=update.callback_query
    if not q: return
    if not (ADMIN_ID and q.from_user and q.from_user.id==ADMIN_ID):
        await q.answer('Доступ закрыт.',show_alert=True); return
    parts=(q.data or '').split(':')
    if parts[0]=='editselect' and len(parts)==2:
        c=db(); r=c.execute('SELECT * FROM tenants WHERE place=? AND active=1',(parts[1],)).fetchone(); c.close()
        if not r: await q.answer('Место не найдено.',show_alert=True); return
        await q.answer(); await show_edit_fields(q.message,parts[1],r); return
    if parts[0]=='editfield' and len(parts)==3:
        place,field=parts[1],parts[2]
        prompts={'tenant':'Введи новое имя арендатора:','car':'Введи новое описание автомобиля:','fee':'Введи новую стоимость в рублях за месяц:','day':'Введи новое число месяца для оплаты (1–31):'}
        if field not in prompts: await q.answer('Неизвестное поле.',show_alert=True); return
        context.user_data['edit_tenant']={'place':place,'field':field}
        await q.answer(); await q.message.reply_text(f"Место {place}. {prompts[field]}\nДля имени или автомобиля можно отправить —, чтобы очистить поле."); return
    await q.answer('Некорректный запрос.',show_alert=True)

async def editdata_text(update,context):
    pending=context.user_data.get('edit_tenant')
    if not pending: return False
    if not is_admin(update): return True
    value=update.message.text.strip(); place=pending['place']; field=pending['field']
    if field in ('fee','day'):
        try: number=int(value)
        except ValueError:
            await update.message.reply_text('Нужно отправить целое число. Попробуй ещё раз.'); return True
        if field=='fee' and number<=0:
            await update.message.reply_text('Стоимость должна быть больше нуля. Попробуй ещё раз.'); return True
        if field=='day' and not 1<=number<=31:
            await update.message.reply_text('День оплаты должен быть от 1 до 31. Попробуй ещё раз.'); return True
        value=number
    elif value=='—': value=''
    c=db(); cur=c.execute(f'UPDATE tenants SET {"monthly_fee" if field=="fee" else "due_day" if field=="day" else field}=? WHERE place=? AND active=1',(value,place)); c.commit(); c.close()
    context.user_data.pop('edit_tenant',None)
    if not cur.rowcount: await update.message.reply_text('Место не найдено или уже архивировано.'); return True
    await update.message.reply_text(f'✅ Данные места {place} обновлены. Оплаченный срок и история платежей не изменены.',reply_markup=MENU)
    return True

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
    if t=='↩️ Отменить оплату': return await cancel_payment_menu(update,context)
    if t=='🔄 Изменить номер места': return await update.message.reply_text('Введи команду: /move СТАРЫЙ_НОМЕР НОВЫЙ_НОМЕР\nНапример: /move 1001 1045')
    if t=='✏️ Изменить данные арендатора': return await editdata_menu(update,context)
    if t=='🗑 Архивировать место': return await archive_menu(update,context)
    if t=='➕ Добавить место': return await update.message.reply_text('Введи /add НОМЕР | ИМЯ | АВТО | СУММА | ДЕНЬ')

async def daily_reminder(context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID: return
    today=datetime.now(TZ).date(); month=today.strftime('%Y-%m'); c=db()
    rows=c.execute('SELECT * FROM tenants WHERE active=1 AND due_day=? ORDER BY place',(today.day,)).fetchall(); c.close()
    rows=[r for r in rows if (r['paid_through'] or '') < month]
    if rows:
        msg='🔔 Сегодня срок оплаты паркинга ('+today.strftime('%d.%m.%Y')+'):\n'+'\n'.join(f"{r['place']} — {r['tenant'] or 'без имени'} — {money(r['monthly_fee'])}" for r in rows)+f'\n\nИтого ожидается: {money(sum(r["monthly_fee"] for r in rows))}'
        await context.bot.send_message(chat_id=ADMIN_ID,text=msg)

async def monthly_report_job(context: ContextTypes.DEFAULT_TYPE):
    if not ADMIN_ID: return
    month=add_month(current_month(),-1); c=db(); rows=c.execute("SELECT place, SUM(amount) amount FROM payments WHERE cancelled=0 AND substr(paid_at,1,7)=? GROUP BY place ORDER BY place",(month,)).fetchall(); c.close()
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
    app.add_handler(CommandHandler('report',report)); app.add_handler(CommandHandler('history',history)); app.add_handler(CommandHandler('add',add_tenant)); app.add_handler(CommandHandler('edit',edit_fee)); app.add_handler(CommandHandler('move',move_place)); app.add_handler(CommandHandler('archive',archive_command)); app.add_handler(CommandHandler('editdata',editdata_menu))
    app.add_handler(CallbackQueryHandler(cancel_payment_callback, pattern=r'^(cancelpay|confirmcancel):'))
    app.add_handler(CallbackQueryHandler(move_place_callback, pattern=r'^(moveconfirm|movecancel):'))
    app.add_handler(CallbackQueryHandler(archive_callback, pattern=r'^(archiveselect|archiveconfirm|archivecancel):'))
    app.add_handler(CallbackQueryHandler(editdata_callback, pattern=r'^(editselect|editfield):'))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, editdata_text), group=0)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_menu), group=0)
    app.run_polling(drop_pending_updates=True)

if __name__=='__main__': main()
