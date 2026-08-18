import { useCallback, useEffect, useState } from "react";

import { createProduct as postProduct, fetchOpportunities, fetchProducts, type Opportunity, type Product } from "../api/client";

type Data = { opportunities: Opportunity[]; products: Product[] };
type ProductPayload = { opportunity_id: string; name: string; target_user: string };
export interface OpportunitiesPageProps { loadOpportunities?: () => Promise<Data>; createProduct?: (payload: ProductPayload) => Promise<Partial<Product> & { id: string }>; }
const defaultLoad = async () => { const [opportunities, products] = await Promise.all([fetchOpportunities(), fetchProducts()]); return { opportunities, products }; };

export function OpportunitiesPage({ loadOpportunities = defaultLoad, createProduct = postProduct }: OpportunitiesPageProps) {
  const [state, setState] = useState<{ kind: "loading" } | { kind: "error" } | { kind: "ready"; data: Data }>({ kind: "loading" });
  const [forms, setForms] = useState<Record<string, { name: string; target: string }>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const refresh = useCallback(async () => { setState({ kind: "loading" }); try { setState({ kind: "ready", data: await loadOpportunities() }); } catch { setState({ kind: "error" }); } }, [loadOpportunities]);
  useEffect(() => { void refresh(); }, [refresh]);
  if (state.kind === "loading") return <main className="workbench-page" id="main-content"><section className="loading-panel" aria-busy="true"><p role="status">Loading opportunities</p></section></main>;
  if (state.kind === "error") return <main className="workbench-page" id="main-content"><section className="message-panel" role="alert"><h1>Could not load opportunities</h1><p>The opportunity or product API did not return a result.</p><button type="button" onClick={() => void refresh()}>Retry opportunities</button></section></main>;
  const { opportunities, products } = state.data;
  return <main className="workbench-page" id="main-content"><header className="page-heading"><p className="eyebrow">Evidence to product</p><h1>Opportunities</h1><p>Only cards persisted by grounded analysis appear here.</p></header>
    {notice ? <p className="action-notice" role="status">{notice}</p> : null}{actionError ? <p className="action-error" role="alert">{actionError}</p> : null}
    {opportunities.length === 0 ? <section className="message-panel" role="status"><h2>No evidence-backed opportunities yet</h2><p>Create a grounded analysis after real evidence is available and the model provider is configured.</p></section> : <ol className="opportunity-list">{opportunities.map(opportunity => { const form = forms[opportunity.id] ?? { name: "", target: "" }; const existing = products.filter(product => product.opportunity_id === opportunity.id); return <li className="opportunity-record" key={opportunity.id}><header><span className="state">{opportunity.status}</span><h2>{opportunity.title}</h2></header><p>{opportunity.summary}</p><p><strong>Next action:</strong> {opportunity.next_action}</p><p className="evidence-line">Evidence: {opportunity.evidence_ids.join(", ")}</p>{existing.length ? <p>{existing.length} product task(s) already bound.</p> : null}<form className="action-form" onSubmit={event => { event.preventDefault(); setActionError(null); void (async () => { try { const product = await createProduct({ opportunity_id: opportunity.id, name: form.name, target_user: form.target }); setNotice(`Created product ${product.id}`); try { setState({ kind: "ready", data: await loadOpportunities() }); } catch { setActionError("Product was created, but persisted facts could not be reloaded."); } } catch (error) { setActionError(error instanceof Error ? error.message : "Product creation failed."); } })(); }}><label>Product name<input aria-label={`Product name for ${opportunity.title}`} required value={form.name} onChange={event => setForms(current => ({ ...current, [opportunity.id]: { ...form, name: event.target.value } }))} /></label><label>Target user<textarea aria-label={`Target user for ${opportunity.title}`} required value={form.target} onChange={event => setForms(current => ({ ...current, [opportunity.id]: { ...form, target: event.target.value } }))} /></label><button type="submit">Create product for {opportunity.title}</button></form></li>; })}</ol>}
  </main>;
}
