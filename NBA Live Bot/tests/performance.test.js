const { test } = require('node:test');
const assert = require('node:assert/strict');
const { Renderer } = require('../render');
const { NBAData } = require('../data');

test('identical renders coalesce and cache; changing stats invalidates the image', async () => {
  const renderer = new Renderer();
  let draws = 0;
  renderer.draw = async model => { draws++; return Buffer.from(String(model.points)); };
  const results = await Promise.all([renderer.render({ points: 7 }), renderer.render({ points: 7 })]);
  assert.equal(draws, 1); assert.equal(results[0], results[1]);
  await renderer.render({ points: 7 }); assert.equal(draws, 1);
  await renderer.render({ points: 9 }); assert.equal(draws, 2);
});

test('render errors do not poison the queue or remain cached', async () => {
  const renderer = new Renderer();
  renderer.draw = async () => { throw Error('browser closed'); };
  await assert.rejects(renderer.render({ points: 1 }), /browser closed/);
  renderer.draw = async () => Buffer.from('recovered');
  assert.equal((await renderer.render({ points: 1 })).toString(), 'recovered');
});

test('the browser page is reused and rendering does not wait for network idle', async () => {
  let pages = 0, contents = 0, disposed = 0;
  const renderer = new Renderer();
  const page = { isClosed: () => false, setViewport: async () => {},
    setContent: async (markup, options) => { contents++; assert.equal(options.waitUntil, 'domcontentloaded'); },
    evaluate: async () => {}, $: async () => ({ screenshot: async () => Buffer.from('png'), dispose: async () => { disposed++; } }) };
  renderer.browser = { connected: true, newPage: async () => { pages++; return page; }, close: async () => {} };
  await renderer.render({ player: { displayName: 'Test' }, stats: { PTS: 1 } });
  await renderer.render({ player: { displayName: 'Test' }, stats: { PTS: 2 } });
  assert.equal(pages, 1); assert.equal(contents, 2);
  assert.equal(disposed, 2);
  await renderer.close();
});

test('player directory warms once and concurrent lookups share the load', async () => {
  const data = new NBAData(); let loads = 0;
  data.loadPlayers = async () => { loads++; return [{ id: 'p', displayName: 'Test Player' }]; };
  await Promise.all([data.findPlayer('Test Player'), data.findPlayer('Test Player')]);
  assert.equal(loads, 1);
  await data.findPlayer('Test Player'); assert.equal(loads, 1);
});

test('a failed directory warm-up can be retried', async () => {
  const data = new NBAData();
  data.loadPlayers = async () => { throw Error('feed unavailable'); };
  await assert.rejects(data.players(), /feed unavailable/);
  data.loadPlayers = async () => [{ id: 'p' }];
  assert.deepEqual(await data.players(), [{ id: 'p' }]);
});
