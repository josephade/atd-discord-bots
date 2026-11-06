const test = require('node:test');
const assert = require('node:assert/strict');
const { createAccess } = require('../access');

test('public installs work outside the configured home server', () => {
  const access = createAccess({ GUILD_ID: 'home' });
  assert.equal(access({ guildId: 'another', channelId: 'general' }), true);
  assert.equal(access({ guildId: 'home', channelId: 'general' }), true);
  assert.equal(access({ guildId: null }), false);
});

test('home channel restrictions do not disable other servers; threads inherit access', () => {
  const access = createAccess({ GUILD_ID: 'home', ALLOWED_CHANNEL_IDS: 'nba' });
  assert.equal(access({ guildId: 'home', channelId: 'general' }), false);
  assert.equal(access({ guildId: 'home', channelId: 'thread', parentId: 'nba' }), true);
  assert.equal(access({ guildId: 'another', channelId: 'general' }), true);
});

test('explicit private mode still restricts the bot to its home server', () => {
  const access = createAccess({ GUILD_ID: 'home', RESTRICT_TO_GUILD: 'true' });
  assert.equal(access({ guildId: 'home' }), true);
  assert.equal(access({ guildId: 'another' }), false);
  assert.throws(() => createAccess({ RESTRICT_TO_GUILD: 'true' }), /requires GUILD_ID/);
});

test('channel allowlist without a home server remains global', () => {
  const access = createAccess({ ALLOWED_CHANNEL_IDS: 'nba' });
  assert.equal(access({ guildId: 'any', channelId: 'other' }), false);
  assert.equal(access({ guildId: 'any', channelId: 'nba' }), true);
});
