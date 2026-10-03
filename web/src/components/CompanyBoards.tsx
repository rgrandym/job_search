import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Plus, RefreshCw, X } from "lucide-react";
import { useState } from "react";
import { api } from "../lib/api";

/** Status of the company job boards under the "Company career sites" toggle, with an Update
 * button. Discovery also runs by itself in the first search that includes company sites. */
export function CompanyBoards({ searching }: { searching: boolean }) {
  const qc = useQueryClient();
  const status = useQuery({
    queryKey: ["companies-status"],
    queryFn: api.companiesStatus,
    // Poll while a pass runs; during a search one may start at any moment.
    refetchInterval: (query) => (query.state.data?.running ? 2_000 : searching ? 5_000 : false),
  });
  const update = useMutation({
    mutationFn: () => api.discoverCompanies("stale"),
    onSuccess: (data) => qc.setQueryData(["companies-status"], data),
  });

  if (status.isPending) return <p className="pr-8 text-[10px] text-faint">Checking company job boards…</p>;
  if (status.isError) return <p className="pr-8 text-[10px] text-bad">Could not load the company job boards.</p>;
  const d = status.data;
  const updated = d.updated_on ? new Date(`${d.updated_on}T00:00:00`).toLocaleDateString() : null;

  return (
    <div className="space-y-0.5 pr-8 text-[10px] text-faint">
      {d.running ? (
        <p className="flex items-center gap-1 text-muted">
          <Loader2 size={10} className="shrink-0 animate-spin" />
          <span>Finding job boards: {d.message || "starting"}</span>
        </p>
      ) : d.unchecked === -1 ? (
        <p>UK life-science companies (BioPharmGuy). Not set up yet: the next search finds their job boards (about 10 minutes, once).</p>
      ) : (
        <p>
          {d.boards} job boards from {d.companies} UK life-science companies
          {updated && ` · updated ${updated}`}
          {d.unchecked > 0 && ` · ${d.unchecked} not checked yet`}
          {d.stale > 0 && ` · ${d.stale} older than 30 days`}
        </p>
      )}
      {d.error && <p className="text-bad">Last update failed: {d.error}</p>}
      {update.isError && <p className="text-bad">{(update.error as Error).message}</p>}
      <YourCompanies />
      {!d.running && (
        <button
          className="inline-flex items-center gap-1 text-accent hover:underline disabled:opacity-50"
          disabled={update.isPending || searching}
          onClick={() => update.mutate()}
          title={searching ? "Wait for the search to finish" : "Check companies not checked yet and results older than 30 days"}
        >
          <RefreshCw size={10} /> {d.unchecked === -1 ? "Find job boards now" : "Update job boards"}
        </button>
      )}
    </div>
  );
}

/** Companies the user added by hand: always searched, never overwritten by discovery. */
function YourCompanies() {
  const qc = useQueryClient();
  const mine = useQuery({ queryKey: ["my-companies"], queryFn: api.myCompanies });
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const refresh = () => {
    void qc.invalidateQueries({ queryKey: ["my-companies"] });
    void qc.invalidateQueries({ queryKey: ["company-boards"] });
  };
  const add = useMutation({
    mutationFn: () => api.addCompany(name, url),
    onSuccess: () => {
      setName("");
      setUrl("");
      refresh();
    },
  });
  const remove = useMutation({ mutationFn: api.removeCompany, onSuccess: refresh });
  const error = (add.error ?? remove.error ?? mine.error) as Error | null;

  return (
    <div className="space-y-1 pt-1">
      <p className="text-muted">Your companies {mine.data && <span className="text-faint">{mine.data.length}</span>}</p>
      {mine.isPending && <p>Loading…</p>}
      {mine.data?.length === 0 && <p>None yet. Add employers you want to watch, even if they are not in the directory.</p>}
      {mine.data?.map((c) => (
        <p key={c.name} className="flex items-center gap-1">
          <span className="min-w-0 truncate text-muted" title={c.url ?? c.token ?? undefined}>
            {c.name}
          </span>
          <span className="shrink-0">· {c.ats === "careers_page" ? "careers page" : c.ats}</span>
          <button
            className="ml-auto shrink-0 hover:text-bad"
            title={`Stop watching ${c.name}`}
            disabled={remove.isPending}
            onClick={() => remove.mutate(c.name)}
          >
            <X size={10} />
          </button>
        </p>
      ))}
      <form
        className="flex flex-wrap items-center gap-1"
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim() && url.trim()) add.mutate();
        }}
      >
        <input
          className="input min-w-[90px] flex-1 py-0.5 text-[11px]"
          placeholder="Company"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <input
          className="input min-w-[140px] flex-[2] py-0.5 text-[11px]"
          placeholder="Website or careers page link"
          value={url}
          onChange={(e) => setUrl(e.target.value)}
        />
        <button
          type="submit"
          className="inline-flex items-center gap-1 text-accent hover:underline disabled:opacity-50"
          disabled={add.isPending || !name.trim() || !url.trim()}
          title="Finds the company's job feed now (can take up to a minute)"
        >
          {add.isPending ? <Loader2 size={10} className="animate-spin" /> : <Plus size={10} />} Add
        </button>
      </form>
      {add.isPending && <p>Looking for {name}'s job feed…</p>}
      {error && <p className="text-bad">{error.message}</p>}
    </div>
  );
}
