import logging
from datetime import datetime, timedelta
from telegram import Update, MenuButtonWebApp, WebAppInfo, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import pytz
import math
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager
from flask import Flask, request, jsonify
from flask_cors import CORS

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.environ.get('BOT_TOKEN', '7460127087:AAFlfEpwUGGY-mgUO0bJPaigvf3y8SkKNvs')
MINI_APP_URL = os.environ.get('MINI_APP_URL', 'https://YOUR-USERNAME.github.io/telegram-miniapp/index.html')
DATABASE_URL = os.environ.get('DATABASE_URL')
TZ = pytz.timezone('Europe/Rome')

flask_app = Flask(__name__)
CORS(flask_app)

@contextmanager
def get_db():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()

def init_database():
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('CREATE TABLE IF NOT EXISTS work_sessions (id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, date TEXT NOT NULL, minutes INTEGER NOT NULL, timestamp TIMESTAMP NOT NULL)')
        cursor.execute('CREATE TABLE IF NOT EXISTS km_records (id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, date TEXT NOT NULL, km REAL NOT NULL, comune TEXT DEFAULT \'Imola\', timestamp TIMESTAMP NOT NULL)')
        cursor.execute('CREATE TABLE IF NOT EXISTS absences (id SERIAL PRIMARY KEY, user_id BIGINT NOT NULL, date TEXT NOT NULL, type TEXT NOT NULL, timestamp TIMESTAMP NOT NULL, UNIQUE(user_id, date, type))')
        cursor.execute('CREATE TABLE IF NOT EXISTS user_state (user_id BIGINT PRIMARY KEY, start_time TIMESTAMP, blocked_until TIMESTAMP)')
        conn.commit()
        logger.info("Database inizializzato")

def get_current_time():
    return datetime.now(TZ)

def format_date(dt):
    return dt.strftime("%d/%m/%Y")

def round_to_quarter(minutes):
    return math.ceil(minutes / 15) * 15

def get_user_state(user_id):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT start_time, blocked_until FROM user_state WHERE user_id = %s', (user_id,))
        row = cursor.fetchone()
        if row:
            start_time = row['start_time']
            if start_time:
                # Sempre considera come timezone italiano
                if start_time.tzinfo is None:
                    start_time = TZ.localize(start_time)
                else:
                    start_time = start_time.astimezone(TZ)
            blocked_until = row['blocked_until']
            if blocked_until:
                if blocked_until.tzinfo is None:
                    blocked_until = TZ.localize(blocked_until)
                else:
                    blocked_until = blocked_until.astimezone(TZ)
            return {'start_time': start_time, 'blocked_until': blocked_until}
        return {'start_time': None, 'blocked_until': None}

def set_user_start_time(user_id, start_time):
    with get_db() as conn:
        cursor = conn.cursor()
        # Rimuovi timezone prima di salvare per evitare conflitti
        if start_time and start_time.tzinfo:
            start_time = start_time.replace(tzinfo=None)
        cursor.execute('INSERT INTO user_state (user_id, start_time) VALUES (%s, %s) ON CONFLICT(user_id) DO UPDATE SET start_time = EXCLUDED.start_time', (user_id, start_time))
        conn.commit()

def set_user_blocked_until(user_id, blocked_until):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO user_state (user_id, blocked_until) VALUES (%s, %s) ON CONFLICT(user_id) DO UPDATE SET blocked_until = EXCLUDED.blocked_until', (user_id, blocked_until))
        conn.commit()

def is_blocked(user_id):
    state = get_user_state(user_id)
    blocked_until = state['blocked_until']
    if blocked_until:
        now = get_current_time()
        if now < blocked_until:
            return True
        else:
            set_user_blocked_until(user_id, None)
    return False

def get_main_menu_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("INIZIO")], [KeyboardButton("MALATTIA")], [KeyboardButton("FERIE")]], resize_keyboard=True)

def get_submenu1_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("FINE")]], resize_keyboard=True)

def get_submenu2_keyboard():
    return ReplyKeyboardMarkup([[KeyboardButton("INIZIO"), KeyboardButton("GIORNATA")]], resize_keyboard=True)

def save_work_session(user_id, date_str, minutes):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO work_sessions (user_id, date, minutes, timestamp) VALUES (%s, %s, %s, %s)', (user_id, date_str, minutes, get_current_time()))
        conn.commit()

