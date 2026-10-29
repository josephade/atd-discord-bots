const { test } = require('node:test');
const assert = require('node:assert/strict');
const { NFLData, playerSuggestions } = require('../data');
const { suggestPlayer } = require('../suggestions');
const players = [{ id: 'ty', displayName: 'Ty Hilton' }, { id: 'tyus', displayName: 'Tyler Jones' }, { id: 'patrick', displayName: 'Patrick Mahomes' }];

test('Ty Hiltonn suggests Ty Hilton but never silently resolves a typo', async () => {
  assert.equal(playerSuggestions('Ty Hiltonn', players)[0].id, 'ty');
  const data = new NFLData(); data.players = async () => players;
  await assert.rejects(data.findPlayer('Ty Hiltonn'), error => error.suggestions[0].id === 'ty');
  assert.equal((await data.findPlayer('Ty Hilton')).id, 'ty');
  assert.equal((await data.findPlayer('Hilton')).id, 'ty');
});
test('unrelated, very short and invalid numeric queries do not get guesses', () => {
  for (const query of ['xyzabcq', 'Ty', '12345678']) assert.deepEqual(playerSuggestions(query, players), []);
});
test('surname misspellings suggest plausible players and keep choices bounded', () => {
  assert.equal(playerSuggestions('Hiltonnn', players)[0].id, 'ty');
  const candidates = ['Jalen Green', 'Jalen Greer', 'Jalen Greet', 'Jalen Greek'].map((displayName, id) => ({ id, displayName }));
  assert.equal(playerSuggestions('Jalen Greem', candidates).length, 3);
});
function fixture(customId) {
  const edits = [], acknowledgements = [];
  return { edits, acknowledgements, options: {
    query: 'Ty Hiltonn', userId: 'owner', suggestions: [players[0]],
    reply: { edit: async body => edits.push(body), awaitMessageComponent: async options => {
      assert.equal(options.filter({ user: { id: 'owner' } }), true);
      assert.equal(options.filter({ user: { id: 'other' }, reply: async () => {} }), false);
      if (customId === 'timeout') throw Error('expired');
      return { customId, deferUpdate: async () => acknowledgements.push(true) };
    } },
  } };
}
test('Yes confirms Ty Hilton and removes suggestion buttons', async () => {
  const f = fixture('nfl-player-0');
  assert.equal((await suggestPlayer(f.options)).id, 'ty');
  assert.match(f.edits[0].content, /Did you mean \*\*Ty Hilton\*\*/);
  assert.deepEqual(f.edits.at(-1).components, []);
  assert.equal(f.acknowledgements.length, 1);
});
test('No and expiration do not select a player and clear buttons', async () => {
  for (const choice of ['nfl-player-no', 'timeout']) {
    const f = fixture(choice);
    assert.equal(await suggestPlayer(f.options), null);
    assert.deepEqual(f.edits.at(-1).components, []);
  }
});
