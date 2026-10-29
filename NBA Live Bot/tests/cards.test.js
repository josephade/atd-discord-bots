const { test } = require('node:test');
const assert = require('node:assert/strict');
const { PlayerCards, PAGE_IDS } = require('../cards');
function fixture(options = {}) {
  const renders = [], edits = [], acks = [], notices = [];
  const renderer = { render: async model => { renders.push({ ...model }); return Buffer.from('png'); } };
  const message = { id: 'message', edit: async body => { edits.push(body); } };
  const interaction = { message, deferUpdate: async () => acks.push(true), reply: async body => notices.push(body), followUp: async body => notices.push(body) };
  const cards = new PlayerCards({ renderer, payload: (model, png, watching) => ({ content: `${model.value}:${watching}`, files: [png] }), ...options });
  const model = { eventId: 'game', state: 'in', value: 1 };
  return { cards, renderer, message, interaction, model, renders, edits, acks, notices };
}
const go = (f, face) => f.cards.select({ ...f.interaction, customId: PAGE_IDS[face] });

test('three pages expose exactly two direct destination buttons each', async () => {
  const f = fixture(); await f.cards.show(f.message, f.model);
  for (const face of ['front', 'shots', 'quarters', 'front']) {
    await go(f, face);
    const buttons = f.edits.at(-1).components[0].toJSON().components;
    assert.equal(buttons.length, 2);
    assert.deepEqual(buttons.map(button => button.custom_id).sort(), Object.entries(PAGE_IDS)
      .filter(([page]) => page !== face).map(([, id]) => id).sort());
  }
  assert.deepEqual(f.renders.map(model => model.face), ['front', 'shots', 'quarters']);
});

test('live updates preserve the shot page and invalidate its cached stats', async () => {
  const f = fixture(); await f.cards.show(f.message, f.model, true);
  await go(f, 'shots');
  await f.cards.show(f.message, { ...f.model, value: 9 }, true);
  assert.equal(f.renders.at(-1).face, 'shots');
  assert.equal(f.edits.at(-1).content, '9:true');
});

test('unrecognized page buttons are ignored without modifying the card', async () => {
  const f = fixture(); await f.cards.show(f.message, f.model);
  await f.cards.select({ ...f.interaction, customId: 'unrelated' });
  assert.equal(f.edits.length, 1); assert.equal(f.acks.length, 0);
});

test('button flips both ways without posting a second message or losing attachment replacement', async () => {
  const f = fixture(); await f.cards.show(f.message, f.model);
  assert.equal(f.edits[0].components[0].toJSON().components[0].custom_id, PAGE_IDS.quarters);
  await go(f, 'quarters'); await go(f, 'front');
  assert.deepEqual(f.renders.map(m => m.face), ['front', 'quarters']);
  assert.equal(f.acks.length, 2);
  assert.deepEqual(f.edits[1].attachments, []);
});
test('live refresh preserves the back face and uses the latest stats', async () => {
  const f = fixture(); await f.cards.show(f.message, f.model, true);
  await go(f, 'quarters');
  await f.cards.show(f.message, { ...f.model, value: 2 }, true);
  assert.equal(f.renders.at(-1).face, 'quarters'); assert.equal(f.renders.at(-1).value, 2);
  assert.equal(f.edits.at(-1).content, '2:true');
});
test('concurrent refresh and flip are serialized and final state stays consistent', async () => {
  const f = fixture(); await f.cards.show(f.message, f.model, true);
  await Promise.all([f.cards.show(f.message, { ...f.model, value: 2 }, true), go(f, 'quarters')]);
  assert.equal(f.renders.at(-1).face, 'quarters'); assert.equal(f.renders.at(-1).value, 2);
});
test('canceled refresh does not overwrite an unwatch and later flips do not claim to refresh', async () => {
  const f = fixture(); await f.cards.show(f.message, f.model, true);
  f.cards.stopWatching(f.message.id);
  const result = await f.cards.show(f.message, { ...f.model, value: 2 }, true, () => false);
  assert.equal(result, null); assert.equal(f.edits.length, 1);
  await go(f, 'quarters');
  assert.equal(f.edits.at(-1).content, '1:false');
});
test('season averages have no game flip button', async () => {
  const f = fixture(); await f.cards.show(f.message, { state: 'season', value: 1 });
  assert.deepEqual(f.edits[0].components, []); assert.equal(f.cards.entries.size, 0);
});
test('expired cards respond privately and retained card count is bounded', async () => {
  let now = 0;
  const f = fixture({ ttl: 10, now: () => now, limit: 1 });
  await f.cards.show(f.message, f.model);
  now = 11; await go(f, 'quarters');
  assert.equal(f.notices[0].flags, 64); assert.match(f.notices[0].content, /expired/);
  await f.cards.show(f.message, f.model);
  await f.cards.show({ id: 'other', edit: async () => {} }, f.model);
  assert.equal(f.cards.entries.size, 1);
});
test('failed flip leaves previous face recoverable and explains retry privately', async () => {
  const f = fixture(); await f.cards.show(f.message, f.model);
  f.renderer.render = async () => { throw Error('render unavailable'); };
  await go(f, 'quarters');
  assert.equal(f.cards.entries.get('message').face, 'front');
  assert.match(f.notices[0].content, /try the button again/);
});

test('both flip directions preserve the original full-resolution PNG buffers', async () => {
  const f = fixture({ payload: (model, bytes) => ({ bytes }) });
  const front = Buffer.from('full-resolution-front-png');
  const back = Buffer.from('full-resolution-back-png');
  f.renderer.render = async model => model.face === 'quarters' ? back : front;
  await f.cards.show(f.message, f.model);
  await go(f, 'quarters');
  assert.equal(f.edits.at(-1).bytes, back);
  await go(f, 'front');
  assert.equal(f.edits.at(-1).bytes, front);
});

test('background preparation starts after posting and reuses both faces', async () => {
  const f = fixture({ prepareFaces: true });
  await f.cards.show(f.message, f.model);
  assert.equal(f.edits.length, 1); assert.equal(f.renders.length, 1);
  await new Promise(resolve => setImmediate(resolve));
  // Let the preparation microtasks finish after its setImmediate callback.
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(f.renders.map(m => m.face), ['front', 'quarters', 'shots']);
  await go(f, 'quarters');
  assert.deepEqual(f.renders.map(m => m.face), ['front', 'quarters', 'shots']);
});
