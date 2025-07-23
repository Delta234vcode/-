# main_bot.py
import os
import asyncio
import json
import logging
import time
from typing import Optional, Tuple

import MetaTrader5 as mt5
import pandas as pd
import pandas_ta as ta
from dotenv import load_dotenv
from openai import AsyncOpenAI
from telegram import Bot
from websockets.client import connect
from websockets.exceptions import ConnectionClosed

# --- 1. КОНФІГУРАЦІЯ ТА ІНІЦІАЛІЗАЦІЯ ---

# Завантажуємо змінні середовища з файлу .env
# Створіть файл .env у тій же папці, що і цей скрипт
load_dotenv()

# --- Налаштування ключів API та ID (з файлу .env) ---
# Важливо: Ніколи не зберігайте ключі прямо в коді!
BENZINGA_API_KEY = os.getenv("BENZINGA_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# --- Налаштування торгівлі ---
RISK_PERCENT = 1.0  # Ризик на одну угоду у відсотках. 1.0 = 1% від балансу. НЕ РЕКОМЕНДУЄТЬСЯ > 2.0
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

# --- Ініціалізація логування ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("trading_bot.log"),
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


# --- 4. ОСНОВНА ТОРГОВА ЛОГІКА ---

async def execute_trade(action: str, symbol: str, reason: str):
    """Виконує повний цикл відкриття угоди."""
    logging.info(f"Починаю процес відкриття угоди: {action} {symbol}")

    symbol_info = mt5.symbol_info(symbol)
    if not symbol_info or not symbol_info.visible:
        msg = f"⚠️ Помилка: Символ {symbol} не знайдено або він не доступний для торгівлі у вашого брокера."
        logging.error(msg)
        await send_telegram_message(msg)
        return

    # 1. Отримати ATR
    atr_value = calculate_atr(symbol)
    if atr_value is None:
        msg = f"⚠️ Помилка: Не вдалося розрахувати ATR для {symbol}."
        logging.error(msg)
        await send_telegram_message(msg)
        return

    # 2. Отримати поточну ціну
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        msg = f"⚠️ Помилка: Не вдалося отримати поточну ціну для {symbol}."
        logging.error(msg)
        await send_telegram_message(msg)
        return

    price = tick.ask if action == "BUY" else tick.bid
    point = symbol_info.point

    # 3. Розрахувати SL і TP
    if action == "BUY":
        stop_loss = price - (atr_value * ATR_SL_MULTIPLIER)
        take_profit = price + (atr_value * ATR_TP_MULTIPLIER)
    else:  # SELL
        stop_loss = price + (atr_value * ATR_SL_MULTIPLIER)
        take_profit = price - (atr_value * ATR_TP_MULTIPLIER)
    
    sl_pips = abs(price - stop_loss) / point

    # 4. Розрахувати розмір позиції
    volume = calculate_position_size(symbol, sl_pips)
    if volume is None or volume == 0:
        msg = f"⚠️ Помилка: Не вдалося розрахувати розмір позиції для {symbol}."
        logging.error(msg)
        await send_telegram_message(msg)
        return

    # 5. Сформувати та відправити запит на ордер
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": mt5.ORDER_TYPE_BUY if action == "BUY" else mt5.ORDER_TYPE_SELL,
        "price": price,
        "sl": stop_loss,
        "tp": take_profit,
        "deviation": 10, # Допустиме прослизання в пунктах
        "magic": MAGIC_NUMBER,
        "comment": f"NewsBot: {reason[:20]}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC, # або FOK, залежить від брокера
    }

    result = mt5.order_send(request)

    # 6. Обробити результат
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
            f"Reason: {reason}"
        )
        logging.info(f"Угода {result.order} успішно відкрита.")
        await send_telegram_message(msg)
    else:
        msg = (
            f"❌ Не вдалося відкрити ордер: {action} {symbol}\n"
            f"Код помилки: {result.retcode} - {result.comment}"
        )
        logging.error(f"Помилка order_send: {result}")
        await send_telegram_message(msg)


# --- 5. ГОЛОВНИЙ ЦИКЛ (ПРОСЛУХОВУВАННЯ WEBSOCKET) ---

async def process_news_item(news_data: dict):
    """Обробляє одну новину: фільтрує, аналізує та торгує."""
    try:
        title = news_data.get('title', '').lower()
        
        # Фільтрація за ключовими словами
        if not any(keyword in title for keyword in IMPORTANT_KEYWORDS):
            return # Ігноруємо неважливу новину

        logging.info(f"Знайдено важливу новину: {news_data.get('title')}")
        
        # Отримання торгового сигналу
        signal_data = await get_trade_signal(news_data.get('title'))
        
        if signal_data:
            action, symbol, reason = signal_data
            if action in ["BUY", "SELL"]:
                # Запускаємо торгівлю в окремому завданні, щоб не блокувати
                asyncio.create_task(execute_trade(action, symbol, reason))
            else:
                logging.info(f"Отримано сигнал SKIP для новини. Торгівлю пропущено.")

    except Exception as e:
        logging.error(f"Критична помилка в обробці новини: {e}")


async def benzinga_listener():
    """Підключається до Benzinga WebSocket і слухає новини."""
    uri = f"wss://api.benzinga.com/v1/news/stream?token={BENZINGA_API_KEY}"
    
    while True: # Цикл для автоматичного перепідключення
        try:
            async with connect(uri) as websocket:
                logging.info("Підключено до Benzinga News Stream.")
                await send_telegram_message("✅ Бот запущено. Підключено до стріму новин.")
                
                async for message in websocket:
                    try:
                        data = json.loads(message)
                        if data.get('type') == 'news':
                            # Запускаємо обробку новини асинхронно
                            asyncio.create_task(process_news_item(data.get('data')))
                    except json.JSONDecodeError:
                        logging.warning(f"Не вдалося розпарсити JSON: {message}")
                    except Exception as e:
                        logging.error(f"Помилка обробки повідомлення з WebSocket: {e}")

        except ConnectionClosed as e:
            logging.warning(f"З'єднання з WebSocket закрито: {e}. Перепідключення через 10 секунд...")
        except Exception as e:
            logging.error(f"Критична помилка WebSocket: {e}. Перепідключення через 10 секунд...")
        
        await asyncio.sleep(10)


if __name__ == "__main__":
    if not all([BENZINGA_API_KEY, OPENAI_API_KEY, OPENAI_ASSISTANT_ID, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID]):
        logging.critical("Не всі необхідні змінні середовища встановлені. Перевірте ваш .env файл.")
    elif not initialize_mt5():
        logging.critical("Вихід з програми через помилку підключення до MT5.")
    else:
        try:
            asyncio.run(benzinga_listener())
        except KeyboardInterrupt:
            logging.info("Бот зупинено вручну.")
        finally:
            mt5.shutdown()
            logging.info("З'єднання з MetaTrader 5 закрито.")
