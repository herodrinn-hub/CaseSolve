from __future__ import annotations

import asyncio
import html
import logging
import os
import re
from datetime import datetime
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, ChatPermissions, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.database import Database
from bot.facts import FACTS

logging.basicConfig(level=logging.INFO)
db = Database(os.getenv("DATABASE_PATH", "casesolve.sqlite3"))
router = Router()


class SetupStates(StatesGroup):
    waiting_for_chat = State()


class CaseStates(StatesGroup):
    waiting_for_roles = State()
    waiting_for_witnesses = State()


def display(user: Any) -> str:
    name = html.escape(user.full_name or "Участник")
    return f'<a href="tg://user?id={user.id}">{name}</a>'


def clean_command(text: str | None) -> str:
    return re.sub(r"[^a-zа-яё]", "", (text or "").lower())


async def is_admin(message: Message, user_id: int) -> bool:
    if message.chat.type == ChatType.PRIVATE:
        return False
    member = await message.bot.get_chat_member(message.chat.id, user_id)
    return member.status in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}


async def owner_id(bot: Bot, chat_id: int) -> int | None:
    settings = db.settings(chat_id)
    if settings:
        return int(settings["owner_id"])
    admins = await bot.get_chat_administrators(chat_id)
    owner = next((a.user.id for a in admins if a.status == ChatMemberStatus.CREATOR), None)
    if owner:
        db.save_settings(chat_id, owner)
    return owner


async def is_owner(message: Message) -> bool:
    current = await owner_id(message.bot, message.chat.id)
    return current == (message.from_user.id if message.from_user else None)


def role_markup(case_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Создать суд", callback_data=f"create:{case_id}")],
    ])


def judge_markup(case_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Остановить", callback_data=f"stop:{case_id}")],
        [InlineKeyboardButton(text="Принять решение", callback_data=f"deliberate:{case_id}")],
    ])


def verdict_markup(case_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сказать итог", callback_data=f"verdict_menu:{case_id}")],
    ])


def verdict_choices(case_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Виновен истец", callback_data=f"verdict:{case_id}:plaintiff")],
        [InlineKeyboardButton(text="Виновен ответчик", callback_data=f"verdict:{case_id}:defendant")],
        [InlineKeyboardButton(text="Ничья", callback_data=f"verdict:{case_id}:draw")],
    ])


def cleanup_markup(case_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить историю чата и участников", callback_data=f"cleanup:{case_id}")],
    ])


def users_from_message(message: Message) -> list[int]:
    found: list[int] = []
    if message.reply_to_message and message.reply_to_message.from_user:
        found.append(message.reply_to_message.from_user.id)
    for entity in message.entities or []:
        if entity.type == "text_mention" and entity.user:
            found.append(entity.user.id)
    return list(dict.fromkeys(found))


async def parse_roles(message: Message) -> dict[str, Any]:
    text = message.text or ""
    ids = users_from_message(message)
    mentions = re.findall(r"@[\w_]{2,}", text)
    # Telegram does not expose arbitrary @username IDs without entity data.
    # Reply/text_mention are therefore preferred; this also avoids guessing users.
    role_ids: dict[str, int | None] = {"plaintiff_id": None, "defendant_id": None, "judge_id": None}
    witness_ids: list[int] = []
    labels = {
        "истец": "plaintiff_id", "истица": "plaintiff_id",
        "ответчик": "defendant_id", "ответчица": "defendant_id",
        "судья": "judge_id",
    }
    for label, key in labels.items():
        match = re.search(label + r"\s*:\s*", text.lower())
        if match:
            index = len([v for v in role_ids.values() if v])
            if index < len(ids):
                role_ids[key] = ids[index]
    witness_count = len(re.findall(r"свидетел", text.lower()))
    if witness_count and len(ids) > 3:
        witness_ids = ids[3:8]
    if not role_ids["plaintiff_id"] and ids:
        role_ids["plaintiff_id"] = ids[0]
    if not role_ids["defendant_id"] and len(ids) > 1:
        role_ids["defendant_id"] = ids[1]
    if not role_ids["judge_id"] and len(ids) > 2:
        role_ids["judge_id"] = ids[2]
    role_ids["witnesses"] = witness_ids
    role_ids["mentions"] = mentions
    return role_ids


