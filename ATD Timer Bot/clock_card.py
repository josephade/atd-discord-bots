"""Anonymous clock card shared by the presenter bots."""
import discord


def clock_embed(data):
    current = data.get('current')
    if data.get('finished'):
        text = 'All picks and makeups are finished. The commissioner can reveal the draft.'
    elif data.get('waiting_for_flux'):
        upcoming = data['waiting_for_flux']
        text = (f'**Round {upcoming - 1} finished.**\n\n'
                'Waiting for a commissioner to run **ATD Flux Bot** (`!flux 3`, `!flux 4` or `!flux 5`).\n'
                f'Round {upcoming} will open after Flux completes. **No team is on the clock.**')
    elif data['status'] != 'active':
        text = f'Draft **{data["status"]}**. Waiting for Theme Bot.'
    elif data.get('transitioning'):
        text = 'Preparing the next team’s turn.'
    elif not current:
        text = 'Scheduled turns finished. Waiting for outstanding makeup picks.'
    else:
        pick_note = f' · Pick {current["pick"]}' if current.get('pick') else ''
        timing = ('Timers are **off**. Picks and next-team DMs continue.' if data.get('timers_off') else
                  'Timer hours are closed. Countdown resumes at **10am ET**; picks can still be submitted.' if data.get('window_closed') else
                  'Restoring the current clock.' if current.get('deadline') is None else
                  f'Turn expires <t:{int(current["deadline"])}:R>.')
        text = (f'{current["logo"]} **is on the clock!**\n\n**Round {current["round"]} of {data["rounds"]}{pick_note}**\n'
                f'{timing}\n'
                'Submit your pick/list privately to ATD Draft Theme Bot.')
    if data.get('window_notice'):
        text += '\n\n'+data['window_notice']['text']
    return discord.Embed(title='Anonymous Partner Draft' if data['mode'] == 'partner' else 'Anonymous Budget Draft',
                         description=text, color=0x3498DB)
