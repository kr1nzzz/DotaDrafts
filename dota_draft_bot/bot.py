import os
import asyncio
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

from engine_static import StaticDraftEngine


# ---------------------------
# Draft state per user
# ---------------------------

@dataclass
class DraftState:
    ally: List[int] = field(default_factory=list)
    enemy: List[int] = field(default_factory=list)
    banned: List[int] = field(default_factory=list)


sessions: Dict[int, DraftState] = {}


def st(uid: int) -> DraftState:
    if uid not in sessions:
        sessions[uid] = DraftState()
    return sessions[uid]


# ---------------------------
# UI state per user
# ---------------------------

@dataclass
class UIState:
    mode: str = "ally"   # ally | enemy | ban
    page: int = 0
    phase: int = 1       # 1..3
    pos: int = 0         # 0=Any, else 1..5


ui_states: Dict[int, UIState] = {}


def ui(uid: int) -> UIState:
    if uid not in ui_states:
        ui_states[uid] = UIState()
    return ui_states[uid]


# ---------------------------
# Search state
# ---------------------------

pending_search: Dict[int, bool] = {}


# ---------------------------
# Helpers
# ---------------------------

def fmt_state(engine: StaticDraftEngine, s: DraftState) -> str:
    def names(ids: List[int]) -> str:
        if not ids:
            return "—"
        return ", ".join(engine.heroes[i].name for i in ids if i in engine.heroes)

    return (
        f"🟦 Союзники: {names(s.ally)}\n"
        f"🟥 Враги: {names(s.enemy)}\n"
        f"🚫 Баны: {names(s.banned)}"
    )


def add_pick(s: DraftState, mode: str, hero_id: int) -> bool:
    # prevent duplicates
    if hero_id in s.ally or hero_id in s.enemy or hero_id in s.banned:
        return False

    if mode == "ally":
        s.ally.append(hero_id)
    elif mode == "enemy":
        s.enemy.append(hero_id)
    else:
        s.banned.append(hero_id)
    return True


def undo_last(s: DraftState) -> bool:
    # undo in this order (you can change):
    for lst in (s.enemy, s.ally, s.banned):
        if lst:
            lst.pop()
            return True
    return False


def get_recs(engine: StaticDraftEngine, uid: int, top_n: int = 10):
    s = st(uid)
    u = ui(uid)
    pos = None if u.pos == 0 else u.pos
    return engine.recommend(s.ally, s.enemy, s.banned, top_n=top_n, phase=u.phase, pos=pos)


# ---------------------------
# Keyboard builders
# ---------------------------

