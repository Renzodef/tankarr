/**
 * The path Tankarr is served under: "" at the root of its host, "/tankarr"
 * behind a reverse proxy sub-path (TANKARR_URL_BASE). The page's own address
 * says which, so no configuration reaches the browser.
 */
export const URL_BASE = (() => {
  try {
    return new URL(".", document.baseURI).pathname.replace(/\/$/, "");
  } catch {
    return "";
  }
})();

/** A server-relative URL ("/api/…") placed under `base`; any other URL passes through. */
export function resolveUnderBase(base: string, path: string): string {
  if (!base || !path.startsWith("/") || path.startsWith("//") || path === base || path.startsWith(`${base}/`)) {
    return path;
  }
  return `${base}${path}`;
}

/** A server-relative URL placed under the URL base this page runs under. */
export function serverUrl(path: string): string {
  return resolveUnderBase(URL_BASE, path);
}
