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

# Строка подключения к базе данных
DATABASE_URL = "postgresql://bothost_db_b1f669c8b755:RNSCsFK4HEwJdhFwsbqV4ulP7C5nEqGimL3wKprZHFQ@node1.pghost.ru:15653/bothost_db_b1f669c8b755"

# ID пользователей, которым разрешено управлять ботом
ADMIN_IDS = [927642459998138418, 500965898476322817, 1426923576229101568]

VALID_ACTIONS = ['аресты', 'собеседования', 'поставки', 'взг', 'бизнесы', 'облавы', 'банки']

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
        self.tree.add_command(AddChannelCommand(self))
        self.tree.add_command(RemoveChannelCommand(self))
        self.tree.add_command(ListChannelsCommand(self))
        self.tree.add_command(DeleteReportCommand(self))

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
                    action_type TEXT NOT NULL,
                    content TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            ''')
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS monitored_channels (
                    guild_id BIGINT NOT NULL,
                    channel_id BIGINT NOT NULL,
                    action_type TEXT NOT NULL,
                    PRIMARY KEY (guild_id, channel_id, action_type)
                )
            ''')
        logger.info("Таблицы в PostgreSQL готовы")

bot = ReportBot()

# -------------------- Обработка сообщений --------------------
@bot.event
async def on_message(message):
    if message.author.bot:
        return

    async with bot.pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT action_type FROM monitored_channels WHERE guild_id = $1 AND channel_id = $2',
            message.guild.id, message.channel.id
        )

    if row:
        action_type = row['action_type']
        async with bot.pool.acquire() as conn:
            try:
                await conn.execute(
                    '''INSERT INTO reports
                       (guild_id, channel_id, message_id, author_id, author_name, action_type, content)
                       VALUES ($1, $2, $3, $4, $5, $6, $7)''',
                    message.guild.id, message.channel.id, message.id,
                    message.author.id, str(message.author), action_type, message.content
                )
                await message.add_reaction('✅')
                logger.info(f'Отчёт [{action_type}] от {message.author} сохранён.')
            except asyncpg.UniqueViolationError:
                pass

    await bot.process_commands(message)

# -------------------- Команда статистики --------------------
class StatsCommand(app_commands.Group):
    def __init__(self, bot_instance):
        super().__init__(name='stats', description='Статистика по действиям')
        self.bot = bot_instance

    @app_commands.command(name='день', description='За сегодня')
    @app_commands.describe(action='Тип действия')
    async def stats_day(self, interaction: discord.Interaction, action: str):
        await self._show_stats(interaction, action, period='day')

    @app_commands.command(name='неделя', description='За последние 7 дней')
    @app_commands.describe(action='Тип действия')
    async def stats_week(self, interaction: discord.Interaction, action: str):
        await self._show_stats(interaction, action, period='week')

    @app_commands.command(name='месяц', description='За последние 30 дней')
    @app_commands.describe(action='Тип действия')
    async def stats_month(self, interaction: discord.Interaction, action: str):
        await self._show_stats(interaction, action, period='month')

    @app_commands.command(name='период', description='За произвольный период')
    @app_commands.describe(
        action='Тип действия',
        start='Дата начала (ГГГГ-ММ-ДД)',
        end='Дата окончания (ГГГГ-ММ-ДД)'
    )
    async def stats_period(self, interaction: discord.Interaction, action: str, start: str, end: str):
        await self._show_stats(interaction, action, period='custom', start=start, end=end)

    async def _show_stats(self, interaction, action, period, start=None, end=None):
        action = action.lower()
        if action not in VALID_ACTIONS:
            await interaction.response.send_message(
                f'❌ Неизвестный тип. Доступные: {", ".join(VALID_ACTIONS)}',
                ephemeral=True
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
                    '❌ Неверный формат даты. Используйте ГГГГ-ММ-ДД.',
                    ephemeral=True
                )
                return

        async with self.bot.pool.acquire() as conn:
            count = await conn.fetchval(
                '''SELECT COUNT(*) FROM reports
                   WHERE guild_id = $1 AND action_type = $2
                   AND created_at >= $3 AND created_at <= $4''',
                interaction.guild_id, action, date_start, date_end
            )

        await interaction.response.send_message(
            f'📊 **{action.capitalize()}** с {date_start.strftime("%d.%m.%Y")} по {date_end.strftime("%d.%m.%Y")}: **{count}** шт.',
            ephemeral=False
        )

