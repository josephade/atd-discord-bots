"""Read the user-selected historical channel. Never posts messages."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import discord
from dotenv import dotenv_values

BASE = Path(__file__).resolve().parent
CHANNEL_ID = 1280197322516467733


async def main():
    values = dotenv_values(BASE.parent/'ATD Voting Bot'/'.env', encoding='utf-8-sig')
    token = (values.get('DISCORD_TOKEN') or '').strip()
    if not token:
        raise SystemExit('Voting bot token is missing; no request made.')
    client = discord.Client(intents=discord.Intents.default())
    async with client:
        await client.login(token)
        channel = await client.fetch_channel(CHANNEL_ID)
        messages = []
        async for message in channel.history(limit=None, oldest_first=True,
                after=datetime(2024, 1, 1, tzinfo=timezone.utc)):
            messages.append({
                'id': str(message.id), 'date': message.created_at.isoformat(),
                'author': str(message.author), 'author_name': message.author.display_name,
                'content': message.content, 'url': message.jump_url,
                'embeds': [e.to_dict() for e in message.embeds],
                'attachments': [{'name': a.filename, 'url': a.url, 'type': a.content_type}
                                for a in message.attachments],
                'reference': str(message.reference.message_id) if message.reference else None,
                'reactions': [{'emoji': str(r.emoji), 'count': r.count} for r in message.reactions],
            })
        target = BASE/'raw'/'historic-channel.json'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({'channel': str(channel), 'channel_id': str(CHANNEL_ID),
            'guild_id': str(channel.guild.id), 'messages': messages}, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'Read {len(messages)} messages from #{channel.name}.')
        if messages:
            print(f"Date range: {messages[0]['date']} to {messages[-1]['date']}")
        print('Saved local research copy; no Discord messages sent.')


if __name__ == '__main__':
    asyncio.run(main())
