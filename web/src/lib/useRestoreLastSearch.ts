import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { openedSearch, useSearch } from "../stores/searchStore";
import { api } from "./api";

/** On load (a reload or a new tab), reopen the search that was on screen last, or the newest
 *  saved search if none was recorded. Nothing is reopened after the user cleared the results,
 *  or once a search is running or shown. */
export function useRestoreLastSearch() {
  const qc = useQueryClient();
  useEffect(() => {
    const start = useSearch.getState();
    if (start.outcome || start.loading || start.lastSearchId === null) return;
    let cancelled = false;
    const restore = async () => {
      const items = await qc.fetchQuery({ queryKey: ["history"], queryFn: api.history });
      const item = start.lastSearchId === undefined ? items[0] : items.find((i) => i.id === start.lastSearchId);
      if (!item || cancelled) return;
      const { request, outcome } = await api.openHistory(item.id);
      const now = useSearch.getState();
      if (cancelled || now.outcome || now.loading) return; // the user started something meanwhile
      now.set(openedSearch(request, outcome, item));
    };
    // A search that can no longer be opened leaves the panel empty; the history list says why.
    restore().catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [qc]);
}
