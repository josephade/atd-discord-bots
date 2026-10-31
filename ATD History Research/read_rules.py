import asyncio,json
from pathlib import Path
import discord
from dotenv import dotenv_values

async def main():
    base=Path(__file__).resolve().parent
    token=dotenv_values(base.parent/'ATD Voting Bot/.env',encoding='utf-8-sig')['DISCORD_TOKEN']
    async with discord.Client(intents=discord.Intents.default()) as client:
        await client.login(token)
        channel=await client.fetch_channel(934052115821764718)
        messages=[]
        async for m in channel.history(limit=None,oldest_first=True):
            messages.append({'id':str(m.id),'date':m.created_at.isoformat(),'content':m.content,
                'url':m.jump_url,'embeds':[e.to_dict() for e in m.embeds],
                'attachments':[{'name':a.filename,'url':a.url,'type':a.content_type} for a in m.attachments]})
        (base/'raw/rules-channel.json').write_text(json.dumps({'name':channel.name,'messages':messages},indent=2,ensure_ascii=False),encoding='utf-8')
        print(f'Read {len(messages)} messages from #{channel.name}; no messages sent.')
asyncio.run(main())
