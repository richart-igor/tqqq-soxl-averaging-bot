import asyncio
import logging

from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

import yfinance as yf

# ------------------ НАСТРОЙКИ ------------------
BOT_TOKEN = "123456789:AAFxxxxxxxxxxxxxxxxxxxxxxxxxxxx"  # ← свой токен от @BotFather

logging.basicConfig(level=logging.INFO)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)

class StrategyForm(StatesGroup):
    strategy = State()
    etf = State()
    current_share = State()
    portfolio_size = State()
    multiple_etfs = State()  # Для хранения списка ETF при "Оба"

# ------------------ ТАБЛИЦЫ ИЗ SHEETS ------------------
DIVIDEND_TQQQ_LEVELS = [  # (price_threshold, level_str, min_perc)
    (60.59, "0%", 2.5),
    (42.41, "-30%", 5.0),
    (30.30, "-50%", 7.5),
    (18.18, "-70%", 10.0),
]

DIVIDEND_SOXL_LEVELS = [
    (72.36, "0%", 2.5),
    (43.42, "-40%", 5.0),
    (28.94, "-60%", 7.5),
    (14.47, "-80%", 10.0),
]

SMART_TQQQ_LEVELS = [
    (60.59, "0%", 5.0),
    (42.41, "-30%", 10.0),
    (30.30, "-50%", 15.0),
    (18.18, "-70%", 20.0),
]

# ------------------ ФУНКЦИИ ------------------
def get_price(ticker: str) -> float:
    try:
        stock = yf.Ticker(ticker)
        price = stock.info.get("currentPrice")
        if price is None:
            hist = stock.history(period="1d")
            price = hist["Close"].iloc[-1] if not hist.empty else 0.0
        return price
    except Exception as e:
        logging.error(f"Ошибка цены {ticker}: {e}")
        return 0.0

def get_min_perc(strategy: str, etf: str, price: float) -> tuple[str, float]:
    if strategy == "DIVIDEND":
        levels = DIVIDEND_TQQQ_LEVELS if etf == "TQQQ" else DIVIDEND_SOXL_LEVELS
    else:
        levels = SMART_TQQQ_LEVELS

    # Сортируем по цене desc
    levels = sorted(levels, key=lambda x: x[0], reverse=True)

    for thresh, lev, mp in levels:
        if price >= thresh:
            return lev, mp

    # Если ниже самого низкого — берём максимум
    return levels[-1][1], levels[-1][2]

def create_strategy_keyboard():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    keyboard.add(KeyboardButton("DIVIDEND"), KeyboardButton("SMART"))
    return keyboard

def create_etf_keyboard():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    keyboard.add(KeyboardButton("TQQQ"), KeyboardButton("SOXL"), KeyboardButton("Оба"))
    keyboard.add(KeyboardButton("Отмена"))
    return keyboard

def create_cancel_keyboard():
    keyboard = ReplyKeyboardMarkup(resize_keyboard=True, one_time_keyboard=True)
    keyboard.add(KeyboardButton("Отмена"))
    return keyboard

# ------------------ ХЕНДЛЕРЫ ------------------
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я бот для усреднения TQQQ/SOXL по стратегиям DIVIDEND и SMART.\n\n"
        "Выбери стратегию:",
        reply_markup=create_strategy_keyboard()
    )
    await state.set_state(StrategyForm.strategy)

@dp.message(StrategyForm.strategy)
async def process_strategy(message: Message, state: FSMContext):
    strategy = message.text.strip().upper()
    if strategy not in ["DIVIDEND", "SMART"]:
        await message.reply("Выбери DIVIDEND или SMART с клавиатуры.")
        return

    await state.update_data(strategy=strategy)

    if strategy == "SMART":
        etfs = ["TQQQ"]
        await state.update_data(multiple_etfs=etfs, etf_index=0)
        await process_next_etf(message, state)
    else:
        await message.reply("Выбрана DIVIDEND. Теперь выбери ETF:", reply_markup=create_etf_keyboard())
        await state.set_state(StrategyForm.etf)

@dp.message(StrategyForm.etf)
async def process_etf_choice(message: Message, state: FSMContext):
    choice = message.text.strip().upper()
    if choice == "ОТМЕНА":
        await state.clear()
        await message.reply("Отменено.", reply_markup=ReplyKeyboardRemove())
        return

    if choice == "ОБА":
        etfs = ["TQQQ", "SOXL"]
    elif choice in ["TQQQ", "SOXL"]:
        etfs = [choice]
    else:
        await message.reply("Выбери TQQQ, SOXL или Оба с клавиатуры.")
        return

    await state.update_data(multiple_etfs=etfs, etf_index=0, results={})
    await process_next_etf(message, state)