def save_km_record(user_id, date_str, km, comune="Imola"):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('INSERT INTO km_records (user_id, date, km, comune, timestamp) VALUES (%s, %s, %s, %s, %s)', (user_id, date_str, km, comune, get_current_time()))
        conn.commit()

def save_absence(user_id, date_str, absence_type):
    with get_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute('INSERT INTO absences (user_id, date, type, timestamp) VALUES (%s, %s, %s, %s)', (user_id, date_str, absence_type, get_current_time()))
            conn.commit()
        except psycopg2.IntegrityError:
            conn.rollback()

def get_daily_minutes(user_id, date_str):
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT SUM(minutes) as total FROM work_sessions WHERE user_id = %s AND date = %s', (user_id, date_str))
        row = cursor.fetchone()
        return row['total'] if row['total'] else 0

def get_weekly_minutes(user_id):
    now = get_current_time()
    days_to_monday = now.weekday()
    monday = now - timedelta(days=days_to_monday)
    monday_str = format_date(monday)
    today_str = format_date(now)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT SUM(minutes) as total FROM work_sessions WHERE user_id = %s AND date BETWEEN %s AND %s', (user_id, monday_str, today_str))
        row = cursor.fetchone()
        return row['total'] if row['total'] else 0

def get_monthly_minutes(user_id):
    now = get_current_time()
    date_pattern = f"%/{now.month:02d}/{now.year}"
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT SUM(minutes) as total FROM work_sessions WHERE user_id = %s AND date LIKE %s', (user_id, date_pattern))
        row = cursor.fetchone()
        return row['total'] if row['total'] else 0

def get_monthly_km(user_id):
    now = get_current_time()
    date_pattern = f"%/{now.month:02d}/{now.year}"
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT SUM(km) as total FROM km_records WHERE user_id = %s AND date LIKE %s', (user_id, date_pattern))
        row = cursor.fetchone()
        return row['total'] if row['total'] else 0

@flask_app.route('/api/action', methods=['POST'])
def handle_action():
    data = request.json
    user_id = data.get('user_id')
    action = data.get('action')
    action_data = data.get('data', {})
    now = get_current_time()
    date_str = format_date(now)
    response = {'success': True}
    
    if action == 'inizio':
        set_user_start_time(user_id, now)
        response['message'] = 'Inizio registrato'
    elif action == 'fine':
        minutes = action_data.get('minutes')
        save_work_session(user_id, date_str, minutes)
        set_user_start_time(user_id, None)
        response['message'] = f'Salvato: {minutes // 60}h {minutes % 60}m'
    elif action == 'giornata':
        daily_minutes = get_daily_minutes(user_id, date_str)
        end_of_day = now.replace(hour=23, minute=59, second=59)
        set_user_blocked_until(user_id, end_of_day)
        response['hours'] = daily_minutes // 60
        response['minutes'] = daily_minutes % 60
    elif action == 'malattia':
        end_of_day = now.replace(hour=23, minute=59, second=59)
        set_user_blocked_until(user_id, end_of_day)
        save_absence(user_id, date_str, 'MALATTIA')
    elif action == 'ferie':
        end_of_day = now.replace(hour=23, minute=59, second=59)
        set_user_blocked_until(user_id, end_of_day)
        save_absence(user_id, date_str, 'FERIE')
    elif action == 'km':
        km = action_data.get('km')
        comune = action_data.get('comune', 'Imola')
        save_km_record(user_id, date_str, km, comune)
    elif action == 'get_stats':
        state = get_user_state(user_id)
        today_minutes = get_daily_minutes(user_id, date_str)
        week_minutes = get_weekly_minutes(user_id)
        month_minutes = get_monthly_minutes(user_id)
        month_km = get_monthly_km(user_id)
        blocked = is_blocked(user_id)
        has_start = state['start_time'] is not None
        response.update({
            'today_hours': today_minutes // 60, 'today_minutes': today_minutes % 60,
            'week_hours': week_minutes // 60, 'week_minutes': week_minutes % 60,
            'month_hours': month_minutes // 60, 'month_minutes': month_minutes % 60,
            'month_km': month_km if month_km else 0, 'blocked': blocked, 'has_start': has_start
        })
    return jsonify(response)

