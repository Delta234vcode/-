# main_bot.py
import os
import asyncio
import json
import logging
import time
import sys
from typing import Optional, Tuple

# Налаштування кодування для Windows
if sys.platform == "win32":
    import codecs
    sys.stdout = codecs.getwriter("utf-8")(sys.stdout.detach())
    sys.stderr = codecs.getwriter("utf-8")(sys.stderr.detach())

import MetaTrader5 as mt5
import pandas as pd
import pandas_ta as ta
from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import Bot
import threading
import datetime
from telegram.ext import Application, CommandHandler, ContextTypes
import requests

TRADE_LOG_FILE = "trade_results.json"
trade_log_lock = threading.Lock()

def log_trade_result(trade_data):
    """Зберігає результат угоди у JSON-файл."""
    with trade_log_lock:
        try:
            if os.path.exists(TRADE_LOG_FILE):
                with open(TRADE_LOG_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = []
            data.append(trade_data)
            with open(TRADE_LOG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"Помилка збереження trade log: {e}")

# --- 1. КОНФІГУРАЦІЯ ТА ІНІЦІАЛІЗАЦІЯ ---

# Завантажуємо змінні середовища з файлу .env
# Створіть файл .env у тій же папці, що і цей скрипт
load_dotenv()

# --- Налаштування ключів API та ID (з файлу .env) ---
# Важливо: Ніколи не зберігайте ключі прямо в коді!
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
POLYGON_API_KEY = os.getenv("POLYGON_API_KEY") or "ТВІЙ_КЛЮЧ"

# --- Налаштування торгівлі ---
RISK_PERCENT = 10.0  # Ризик на одну угоду у відсотках. 10.0 = 10% від балансу. НЕ РЕКОМЕНДУЄТЬСЯ > 10.0
ATR_TIMEFRAME = mt5.TIMEFRAME_M15  # Таймфрейм для розрахунку ATR
ATR_PERIOD = 14  # Період ATR
ATR_SL_MULTIPLIER = 1.5  # Множник ATR для Stop Loss
ATR_TP_MULTIPLIER = 3.0  # Множник ATR для Take Profit
MAGIC_NUMBER = 230523 # Унікальний ідентифікатор для ордерів цього бота

# --- Налаштування фільтрації новин ---
# Бот буде реагувати тільки на новини, що містять ці слова (для економії ресурсів)
IMPORTANT_KEYWORDS = [
    "gdp", "cpi", "ppi", "nfp", "non-farm", "unemployment", "interest rate",
    "fed", "ecb", "boj", "boe", "fomc", "inflation", "retail sales"
]

# --- Розширена фільтрація новин ---
# Whitelist/Blacklist ключових слів
NEWS_WHITELIST = [
    # Макроекономіка
    "rate hike", "rate cut", "interest rate", "fed decision", "fomc", "ecb", "boj", "boe", "central bank", "policy statement", "minutes", "gdp", "cpi", "ppi", "inflation", "unemployment", "jobs report", "nfp", "non-farm", "retail sales", "trade balance", "manufacturing", "services pmi", "consumer confidence", "housing starts", "building permits", "core durable goods", "initial jobless claims", "ISM", "recession", "expansion", "deficit", "surplus", "stimulus", "taper", "quantitative easing", "qt", "yield curve", "bond auction", "downgrade", "upgrade", "credit rating", "default", "debt ceiling", "shutdown", "budget", "fiscal", "monetary",
    # Геополітика та ризики
    "war", "conflict", "sanctions", "tariffs", "trade war", "embargo", "summit", "agreement", "deal", "brexit", "election", "referendum", "protest", "strike", "emergency", "earthquake", "hurricane", "pandemic", "covid", "lockdown",
    # Корпоративні новини
    "earnings", "profit warning", "guidance", "dividend cut", "dividend increase", "buyback", "merger", "acquisition", "ipo", "bankruptcy", "chapter 11", "layoffs", "job cuts", "ceo resigns", "scandal", "investigation", "fine", "lawsuit", "settlement", "antitrust", "regulation", "approval", "fda", "clinical trial", "recall", "hack", "cyberattack", "data breach",
    # Ринки та активи
    "oil", "crude", "gold", "silver", "commodity", "opec", "supply cut", "output", "inventory", "production", "export", "import", "price cap", "price floor", "volatility", "flash crash", "limit up", "limit down", "halted", "circuit breaker",
    # Додаткові важливі слова
    "unexpected", "record high", "missed forecast", "surprise"
]
NEWS_BLACKLIST = [
    "rumor", "speculation", "unconfirmed", "minor", "recall", "dividend", "buyback"
]

# Типи новин для фільтрації (можна розширити)
IMPORTANT_NEWS_TYPES = ["economic", "earnings", "central_bank", "macro"]

# Мінімальна важливість (impact) новини для торгівлі
MIN_NEWS_IMPORTANCE = 2  # 1 - low, 2 - medium, 3 - high

# --- Динамічний розмір лоту ---
def dynamic_risk_percent(news_data):
    """Визначає risk_percent залежно від важливості новини."""
    impact = news_data.get('importance', 1)
    if impact == 3:
        return 10.0  # High impact
    elif impact == 2:
        return 5.0   # Medium impact
    else:
        return 2.0   # Low impact

# --- Перевірка ринкових умов ---
MIN_LIQUIDITY_HOUR = 6   # Не торгувати з 00:00 до 06:00 UTC
MAX_SPREAD_POINTS = 30   # Максимально допустимий спред у пунктах

# --- Трейлінг-стоп (структура, реалізація залежить від брокера/MT5) ---
TRAILING_STOP_MULTIPLIER = 1.0  # 1 x ATR, можна налаштувати

# --- Захист від дублювання угод ---
open_positions = set()
open_positions_lock = threading.Lock()

# --- Мультистратегія (структура для майбутнього) ---
STRATEGY_MODE = "news"  # news, trend, scalping, hybrid

# --- Додаткове логування новин ---
def log_news(news_data, filtered, reason=None):
    with trade_log_lock:
        try:
            if os.path.exists("news_log.json"):
                with open("news_log.json", "r", encoding="utf-8") as f:
                    data = json.load(f)
            else:
                data = []
            entry = {
                "time": datetime.datetime.utcnow().isoformat(),
                "title": news_data.get('title'),
                "type": news_data.get('type'),
                "impact": news_data.get('importance'),
                "filtered": filtered,
                "reason": reason
            }
            data.append(entry)
            with open("news_log.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.error(f"Помилка збереження news log: {e}")

# --- Ініціалізація логування ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("trading_bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# --- Ініціалізація клієнтів API ---
try:
    openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    telegram_bot = Bot(token=TELEGRAM_BOT_TOKEN)
    logging.info("Клієнти OpenAI та Telegram успішно ініціалізовані.")
except Exception as e:
    logging.critical(f"Помилка ініціалізації клієнтів API: {e}")
    exit()

# --- 2. ПІДКЛЮЧЕННЯ ДО METATRADER 4/5 ---

def initialize_mt5():
    """Ініціалізує з'єднання з терміналом MetaTrader 5."""
    if not mt5.initialize():
        logging.critical("Не вдалося ініціалізувати MetaTrader 5. Перевірте, чи запущений термінал.")
        return False
    
    account_info = mt5.account_info()
    if account_info is None:
        logging.critical("Не вдалося отримати інформацію про рахунок. Перевірте підключення.")
        mt5.shutdown()
        return False
        
    logging.info(f"Підключено до рахунку MT5: {account_info.login}, Сервер: {account_info.server}, Баланс: {account_info.balance} {account_info.currency}")
    return True

# --- 3. ДОПОМІЖНІ ФУНКЦІЇ (TELEGRAM, AI, РОЗРАХУНКИ) ---

async def send_telegram_message(message: str):
    """Асинхронно надсилає повідомлення в Telegram."""
    try:
        await telegram_bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)
        logging.info(f"Надіслано повідомлення в Telegram: {message[:70]}...")
    except Exception as e:
        logging.error(f"Помилка надсилання повідомлення в Telegram: {e}")

async def get_trade_signal(news_text: str) -> Optional[Tuple[str, str, str]]:
    """
    Надсилає новину до OpenAI Assistant і отримує торговий сигнал.
    Повертає кортеж (ACTION, SYMBOL, REASON) або None у разі помилки.
    """
    logging.info(f"Надсилаю новину в OpenAI для аналізу: {news_text}")
    try:
        thread = await openai_client.beta.threads.create()
        await openai_client.beta.threads.messages.create(
            thread_id=thread.id,
            role="user",
            content=f"Analyze this news and provide a trading signal in the format 'ACTION SYMBOL' on the first line, and a brief reason on the second. News: \"{news_text}\""
        )
        
        run = await openai_client.beta.threads.runs.create(
            thread_id=thread.id,
            assistant_id=OPENAI_ASSISTANT_ID,
        )

        # Очікування завершення роботи асистента
        start_time = time.time()
        while run.status not in ["completed", "failed", "cancelled"]:
            await asyncio.sleep(1)
            run = await openai_client.beta.threads.runs.retrieve(thread_id=thread.id, run_id=run.id)
            if time.time() - start_time > 60: # Таймаут 60 секунд
                logging.error("OpenAI Assistant не відповів за 60 секунд.")
                await openai_client.beta.threads.runs.cancel(thread_id=thread.id, run_id=run.id)
                return None

        if run.status == "completed":
            messages = await openai_client.beta.threads.messages.list(thread_id=thread.id)
            response = messages.data[0].content[0].text.value
            logging.info(f"Отримана відповідь від OpenAI: {response}")
            
            # Парсинг відповіді
            lines = response.strip().split('\n')
            if len(lines) < 1:
                logging.warning("Отримана пуста відповідь від OpenAI.")
                return None
            
            first_line_parts = lines[0].strip().upper().split()
            if len(first_line_parts) != 2 or first_line_parts[0] not in ["BUY", "SELL", "SKIP"]:
                logging.warning(f"Некоректний формат сигналу від OpenAI: {lines[0]}")
                return None

            action, symbol = first_line_parts
            reason = lines[1].strip() if len(lines) > 1 else "Причина не вказана."
            
            return action, symbol.replace("/", ""), reason
        else:
            logging.error(f"Робота OpenAI Assistant завершилася зі статусом: {run.status}")
            return None

    except Exception as e:
        logging.error(f"Помилка під час взаємодії з OpenAI API: {e}")
        return None

def calculate_atr(symbol: str) -> Optional[float]:
    """Розраховує ATR для вказаного символу."""
    rates = mt5.copy_rates_from_pos(symbol, ATR_TIMEFRAME, 0, ATR_PERIOD + 1)
    if rates is None or len(rates) < ATR_PERIOD:
        logging.error(f"Недостатньо даних для розрахунку ATR для {symbol}")
        return None
    
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    atr = df.ta.atr(length=ATR_PERIOD)
    
    if atr is None or atr.empty:
        logging.error(f"Не вдалося розрахувати ATR для {symbol}")
        return None
        
    return atr.iloc[-1]

def calculate_position_size(symbol: str, sl_pips: float) -> Optional[float]:
    """Розраховує розмір позиції на основі ризику."""
    account_info = mt5.account_info()
    symbol_info = mt5.symbol_info(symbol)

    if account_info is None or symbol_info is None:
        logging.error("Не вдалося отримати інформацію про рахунок або символ.")
        return None

    balance = account_info.balance
    risk_amount = balance * (RISK_PERCENT / 100.0)
    
    # Отримуємо вартість одного пункту для одного лота
    # mt5.symbol_info_tick(...).point - розмір пункту (напр. 0.00001)
    # mt5.symbol_info(...).trade_tick_value - вартість зміни на один тік
    # mt5.symbol_info(...).trade_tick_size - розмір тіка
    tick_value = symbol_info.trade_tick_value
    tick_size = symbol_info.trade_tick_size
    point = symbol_info.point
    
    if tick_size == 0 or point == 0:
        logging.error(f"Некоректні дані tick_size/point для {symbol}")
        return None
        
    value_per_pip = tick_value * (point / tick_size)
    
    if value_per_pip == 0 or sl_pips == 0:
        logging.error(f"Некоректні дані value_per_pip/sl_pips для розрахунку лоту.")
        return None

    lot_size = risk_amount / (sl_pips * value_per_pip)
    
    # Округлення до мінімально можливого кроку лота
    lot_step = symbol_info.volume_step
    lot_size = round(lot_size / lot_step) * lot_step
    
    # Перевірка на мінімальний та максимальний об'єм
    min_lot = symbol_info.volume_min
    max_lot = symbol_info.volume_max
    
    if lot_size < min_lot:
        logging.warning(f"Розрахований лот {lot_size} менший за мінімальний {min_lot}. Використовуємо мінімальний.")
        lot_size = min_lot
    if lot_size > max_lot:
        logging.warning(f"Розрахований лот {lot_size} більший за максимальний {max_lot}. Використовуємо максимальний.")
        lot_size = max_lot

    return lot_size

def check_market_conditions(symbol: str) -> bool:
    """Перевіряє ринкові умови: ліквідність, спред, час."""
    now = datetime.datetime.utcnow()
    if now.hour < MIN_LIQUIDITY_HOUR:
        logging.info(f"Не торгуємо вночі (UTC < {MIN_LIQUIDITY_HOUR})")
        return False
    tick = mt5.symbol_info_tick(symbol)
    symbol_info = mt5.symbol_info(symbol)
    if not tick or not symbol_info:
        logging.warning(f"Не вдалося отримати tick/symbol_info для {symbol}")
        return False
    spread = abs(tick.ask - tick.bid) / symbol_info.point
    if spread > MAX_SPREAD_POINTS:
        logging.info(f"Завеликий спред для {symbol}: {spread} пунктів")
        return False
    return True

# --- 4. ОСНОВНА ТОРГОВА ЛОГІКА ---

async def execute_trade(action: str, symbol: str, reason: str, news_received_time=None):
    openai_response_time = datetime.datetime.utcnow()
    logging.info(f"Починаю процес відкриття угоди: {action} {symbol}")

    # --- Захист від дублювання угод ---
    with open_positions_lock:
        if symbol in open_positions:
            logging.info(f"Вже є відкрита позиція по {symbol}, нову не відкриваємо.")
            return
        open_positions.add(symbol)

    try:
        if not can_trade(symbol):
            logging.info(f"Ліміт або cooldown: угода не відкривається.")
            return
        if not check_market_conditions(symbol):
            logging.info(f"Ринкові умови не підходять для {symbol}, угода не відкривається.")
            return
        
        symbol_info = mt5.symbol_info(symbol)
        if not symbol_info or not symbol_info.visible:
            msg = f"⚠️ Помилка: Символ {symbol} не знайдено або він не доступний для торгівлі у вашого брокера."
            logging.error(msg)
            await send_telegram_message(msg)
            return
        atr_value = calculate_atr(symbol)
        if atr_value is None:
            msg = f"⚠️ Помилка: Не вдалося розрахувати ATR для {symbol}."
            logging.error(msg)
            await send_telegram_message(msg)
            return
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            msg = f"⚠️ Помилка: Не вдалося отримати поточну ціну для {symbol}."
            logging.error(msg)
            await send_telegram_message(msg)
            return
        price = tick.ask if action == "BUY" else tick.bid
        point = symbol_info.point
        # --- Трейлінг-стоп (додаємо до SL, якщо потрібно) ---
        trailing_stop = atr_value * TRAILING_STOP_MULTIPLIER
        # 3. Розрахувати SL і TP
        if action == "BUY":
            stop_loss = price - (atr_value * ATR_SL_MULTIPLIER)
            take_profit = price + (atr_value * ATR_TP_MULTIPLIER)
            trailing_sl = price - trailing_stop
        else:  # SELL
            stop_loss = price + (atr_value * ATR_SL_MULTIPLIER)
            take_profit = price - (atr_value * ATR_TP_MULTIPLIER)
            trailing_sl = price + trailing_stop
        sl_pips = abs(price - stop_loss) / point
        volume = calculate_position_size(symbol, sl_pips)
        if volume is None or volume == 0:
            msg = f"⚠️ Помилка: Не вдалося розрахувати розмір позиції для {symbol}."
            logging.error(msg)
            await send_telegram_message(msg)
            return
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": volume,
            "type": mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL,
            "price": price,
            "sl": stop_loss,
            "tp": take_profit,
            "deviation": 10,
            "magic": MAGIC_NUMBER,
            "comment": f"NewsBot: {reason[:20]}",
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        trade_sent_time = datetime.datetime.utcnow()
        latency = (trade_sent_time - news_received_time).total_seconds() if news_received_time else None
        result = mt5.order_send(request)
        if result is None:
            msg = f"❌ Помилка відкриття ордеру для {symbol}. Код: Невідома помилка."
            logging.error(msg)
            await send_telegram_message(msg)
        elif result.retcode == mt5.TRADE_RETCODE_DONE:
            msg = (
                f"✅ Угоду відкрито: {action} {symbol}\n"
                f"🔹 Ціна входу: {result.price}\n"
                f"🔹 Об'єм: {result.volume}\n"
                f"🛑 Stop Loss: {result.sl}\n"
                f"🎯 Take Profit: {result.tp}\n"
                f"Reason: {reason}\n"
                f"⏱️ Latency: {latency:.2f} сек."
            )
            logging.info(f"Угода {result.order} успішно відкрита. Latency: {latency}")
            await send_telegram_message(msg)
            log_trade_result({
                "type": "open",
                "order": result.order,
                "symbol": symbol,
                "action": action,
                "volume": result.volume,
                "price": result.price,
                "sl": result.sl,
                "tp": result.tp,
                "trailing_sl": trailing_sl,
                "reason": reason,
                "open_time": trade_sent_time.isoformat(),
                "latency": latency
            })
        else:
            msg = (
                f"❌ Помилка відкриття ордеру для {symbol}.\n"
                f"Код: {result.retcode}\n"
                f"Коментар: {result.comment}"
            )
            logging.error(msg)
            await send_telegram_message(msg)
    except Exception as e:
        logging.error(f"Помилка під час виконання угоди: {e}")
        await send_telegram_message(f"❌ Помилка під час виконання угоди: {e}")
    finally:
        # Видаляємо символ з відкритих позицій
        with open_positions_lock:
            open_positions.discard(symbol)

# --- Відстеження закриття угод ---
async def monitor_closed_trades():
    global cooldown_until
    last_ticket_set = set()
    while True:
        try:
            closed_orders = mt5.history_deals_get(datetime.datetime.now() - datetime.timedelta(days=2), datetime.datetime.now())
            if closed_orders:
                for deal in closed_orders:
                    if deal.type in [mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL] and deal.entry == 1:  # закриття позиції
                        if deal.ticket not in last_ticket_set:
                            last_ticket_set.add(deal.ticket)
                            profit = deal.profit
                            if profit < 0:
                                cooldown_until = datetime.datetime.utcnow() + datetime.timedelta(seconds=COOLDOWN_AFTER_LOSS)
                            msg = (
                                f"❌ Угоду закрито: {deal.symbol}\n"
                                f"Тип: {'BUY' if deal.type == mt5.DEAL_TYPE_BUY else 'SELL'}\n"
                                f"Об'єм: {deal.volume}\n"
                                f"Ціна закриття: {deal.price}\n"
                                f"Прибуток: {profit}"
                            )
                            await send_telegram_message(msg)
                            log_trade_result({
                                "type": "close",
                                "ticket": deal.ticket,
                                "symbol": deal.symbol,
                                "action": 'BUY' if deal.type == mt5.DEAL_TYPE_BUY else 'SELL',
                                "volume": deal.volume,
                                "close_price": deal.price,
                                "profit": profit,
                                "close_time": datetime.datetime.utcfromtimestamp(deal.time).isoformat()
                            })
            await asyncio.sleep(5)
        except Exception as e:
            logging.error(f"Помилка моніторингу закриття угод: {e}")
            await asyncio.sleep(10)


# --- 5. ГОЛОВНИЙ ЦИКЛ (ПРОСЛУХОВОВАННЯ WEBSOCKET) ---

async def process_news_item(news_data: dict):
    try:
        title = news_data.get('title', '').lower()
        news_received_time = datetime.datetime.utcnow()
        # --- Розширена фільтрація ---
        filtered = False
        reason = None
        if any(bad in title for bad in NEWS_BLACKLIST):
            filtered = True
            reason = "blacklist"
            log_news(news_data, filtered, reason)
            logging.info(f"Новина проігнорована через blacklist: {news_data.get('title')}")
            return
        if not any(good in title for good in NEWS_WHITELIST):
            filtered = True
            reason = "not in whitelist"
            log_news(news_data, filtered, reason)
            logging.info(f"Новина не містить whitelist-ключових слів: {news_data.get('title')}")
            return
        impact = news_data.get('importance', 1)
        if impact < MIN_NEWS_IMPORTANCE:
            filtered = True
            reason = f"impact={impact}"
            log_news(news_data, filtered, reason)
            logging.info(f"Новина має низьку важливість (impact={impact}): {news_data.get('title')}")
            return
        news_type = news_data.get('type', '').lower()
        if news_type and news_type not in IMPORTANT_NEWS_TYPES:
            filtered = True
            reason = f"type={news_type}"
            log_news(news_data, filtered, reason)
            logging.info(f"Новина не входить до важливих типів: {news_data.get('title')}")
            return
        log_news(news_data, False)
        logging.info(f"Знайдено важливу новину: {news_data.get('title')}")
        global RISK_PERCENT
        RISK_PERCENT = dynamic_risk_percent(news_data)
        signal_data = await get_trade_signal(news_data.get('title'))
        openai_response_time = datetime.datetime.utcnow()
        logging.info(f"OpenAI latency: {(openai_response_time - news_received_time).total_seconds():.2f} сек.")
        if signal_data:
            action, symbol, reason = signal_data
            if action in ["BUY", "SELL"]:
                asyncio.create_task(execute_trade(action, symbol, reason, news_received_time=news_received_time))
            else:
                logging.info(f"Отримано сигнал SKIP для новини. Торгівлю пропущено.")
    except Exception as e:
        logging.error(f"Критична помилка в обробці новини: {e}")


# --- Polygon.io REST API polling ---
POLYGON_NEWS_URL = "https://api.polygon.io/v2/reference/news"
last_polygon_news_id = None

async def polygon_news_poller():
    global last_polygon_news_id
    while True:
        try:
            params = {"apiKey": POLYGON_API_KEY, "limit": 1}
            response = requests.get(POLYGON_NEWS_URL, params=params)
            data = response.json()
            results = data.get("results", [])
            if results:
                news = results[0]
                if news["id"] != last_polygon_news_id:
                    last_polygon_news_id = news["id"]
                    logging.info(f"НОВА НОВИНА (Polygon): {news['title']}")
                    await process_news_item(news)
        except Exception as e:
            logging.error(f"Помилка отримання новин Polygon: {e}")
        await asyncio.sleep(2)  # polling кожні 2 секунди

# --- Telegram-інтерфейс ---
MAX_TRADES_PER_DAY = 10
COOLDOWN_AFTER_LOSS = 600  # секунд (10 хвилин)
last_trade_time = None
last_trade_profit = 0
cooldown_until = None
trade_counter = {}

async def stats_command(update, context):
    """Відправляє статистику за день."""
    try:
        today = datetime.datetime.utcnow().date()
        if os.path.exists(TRADE_LOG_FILE):
            with open(TRADE_LOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = []
        trades_today = [t for t in data if t.get("open_time", "").startswith(str(today))]
        profit = sum(t.get("profit", 0) for t in trades_today if t["type"] == "close")
        msg = f"Статистика за {today} UTC:\nКількість угод: {len(trades_today)}\nСумарний прибуток: {profit:.2f}"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Помилка stats: {e}")

async def last_command(update, context):
    """Відправляє інформацію про останню угоду."""
    try:
        if os.path.exists(TRADE_LOG_FILE):
            with open(TRADE_LOG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = []
        if not data:
            await update.message.reply_text("Ще не було жодної угоди.")
            return
        last = data[-1]
        msg = f"Остання угода:\n{json.dumps(last, ensure_ascii=False, indent=2)}"
        await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"Помилка last: {e}")

async def help_command(update, context):
    msg = "/stats — статистика за день\n/last — остання угода\n/help — ця довідка"
    await update.message.reply_text(msg)

# --- Ліміти та cooldown ---
def can_trade(symbol):
    global cooldown_until
    now = datetime.datetime.utcnow()
    today = now.date()
    # Ліміт угод на день
    if today not in trade_counter:
        trade_counter.clear()
        trade_counter[today] = 0
    if trade_counter[today] >= MAX_TRADES_PER_DAY:
        logging.info(f"Досягнуто ліміту угод на день: {MAX_TRADES_PER_DAY}")
        return False
    # Cooldown
    if cooldown_until and now < cooldown_until:
        logging.info(f"Активний cooldown до {cooldown_until}")
        return False
    return True

def register_trade():
    today = datetime.datetime.utcnow().date()
    if today not in trade_counter:
        trade_counter.clear()
        trade_counter[today] = 0
    trade_counter[today] += 1

# --- Симулятор (структура для майбутнього) ---
def run_simulation(news_history, strategy_func):
    """Симулятор для тестування стратегії на історичних новинах."""
    # news_history: список новин (dict)
    # strategy_func: функція, яка приймає новину і повертає дію
    results = []
    for news in news_history:
        action, symbol, reason = strategy_func(news)
        # Тут можна симулювати відкриття/закриття угоди, рахувати прибуток тощо
        results.append({"news": news, "action": action, "symbol": symbol, "reason": reason})
    return results

# --- Інтеграція Telegram-бота ---
telegram_app = None

def start_telegram_bot():
    global telegram_app
    telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("stats", stats_command))
    telegram_app.add_handler(CommandHandler("last", last_command))
    telegram_app.add_handler(CommandHandler("help", help_command))
    # Виправляємо помилку з event loop
    import asyncio
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        telegram_app.run_polling()
    except Exception as e:
        logging.error(f"Помилка запуску Telegram-бота: {e}")

# --- Модифікація monitor_closed_trades для cooldown ---
async def monitor_closed_trades():
    global cooldown_until
    last_ticket_set = set()
    while True:
        try:
            closed_orders = mt5.history_deals_get(datetime.datetime.now() - datetime.timedelta(days=2), datetime.datetime.now())
            if closed_orders:
                for deal in closed_orders:
                    if deal.type in [mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL] and deal.entry == 1:
                        if deal.ticket not in last_ticket_set:
                            last_ticket_set.add(deal.ticket)
                            profit = deal.profit
                            if profit < 0:
                                cooldown_until = datetime.datetime.utcnow() + datetime.timedelta(seconds=COOLDOWN_AFTER_LOSS)
                            msg = (
                                f"❌ Угоду закрито: {deal.symbol}\n"
                                f"Тип: {'BUY' if deal.type == mt5.DEAL_TYPE_BUY else 'SELL'}\n"
                                f"Об'єм: {deal.volume}\n"
                                f"Ціна закриття: {deal.price}\n"
                                f"Прибуток: {profit}"
                            )
                            await send_telegram_message(msg)
                            log_trade_result({
                                "type": "close",
                                "ticket": deal.ticket,
                                "symbol": deal.symbol,
                                "action": 'BUY' if deal.type == mt5.DEAL_TYPE_BUY else 'SELL',
                                "volume": deal.volume,
                                "close_price": deal.price,
                                "profit": profit,
                                "close_time": datetime.datetime.utcfromtimestamp(deal.time).isoformat()
                            })
            await asyncio.sleep(5)
        except Exception as e:
            logging.error(f"Помилка моніторингу закриття угод: {e}")
        await asyncio.sleep(10)

# --- Запуск Telegram-бота у окремому потоці ---
import threading as _threading

def start_telegram_thread():
    t = _threading.Thread(target=start_telegram_bot, daemon=True)
    t.start()

# --- main ---
if __name__ == "__main__":
    if not all([OPENAI_API_KEY, OPENAI_ASSISTANT_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, POLYGON_API_KEY]):
        logging.critical("Не всі необхідні змінні середовища встановлені. Перевірте ваш .env файл.")
    elif not initialize_mt5():
        logging.critical("Вихід з програми через помилку підключення до MT5.")
    else:
        try:
            start_telegram_thread()
            loop = asyncio.get_event_loop()
            loop.create_task(polygon_news_poller())
            loop.create_task(monitor_closed_trades())
            loop.run_forever()
        except KeyboardInterrupt:
            logging.info("Бот зупинено вручну.")
        finally:
            mt5.shutdown()
            logging.info("З'єднання з MetaTrader 5 закрито.")