@router.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "⚖️ <b>CaseSolve</b>\n\n"
        "Приветствую. Я организую честный и последовательный суд в группе.\n\n"
        "🛠 <b>В группе:</b>\n"
        "• <code>! Суд</code> или <code>/lawsuit</code> — вызвать участника в суд\n"
        "• Ответьте этой командой на сообщение участника или укажите его через Telegram-упоминание\n"
        "• Владелец может создать суд по ролям: Истец, Ответчик, Свидетель, Судья\n\n"
        "🔧 <b>Для владельца:</b>\n"
        "• <code>/setup</code> — настроить чат для судов\n"
        "• <code>/status</code> — показать настройки\n\n"
        "В личных сообщениях настройка выполняется командой <code>/setup ID_группы</code>."
    )


@router.message(Command("setup"))
async def setup(message: Message, state: FSMContext) -> None:
    if message.chat.type == ChatType.PRIVATE:
        args = (message.text or "").split(maxsplit=1)
        if len(args) == 2 and args[1].lstrip("-").isdigit():
            chat_id = int(args[1])
            try:
                member = await message.bot.get_chat_member(chat_id, message.from_user.id)
                if member.status not in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
                    await message.answer("🔒 Настроить чат может только его администратор или владелец.")
                    return
            except Exception:
                await message.answer("❕️ Не удалось проверить группу. Добавьте меня в группу и выдайте права администратора.")
                return
            await state.update_data(chat_id=chat_id)
            await state.set_state(SetupStates.waiting_for_chat)
            await message.answer("📇 Настройка запущена. Напишите название суда или отправьте «готово».")
            return
        await message.answer("📇 Укажите ID группы: <code>/setup -1001234567890</code>\nПолучить ID можно через служебного бота или настроить суд командой /setup прямо в группе.")
        return
    if not message.from_user or not await is_admin(message, message.from_user.id):
        await message.answer("🔒 Настроить чат может только администратор.")
        return
    db.save_settings(message.chat.id, message.from_user.id, message.chat.id, message.chat.title or "")
    await message.answer("🛡 Этот чат сохранён как чат для проведения судов.\nТеперь я буду использовать его автоматически.")


