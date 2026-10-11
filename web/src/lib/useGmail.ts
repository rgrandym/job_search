import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "./api";

/** The Gmail alert account: its status (`problem`: a stored sign-in Google no longer accepts)
 *  and a reconnect that opens Google's sign-in in the browser, then waits here until the new
 *  sign-in works. Shared by the sidebar and the failed-source actions under the results. */
export function useGmail() {
  const qc = useQueryClient();
  const [waiting, setWaiting] = useState(false);
  const status = useQuery({
    queryKey: ["gmail-status"],
    queryFn: api.gmailStatus,
    refetchInterval: waiting ? 2_000 : false, // the sign-in runs in the browser
    refetchOnWindowFocus: true,
  });
  const healthy = !!status.data?.connected && !status.data.problem;
  useEffect(() => {
    if (!waiting || !healthy) return;
    setWaiting(false);
    void qc.invalidateQueries({ queryKey: ["state"] }); // Gmail now appears as a source
  }, [waiting, healthy, qc]);
  const connect = () => {
    window.open("/api/gmail/connect", "_blank");
    setWaiting(true);
  };
  return { status, healthy, waiting, connect, cancel: () => setWaiting(false) };
}
