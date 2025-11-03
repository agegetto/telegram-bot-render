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

# Configurazione logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

logger = logging.getLogger(__name__)

# Token del bot
TOKEN = os.environ.get('BOT_TOKEN', '7460127087:AAFlfEpwUGGY-mgUO0bJPaigvf3y8SkKNvs')

# URL della Mini App
MINI_APP_URL = os.environ.get('MINI_APP_URL', 'https://YOUR-USERNAME.github.io/telegram-miniapp/index.html')

# Database URL da Render
DATABASE_URL = os.environ.get('DATABASE_URL')

# Timezone italiano
TZ = pytz.timezone('Europe/Rome')

# Flask app per API
flask_app = Flask(__name__)
CORS(flask_app)

@contextmanager
def get_db():
    """Context manager per la connessione al database PostgreSQL"""
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()

def init_database():
    """Inizializza il database PostgreSQL"""
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS work_sessions (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                date TEXT NOT NULL,
                minutes INTEGER NOT NULL,
                timestamp TIMESTAMP NOT NULL
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS km_records (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                date TEXT NOT NULL,
                km REAL NOT NULL,
                comune TEXT DEFAULT 'Imola',
                timestamp TIMESTAMP NOT NULL
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS absences (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                date TEXT NOT NULL,
                type TEXT NOT NULL,
                timestamp TIMESTAMP NOT NULL,
                UNIQUE(user_id, date, type)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_state (
                user_id BIGINT PRIMARY KEY,
                start_time TIMESTAMP,
                blocked_until TIMESTAMP
            )
        ''')
        
        conn.commit()
        logger.info("Database PostgreSQL inizializzato correttamente")

def get_current_time():
    """Restituisce l'ora corrente in timezone italiano"""
    return datetime.now(TZ)

def format_date(dt):
    """Formatta la data"""
    return dt.strftime("%d/%m/%Y")

def round_to_quarter(minutes):
    """Arrotonda i minuti al quarto d'ora più vicino"""
    return math.ceil(minutes / 15) * 15

def get_user_state(user_id):
    """Recupera lo stato dell'utente dal database"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT start_time, blocked_until FROM user_state WHERE user_id = %s', (user_id,))
        row = cursor.fetchone()
        
        if row:
            start_time = row['start_time']
            if start_time and start_time.tzinfo is None:
                start_time = TZ.localize(start_time)
            
            blocked_until = row['blocked_until']
            if blocked_until and blocked_until.tzinfo is None:
                blocked_until = TZ.localize(blocked_until)
            
            return {'start_time': start_time, 'blocked_until': blocked_until}
        return {'start_time': None, 'blocked_until': None}

def set_user_start_time(user_id, start_time):
    """Imposta il tempo di inizio per l'utente"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO user_state (user_id, start_time) 
            VALUES (%s, %s)
            ON CONFLICT(user_id) DO UPDATE SET start_time = EXCLUDED.start_time
        ''', (user_id, start_time))
        conn.commit()

def set_user_blocked_until(user_id, blocked_until):
    """Imposta il blocco dell'utente"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO user_state (user_id, blocked_until) 
            VALUES (%s, %s)
            ON CONFLICT(user_id) DO UPDATE SET blocked_until = EXCLUDED.blocked_until
        ''', (user_id, blocked_until))
        conn.commit()

def is_blocked(user_id):
    """Controlla se l'utente è bloccato"""
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
    """Crea la tastiera del menu principale"""
    keyboard = [
        [KeyboardButton("INIZIO")],
        [KeyboardButton("MALATTIA")],
        [KeyboardButton("FERIE")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_submenu1_keyboard():
    """Crea la tastiera del sottomenu1"""
    keyboard = [
        [KeyboardButton("FINE")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def get_submenu2_keyboard():
    """Crea la tastiera del sottomenu2"""
    keyboard = [
        [KeyboardButton("INIZIO"), KeyboardButton("GIORNATA")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def save_work_session(user_id, date_str, minutes):
    """Salva una sessione di lavoro"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO work_sessions (user_id, date, minutes, timestamp)
            VALUES (%s, %s, %s, %s)
        ''', (user_id, date_str, minutes, get_current_time()))
        conn.commit()

def save_km_record(user_id, date_str, km, comune="Imola"):
    """Salva un record di chilometri"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO km_records (user_id, date, km, comune, timestamp)
            VALUES (%s, %s, %s, %s, %s)
        ''', (user_id, date_str, km, comune, get_current_time()))
        conn.commit()

def save_absence(user_id, date_str, absence_type):
    """Salva malattia o ferie"""
    with get_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO absences (user_id, date, type, timestamp)
                VALUES (%s, %s, %s, %s)
            ''', (user_id, date_str, absence_type, get_current_time()))
            conn.commit()
        except psycopg2.IntegrityError:
            conn.rollback()

def get_daily_minutes(user_id, date_str):
    """Recupera il totale dei minuti lavorati in un giorno"""
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT SUM(minutes) as total FROM work_sessions
            WHERE user_id = %s AND date = %s
        ''', (user_id, date_str))
        row = cursor.fetchone()
        return row['total'] if row['total'] else 0

def get_weekly_minutes(user_id):
    """Recupera il totale dei minuti della settimana corrente"""
    now = get_current_time()
    days_to_monday = now.weekday()
    monday = now - timedelta(days=days_to_monday)
    
    monday_str = format_date(monday)
    today_str = format_date(now)
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT SUM(minutes) as total FROM work_sessions
            WHERE user_id = %s AND date BETWEEN %s AND %s
        ''', (user_id, monday_str, today_str))
        row = cursor.fetchone()
        return row['total'] if row['total'] else 0

def get_monthly_minutes(user_id):
    """Recupera il totale dei minuti del mese corrente"""
    now = get_current_time()
    date_pattern = f"%/{now.month:02d}/{now.year}"
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT SUM(minutes) as total FROM work_sessions
            WHERE user_id = %s AND date LIKE %s
        ''', (user_id, date_pattern))
        row = cursor.fetchone()
        return row['total'] if row['total'] else 0

def get_monthly_km(user_id):
    """Recupera il totale dei km del mese corrente"""
    now = get_current_time()
    date_pattern = f"%/{now.month:02d}/{now.year}"
    
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            SELECT SUM(km) as total FROM km_records
            WHERE user_id = %s AND date LIKE %s
        ''', (user_id, date_pattern))
        row = cursor.fetchone()
        return row['total'] if row['total'] else 0

# API Flask per la Mini App
@flask_app.route('/api/action', methods=['POST'])
def handle_action():
    """Gestisce le azioni dalla Mini App"""
    data = request.json
    user_id = data.get('user_id')
    action = data.get('action')
    action_data = data.get('data', {})
    
    now = get_current_time()
    date_str = format_date(now)
    
    response = {'success': True}
    
    if action == 'inizio':
        pass
    
    elif action == 'fine':
        minutes = action_data.get('minutes')
        save_work_session(user_id, date_str, minutes)
        response['message'] = f'Salvato: {minutes // 60}h {minutes % 60}m'
    
    elif action == 'giornata':
        daily_minutes = get_daily_minutes(user_id, date_str)
        response['hours'] = daily_minutes // 60
        response['minutes'] = daily_minutes % 60
    
    elif action == 'malattia':
        save_absence(user_id, date_str, 'MALATTIA')
    
    elif action == 'ferie':
        save_absence(user_id, date_str, 'FERIE')
    
    elif action == 'km':
        km = action_data.get('km')
        comune = action_data.get('comune', 'Imola')
        save_km_record(user_id, date_str, km, comune)
    
    elif action == 'get_stats':
        today_minutes = get_daily_minutes(user_id, date_str)
        week_minutes = get_weekly_minutes(user_id)
        month_minutes = get_monthly_minutes(user_id)
        month_km = get_monthly_km(user_id)
        
        # Controlla se bloccato
        blocked = is_blocked(user_id)
        
        response.update({
            'today_hours': today_minutes // 60,
            'today_minutes': today_minutes % 60,
            'week_hours': week_minutes // 60,
            'week_minutes': week_minutes % 60,
            'month_hours': month_minutes // 60,
            'month_minutes': month_minutes % 60,
            'month_km': month_km if month_km else 0,
            'blocked': blocked
        })
    
    return jsonify(response)

@flask_app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({'status': 'ok'})

# Comandi Telegram
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /start - mostra menu e Mini App"""
    user_id = update.effective_user.id
    logger.info(f"Comando /start da user {user_id}")
    
    await context.bot.set_chat_menu_button(
        chat_id=user_id,
        menu_button=MenuButtonWebApp(
            text="📱 Mini App",
            web_app=WebAppInfo(url=MINI_APP_URL)
        )
    )
    
    await update.message.reply_text(
        "✅ Bot inizializzato!\n\n"
        "Hai DUE modi per usare il bot:\n\n"
        "1️⃣ <b>Usa i bottoni qui sotto</b> (tastiera)\n"
        "2️⃣ <b>Apri la Mini App</b> (clicca ☰ menu in basso)\n\n"
        "Scegli quello che preferisci! 🚀",
        reply_markup=get_main_menu_keyboard(),
        parse_mode='HTML'
    )

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /menu - mostra di nuovo la tastiera"""
    await update.message.reply_text(
        "🎛️ Ecco il menu con i bottoni:",
        reply_markup=get_main_menu_keyboard()
    )

async def cals_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /cals - somma ore settimanali"""
    user_id = update.effective_user.id
    total_minutes = get_weekly_minutes(user_id)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    
    now = get_current_time()
    week_number = now.isocalendar()[1]
    
    await update.message.reply_text(
        f"📊 SETTIMANA {week_number} (fino ad oggi)\nTotale: {int(hours)}h {int(minutes)}m"
    )

async def calm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /calm - somma ore mensili"""
    user_id = update.effective_user.id
    total_minutes = get_monthly_minutes(user_id)
    hours = total_minutes // 60
    minutes = total_minutes % 60
    
    now = get_current_time()
    month_name = now.strftime("%B %Y")
    
    await update.message.reply_text(
        f"📊 {month_name.upper()} (fino ad oggi)\nTotale ore: {int(hours)}h {int(minutes)}m"
    )

async def kmm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /kmm - report km mensile dettagliato"""
    user_id = update.effective_user.id
    now = get_current_time()
    month_name = now.strftime("%B %Y")
    date_pattern = f"%/{now.month:02d}/{now.year}"
    
    with get_db() as conn:
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT SUM(km) as total FROM km_records
            WHERE user_id = %s AND date LIKE %s
        ''', (user_id, date_pattern))
        row = cursor.fetchone()
        total_km = row['total'] if row['total'] else 0
        
        cursor.execute('''
            SELECT SUM(km) as total FROM km_records
            WHERE user_id = %s AND date LIKE %s AND comune = 'Imola'
        ''', (user_id, date_pattern))
        row = cursor.fetchone()
        imola_km = row['total'] if row['total'] else 0
        
        cursor.execute('''
            SELECT SUM(km) as total FROM km_records
            WHERE user_id = %s AND date LIKE %s AND comune != 'Imola'
        ''', (user_id, date_pattern))
        row = cursor.fetchone()
        altri_km = row['total'] if row['total'] else 0
        
        cursor.execute('''
            SELECT date, km, comune FROM km_records
            WHERE user_id = %s AND date LIKE %s
            ORDER BY date ASC
        ''', (user_id, date_pattern))
        records = cursor.fetchall()
    
    message_lines = [
        f"🚗 REPORT KM {month_name.upper()}\n",
        f"📊 Totale: {total_km} km",
        f"📍 Imola: {imola_km} km",
        f"🌍 Altri comuni: {altri_km} km\n",
        "📅 DETTAGLIO:\n"
    ]
    
    if records:
        for record in records:
            message_lines.append(f"{record['date']}: {record['km']} km - {record['comune']}")
    else:
        message_lines.append("Nessun record trovato")
    
    message = "\n".join(message_lines)
    await update.message.reply_text(message)

async def km_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Comando /km <numero> [comune]"""
    if not context.args or len(context.args) < 1:
        await update.message.reply_text(
            "❌ Usa il comando così: /km <numero> [comune]\n"
            "Es: /km 45.5 Bologna\n"
            "Se non specifichi il comune, viene usato 'Imola'"
        )
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
        await update.message.reply_text("❌ Il numero inserito non è valido")

async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gestisce i messaggi dai bottoni della tastiera"""
    user_id = update.effective_user.id
    text = update.message.text
    logger.info(f"Messaggio ricevuto: {text} da user {user_id}")
    
    if is_blocked(user_id):
        await update.message.reply_text(
            "❌ Sei bloccato fino alle 23:59 di oggi.",
            reply_markup=ReplyKeyboardRemove()
        )
        return
    
    if text == "INIZIO":
        now = get_current_time()
        set_user_start_time(user_id, now)
        
        await update.message.reply_text(
            f"⏰ INIZIO: {format_date(now)} {now.strftime('%H:%M')}",
            reply_markup=get_submenu1_keyboard()
        )
    
    elif text == "MALATTIA":
        now = get_current_time()
        date_str = format_date(now)
        
        end_of_day = now.replace(hour=23, minute=59, second=59)
        set_user_blocked_until(user_id, end_of_day)
        save_absence(user_id, date_str, 'MALATTIA')
        
        await update.message.reply_text(
            f"🏥 {date_str} MALATTIA\n\n⚠️ Bloccato fino alle 23:59",
            reply_markup=ReplyKeyboardRemove()
        )
    
    elif text == "FERIE":
        now = get_current_time()
        date_str = format_date(now)
        
        end_of_day = now.replace(hour=23, minute=59, second=59)
        set_user_blocked_until(user_id, end_of_day)
        save_absence(user_id, date_str, 'FERIE')
        
        await update.message.reply_text(
            f"🏖️ {date_str} FERIE\n\n⚠️ Bloccato fino alle 23:59",
            reply_markup=ReplyKeyboardRemove()
        )
    
    elif text == "FINE":
        state = get_user_state(user_id)
        start_time = state['start_time']
        
        if not start_time:
            await update.message.reply_text(
                "❌ Devi prima premere INIZIO!",
                reply_markup=get_main_menu_keyboard()
            )
            return
        
        now = get_current_time()
        
        # Assicura che entrambi abbiano timezone
        if start_time.tzinfo is None:
            start_time = TZ.localize(start_time)
        
        elapsed = (now - start_time).total_seconds() / 60
        rounded_minutes = round_to_quarter(elapsed)
        
        date_str = format_date(now)
        save_work_session(user_id, date_str, rounded_minutes)
        
        set_user_start_time(user_id, None)
        
        hours = rounded_minutes // 60
        minutes = rounded_minutes % 60
        
        await update.message.reply_text(
            f"⏱️ FINE: Tempo trascorso {int(hours)}h {int(minutes)}m\n(Da {start_time.strftime('%H:%M')} a {now.strftime('%H:%M')})",
            reply_markup=get_submenu2_keyboard()
        )
    
    elif text == "GIORNATA":
        now = get_current_time()
        date_str = format_date(now)
        
        daily_minutes = get_daily_minutes(user_id, date_str)
        hours = daily_minutes // 60
        minutes = daily_minutes % 60
        
        end_of_day = now.replace(hour=23, minute=59, second=59)
        set_user_blocked_until(user_id, end_of_day)
        
        await update.message.reply_text(
            f"📅 {date_str}\nTotale giornata: {int(hours)}h {int(minutes)}m\n\n⚠️ Bloccato fino alle 23:59",
            reply_markup=ReplyKeyboardRemove()
        )

def main():
    """Avvia tutti i servizi - SENZA THREADING"""
    logger.info("🚀 Inizializzazione servizi...")
    
    try:
        init_database()
        
        # Crea applicazione bot
        application = Application.builder().token(TOKEN).build()
        
        # Aggiungi handler
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("menu", menu_command))
        application.add_handler(CommandHandler("km", km_command))
        application.add_handler(CommandHandler("cals", cals_command))
        application.add_handler(CommandHandler("calm", calm_command))
        application.add_handler(CommandHandler("kmm", kmm_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
        
        port = int(os.environ.get('PORT', 10000))
        logger.info(f"✅ Bot configurato - API Flask sulla porta {port}")
        logger.info(f"📱 Mini App URL: {MINI_APP_URL}")
        
        # Avvia Flask in un processo separato usando waitress
        import multiprocessing
        flask_process = multiprocessing.Process(target=lambda: __import__('waitress').serve(flask_app, host='0.0.0.0', port=port))
        flask_process.start()
        
        logger.info("✅ Bot Telegram in avvio...")
        
        # Avvia bot con polling
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True
        )
        
    except Exception as e:
        logger.error(f"❌ ERRORE CRITICO: {e}", exc_info=True)
        raise

if __name__ == '__main__':
    main()