@router.message(SetupStates.waiting_for_chat)
async def finish_setup(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    chat_id = int(data["chat_id"])
    db.save_settings(chat_id, message.from_user.id, chat_id, message.text or "")
    await state.clear()
    await message.answer("✔️ Чат для судов настроен. Все новые дела будут направляться туда.")


@router.message(Command("status"))
async def status(message: Message) -> None:
    settings = db.settings(message.chat.id)
    if not settings:
        await message.answer("❕️ Чат для судов ещё не настроен. Используйте /setup.")
        return
    court = settings["court_title"] or str(settings["court_chat_id"])
    await message.answer(f"⚙️ Чат для судов: <b>{html.escape(court)}</b>\nСтатус: готов к новым делам.")


@router.message(F.text)
async def lawsuit(message: Message, state: FSMContext) -> None:
    command = clean_command(message.text)
    active = db.active_case(message.chat.id) if message.chat.type != ChatType.PRIVATE else None
    if active and active["state"] == "active" and message.from_user:
        if message.from_user.id == active["plaintiff_id"] and active["turn"] == "plaintiff":
            db.update_case(active["id"], turn="defendant")
            await apply_permissions(message.bot, active, "defendant")
            await message.answer("⚖️ Слово передаётся Ответчику.")
            return
        if message.from_user.id == active["defendant_id"] and active["turn"] == "defendant":
            db.update_case(active["id"], turn="plaintiff")
            await apply_permissions(message.bot, active, "plaintiff")
            await message.answer("⚖️ Слово снова передаётся Истцу.")
            return
    if command not in {"суд", "lawsuit"} and not command.startswith("суд"):
        return
    if message.chat.type == ChatType.PRIVATE:
        await message.answer("❕️ Команды суда работают только внутри группы.")
        return
    settings = db.settings(message.chat.id)
    if not settings:
        if message.from_user and await is_admin(message, message.from_user.id):
            db.save_settings(message.chat.id, message.from_user.id, message.chat.id, message.chat.title or "")
            settings = db.settings(message.chat.id)
        else:
            await message.answer("❕️ Владелец ещё не настроил чат для судов. Администратор должен выполнить /setup.")
            return
    roles = await parse_roles(message)
    is_admin_user = message.from_user and await is_admin(message, message.from_user.id)
    if is_admin_user and not re.search(r"истец\s*:", message.text or "", re.IGNORECASE):
        await state.set_state(CaseStates.waiting_for_roles)
        await message.answer(
            "❔️ <b>Кто в этом суде?</b>\n\n"
            "Пример:\n"
            "<code>Истец: [упоминание]; Ответчик: [упоминание]; "
            "Свидетель: [упоминание]; Судья: [упоминание]</code>\n\n"
            "Свидетелей может быть максимум пять. Для запуска достаточно Истца и Ответчика.",
        )
        return
    if is_admin_user and ("истец" in (message.text or "").lower() or "ответчик" in (message.text or "").lower()):
        if not roles["plaintiff_id"] or not roles["defendant_id"]:
            await message.answer("❔️ Нужны как минимум Истец и Ответчик. Используйте Telegram-упоминания или ответьте на сообщения пользователей.")
            return
        case_id = db.create_case({
            **roles,
            "judge_id": roles["judge_id"] or settings["owner_id"],
            "source_chat_id": message.chat.id,
            "court_chat_id": settings["court_chat_id"],
            "complainant_id": message.from_user.id,
            "state": "pending",
        })
        await message.answer(f"📨 Дело №{case_id} зарегистрировано.\nОжидайте подтверждения владельца.", reply_markup=role_markup(case_id))
        return
    target = roles["defendant_id"]
    if not target:
        await message.answer("❔️ Чтобы вызвать участника, ответьте командой на его сообщение или используйте Telegram-упоминание.")
        return
    case_id = db.create_case({
        "source_chat_id": message.chat.id,
        "court_chat_id": settings["court_chat_id"],
        "plaintiff_id": message.from_user.id,
        "defendant_id": target,
        "judge_id": settings["owner_id"],
        "complainant_id": message.from_user.id,
    })
    await message.answer(
        f"‼️\n{display(message.from_user)} вызывает в суд участника.\n"
        f"Ответчик: <a href='tg://user?id={target}'>участник</a>\n‼️",
        reply_markup=role_markup(case_id),
    )
    owner = await owner_id(message.bot, message.chat.id)
    if owner:
        await message.answer(f"✔️ Вызван <a href='tg://user?id={owner}'>Владелец</a> группы.\nОжидайте создания суда.\n✔️")


@router.callback_query(F.data.startswith("create:"))
async def create_case_callback(callback: CallbackQuery) -> None:
    case_id = int(callback.data.split(":")[1])
    case = db.case(case_id)
    if not case or not callback.message or not await is_owner(callback.message):
        await callback.answer("🔒 Только владелец может создать суд.", show_alert=True)
        return
    if not case["plaintiff_id"] or not case["defendant_id"]:
        await callback.answer("Нужны истец и ответчик.", show_alert=True)
        return
    db.update_case(case_id, state="ready")
    await callback.message.answer(
        f"🗃 <b>Суд №{case_id} создаётся.</b>\n"
        "При необходимости добавьте до пяти свидетелей в следующем сообщении:\n"
        "Свидетель: [Telegram-упоминание]; Свидетель: [Telegram-упоминание]\n\n"
        "Если свидетелей нет, отправьте «без свидетелей».",
    )
    await callback.answer("Суд создан")


@router.message(F.text.regexp(r"(?i)^(без свидетелей|свидетел)"))
async def witnesses(message: Message) -> None:
    if not message.from_user or not await is_owner(message):
        return
    case = db.active_case(message.chat.id)
    if not case:
        return
    roles = await parse_roles(message)
    witness_ids = roles.get("witnesses", [])[:5]
    db.update_case(case["id"], witnesses=witness_ids, state="active", turn="plaintiff")
    court_id = case["court_chat_id"] or message.chat.id
    await begin_court(message.bot, int(case["id"]), court_id)


@router.message(CaseStates.waiting_for_roles)
async def receive_roles(message: Message, state: FSMContext) -> None:
    if not message.from_user or not await is_admin(message, message.from_user.id):
        return
    roles = await parse_roles(message)
    if not roles["plaintiff_id"] or not roles["defendant_id"]:
        await message.answer("❕️ Не хватает Истца или Ответчика. Используйте Telegram-упоминания или ответьте на сообщения участников.")
        return
    settings = db.settings(message.chat.id)
    if not settings:
        await message.answer("❕️ Сначала настройте чат командой /setup.")
        await state.clear()
        return
    case_id = db.create_case({
        **roles,
        "source_chat_id": message.chat.id,
        "court_chat_id": settings["court_chat_id"],
        "complainant_id": message.from_user.id,
        "state": "ready",
    })
    await state.clear()
    await message.answer(
        f"🗃 <b>Дело №{case_id} подготовлено.</b>\n"
        "Добавьте до пяти свидетелей следующим сообщением или отправьте «без свидетелей».",
    )


async def begin_court(bot: Bot, case_id: int, court_id: int) -> None:
    case = db.case(case_id)
    if not case:
        return
    await bot.send_message(
        court_id,
        f"⚖️ <b>Суд №{case_id} начинается.</b>\n\n"
        f"Истец: <a href='tg://user?id={case['plaintiff_id']}'>участник</a>\n"
        f"Ответчик: <a href='tg://user?id={case['defendant_id']}'>участник</a>\n"
        "Судья может говорить в любой момент. Выступления истца и ответчика проходят по очереди.",
        reply_markup=judge_markup(case_id),
    )
    await apply_permissions(bot, case, "plaintiff")


async def apply_permissions(bot: Bot, case: Any, turn: str) -> None:
    chat_id = case["court_chat_id"]
    if not chat_id:
        return
    owner = await owner_id(bot, chat_id)
    participants = [case["plaintiff_id"], case["defendant_id"], case["judge_id"], *(__import__("json").loads(case["witnesses"]))]
    for user_id in participants:
        if not user_id or user_id in {owner, case["judge_id"]}:
            continue
        can_speak = (turn == "plaintiff" and user_id == case["plaintiff_id"]) or (turn == "defendant" and user_id == case["defendant_id"])
        try:
            await bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(can_send_messages=can_speak))
        except Exception:
            logging.warning("Could not change permissions for %s", user_id)


