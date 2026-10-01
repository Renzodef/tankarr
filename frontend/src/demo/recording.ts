// The online demo (docs site, /demo/) is the real interface in front of a
// recording of the API: every response the demo library gave while the
// recorder walked the pages (e2e/demo-record.mjs). The recording is an index
// of requests; each body is its own file under demo-data/, fetched when a
// request needs it, so a library of thousands of chapters costs a visitor
// only the pages they open. This module answers a request from that index;
// the service worker (sw.ts) wires it to the browser, and the unit tests pin
// its rules.

export type Recorded = {
  status: number;
  /** The Content-Type header of the recorded response. */
  type: string;
  /** The body inline, for small or synthesized responses. */
  body?: string;
  /** The body as a file under demo-data/ (JSON, covers, pages). */
  file?: string;
};

export type Recording = {
  version: 2;
  recorded_at: string;
  responses: Record<string, Recorded>;
};

/** Reads the body of a recorded response, inline or from its file. */
export type ReadBody = (entry: Recorded) => Promise<string>;

export const RECORDING_VERSION = 2;

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
export async function lookup(
  recording: Recording,
  method: string,
  target: string,
  read: ReadBody,
): Promise<Recorded> {
  const verb = method.toUpperCase();
  if (verb === "GET" || verb === "HEAD") {
    return recordedGet(recording, target) ?? json(404, { detail: "This request is not part of the demo recording" });
  }
  const exact = recording.responses[recordingKey(verb, target)];
  if (exact) return exact;
  const { path } = splitTarget(target);
  if (path === "/api/settings") {
    const settings = recordedGet(recording, "/api/settings");
    if (settings) return json(200, { applied: [], settings: JSON.parse(await read(settings)) });
  }
  if (verb === "DELETE") return { status: 204, type: JSON_TYPE };
  const recorded = recordedGet(recording, target);
  if (recorded && recorded.type.startsWith(JSON_TYPE)) return { ...recorded, status: 200 };
  return json(200, { ok: true, demo: true });
}
