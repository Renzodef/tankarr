import { useEffect, useSyncExternalStore } from "react";
import { libraryStore } from "./api";

// Fresh navigation reads can be shared for a few seconds. Return visits and
// browser focus revalidate conditionally while keeping the last view usable.
const RECENT_SNAPSHOT_MS = 10_000;

export function useLibrary(enabled = true) {
  const snapshot = useSyncExternalStore(libraryStore.subscribe, libraryStore.getSnapshot);

  useEffect(() => {
    if (!enabled) return;
    const refreshIfStale = () => {
      if (document.hidden) return;
      if (Date.now() - libraryStore.getSnapshot().updatedAt >= RECENT_SNAPSHOT_MS) {
        void libraryStore.revalidate().catch(() => undefined);
      }
    };
    refreshIfStale();
    window.addEventListener("focus", refreshIfStale);
    document.addEventListener("visibilitychange", refreshIfStale);
    return () => {
      window.removeEventListener("focus", refreshIfStale);
      document.removeEventListener("visibilitychange", refreshIfStale);
    };
  }, [enabled]);

  return { ...snapshot, refresh: libraryStore.refresh };
}