@router.callback_query(F.data.startswith("stop:"))
async def stop_case(callback: CallbackQuery) -> None:
    await finish_control(callback, "stopped")


@router.callback_query(F.data.startswith("deliberate:"))
async def deliberate(callback: CallbackQuery) -> None:
    case_id = int(callback.data.split(":")[1])
    case = db.case(case_id)
    if not case or not callback.from_user or callback.from_user.id != case["judge_id"]:
        await callback.answer("🔒 Только назначенный судья может управлять судом.", show_alert=True)
        return
    db.update_case(case_id, state="deliberation")
    owner = await owner_id(callback.bot, int(case["court_chat_id"]))
    await apply_permissions(callback.bot, case, "none")
    await callback.message.answer("🔒 Суд удаляется в совещательную комнату для принятия решения.\nПисать может только владелец группы.")
    await callback.message.answer("Судья, когда будете готовы, нажмите кнопку ниже.", reply_markup=verdict_markup(case_id))
    await callback.answer()


async def finish_control(callback: CallbackQuery, state: str) -> None:
    case_id = int(callback.data.split(":")[1])
    case = db.case(case_id)
    if not case or not callback.from_user or callback.from_user.id != case["judge_id"]:
        await callback.answer("🔒 Только судья может остановить суд.", show_alert=True)
        return
    db.update_case(case_id, state=state)
    await callback.message.answer("🛡 Суд остановлен. Дело закрыто без вынесения решения.")
    await callback.answer()