# -------------------- Управление каналами --------------------
class AddChannelCommand(app_commands.Group):
    def __init__(self, bot_instance):
        super().__init__(name='add_channel', description='Добавить канал для отслеживания')
        self.bot = bot_instance

    @app_commands.command(name='действие', description='Назначить канал для действия')
    @app_commands.describe(action='Тип действия', channel='Канал')
    @is_admin()
    async def add_channel(self, interaction: discord.Interaction, action: str, channel: discord.TextChannel):
        action = action.lower()
        if action not in VALID_ACTIONS:
            await interaction.response.send_message(
                f'❌ Неверный тип. Допустимые: {", ".join(VALID_ACTIONS)}',
                ephemeral=True
            )
            return

        async with self.bot.pool.acquire() as conn:
            await conn.execute(
                'INSERT INTO monitored_channels (guild_id, channel_id, action_type) VALUES ($1, $2, $3) ON CONFLICT DO NOTHING',
                interaction.guild_id, channel.id, action
            )
        await interaction.response.send_message(
            f'✅ Канал {channel.mention} теперь принимает отчёты типа **{action}**',
            ephemeral=True
        )

class RemoveChannelCommand(app_commands.Group):
    def __init__(self, bot_instance):
        super().__init__(name='remove_channel', description='Удалить канал из отслеживания')
        self.bot = bot_instance

    @app_commands.command(name='действие', description='Убрать канал для действия')
    @app_commands.describe(action='Тип действия', channel='Канал')
    @is_admin()
    async def remove_channel(self, interaction: discord.Interaction, action: str, channel: discord.TextChannel):
        action = action.lower()
        async with self.bot.pool.acquire() as conn:
            await conn.execute(
                'DELETE FROM monitored_channels WHERE guild_id = $1 AND channel_id = $2 AND action_type = $3',
                interaction.guild_id, channel.id, action
            )
        await interaction.response.send_message(
            f'✅ Канал {channel.mention} больше не отслеживается для **{action}**',
            ephemeral=True
        )

class ListChannelsCommand(app_commands.Group):
    def __init__(self, bot_instance):
        super().__init__(name='list_channels', description='Показать все отслеживаемые каналы')
        self.bot = bot_instance

    @app_commands.command(name='все', description='Список каналов и назначенных действий')
    @is_admin()
    async def list_channels(self, interaction: discord.Interaction):
        async with self.bot.pool.acquire() as conn:
            rows = await conn.fetch(
                'SELECT channel_id, action_type FROM monitored_channels WHERE guild_id = $1 ORDER BY action_type',
                interaction.guild_id
            )
        if not rows:
            await interaction.response.send_message('Нет отслеживаемых каналов.', ephemeral=True)
            return

        text = '**Отслеживаемые каналы:**\n'
        for r in rows:
            channel = interaction.guild.get_channel(r['channel_id'])
            ch_name = channel.mention if channel else f'удалённый канал ({r["channel_id"]})'
            text += f'{r["action_type"]}: {ch_name}\n'
        await interaction.response.send_message(text, ephemeral=True)

class DeleteReportCommand(app_commands.Group):
    def __init__(self, bot_instance):
        super().__init__(name='delete_report', description='Удалить ошибочный отчёт')
        self.bot = bot_instance

    @app_commands.command(name='по_id', description='Удалить отчёт по ID сообщения')
    @app_commands.describe(message_id='ID сообщения с отчётом')
    @is_admin()
    async def delete_report(self, interaction: discord.Interaction, message_id: str):
        try:
            msg_id = int(message_id)
        except ValueError:
            await interaction.response.send_message(
                '❌ Неверный ID. Скопируйте числовой ID сообщения.',
                ephemeral=True
            )
            return

        async with self.bot.pool.acquire() as conn:
            result = await conn.execute(
                'DELETE FROM reports WHERE guild_id = $1 AND message_id = $2',
                interaction.guild_id, msg_id
            )

        if 'DELETE 0' in result:
            await interaction.response.send_message(
                '❌ Отчёт с таким ID не найден.',
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f'✅ Отчёт с ID {msg_id} удалён из статистики.',
                ephemeral=True
            )

# -------------------- Запуск --------------------
if __name__ == '__main__':
    bot.run(TOKEN)
