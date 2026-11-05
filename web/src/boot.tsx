import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";
import { queryClient } from "./lib/queryClient";
import { initZoom } from "./stores/zoomStore";

/** Render the app. Loaded by main.tsx after the remembered settings are restored, because the
 *  stores read local storage as soon as they are imported. */
export function boot(): void {
  initZoom(); // before the first render: the page opens at its remembered size

  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <QueryClientProvider client={queryClient}>
        <App />
      </QueryClientProvider>
    </StrictMode>,
  );
}
