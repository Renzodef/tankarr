import { StrictMode, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import AuthGate from "./AuthGate";
import { I18nProvider } from "./i18n";
import "./styles.css";

async function bootstrap(): Promise<ReactNode> {
  // The online demo (docs site) answers the API from a recording; the
  // ordinary build never ships this code.
  if (import.meta.env.VITE_TANKARR_DEMO === "1") {
    const [{ startDemo }, { default: DemoBanner }] = await Promise.all([
      import("./demo/boot"),
      import("./demo/DemoBanner"),
    ]);
    try {
      await startDemo();
      return <DemoBanner />;
    } catch (error) {
      return <DemoBanner error={error instanceof Error ? error.message : String(error)} />;
    }
  }
  return null;
}

void bootstrap().then((banner) => {
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <I18nProvider>
        {banner}
        <AuthGate />
      </I18nProvider>
    </StrictMode>,
  );
});
