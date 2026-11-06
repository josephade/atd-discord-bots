require('dotenv').config();
const { Client, GatewayIntentBits, AttachmentBuilder } = require('discord.js');
const { NBAData } = require('./data');
const { Renderer } = require('./render');
const { parse } = require('./commands');
const { suggestGame, suggestPlayer } = require('./suggestions');
const { PlayerCards, PAGE_IDS } = require('./cards');
const { gamesPayload, schedulePayload } = require('./games');
const { Boxscores } = require('./boxscores');
const { Injuries } = require('./injuries');
const { Salaries } = require('./salary');
const { Standings } = require('./standings');
const { Picks } = require('./picks');
const { createAccess } = require('./access');
const { helpPayload, HELP_ID } = require('./help');
const prefix = process.env.PREFIX || '!';
const canUse = createAccess();
const seconds = Math.max(30, Number(process.env.REFRESH_SECONDS) || 30);
const maxWatches = Math.max(1, Number(process.env.MAX_WATCHES) || 20);
const client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildMessages, GatewayIntentBits.MessageContent] });
const data = new NBAData(), renderer = new Renderer(), watches = new Map(), busy = new Set();

const payload = (model, png, watching = false) => ({
  content: `**${model.player.displayName}** · ${model.matchup} · ${model.status}${watching ? `\n↻ Refreshing every ${seconds}s · ${prefix}unwatch to stop` : ''}`,
  files: [new AttachmentBuilder(png, { name: 'nba-stats.png', description: `${model.player.displayName} NBA statistics` })],
  allowedMentions: { parse: [] },
});
const cards = new PlayerCards({ renderer, payload, prepareFaces: true });
const boxscores = new Boxscores({ data, renderer, timeZone: process.env.GAMES_TIME_ZONE || 'Europe/Dublin' });
const injuries = new Injuries({ data, renderer });
const salaries = new Salaries({ data, renderer });
const standings = new Standings({ data, renderer, timeZone: process.env.GAMES_TIME_ZONE || 'Europe/Dublin' });
const picks = new Picks({ data, renderer });
client.on('interactionCreate', async interaction => {
  if (!canUse({ guildId: interaction.guildId, channelId: interaction.channelId, parentId: interaction.channel?.parentId })) return;
  if (interaction.isButton() && interaction.customId.startsWith(HELP_ID)) {
    await interaction.update(helpPayload({ page: interaction.customId.slice(HELP_ID.length), prefix, seconds, avatar: client.user.displayAvatarURL() })).catch(console.error);
    return;
  }
  if ((interaction.isStringSelectMenu() || interaction.isButton()) && injuries.owns(interaction.customId)) {
    try { await injuries.handle(interaction); }
    catch (error) { console.error('Injury interaction:', error.message); }
    return;
  }
  if (interaction.isButton() && standings.owns(interaction.customId)) {
    try { await standings.handle(interaction); }
    catch (error) { console.error('Standings interaction:', error.message); }
    return;
  }
  if (interaction.isButton() && salaries.owns(interaction.customId)) {
    try { await salaries.handle(interaction); }
    catch (error) { console.error('Salary interaction:', error.message); }
    return;
  }
  if ((interaction.isStringSelectMenu() || interaction.isButton()) && boxscores.owns(interaction.customId)) {
    try { await boxscores.handle(interaction); }
    catch (error) { console.error('Boxscore interaction:', error.message); }
    return;
  }
  if (!interaction.isButton() || !Object.values(PAGE_IDS).includes(interaction.customId)) return;
  try { await cards.select(interaction); }
  catch (error) { console.error('Flip interaction:', error.message); }
});
client.once('clientReady', () => {
  console.log(`NBA Live ready as ${client.user.tag}; prefix ${prefix}`);
  void Promise.allSettled([data.players(), renderer.warm()]).then(results => {
    for (const result of results) if (result.status === 'rejected') console.error('Warm-up:', result.reason.message);
  });
});
client.on('messageCreate', async message => {
  if (message.author.bot || !message.guild || !message.content.startsWith(prefix)) return;
  if (!canUse({ guildId: message.guildId, channelId: message.channelId, parentId: message.channel.parentId })) return;
  const key = `${message.channelId}:${message.author.id}`;
  let command;
  try { command = parse(message.content.slice(prefix.length)); }
  catch (error) { await message.reply({ content: error.message, allowedMentions: { parse: [] } }).catch(console.error); return; }
  if (!command) return;
  if (command.command === 'picks') {
    if (busy.has(key)) return;
    busy.add(key);
    let reply;
    try {
      reply = await message.reply('Loading draft picks…');
      await picks.open(reply, command.query);
    } catch (error) {
      console.error('NBA picks:', error.message);
      const body = { content: `Could not load draft picks: ${error.message}`.slice(0, 1900), components: [], allowedMentions: { parse: [] } };
      if (reply) await reply.edit(body).catch(console.error);
      else await message.reply(body).catch(console.error);
    } finally { busy.delete(key); }
    return;
  }
  if (command.command === 'injury') {
    if (busy.has(key)) return;
    busy.add(key);
    let reply;
    try {
      reply = await message.reply('Loading the NBA injury report…');
      await injuries.open(reply, command.query);
    } catch (error) {
      console.error('NBA injury:', error.message);
      const body = { content: `Could not load injuries: ${error.message}`.slice(0, 1900), components: [], allowedMentions: { parse: [] } };
      if (reply) await reply.edit(body).catch(console.error);
      else await message.reply(body).catch(console.error);
    } finally { busy.delete(key); }
    return;
  }
  if (command.command === 'boxscores') {
    if (busy.has(key)) return;
    busy.add(key);
    let reply;
    try {
      reply = await message.reply('Loading NBA games…');
      await boxscores.open(reply, await data.games({ all: true, date: command.date }));
    } catch (error) {
      console.error('NBA boxscores:', error.message);
      const body = { content: `Could not load NBA boxscores: ${error.message}`.slice(0, 1900), components: [], allowedMentions: { parse: [] } };
      if (reply) await reply.edit(body).catch(console.error);
      else await message.reply(body).catch(console.error);
    } finally { busy.delete(key); }
    return;
  }
  if (command.command === 'standings') {
    if (busy.has(key)) return;
    busy.add(key);
    let reply;
    try {
      reply = await message.reply('Loading NBA standings…');
      await standings.open(reply);
    } catch (error) {
      console.error('NBA standings:', error.message);
      const body = { content: `Could not load standings: ${error.message}`.slice(0, 1900), components: [], allowedMentions: { parse: [] } };
      if (reply) await reply.edit(body).catch(console.error);
      else await message.reply(body).catch(console.error);
    } finally { busy.delete(key); }
    return;
  }
  if (command.command === 'salary') {
    if (busy.has(key)) return;
    busy.add(key);
    let reply;
    try {
      reply = await message.reply('Loading team salary…');
      await salaries.open(reply, command.query);
    } catch (error) {
      console.error('NBA salary:', error.message);
      const body = { content: `Could not load salary: ${error.message}`.slice(0, 1900), components: [], allowedMentions: { parse: [] } };
      if (reply) await reply.edit(body).catch(console.error);
      else await message.reply(body).catch(console.error);
    } finally { busy.delete(key); }
    return;
  }
  if (command.command === 'schedule') {
    if (busy.has(key)) return;
    busy.add(key);
    try {
      const schedule = { ...await data.schedule(command.query), timeZone: process.env.GAMES_TIME_ZONE || 'Europe/Dublin' };
      await message.reply(schedulePayload(schedule, await renderer.render(schedule)));
    }
    catch (error) {
      console.error('NBA schedule:', error.message);
      await message.reply({ content: `Could not load the schedule: ${error.message}`.slice(0, 1900), allowedMentions: { parse: [] } }).catch(console.error);
    } finally { busy.delete(key); }
    return;
  }
  if (command.command === 'games') {
    if (busy.has(key)) return;
    busy.add(key);
    try {
      const slate = { ...await data.games(), type: 'games', timeZone: process.env.GAMES_TIME_ZONE || 'Europe/Dublin' };
      await message.reply(gamesPayload(slate, await renderer.render(slate)));
    }
    catch (error) {
      console.error('NBA games:', error.message);
      await message.reply({ content: `Could not load NBA games: ${error.message}`.slice(0, 1900), allowedMentions: { parse: [] } }).catch(console.error);
    } finally { busy.delete(key); }
    return;
  }
  if (command.command === 'nbahelp') { await message.reply(helpPayload({ prefix, seconds, avatar: client.user.displayAvatarURL() })).catch(console.error); return; }
  if (command.command === 'unwatch') {
    const watch = watches.get(key);
    watches.delete(key);
    if (watch) cards.stopWatching(watch.message.id);
    if (watch) await watch.message.edit({ content: `${watch.content}\nWatch stopped.` }).catch(console.error);
    await message.reply(watch ? 'Stopped your live watch in this channel.' : 'You have no live watch in this channel.').catch(console.error);
    return;
  }
  if (busy.has(key)) return;
  busy.add(key);
  let reply;
  try {
    if (command.command === 'watch' && !watches.has(key) && watches.size >= maxWatches) throw new Error('All live watch slots are in use. Try !nba for a snapshot.');
    reply = await message.reply('Looking up NBA stats…');
    if (command.command === 'compare') {
      const players = [];
      for (const side of await data.comparePlayers(command.query)) {
        const player = side.player || await suggestPlayer({ reply, userId: message.author.id, query: side.query, suggestions: side.suggestions });
        if (!player) return;
        players.push(player);
      }
      if (players[0].id === players[1].id) throw new Error('Pick two different players to compare.');
      const model = await data.compare(players);
      await reply.edit({ content: '', components: [], allowedMentions: { parse: [] },
        files: [new AttachmentBuilder(await renderer.render(model), { name: 'nba-compare.png', description: `${players[0].displayName} vs ${players[1].displayName}` })] });
      return;
    }
    let player;
    try { player = await data.findPlayer(command.query); }
    catch (error) {
      if (!error.suggestions?.length) throw error;
      player = await suggestPlayer({ reply, userId: message.author.id, query: command.query, suggestions: error.suggestions });
      if (!player) return;
    }
    if (command.command === 'nba' && command.date) {
      const result = await data.datedGame(player, command.date);
      if (!result.model) {
        await suggestGame({ reply, userId: message.author.id, player, date: command.date, suggestion: result.suggestion, data, renderer, payload, showCard: (message, model) => cards.show(message, model) });
        return;
      }
      await cards.show(reply, result.model);
      return;
    }
    if (command.command === 'recent') {
      const recent = await data.lastGames(player);
      await reply.edit({ content: '', components: [], allowedMentions: { parse: [] },
        files: [new AttachmentBuilder(await renderer.render(recent), { name: 'nba-last10.png', description: `${player.displayName} last ${recent.games.length} games` })] });
      return;
    }
    const model = command.command === 'season' ? await data.season(player, command.year, command.playoffs) : await data.game(player, command.date, undefined, { includeUpcoming: command.command === 'watch' });
    const watching = command.command === 'watch' && ['pre', 'in'].includes(model.state);
    const body = await cards.show(reply, model, watching);
    if (command.command === 'watch') {
      const previous = watches.get(key);
      if (previous) cards.stopWatching(previous.message.id);
      if (previous) await previous.message.edit({ content: `${previous.content}\nWatch replaced by a newer request.` }).catch(console.error);
      watches.delete(key);
      if (watching) watches.set(key, { player, eventId: model.eventId, message: reply, started: Date.now(), content: body.content.split('\n')[0], errors: 0 });
    }
  } catch (error) {
    console.error(error);
    const content = `Could not load stats: ${error.message}`.slice(0, 1900);
    if (reply) await reply.edit({ content, components: [] }).catch(console.error);
    else await message.reply({ content, allowedMentions: { parse: [] } }).catch(console.error);
  } finally { busy.delete(key); }
});
let updating = false;
const timer = setInterval(async () => {
  if (updating) return;
  updating = true;
  try {
    for (const [key, watch] of watches) {
      if (watches.get(key) !== watch || busy.has(key)) continue;
      busy.add(key);
      try {
        if (Date.now() - watch.started > 8 * 3600000) {
          watches.delete(key);
          cards.stopWatching(watch.message.id);
          await watch.message.edit({ content: `${watch.content}\nWatch expired after 8 hours. Run ${prefix}watch again.` });
          continue;
        }
        const model = await data.game(watch.player, null, watch.eventId);
        const active = ['pre', 'in'].includes(model.state);
        if (watches.get(key) !== watch) continue;
        const body = await cards.show(watch.message, model, active, () => watches.get(key) === watch);
        if (!body) continue;
        watch.content = body.content.split('\n')[0]; watch.errors = 0;
        if (!active) watches.delete(key);
      } catch (error) {
        if (watches.get(key) !== watch) continue;
        console.error('Watch refresh:', error.message);
        watch.errors++;
        const stopped = watch.errors >= 5 || [10003, 10008, 50001, 50013].includes(error.code);
        if (stopped) { watches.delete(key); cards.stopWatching(watch.message.id); }
        await watch.message.edit({ content: `${watch.content}\n${stopped ? 'Watch stopped after repeated errors. Run the watch command again.' : 'Refresh delayed; showing the last successful update. Retrying.'}`, allowedMentions: { parse: [] } }).catch(() => {});
      } finally { busy.delete(key); }
    }
  } finally { updating = false; }
}, seconds * 1000);
async function shutdown() { clearInterval(timer); client.destroy(); await renderer.close(); process.exit(0); }
process.on('SIGINT', shutdown); process.on('SIGTERM', shutdown);
if (!process.env.DISCORD_TOKEN) { console.error('Set DISCORD_TOKEN in .env first. See README.md.'); clearInterval(timer); process.exitCode = 1; }
else client.login(process.env.DISCORD_TOKEN).catch(error => { console.error('Discord login failed:', error.message); clearInterval(timer); process.exitCode = 1; });
