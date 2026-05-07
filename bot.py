import os
import asyncio
import logging
from datetime import datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands
import asyncpg

# -------------------- Конфигурация --------------------
TOKEN = os.getenv('DISCORD_TOKEN')
if not TOKEN:
    raise RuntimeError("❌ DISCORD_TOKEN не установлен!")

DATABASE_URL = "postgresql://bothost_db_b1f669c8b755:RNSCsFK4HEwJdhFwsbqV4ulP7C5nEqGimL3wKprZHFQ@node1.pghost.ru:15653/bothost_db_b1f669c8b755"

# ID пользователей, которым разрешены админ-команды
ADMIN_IDS = [927642459998138418, 500965898476322817, 1426923576229101568]

VALID_ACTIONS = ['аресты', 'собеседования', 'поставки', 'взг', 'бизнесы', 'облавы', 'штрафы']

# ID серверов
FSB_SERVER = 768174465745813531
MVD_SERVER = 767392766606049330

# -------------------- КАНАЛЫ --------------------
CHANNELS_CONFIG = {
    # ФСБ
    768174465745813531: {
        1180251537734377513: {"faction": "ФСБ", "action": "аресты"},
        1405961555904041021: {"faction": "ФСБ", "action": "собеседования"},
        1444587738446827570: {"faction": "ФСБ", "action": "поставки"},
        1444587863890202754: {"faction": "ФСБ", "action": "взг"},
        1474465615073775791: {"faction": "ФСБ", "action": "бизнесы"},
        1444587786572402728: {"faction": "ФСБ", "action": "облавы"},
    },
    # МВД
    767392766606049330: {
        833422076596715581:  {"faction": "МВД", "action": "аресты"},
        1175871168947949658: {"faction": "МВД", "action": "собеседования"},
        1266104348455207014: {"faction": "МВД", "action": "штрафы"},
        1204728768912949249: {"faction": "МВД", "action": "поставки"},
        1204730709990703145: {"faction": "МВД", "action": "бизнесы"},
        1472542670659518635: {"faction": "МВД", "action": "облавы"},
    },
}

FACTIONS = ['МВД', 'ФСБ']

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# -------------------- Проверка на админа --------------------
def is_admin():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.user.id not in ADMIN_IDS:
            await interaction.response.send_message(
                "❌ У вас нет прав для использования этой команды.",
                ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)

# -------------------- Инициализация бота --------------------
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True

class ReportBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)
        self.pool = None

    async def setup_hook(self):
        self.pool = await asyncpg.create_pool(DATABASE_URL, min_size=2, max_size=10)
        logger.info("Подключение к PostgreSQL установлено")

        await self.init_db()

        self.tree.add_command(StatsCommand(self))
        self.tree.add_command(DeleteReportCommand(self))
        self.tree.add_command(ListChannelsCommand(self))

        await self.tree.sync()
        logger.info("Команды синхронизированы")

    async def init_db(self):
        async with self.pool.acquire() as conn:
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS reports (
                    id SERIAL PRIMARY KEY,
                    guild_id BIGINT NOT NULL,
                    channel_id BIGINT NOT NULL,
                    message_id BIGINT NOT NULL UNIQUE,
                    author_id BIGINT NOT NULL,
                    author_name TEXT NOT NULL,
                    faction TEXT NOT NULL DEFAULT 'Без фракции',
                    action_type TEXT NOT NULL,
                    content TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            ''')
        logger.info("Таблицы в PostgreSQL готовы")

bot = ReportBot()

# -------------------- Обработка сообщений --------------------
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    guild_id = message.guild.id
    channel_id = message.channel.id

    # Проверяем, есть ли этот сервер и канал в конфиге
    if guild_id in CHANNELS_CONFIG and channel_id in CHANNELS_CONFIG[guild_id]:
        config = CHANNELS_CONFIG[guild_id][channel_id]
        faction = config["faction"]
        action_type = config["action"]

        async with bot.pool.acquire() as conn:
            try:
                await conn.execute(
                    '''INSERT INTO reports
                       (guild_id, channel_id, message_id, author_id, author_name, faction, action_type, content)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8)''',
                    guild_id, channel_id, message.id,
                    message.author.id, str(message.author), faction, action_type, message.content
                )
                await message.add_reaction('✅')
                logger.info(f'[{faction}] {action_type} от {message.author} сохранён.')
            except asyncpg.UniqueViolationError:
                pass

    await bot.process_commands(message)

# -------------------- Команда статистики --------------------
class StatsCommand(app_commands.Group):
    def __init__(self, bot_instance):
        super().__init__(name='stats', description='Статистика по действиям')
        self.bot = bot_instance

    @app_commands.command(name='день', description='За сегодня')
    @app_commands.describe(action='Тип действия', faction='Фракция (пусто — все)')
    @app_commands.choices(
        action=[app_commands.Choice(name=a, value=a) for a in VALID_ACTIONS],
        faction=[app_commands.Choice(name=f, value=f) for f in FACTIONS]
    )
    async def stats_day(self, interaction: discord.Interaction, action: str, faction: str = None):
        await self._show_stats(interaction, action, period='day', faction=faction)

    @app_commands.command(name='неделя', description='За 7 дней')
    @app_commands.describe(action='Тип действия', faction='Фракция (пусто — все)')
    @app_commands.choices(
        action=[app_commands.Choice(name=a, value=a) for a in VALID_ACTIONS],
        faction=[app_commands.Choice(name=f, value=f) for f in FACTIONS]
    )
    async def stats_week(self, interaction: discord.Interaction, action: str, faction: str = None):
        await self._show_stats(interaction, action, period='week', faction=faction)

    @app_commands.command(name='месяц', description='За 30 дней')
    @app_commands.describe(action='Тип действия', faction='Фракция (пусто — все)')
    @app_commands.choices(
        action=[app_commands.Choice(name=a, value=a) for a in VALID_ACTIONS],
        faction=[app_commands.Choice(name=f, value=f) for f in FACTIONS]
    )
    async def stats_month(self, interaction: discord.Interaction, action: str, faction: str = None):
        await self._show_stats(interaction, action, period='month', faction=faction)

    @app_commands.command(name='период', description='За произвольный период')
    @app_commands.describe(
        action='Тип действия',
        start='Дата начала (ГГГГ-ММ-ДД)',
        end='Дата окончания (ГГГГ-ММ-ДД)',
        faction='Фракция (пусто — все)'
    )
    @app_commands.choices(
        action=[app_commands.Choice(name=a, value=a) for a in VALID_ACTIONS],
        faction=[app_commands.Choice(name=f, value=f) for f in FACTIONS]
    )
    async def stats_period(self, interaction: discord.Interaction, action: str, start: str, end: str, faction: str = None):
        await self._show_stats(interaction, action, period='custom', start=start, end=end, faction=faction)

    async def _show_stats(self, interaction, action, period, start=None, end=None, faction=None):
        action = action.lower()
        if action not in VALID_ACTIONS:
            await interaction.response.send_message(
                f'❌ Неизвестный тип. Доступные: {", ".join(VALID_ACTIONS)}', ephemeral=True
            )
            return

        now = datetime.utcnow()
        if period == 'day':
            date_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            date_end = now
        elif period == 'week':
            date_end = now
            date_start = now - timedelta(days=7)
        elif period == 'month':
            date_end = now
            date_start = now - timedelta(days=30)
        elif period == 'custom':
            try:
                date_start = datetime.strptime(start, '%Y-%m-%d')
                date_end = datetime.strptime(end, '%Y-%m-%d') + timedelta(days=1)
            except ValueError:
                await interaction.response.send_message(
                    '❌ Неверный формат даты. Используйте ГГГГ-ММ-ДД.', ephemeral=True
                )
                return

        # Запрос
        if faction:
            query = '''SELECT COUNT(*) FROM reports
                       WHERE guild_id = $1 AND action_type = $2 AND faction = $3
                       AND created_at >= $4 AND created_at <= $5'''
            params = (interaction.guild_id, action, faction, date_start, date_end)
        else:
            query = '''SELECT COUNT(*) FROM reports
                       WHERE guild_id = $1 AND action_type = $2
                       AND created_at >= $3 AND created_at <= $4'''
            params = (interaction.guild_id, action, date_start, date_end)

        async with self.bot.pool.acquire() as conn:
            count = await conn.fetchval(query, *params)

        faction_text = f" [{faction}]" if faction else " (все)"
        await interaction.response.send_message(
            f'📊 **{action.capitalize()}**{faction_text}: **{count}** шт.\n'
            f'📅 {date_start.strftime("%d.%m.%Y")} — {date_end.strftime("%d.%m.%Y")}',
            ephemeral=False
        )

# -------------------- Показать каналы --------------------
class ListChannelsCommand(app_commands.Group):
    def __init__(self, bot_instance):
        super().__init__(name='list_channels', description='Показать все каналы из конфига')
        self.bot = bot_instance

    @app_commands.command(name='все', description='Список настроенных каналов')
    @is_admin()
    async def list_channels(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        if guild_id not in CHANNELS_CONFIG:
            await interaction.response.send_message('Для этого сервера нет настроенных каналов.', ephemeral=True)
            return

        text = '**Настроенные каналы:**\n'
        for ch_id, cfg in CHANNELS_CONFIG[guild_id].items():
            channel = interaction.guild.get_channel(ch_id)
            ch_name = channel.mention if channel else f'❌ {ch_id}'
            text += f'[{cfg["faction"]}] {cfg["action"]}: {ch_name}\n'
        await interaction.response.send_message(text, ephemeral=True)

# -------------------- Удаление отчёта --------------------
class DeleteReportCommand(app_commands.Group):
    def __init__(self, bot_instance):
        super().__init__(name='delete_report', description='Удалить ошибочный отчёт')
        self.bot = bot_instance

    @app_commands.command(name='по_id', description='Удалить отчёт по ID сообщения')
    @app_commands.describe(message_id='ID сообщения')
    @is_admin()
    async def delete_report(self, interaction: discord.Interaction, message_id: str):
        try:
            msg_id = int(message_id)
        except ValueError:
            await interaction.response.send_message('❌ Неверный ID.', ephemeral=True)
            return

        async with self.bot.pool.acquire() as conn:
            result = await conn.execute(
                'DELETE FROM reports WHERE guild_id = $1 AND message_id = $2',
                interaction.guild_id, msg_id
            )

        if 'DELETE 0' in result:
            await interaction.response.send_message('❌ Отчёт с таким ID не найден.', ephemeral=True)
        else:
            await interaction.response.send_message(f'✅ Отчёт {msg_id} удалён.', ephemeral=True)

# -------------------- Запуск --------------------
if __name__ == '__main__':
    bot.run(TOKEN)
