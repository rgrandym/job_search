import { keepUiStateSaved, restoreUiState } from "./lib/uiState";

// The remembered settings are restored before the app (and its stores) load.
void restoreUiState().then(async (restored) => {
  const { boot } = await import("./boot");
  boot();
  if (restored) keepUiStateSaved();
});
