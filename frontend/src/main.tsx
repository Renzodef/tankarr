import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import AuthGate from "./AuthGate";
import { I18nProvider } from "./i18n";
import "./styles.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <I18nProvider>
      <AuthGate />
    </I18nProvider>
  </StrictMode>,
);
