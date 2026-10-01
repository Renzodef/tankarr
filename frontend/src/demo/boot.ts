// Puts the demo's service worker in front of the page before the interface
// makes its first request. The worker claims the page as soon as it is
// active, so the first visit needs no reload; a browser that keeps the page
// uncontrolled (a hard reload) is reloaded once.

const RELOAD_FLAG = "tankarr.demo.reloaded";

export async function startDemo(): Promise<void> {
  if (!("serviceWorker" in navigator)) {
    throw new Error("The online demo needs a browser with service workers");
  }
  const base = new URL(".", document.baseURI);
  await navigator.serviceWorker.register(new URL("sw.js", base), { scope: base.pathname, type: "module" });
  if (navigator.serviceWorker.controller) return;
  const controlled = new Promise<void>((resolve) => {
    navigator.serviceWorker.addEventListener("controllerchange", () => resolve(), { once: true });
  });
  const timeout = new Promise<"timeout">((resolve) => window.setTimeout(() => resolve("timeout"), 4000));
  if ((await Promise.race([controlled, timeout])) === "timeout") {
    if (window.sessionStorage.getItem(RELOAD_FLAG)) {
      throw new Error("The demo's service worker did not take control of the page");
    }
    window.sessionStorage.setItem(RELOAD_FLAG, "1");
    window.location.reload();
    await new Promise<never>(() => undefined);
  }
  window.sessionStorage.removeItem(RELOAD_FLAG);
}