@flask_app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await context.bot.set_chat_menu_button(chat_id=user_id, menu_button=MenuButtonWebApp(text="📱 Mini App", web_app=WebAppInfo(url=MINI_APP_URL)))
    await update.message.reply_text("✅ Bot inizializzato!\n\n1️⃣ Usa i bottoni\n2️⃣ Apri Mini App (☰)\n\nScegli! 🚀", reply_markup=get_main_menu_keyboard(), parse_mode='HTML')

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎛️ Menu:", reply_markup=get_main_menu_keyboard())

async def cals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    total_minutes = get_weekly_minutes(user_id)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    now = get_current_time()
    week_number = now.isocalendar()[1]
    await update.message.reply_text(f"📊 SETTIMANA {week_number}\nTotale: {int(hours)}h {int(minutes)}m")

async def calm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    total_minutes = get_monthly_minutes(user_id)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    now = get_current_time()
    month_name = now.strftime("%B %Y")
    await update.message.reply_text(f"📊 {month_name.upper()}\nTotale: {int(hours)}h {int(minutes)}m")

async def kmm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = get_current_time()
    month_name = now.strftime("%B %Y")
    date_pattern = f"%/{now.month:02d}/{now.year}"
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT SUM(km) as total FROM km_records WHERE user_id = %s AND date LIKE %s', (user_id, date_pattern))
        total_km = cursor.fetchone()['total'] or 0
        cursor.execute('SELECT SUM(km) as total FROM km_records WHERE user_id = %s AND date LIKE %s AND comune = \'Imola\'', (user_id, date_pattern))
        imola_km = cursor.fetchone()['total'] or 0
        cursor.execute('SELECT SUM(km) as total FROM km_records WHERE user_id = %s AND date LIKE %s AND comune != \'Imola\'', (user_id, date_pattern))
        altri_km = cursor.fetchone()['total'] or 0
        cursor.execute('SELECT date, km, comune FROM km_records WHERE user_id = %s AND date LIKE %s ORDER BY date ASC', (user_id, date_pattern))
        records = cursor.fetchall()
    message_lines = [f"🚗 REPORT KM {month_name.upper()}\n", f"📊 Totale: {total_km} km", f"📍 Imola: {imola_km} km", f"🌍 Altri: {altri_km} km\n", "📅 DETTAGLIO:\n"]
    if records:
        for r in records:
            message_lines.append(f"{r['date']}: {r['km']} km - {r['comune']}")
    else:
        message_lines.append("Nessun record")
    await update.message.reply_text("\n".join(message_lines))

async def km_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or len(context.args) < 1:
        await update.message.reply_text("❌ Usa: /km <numero> [comune]\nEs: /km 45.5 Bologna")
        return
    try:
        km_value = float(context.args[0])
        comune = " ".join(context.args[1:]) if len(context.args) > 1 else "Imola"
        user_id = update.effective_user.id
        now = get_current_time()
        date_str = format_date(now)
        save_km_record(user_id, date_str, km_value, comune)
        await update.message.reply_text(f"🚗 {date_str} {km_value} KM - {comune}")
    except ValueError:
        await update.message.reply_text("❌ Numero non valido")

async def reset_oggi_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = get_current_time()
    date_str = format_date(now)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM work_sessions WHERE user_id = %s AND date = %s', (user_id, date_str))
        ws = cursor.rowcount
        cursor.execute('DELETE FROM km_records WHERE user_id = %s AND date = %s', (user_id, date_str))
        km = cursor.rowcount
        cursor.execute('DELETE FROM absences WHERE user_id = %s AND date = %s', (user_id, date_str))
        ab = cursor.rowcount
        cursor.execute('DELETE FROM user_state WHERE user_id = %s', (user_id,))
        conn.commit()
    await update.message.reply_text(f"🗑️ Cancellati:\n• {ws} sessioni\n• {km} km\n• {ab} assenze", reply_markup=get_main_menu_keyboard())

