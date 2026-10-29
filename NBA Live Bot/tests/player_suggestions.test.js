const { test } = require('node:test');
const assert = require('node:assert/strict');
const { NBAData, playerSuggestions } = require('../data');
const { suggestPlayer } = require('../suggestions');
const players = [{ id: 'ty', displayName: 'Ty Jerome' }, { id: 'tyus', displayName: 'Tyus Jones' }, { id: 'lebron', displayName: 'LeBron James' }];

test('Ty Jermone suggests Ty Jerome but never silently resolves a typo', async () => {
  assert.equal(playerSuggestions('Ty Jermone', players)[0].id, 'ty');
  const data = new NBAData(); data.players = async () => players;
  await assert.rejects(data.findPlayer('Ty Jermone'), error => error.suggestions[0].id === 'ty');
  assert.equal((await data.findPlayer('Ty Jerome')).id, 'ty');
  assert.equal((await data.findPlayer('Jerome')).id, 'ty');
});
test('unrelated, very short and invalid numeric queries do not get guesses', () => {
  for (const query of ['xyzabcq', 'Ty', '12345678']) assert.deepEqual(playerSuggestions(query, players), []);
});
test('surname misspellings suggest plausible players and keep choices bounded', () => {
  assert.equal(playerSuggestions('Jeromee', players)[0].id, 'ty');
  const candidates = ['Jalen Green', 'Jalen Greer', 'Jalen Greet', 'Jalen Greek'].map((displayName, id) => ({ id, displayName }));
  assert.equal(playerSuggestions('Jalen Greem', candidates).length, 3);
});
function fixture(customId) {
  const edits = [], acknowledgements = [];
  return { edits, acknowledgements, options: {
    query: 'Ty Jermone', userId: 'owner', suggestions: [players[0]],
    reply: { edit: async body => edits.push(body), awaitMessageComponent: async options => {
      assert.equal(options.filter({ user: { id: 'owner' } }), true);
      assert.equal(options.filter({ user: { id: 'other' }, reply: async () => {} }), false);
      if (customId === 'timeout') throw Error('expired');
      return { customId, deferUpdate: async () => acknowledgements.push(true) };
    } },
  } };
}
test('Yes confirms Ty Jerome and removes suggestion buttons', async () => {
  const f = fixture('nba-player-0');
  assert.equal((await suggestPlayer(f.options)).id, 'ty');
  assert.match(f.edits[0].content, /Did you mean \*\*Ty Jerome\*\*/);
  assert.deepEqual(f.edits.at(-1).components, []);
  assert.equal(f.acknowledgements.length, 1);
});
test('No and expiration do not select a player and clear buttons', async () => {
  for (const choice of ['nba-player-no', 'timeout']) {
    const f = fixture(choice);
    assert.equal(await suggestPlayer(f.options), null);
    assert.deepEqual(f.edits.at(-1).components, []);
  }
});
test('nicknames and partial surnames suggest the intended player (Victor Wemby -> Victor Wembanyama)', async () => {
  const roster = [{ id: 'wemby', displayName: 'Victor Wembanyama' }, { id: 'oladipo', displayName: 'Victor Oladipo' }, { id: 'giannis', displayName: 'Giannis Antetokounmpo' }, ...players];
  assert.equal(playerSuggestions('Victor Wemby', roster)[0].id, 'wemby');
  assert.equal(playerSuggestions('Wemby', roster)[0].id, 'wemby');
  assert.equal(playerSuggestions('Giannis Antetok', roster)[0].id, 'giannis');
  const data = new NBAData(); data.players = async () => roster;
  // Still a question, never a silent guess.
  await assert.rejects(data.findPlayer('Victor Wemby'), error => error.suggestions[0].id === 'wemby');
  // A first name alone is not enough to guess a surname.
  assert.ok(!playerSuggestions('Victor Smith', roster).length);
});
test('nicknames resolve before name matching (KD, SGA, PG13, Tatum)', async () => {
  const roster = [{ id: 'kd', displayName: 'Kevin Durant' }, { id: 'dia', displayName: 'Malik Dia' }, { id: 'sga', displayName: 'Shai Gilgeous-Alexander' },
    { id: 'garland', displayName: 'Darius Garland' }, { id: 'pg', displayName: 'Paul George' }, { id: 'jt', displayName: 'Jayson Tatum' }];
  const data = new NBAData(); data.players = async () => roster;
  // "KD" used to substring-match Malik Dia; the nickname must win.
  for (const [query, id] of [['KD', 'kd'], ['kd', 'kd'], ['SGA', 'sga'], ['PG13', 'pg'], ['PG-13', 'pg'], ['Tatum', 'jt']]) {
    assert.equal((await data.findPlayer(query)).id, id, query);
  }
  assert.deepEqual((await data.comparePlayers('KD SGA')).map(s => s.player.id), ['kd', 'sga']);
  assert.deepEqual((await data.comparePlayers('PG13 vs Tatum')).map(s => s.player.id), ['pg', 'jt']);
});
