const test = require('node:test');
const assert = require('node:assert/strict');
const {bootstrap} = require('../observability/bootstrap-dashboards');

function fixture(objects = [], settings = null) {
  const calls = [];
  return {objects, calls, async api(method, path, body) {
    calls.push({method, path, body});
    if (path.includes('_find')) return {saved_objects: objects};
    if (path === '/api/status') return {version: {number: '3.6.0'}};
    if (path.includes('/config/')) {
      if (method !== 'GET') settings = {attributes: {...(settings?.attributes || {}), ...body.attributes}};
      return settings;
    }
    const id = path.split('/').pop();
    if (method === 'GET') return objects.find(o => o.id === id) || null;
    const object = {id, ...body}; objects.push(object); return object;
  }};
}
test('fresh setup creates both patterns, timestamp and Discover defaults; rerun writes nothing', async () => {
  const f = fixture();
  await bootstrap(f.api);
  assert.deepEqual(f.objects.map(o => o.attributes.title), ['logs-ping-*', 'logs-containers-*']);
  assert.ok(f.objects.every(o => o.attributes.timeFieldName === '@timestamp'));
  assert.deepEqual(f.calls.find(c => c.path.includes('/config/') && c.method === 'POST').body.attributes,
    {defaultIndex: 'logs-ping', defaultRoute: '/app/discover'});
  f.calls.length = 0;
  await bootstrap(f.api);
  assert.ok(f.calls.every(c => c.method === 'GET'));
});
test('reuse manually created pattern IDs and preserve custom defaults', async () => {
  const f = fixture([{id: 'manual-uuid', attributes: {title: 'logs-ping-*'}}],
    {attributes: {defaultIndex: 'custom', defaultRoute: '/app/custom'}});
  await bootstrap(f.api);
  assert.equal(f.objects.filter(o => o.attributes.title === 'logs-ping-*').length, 1);
  assert.ok(!f.calls.some(c => c.path.includes('/config/') && c.method !== 'GET'));
});
test('conflicting stable ID fails without overwriting another pattern', async () => {
  const f = fixture([{id: 'logs-ping', attributes: {title: 'other-*'}}]);
  await assert.rejects(bootstrap(f.api), /already in use/);
  assert.ok(f.calls.every(c => c.method === 'GET'));
});