async def reset_tutto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args or context.args[0] != 'CONFERMA':
        await update.message.reply_text("⚠️ ATTENZIONE: Cancellerà TUTTO!\n\nPer confermare:\n/resettutto CONFERMA")
        return
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('DELETE FROM work_sessions WHERE user_id = %s', (user_id,))
        ws = cursor.rowcount
        cursor.execute('DELETE FROM km_records WHERE user_id = %s', (user_id,))
        km = cursor.rowcount
        cursor.execute('DELETE FROM absences WHERE user_id = %s', (user_id,))
        ab = cursor.rowcount
        cursor.execute('DELETE FROM user_state WHERE user_id = %s', (user_id,))
        conn.commit()
    await update.message.reply_text(f"🗑️ TUTTO cancellato:\n• {ws} sessioni\n• {km} km\n• {ab} assenze\n\n✅ Database pulito!", reply_markup=get_main_menu_keyboard())

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    if is_blocked(user_id):
        await update.message.reply_text("❌ Bloccato fino alle 23:59", reply_markup=ReplyKeyboardRemove())
        return
    
    if text == "INIZIO":
        now = get_current_time()
        set_user_start_time(user_id, now)
        await update.message.reply_text(f"⏰ INIZIO: {now.strftime('%H:%M')}", reply_markup=get_submenu1_keyboard())
    elif text == "MALATTIA":
        now = get_current_time()
        date_str = format_date(now)
        end_of_day = now.replace(hour=23, minute=59, second=59)
        set_user_blocked_until(user_id, end_of_day)
        save_absence(user_id, date_str, 'MALATTIA')
        await update.message.reply_text(f"🏥 {date_str} MALATTIA\n⚠️ Bloccato fino alle 23:59", reply_markup=ReplyKeyboardRemove())
    elif text == "FERIE":
        now = get_current_time()
        date_str = format_date(now)
        end_of_day = now.replace(hour=23, minute=59, second=59)
        set_user_blocked_until(user_id, end_of_day)
        save_absence(user_id, date_str, 'FERIE')
        await update.message.reply_text(f"🏖️ {date_str} FERIE\n⚠️ Bloccato fino alle 23:59", reply_markup=ReplyKeyboardRemove())
    elif text == "FINE":
        state = get_user_state(user_id)
        start_time = state['start_time']
        if not start_time:
            await update.message.reply_text("❌ Prima premi INIZIO!", reply_markup=get_main_menu_keyboard())
            return
        now = get_current_time()
        if start_time.tzinfo is None:
            start_time = TZ.localize(start_time)
        elapsed = (now - start_time).total_seconds() / 60
        rounded_minutes = round_to_quarter(elapsed)
        date_str = format_date(now)
        save_work_session(user_id, date_str, rounded_minutes)
        set_user_start_time(user_id, None)
        hours = rounded_minutes // 60
        minutes = rounded_minutes % 60
        await update.message.reply_text(f"⏱️ FINE: {int(hours)}h {int(minutes)}m\n(Da {start_time.strftime('%H:%M')} a {now.strftime('%H:%M')})", reply_markup=get_submenu2_keyboard())
    elif text == "GIORNATA":
        now = get_current_time()
        date_str = format_date(now)
        daily_minutes = get_daily_minutes(user_id, date_str)
        hours = daily_minutes // 60
        minutes = daily_minutes % 60
        end_of_day = now.replace(hour=23, minute=59, second=59)
        set_user_blocked_until(user_id, end_of_day)
        await update.message.reply_text(f"📅 {date_str}\nTotale: {int(hours)}h {int(minutes)}m\n⚠️ Bloccato fino alle 23:59", reply_markup=ReplyKeyboardRemove())

def main():
    logger.info("🚀 Avvio...")
    try:
        init_database()
        application = Application.builder().token(TOKEN).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("menu", menu_command))
        application.add_handler(CommandHandler("km", km_command))
        application.add_handler(CommandHandler("cals", cals_command))
        application.add_handler(CommandHandler("calm", calm_command))
        application.add_handler(CommandHandler("kmm", kmm_command))
        application.add_handler(CommandHandler("resetoggi", reset_oggi_command))
        application.add_handler(CommandHandler("resettutto", reset_tutto_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
        port = int(os.environ.get('PORT', 10000))
        logger.info(f"✅ Configurato - Porta {port}")
        import multiprocessing
        flask_process = multiprocessing.Process(target=lambda: __import__('waitress').serve(flask_app, host='0.0.0.0', port=port))
        flask_process.start()
        logger.info("✅ Bot avviato")
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    except Exception as e:
        logger.error(f"❌ ERRORE: {e}", exc_info=True)
        raise

if __name__ == '__main__':
    main()
