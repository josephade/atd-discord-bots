"""Anonymous clock card shared by the presenter bots."""
import discord
import math


def active_duration(seconds):
    minutes,seconds=divmod(max(0,math.ceil(seconds)),60)
    return f'{minutes}m {seconds}s'


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
        if not data.get('timers_off') and current.get('deadline') is not None:
            if data.get('window_closed') and 'paused_seconds' in current:
                timing=(f"Timer paused until **10:00 AM ET**. **Active time saved: {active_duration(current['paused_seconds'])}.**\n"
                        f"Pick deadline: **{current['deadline_et']}**.")
            elif current.get('overnight_pause'):
                timing=(f"**Active timer allowance: {current['timer_minutes']:g} minutes.**\n"
                        '**Overnight pause: midnight to 10:00 AM ET** (no timer time used).\n'
                        f"Pick deadline after the pause: **{current['deadline_et']}**.")
        text = (f'{current["logo"]} **is on the clock!**\n\n**Round {current["round"]} of {data["rounds"]}{pick_note}**\n'
                f'{timing}\n'
                'Submit your pick/list privately to ATD Draft Theme Bot.')
    if data.get('window_notice'):
        text += '\n\n'+data['window_notice']['text']
    return discord.Embed(title='Anonymous Partner Draft' if data['mode'] == 'partner' else 'Anonymous Budget Draft',
                         description=text, color=0x3498DB)
