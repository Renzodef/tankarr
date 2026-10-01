// Service worker of the online demo: answers every request under
// <scope>/api/ from the recording in <scope>/demo-data/, so the real
// interface runs on a static host (GitHub Pages) without a server.
import { lookup, type Recorded, type Recording } from "./recording";

type FetchLikeEvent = Event & {
  request: Request;
  respondWith(response: Promise<Response> | Response): void;
};
type ExtendableLikeEvent = Event & { waitUntil(promise: Promise<unknown>): void };
type WorkerScope = {
  location: Location;
  registration: { scope: string };
  skipWaiting(): Promise<void>;
  clients: { claim(): Promise<void> };
  addEventListener(type: "install" | "activate", listener: (event: ExtendableLikeEvent) => void): void;
  addEventListener(type: "fetch", listener: (event: FetchLikeEvent) => void): void;
};

const worker = self as unknown as WorkerScope;
const scope = new URL(worker.registration.scope);
const apiPrefix = `${scope.pathname}api/`;
let recording: Promise<Recording> | null = null;

function loadRecording(): Promise<Recording> {
  recording ??= fetch(new URL("demo-data/recording.json", scope), { cache: "no-cache" })
    .then((response) => {
      if (!response.ok) throw new Error(`recording.json: HTTP ${response.status}`);
      return response.json() as Promise<Recording>;
    })
    .catch((error: unknown) => {
      recording = null;
      throw error;
    });
  return recording;
}

function fileOf(entry: Recorded): Promise<Response> {
  return fetch(new URL(`demo-data/${entry.file}`, scope));
}

async function readBody(entry: Recorded): Promise<string> {
  if (entry.file === undefined) return entry.body ?? "";
  const file = await fileOf(entry);
  if (!file.ok) throw new Error(`${entry.file}: HTTP ${file.status}`);
  return file.text();
}

async function respond(request: Request): Promise<Response> {
  const url = new URL(request.url);
  const target = url.pathname.slice(scope.pathname.length - 1) + url.search;
  const entry = await lookup(await loadRecording(), request.method, target, readBody);
  if (entry.file) {
    const file = await fileOf(entry);
    return new Response(file.body, {
      status: file.ok ? entry.status : file.status,
      headers: { "Content-Type": entry.type, "Cache-Control": "max-age=3600" },
    });
  }
  return new Response(entry.status === 204 ? null : (entry.body ?? ""), {
    status: entry.status,
    headers: { "Content-Type": entry.type, "Cache-Control": "no-store" },
  });
}

worker.addEventListener("install", (event) => {
  event.waitUntil(worker.skipWaiting());
});

worker.addEventListener("activate", (event) => {
  event.waitUntil(worker.clients.claim());
});

worker.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.origin !== scope.origin || !url.pathname.startsWith(apiPrefix)) return;
  event.respondWith(
    respond(event.request).catch(
      (error: unknown) =>
        new Response(JSON.stringify({ detail: `Demo recording unavailable: ${String(error)}` }), {
          status: 503,
          headers: { "Content-Type": "application/json" },
        }),
    ),
  );
});