def build_main_keyboard(engine: StaticDraftEngine, uid: int) -> Tuple[str, InlineKeyboardBuilder]:
    s = st(uid)
    u = ui(uid)

    heroes_sorted = sorted(engine.heroes.values(), key=lambda h: h.name.lower())

    # Pagination
    per_page = 24  # 8 rows x 3 columns
    total_pages = max(1, (len(heroes_sorted) + per_page - 1) // per_page)
    u.page = max(0, min(u.page, total_pages - 1))

    start = u.page * per_page
    end = start + per_page
    page_slice = heroes_sorted[start:end]

    mode_label = {"ally": "🟦 Союзник", "enemy": "🟥 Враг", "ban": "🚫 Бан"}.get(u.mode, "🟦 Союзник")
    pos_label = "Any" if u.pos == 0 else f"pos{u.pos}"

    header = (
        f"{mode_label} — выбирай героя кнопкой\n"
        f"⏱ Phase: P{u.phase} | 🎯 Pos: {pos_label}\n"
        f"📄 Страница {u.page + 1}/{total_pages}\n\n"
        f"{fmt_state(engine, s)}"
    )

    kb = InlineKeyboardBuilder()

    # Mode row
    kb.button(text="🟦 Союзник", callback_data="mode:ally")
    kb.button(text="🟥 Враг", callback_data="mode:enemy")
    kb.button(text="🚫 Бан", callback_data="mode:ban")
    kb.adjust(3)

    # Phase row
    kb.button(text="⏱ P1", callback_data="phase:1")
    kb.button(text="⏱ P2", callback_data="phase:2")
    kb.button(text="⏱ P3", callback_data="phase:3")
    kb.adjust(3)

    # Pos row
    kb.button(text="🎯 Any", callback_data="pos:0")
    kb.button(text="1", callback_data="pos:1")
    kb.button(text="2", callback_data="pos:2")
    kb.button(text="3", callback_data="pos:3")
    kb.button(text="4", callback_data="pos:4")
    kb.button(text="5", callback_data="pos:5")
    kb.adjust(6)

    # Hero buttons
    picked = set(s.ally) | set(s.enemy) | set(s.banned)
    for hero in page_slice:
        label = hero.name if hero.id not in picked else f"✅ {hero.name}"
        kb.button(text=label, callback_data=f"pick:{hero.id}")
    kb.adjust(3)

    # Navigation + search
    kb.button(text="⬅️", callback_data="nav:prev")
    kb.button(text="➡️", callback_data="nav:next")
    kb.button(text="🔎 Поиск", callback_data="action:search")
    kb.adjust(3)

    # Actions
    kb.button(text="📋 Драфт", callback_data="action:draft")
    kb.button(text="🧠 Рекомендовать", callback_data="action:rec")
    kb.adjust(2)

    kb.button(text="↩️ Undo", callback_data="action:undo")
    kb.button(text="♻️ Сброс", callback_data="action:reset")
    kb.adjust(2)

    kb.button(text=f"ℹ️ {engine.patch_name}", callback_data="action:info")

    return header, kb


def build_search_results_keyboard(engine: StaticDraftEngine, uid: int, query: str) -> Tuple[str, InlineKeyboardBuilder]:
    s = st(uid)
    u = ui(uid)

    q = (query or "").strip().lower()
    heroes_sorted = sorted(engine.heroes.values(), key=lambda h: h.name.lower())

    matches = [h for h in heroes_sorted if q in h.name.lower()]
    matches = matches[:30]

    mode_label = {"ally": "🟦 Союзник", "enemy": "🟥 Враг", "ban": "🚫 Бан"}.get(u.mode, "🟦 Союзник")
    pos_label = "Any" if u.pos == 0 else f"pos{u.pos}"

    text = (
        f"{mode_label} — поиск: “{query}”\n"
        f"⏱ Phase: P{u.phase} | 🎯 Pos: {pos_label}\n"
        f"Нашёл: {len(matches)} (показываю до 30)\n\n"
        f"{fmt_state(engine, s)}"
    )

    kb = InlineKeyboardBuilder()

    # Mode row
    kb.button(text="🟦 Союзник", callback_data="mode:ally")
    kb.button(text="🟥 Враг", callback_data="mode:enemy")
    kb.button(text="🚫 Бан", callback_data="mode:ban")
    kb.adjust(3)

    # Phase row
    kb.button(text="⏱ P1", callback_data="phase:1")
    kb.button(text="⏱ P2", callback_data="phase:2")
    kb.button(text="⏱ P3", callback_data="phase:3")
    kb.adjust(3)

    # Pos row
    kb.button(text="🎯 Any", callback_data="pos:0")
    kb.button(text="1", callback_data="pos:1")
    kb.button(text="2", callback_data="pos:2")
    kb.button(text="3", callback_data="pos:3")
    kb.button(text="4", callback_data="pos:4")
    kb.button(text="5", callback_data="pos:5")
    kb.adjust(6)

    picked = set(s.ally) | set(s.enemy) | set(s.banned)
    for h in matches:
        label = h.name if h.id not in picked else f"✅ {h.name}"
        kb.button(text=label, callback_data=f"pick:{h.id}")
    kb.adjust(2)

    kb.button(text="⬅️ Назад к списку", callback_data="action:back")
    return text, kb


# ---------------------------
# Bot
# ---------------------------

async def main():
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("BOT_TOKEN is empty in .env")

    engine = StaticDraftEngine()

    bot = Bot(token)
    dp = Dispatcher()

    async def show_menu(message_or_cb, uid: int, edit: bool = False):
        text, kb = build_main_keyboard(engine, uid)
        markup = kb.as_markup()
        if isinstance(message_or_cb, Message):
            await message_or_cb.answer(text, reply_markup=markup)
        else:
            if edit:
                await message_or_cb.message.edit_text(text, reply_markup=markup)
            else:
                await message_or_cb.message.answer(text, reply_markup=markup)

    @dp.message(Command("start"))
    async def start(m: Message):
        await m.answer(
            "Меню с героями: кнопки + поиск + позиции + фазы.\n\n"
            "/menu — открыть меню\n"
            "/reset — сброс\n"
        )
        await show_menu(m, m.from_user.id)

    @dp.message(Command("menu"))
    async def menu(m: Message):
        await show_menu(m, m.from_user.id)

    @dp.message(Command("reset"))
    async def reset(m: Message):
        sessions[m.from_user.id] = DraftState()
        ui_states[m.from_user.id] = UIState()
        pending_search[m.from_user.id] = False
        await show_menu(m, m.from_user.id)

    # ---------------------------
    # Callbacks
    # ---------------------------

    @dp.callback_query(F.data.startswith("mode:"))
    async def cb_mode(c: CallbackQuery):
        u = ui(c.from_user.id)
        u.mode = c.data.split(":", 1)[1]
        await c.answer()
        await show_menu(c, c.from_user.id, edit=True)

    @dp.callback_query(F.data.startswith("phase:"))
    async def cb_phase(c: CallbackQuery):
        u = ui(c.from_user.id)
        u.phase = int(c.data.split(":", 1)[1])
        await c.answer()
        await show_menu(c, c.from_user.id, edit=True)

    @dp.callback_query(F.data.startswith("pos:"))
    async def cb_pos(c: CallbackQuery):
        u = ui(c.from_user.id)
        u.pos = int(c.data.split(":", 1)[1])  # 0=Any
        await c.answer()
        await show_menu(c, c.from_user.id, edit=True)

    @dp.callback_query(F.data.startswith("nav:"))
    async def cb_nav(c: CallbackQuery):
        u = ui(c.from_user.id)
        action = c.data.split(":", 1)[1]
        if action == "prev":
            u.page -= 1
        else:
            u.page += 1
        await c.answer()
        await show_menu(c, c.from_user.id, edit=True)

    @dp.callback_query(F.data.startswith("pick:"))
    async def cb_pick(c: CallbackQuery):
        s = st(c.from_user.id)
        u = ui(c.from_user.id)
        hero_id = int(c.data.split(":", 1)[1])
        ok = add_pick(s, u.mode, hero_id)
        await c.answer("Добавлено ✅" if ok else "Уже есть")
        await show_menu(c, c.from_user.id, edit=True)

    @dp.callback_query(F.data == "action:draft")
    async def cb_draft(c: CallbackQuery):
        await c.answer()
        await c.message.answer(fmt_state(engine, st(c.from_user.id)))

    @dp.callback_query(F.data == "action:rec")
    async def cb_rec(c: CallbackQuery):
        await c.answer()
        recs = get_recs(engine, c.from_user.id, top_n=10)
        if not recs:
            await c.message.answer("Некого рекомендовать.")
            return

        u = ui(c.from_user.id)
        pos_label = "Any" if u.pos == 0 else f"pos{u.pos}"

        lines = [f"🧠 Топ-10 (Phase P{u.phase}, Pos {pos_label}):"]
        for i, r in enumerate(recs, 1):
            lines.append(
                f"{i}. {r['name']} — {r['score']:.3f} "
                f"(meta {r['meta']:+.2f}, syn {r['syn']:+.2f}, cnt {r['cnt']:+.2f}, role {r['role']:+.2f})"
            )
        await c.message.answer("\n".join(lines))

    @dp.callback_query(F.data == "action:undo")
    async def cb_undo(c: CallbackQuery):
        ok = undo_last(st(c.from_user.id))
        await c.answer("Откатил ↩️" if ok else "Нечего откатывать")
        await show_menu(c, c.from_user.id, edit=True)

    @dp.callback_query(F.data == "action:reset")
    async def cb_reset(c: CallbackQuery):
        sessions[c.from_user.id] = DraftState()
        ui_states[c.from_user.id] = UIState()
        pending_search[c.from_user.id] = False
        await c.answer("Сбросил ♻️")
        await show_menu(c, c.from_user.id, edit=True)

    @dp.callback_query(F.data == "action:info")
    async def cb_info(c: CallbackQuery):
        await c.answer()
        await c.message.answer(
            f"ℹ️ Датасет: {engine.patch_name}\n"
            f"Героев: {len(engine.heroes)}\n"
            "Данные офлайн (static_patch.json)."
        )

    @dp.callback_query(F.data == "action:search")
    async def cb_search(c: CallbackQuery):
        pending_search[c.from_user.id] = True
        await c.answer()
        await c.message.answer("🔎 Напиши часть имени героя (пример: `spirit`, `shadow`, `mage`).", parse_mode="Markdown")

    @dp.callback_query(F.data == "action:back")
    async def cb_back(c: CallbackQuery):
        await c.answer()
        await show_menu(c, c.from_user.id, edit=True)

    # ---------------------------
    # Text messages (search)
    # ---------------------------

    @dp.message(F.text)
    async def on_text(m: Message):
        uid = m.from_user.id
        if pending_search.get(uid):
            pending_search[uid] = False
            query = m.text.strip()
            text, kb = build_search_results_keyboard(engine, uid, query)
            await m.answer(text, reply_markup=kb.as_markup())
            return

        await m.answer("Открой меню: /menu")

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())