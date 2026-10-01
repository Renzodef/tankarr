// The online demo (docs site, /demo/) is the real interface in front of a
// recording of the API: every response the fictional library gave while the
// recorder walked the pages (e2e/demo-record.mjs). This module answers a
// request from that recording; the service worker (sw.ts) wires it to the
// browser, and the unit tests pin its rules.

export type Recorded = {
  status: number;
  /** The Content-Type header of the recorded response. */
  type: string;
  /** The body, for text responses. */
  body?: string;
  /** A file under demo-data/, for binary responses (covers, pages). */
  file?: string;
};

export type Recording = {
  version: 1;
  recorded_at: string;
  responses: Record<string, Recorded>;
};

export const RECORDING_VERSION = 1;

/** Query parameters that only steer caching on the server. */
const VOLATILE_PARAMETERS = new Set(["fresh", "cached", "_", "t", "ts"]);

const JSON_TYPE = "application/json";

function json(status: number, value: unknown): Recorded {
  return { status, type: JSON_TYPE, body: JSON.stringify(value) };
}

/** The key a request is recorded under: method, path and query. */
export function recordingKey(method: string, target: string): string {
  return `${method.toUpperCase()} ${target}`;
}

function splitTarget(target: string): { path: string; query: string } {
  const index = target.indexOf("?");
  return index === -1
    ? { path: target, query: "" }
    : { path: target.slice(0, index), query: target.slice(index + 1) };
}

function withoutVolatileParameters(query: string): string {
  const parameters = new URLSearchParams(query);
  for (const name of VOLATILE_PARAMETERS) parameters.delete(name);
  return parameters.toString();
}

function stableTarget(target: string): string {
  const { path, query } = splitTarget(target);
  const stable = withoutVolatileParameters(query);
  return stable ? `${path}?${stable}` : path;
}

type Index = { stable: Map<string, Recorded>; path: Map<string, Recorded> };
const indexes = new WeakMap<Recording, Index>();

/** The recorded reads, also by their cache-neutral query and by path alone. */
function indexOf(recording: Recording): Index {
  let index = indexes.get(recording);
  if (index) return index;
  index = { stable: new Map(), path: new Map() };
  for (const [key, entry] of Object.entries(recording.responses)) {
    if (!key.startsWith("GET ")) continue;
    const target = key.slice(4);
    const stable = stableTarget(target);
    if (!index.stable.has(stable)) index.stable.set(stable, entry);
    const { path } = splitTarget(target);
    if (!index.path.has(path)) index.path.set(path, entry);
  }
  indexes.set(recording, index);
  return index;
}

function recordedGet(recording: Recording, target: string): Recorded | null {
  const exact = recording.responses[recordingKey("GET", target)];
  if (exact) return exact;
  const index = indexOf(recording);
  return index.stable.get(stableTarget(target)) ?? index.path.get(splitTarget(target).path) ?? null;
}

/**
 * The response the demo gives to `method target` ("/api/…", with its query).
 *
 * Reads come from the recording: the exact request, else the same request
 * without its cache-steering parameters, else the path alone. Writes are
 * acknowledged, never applied: the banner says so, and the next read shows
 * the recorded state again.
 */
export function lookup(recording: Recording, method: string, target: string): Recorded {
  const verb = method.toUpperCase();
  if (verb === "GET" || verb === "HEAD") {
    return recordedGet(recording, target) ?? json(404, { detail: "This request is not part of the demo recording" });
  }
  const exact = recording.responses[recordingKey(verb, target)];
  if (exact) return exact;
  const { path } = splitTarget(target);
  if (path === "/api/settings") {
    const settings = recordedGet(recording, "/api/settings");
    if (settings?.body) return json(200, { applied: [], settings: JSON.parse(settings.body) });
  }
  if (verb === "DELETE") return { status: 204, type: JSON_TYPE };
  const read = recordedGet(recording, target);
  if (read && read.type.startsWith(JSON_TYPE)) return { ...read, status: 200 };
  return json(200, { ok: true, demo: true });
}