async def process_next_etf(message: Message, state: FSMContext):
    data = await state.get_data()
    etfs = data.get("multiple_etfs", [])
    index = data.get("etf_index", 0)

    if index >= len(etfs):
        # Все ETF обработаны — выводим результаты
        await show_results(message, state)
        return

    etf = etfs[index]
    await state.update_data(current_etf=etf)

    price = get_price(etf)
    if price <= 0:
        await message.reply(f"Ошибка получения цены для {etf} 😢. Попробуй позже.", reply_markup=ReplyKeyboardRemove())
        await state.clear()
        return

    strategy = data["strategy"]
    level, min_perc = get_min_perc(strategy, etf, price)

    await state.update_data(price=price, level=level, min_perc=min_perc)

    await message.reply(
        f"{etf} текущая цена: ${price:.2f}\n"
        f"Уровень просадки: {level}\n"
        f"Минимальная доля: {min_perc}%",
        reply_markup=ReplyKeyboardRemove()
    )

    await message.reply(
        f"Введи свою текущую долю {etf} в портфеле (%) или 'Отмена':",
        reply_markup=create_cancel_keyboard()
    )
    await state.set_state(StrategyForm.current_share)

@dp.message(StrategyForm.current_share)
async def process_current_share(message: Message, state: FSMContext):
    text = message.text.strip()
    if text.upper() == "ОТМЕНА":
        await state.clear()
        await message.reply("Отменено.", reply_markup=ReplyKeyboardRemove())
        return

    try:
        current = float(text)
        if current < 0 or current > 100:
            raise ValueError
    except:
        await message.reply("Введи число от 0 до 100 или 'Отмена'.")
        return

    data = await state.get_data()
    min_perc = data["min_perc"]

    if current >= min_perc:
        await add_result(state, f"Для {data['current_etf']}: Текущая доля {current}% >= минимальной ({min_perc}%). Не нужно докупать.")
        await next_etf_or_finish(message, state)
    else:
        await state.update_data(current_share=current)
        await message.reply(
            "Введи общую сумму твоего портфеля в $ (чтобы посчитать сумму докупки) или 'Отмена':",
            reply_markup=create_cancel_keyboard()
        )
        await state.set_state(StrategyForm.portfolio_size)

@dp.message(StrategyForm.portfolio_size)
async def process_portfolio_size(message: Message, state: FSMContext):
    text = message.text.strip()
    if text.upper() == "ОТМЕНА":
        await state.clear()
        await message.reply("Отменено.", reply_markup=ReplyKeyboardRemove())
        return

    try:
        port_size = float(text)
        if port_size <= 0:
            raise ValueError
    except:
        await message.reply("Введи положительное число или 'Отмена'.")
        return

    data = await state.get_data()
    etf = data["current_etf"]
    price = data["price"]
    min_perc = data["min_perc"]
    current = data["current_share"]

    target_amount = port_size * (min_perc / 100)
    current_amount = port_size * (current / 100)
    to_buy_amount = target_amount - current_amount
    to_buy_shares = to_buy_amount / price if price > 0 else 0

    result = (
        f"Для {etf}: Нужно докупить на ${to_buy_amount:.2f} "
        f"(примерно {to_buy_shares:.0f} акций по текущей цене ${price:.2f}), "
        f"чтобы достичь {min_perc}% в портфеле."
    )
    await add_result(state, result)
    await next_etf_or_finish(message, state)

async def add_result(state: FSMContext, result: str):
    data = await state.get_data()
    results = data.get("results", {})
    results[data["current_etf"]] = result
    await state.update_data(results=results)

async def next_etf_or_finish(message: Message, state: FSMContext):
    data = await state.get_data()
    index = data.get("etf_index", 0) + 1
    await state.update_data(etf_index=index)
    await process_next_etf(message, state)

async def show_results(message: Message, state: FSMContext):
    data = await state.get_data()
    results = data.get("results", {})
    text = "Результаты:\n\n" + "\n\n".join(results.values())
    await message.reply(text, reply_markup=ReplyKeyboardRemove())
    await state.clear()

@dp.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.reply("Отменено.", reply_markup=ReplyKeyboardRemove())

# ------------------ ЗАПУСК ------------------
async def main():
    bot = Bot(token=BOT_TOKEN)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())