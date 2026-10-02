// Seams to the reference stack. Remote adapters touch the network only when constructed AND called.
import { bus as defaultBus } from './bus.mjs';
import { ConcurrencyError } from './store.mjs';

const JSON_HEADERS = { 'Content-Type': 'application/json', Accept: 'application/json' };
const trimSlash = (u) => String(u).replace(/\/+$/, '');

async function request(label, fetchFn, method, url, { body, headers = {} } = {}) {
  if (typeof fetchFn !== 'function') throw new Error(`${label}: fetch is not available`);
  let res;
  try {
    res = await fetchFn(url, {
      method,
      headers: { ...JSON_HEADERS, ...headers },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    throw new Error(`${label}: network error calling ${method} ${url}: ${err?.message ?? err}`);
  }
  if (!res.ok) {
    let detail = '';
    try { detail = (await res.text()).slice(0, 300); } catch { /* ignore */ }
    const e = new Error(`${label}: ${method} ${url} failed with HTTP ${res.status}${detail ? ': ' + detail : ''}`);
    e.status = res.status;
    throw e;
  }
  if (res.status === 204) return null;
  try { return await res.json(); } catch { return null; }
}

const defaultFetch = (...a) => globalThis.fetch(...a);

export class LocalBroker {
  constructor(bus = defaultBus) { this.bus = bus; }
  async publish(topic, event) { this.bus.emit(topic, event); }
  subscribe(topic, fn) {
    this.bus.on(topic, fn);
    return () => this.bus.off(topic, fn);
  }
}

class RemoteBroker {
  subscribe() {
    throw new Error(`${this.constructor.name}: subscribe is not supported over REST; use LocalBroker for in-browser delivery`);
  }
}

export class KafkaRestBroker extends RemoteBroker {
  constructor({ url, clusterId, topicPrefix = 'ingqiqo.', fetch: f = defaultFetch } = {}) {
    super();
    if (!url) throw new Error('KafkaRestBroker: url is required');
    if (!clusterId) throw new Error('KafkaRestBroker: clusterId is required');
    this.url = trimSlash(url);
    this.clusterId = clusterId;
    this.topicPrefix = topicPrefix;
    this._fetch = f;
  }
  async publish(topic, event) {
    const name = this.topicPrefix + topic;
    const url = `${this.url}/v3/clusters/${encodeURIComponent(this.clusterId)}/topics/${encodeURIComponent(name)}/records`;
    const res = await request('KafkaRestBroker', this._fetch, 'POST', url, {
      body: { key: { type: 'STRING', data: event.stream }, value: { type: 'JSON', data: event } },
    });
    // REST Proxy v3 can answer HTTP 200 with a per-record error_code; treat that as failure.
    if (res && typeof res.error_code === 'number' && res.error_code >= 400) {
      const e = new Error(`KafkaRestBroker: record rejected (error_code ${res.error_code}): ${res.message ?? ''}`.trim());
      e.status = res.error_code;
      throw e;
    }
    return res;
  }
}

export class SolaceRestBroker extends RemoteBroker {
  constructor({ url, vpn, fetch: f = defaultFetch } = {}) {
    super();
    if (!url) throw new Error('SolaceRestBroker: url is required');
    this.url = trimSlash(url);
    this.vpn = vpn;
    this._fetch = f;
  }
  async publish(topic, event) {
    const url = `${this.url}/TOPIC/${topic.split('/').map(encodeURIComponent).join('/')}`;
    const headers = { 'Solace-Message-ID': event.id };
    // The Message VPN is selected by the REST service port, not a header; vpn is kept for documentation only.
    return request('SolaceRestBroker', this._fetch, 'POST', url, { body: event, headers });
  }
}

export class GatewayStore {
  constructor({ url, fetch: f = defaultFetch } = {}) {
    if (!url) throw new Error('GatewayStore: url is required');
    this.url = trimSlash(url);
    this._fetch = f;
  }
  async append(stream, events, { expectedVersion } = {}) {
    const url = `${this.url}/streams/${encodeURIComponent(stream)}`;
    const body = { events };
    if (expectedVersion !== undefined) body.expectedVersion = expectedVersion;
    try {
      return await request('GatewayStore', this._fetch, 'POST', url, { body });
    } catch (err) {
      if (err.status === 409) throw new ConcurrencyError(`GatewayStore: ${err.message}`, { stream, expected: expectedVersion });
      throw err;
    }
  }
  async read({ fromPosition = 0, limit } = {}) {
    const q = new URLSearchParams({ from: String(fromPosition) });
    if (limit !== undefined) q.set('limit', String(limit));
    const r = await request('GatewayStore', this._fetch, 'GET', `${this.url}/events?${q}`);
    return Array.isArray(r) ? r : (r?.events ?? []);
  }
  async readStream(stream) {
    const r = await request('GatewayStore', this._fetch, 'GET', `${this.url}/streams/${encodeURIComponent(stream)}`);
    return Array.isArray(r) ? r : (r?.events ?? []);
  }
  async lastPosition() {
    const r = await request('GatewayStore', this._fetch, 'GET', `${this.url}/events/last`);
    return typeof r === 'number' ? r : (r?.position ?? -1);
  }
  async has(id) {
    try {
      await request('GatewayStore', this._fetch, 'GET', `${this.url}/events/${encodeURIComponent(id)}`);
      return true;
    } catch (err) {
      if (err.status === 404) return false;
      throw err;
    }
  }
}

// cfg.broker: "local" | { type: "kafka-rest", url, clusterId, topicPrefix? } | { type: "solace-rest", url, vpn? }
// cfg.remoteStore: null | { url }
export function fromConfig(cfg = {}, { bus = defaultBus, fetch: f } = {}) {
  const opt = f ? { fetch: f } : {};
  let broker;
  const b = cfg.broker ?? 'local';
  if (b === 'local' || b === null) broker = new LocalBroker(bus);
  else if (b.type === 'kafka-rest') broker = new KafkaRestBroker({ ...b, ...opt });
  else if (b.type === 'solace-rest') broker = new SolaceRestBroker({ ...b, ...opt });
  else throw new Error(`fromConfig: unknown broker ${JSON.stringify(b)}`);
  const remoteStore = cfg.remoteStore ? new GatewayStore({ ...cfg.remoteStore, ...opt }) : null;
  return { broker, remoteStore };
}
