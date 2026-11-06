// GUILD_ID identifies the home server; public installs work unless explicitly locked.
function createAccess(env = process.env) {
  const home = (env.GUILD_ID || '').trim();
  const locked = /^(true|1)$/i.test(env.RESTRICT_TO_GUILD || '');
  const channels = new Set((env.ALLOWED_CHANNEL_IDS || '').split(',').map(s => s.trim()).filter(Boolean));
  if (locked && !home) throw new Error('RESTRICT_TO_GUILD requires GUILD_ID.');
  return ({ guildId, channelId, parentId }) => {
    if (!guildId || (locked && guildId !== home)) return false;
    if (channels.size && (!home || guildId === home)) {
      return channels.has(channelId) || channels.has(parentId);
    }
    return true;
  };
}

module.exports = { createAccess };
