import { useEffect, useRef } from "react";
import { t } from "./i18n";

export function useUnsavedChangesGuard(dirty: boolean) {
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  useEffect(() => {
    let lastUrl = window.location.href;
    const beforeUnload = (event: BeforeUnloadEvent) => {
      if (!dirtyRef.current) return;
      event.preventDefault();
      event.returnValue = "";
    };
    const navigate = (event: Event) => {
      const remainsInSettings = /^#\/settings(?:\?|$)/.test(window.location.hash);
      if (dirtyRef.current && !remainsInSettings && !window.confirm(t("You have unsaved settings. Leave and discard these changes?"))) {
        // The router emits this cancelable event before committing navigation,
        // so cancelling never unmounts the editable form or loses its draft.
        window.history.replaceState(null, "", lastUrl);
        event.preventDefault();
        return;
      }
      lastUrl = window.location.href;
    };
    window.addEventListener("beforeunload", beforeUnload);
    window.addEventListener("tankarr:before-navigation", navigate);
    return () => {
      window.removeEventListener("beforeunload", beforeUnload);
      window.removeEventListener("tankarr:before-navigation", navigate);
    };
  }, []);
}