@router.callback_query(F.data.startswith("verdict_menu:"))
async def verdict_menu(callback: CallbackQuery) -> None:
    case_id = int(callback.data.split(":")[1])
    case = db.case(case_id)
    if not case or callback.from_user.id != case["judge_id"]:
        await callback.answer("🔒 Только судья может сказать итог.", show_alert=True)
        return
    await callback.message.answer("⚖️ Выберите итог суда:", reply_markup=verdict_choices(case_id))
    await callback.answer()


@router.callback_query(F.data.startswith("verdict:"))
async def verdict(callback: CallbackQuery) -> None:
    _, raw_id, result = callback.data.split(":")
    case_id = int(raw_id)
    case = db.case(case_id)
    if not case or callback.from_user.id != case["judge_id"]:
        await callback.answer("🔒 Только судья может вынести решение.", show_alert=True)
        return
    labels = {"plaintiff": "Истец", "defendant": "Ответчик", "draw": "Ничья"}
    db.update_case(case_id, state="finished")
    await callback.message.answer(
        f"⚖️ <b>Решение по делу №{case_id}</b>\n\n"
        f"Итог: <b>{labels[result]}</b>\n"
        "Судья может самостоятельно объявить наказание в группе.\n\n"
        "🗑 После завершения владелец может удалить историю и участников.",
        reply_markup=cleanup_markup(case_id),
    )
    await callback.answer("Решение сохранено")


@router.callback_query(F.data.startswith("cleanup:"))
async def cleanup(callback: CallbackQuery) -> None:
    case_id = int(callback.data.split(":")[1])
    case = db.case(case_id)
    if not case or not callback.message or not await is_owner(callback.message):
        await callback.answer("🔒 Только владелец может очистить дело.", show_alert=True)
        return
    chat_id = int(case["court_chat_id"] or callback.message.chat.id)
    participant_ids = [case["plaintiff_id"], case["defendant_id"], case["judge_id"], *(__import__("json").loads(case["witnesses"]))]
    for user_id in participant_ids:
        if not user_id or user_id == (await owner_id(callback.bot, chat_id)):
            continue
        try:
            member = await callback.bot.get_chat_member(chat_id, user_id)
            if member.status in {ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED}:
                await callback.bot.ban_chat_member(chat_id, user_id)
                await callback.bot.unban_chat_member(chat_id, user_id)
        except Exception:
            logging.warning("Could not remove participant %s", user_id)
    db.delete_case(case_id)
    await callback.message.answer("✔️ Участники удалены из дела, а запись суда очищена. Историю сообщений Telegram может удалить только бот с соответствующими правами.")
    await callback.answer("Дело очищено")


@router.message(F.new_chat_members)
async def new_members(message: Message) -> None:
    await message.answer("⚖️ Добро пожаловать в CaseSolve. Для настройки суда владелец группы может использовать /setup.")


async def facts_loop(bot: Bot) -> None:
    index = 0
    while True:
        await asyncio.sleep(7200)
        chats = db.connection.execute("SELECT court_chat_id FROM settings WHERE court_chat_id IS NOT NULL").fetchall()
        subject, fact = FACTS[index % len(FACTS)]
        index += 1
        for row in chats:
            try:
                await bot.send_message(int(row["court_chat_id"]), f"💼 <b>Интересный факт про {subject}!</b>\n\n°{html.escape(fact)} >:3°")
            except Exception:
                logging.exception("Could not send fact")


async def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    bot = Bot(token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(facts_loop(bot))
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())