// Run after Dashboards is healthy. Never log credentials or response bodies.
const fs = require('fs');
const https = require('https');
const base = process.env.DASHBOARDS_URL || 'https://dashboards.logging.svc.cluster.local:5601';

async function request(method, path, body) {
  const password = fs.readFileSync('/bootstrap-auth/admin-password', 'utf8');
  const ca = fs.readFileSync('/bootstrap-ca/ca.crt');
  const data = body === undefined ? undefined : JSON.stringify(body);
  return new Promise((resolve, reject) => {
    const req = https.request(new URL(path, base), {
      method, ca, rejectUnauthorized: true,
      headers: {'Authorization': 'Basic '+Buffer.from('admin:'+password).toString('base64'),
        'securitytenant': 'global', 'osd-xsrf': 'true', 'Content-Type': 'application/json'},
    }, response => {
      let text = '';
      response.on('data', chunk => { text += chunk; });
      response.on('end', () => {
        if (response.statusCode === 404) return resolve(null);
        if (response.statusCode < 200 || response.statusCode >= 300)
          return reject(new Error('Dashboards API HTTP '+response.statusCode));
        try { resolve(JSON.parse(text)); } catch { reject(new Error('Invalid Dashboards API response')); }
      });
    });
    req.setTimeout(15000, () => req.destroy(new Error('Dashboards API timeout')));
    req.on('error', reject);
    req.end(data);
  });
}

async function bootstrap(api = request) {
  let defaultId;
  for (const id of ['logs-ping', 'logs-containers']) {
    const title = id+'-*';
    const found = await api('GET', '/api/saved_objects/_find?type=index-pattern&per_page=10000');
    if (!found || !Array.isArray(found.saved_objects)) throw new Error('Index pattern lookup failed');
    const existing = found.saved_objects.find(object => object.attributes.title === title);
    let object = existing;
    if (!object) {
      object = await api('GET', '/api/saved_objects/index-pattern/'+id);
      if (object && object.attributes.title !== title) throw new Error('Bootstrap index pattern ID is already in use');
      if (!object) object = await api('POST', '/api/saved_objects/index-pattern/'+id,
        {attributes: {title, timeFieldName: '@timestamp'}});
    }
    if (!object || !object.id) throw new Error('Index pattern creation failed');
    if (id === 'logs-ping') defaultId = object.id;
  }
  const status = await api('GET', '/api/status');
  const version = status && status.version && status.version.number;
  if (!version || !/^\d+\.\d+\.\d+/.test(version)) throw new Error('Dashboards version unavailable');
  const path = '/api/saved_objects/config/'+encodeURIComponent(version);
  const settings = await api('GET', path);
  const attributes = {};
  if (!settings || !settings.attributes.defaultIndex) attributes.defaultIndex = defaultId;
  if (!settings || !settings.attributes.defaultRoute) attributes.defaultRoute = '/app/discover';
  if (Object.keys(attributes).length) await api(settings ? 'PUT' : 'POST', path, {attributes});
}

module.exports = {bootstrap};
if (require.main === module) {
  (async () => {
    for (let attempt = 0; attempt < 30; attempt++) {
      try { await bootstrap(); console.log('Global log index patterns and defaults are ready.'); return; }
      catch (error) {
        if (attempt === 29) throw new Error('Dashboards bootstrap failed; inspect API access and saved-object compatibility.');
        await new Promise(resolve => setTimeout(resolve, 5000));
      }
    }
  })().catch(error => { console.error(error.message); process.exitCode = 1; });
}
