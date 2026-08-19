import { useCallback, useEffect, useRef, useState } from "react";

import {
  addProductMaterial, createContentItem, exportContentItem, fetchContentItems, fetchContentPackages,
  fetchProducts, regenerateContentItem, reviewContentItem, fetchContentMediaAssessment,
  fetchContentMediaRuns, startContentImageAnalysis, startContentImageGeneration,
  type ContentItem, type ContentPackage, type ContentMediaAssessment, type ContentMediaRun, type Product,
} from "../api/client";

type StudioData = { products: Product[]; items: ContentItem[]; packages: ContentPackage[]; mediaRuns?: Record<string, ContentMediaRun[]>; assessments?: Record<string, ContentMediaAssessment> };
type Action = (id: string, payload: Record<string, unknown>) => Promise<unknown>;
type GenerationAction = (id: string, payload: { expected_revision_id: string; image_plan_entry_id: string }) => Promise<unknown>;
type AnalysisAction = (id: string, payload: { expected_revision_id: string; material_ids: string[] }) => Promise<unknown>;
export interface ContentStudioPageProps {
  loadStudio?: () => Promise<StudioData>;
  addMaterial?: Action;
  createItem?: (payload: Record<string, unknown>) => Promise<unknown>;
  reviewItem?: Action;
  regenerateItem?: Action;
  exportItem?: Action;
  generateImage?: GenerationAction;
  analyzeImages?: AnalysisAction;
}
const defaultLoad = async () => {
  const [products, items, packages] = await Promise.all([fetchProducts(), fetchContentItems(), fetchContentPackages()]);
  const mediaRuns: Record<string, ContentMediaRun[]> = {};
  const assessments: Record<string, ContentMediaAssessment> = {};
  await Promise.all(items.map(async item => {
    const runs = await fetchContentMediaRuns(item.id);
    mediaRuns[item.id] = runs;
    await Promise.all(runs.filter(run => run.capability === "analyze" && run.status === "succeeded").map(async run => {
      assessments[run.id] = await fetchContentMediaAssessment(run.id);
    }));
  }));
  return { products, items, packages, mediaRuns, assessments };
};

export function ContentStudioPage({ loadStudio = defaultLoad, addMaterial = addProductMaterial, createItem = createContentItem, reviewItem = reviewContentItem, regenerateItem = regenerateContentItem, exportItem = exportContentItem, generateImage = startContentImageGeneration, analyzeImages = startContentImageAnalysis }: ContentStudioPageProps) {
  const [state, setState] = useState<{ kind: "loading" } | { kind: "error" } | { kind: "ready"; data: StudioData }>({ kind: "loading" });
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const pendingRef = useRef(false);
  const refresh = useCallback(async () => { setState({ kind: "loading" }); try { setState({ kind: "ready", data: await loadStudio() }); } catch { setState({ kind: "error" }); } }, [loadStudio]);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    if (state.kind !== "ready" || !Object.values(state.data.mediaRuns ?? {}).flat().some(run => run.status === "queued" || run.status === "running")) return;
    const timer = window.setTimeout(() => void refresh(), 500);
    return () => window.clearTimeout(timer);
  }, [state, refresh]);
  const run = async (action: () => Promise<unknown>, success: (value: unknown) => string) => { if (pendingRef.current) return; pendingRef.current = true; setPending(true); setActionError(null); try { const value = await action(); setNotice(success(value)); try { setState({ kind: "ready", data: await loadStudio() }); } catch { setActionError("Action completed, but persisted facts could not be reloaded."); } } catch (error) { setActionError(error instanceof Error ? error.message : "Action failed."); } finally { pendingRef.current = false; setPending(false); } };

  if (state.kind === "loading") return <main className="workbench-page" id="main-content"><section className="loading-panel" aria-busy="true"><p role="status">Loading content studio</p></section></main>;
  if (state.kind === "error") return <main className="workbench-page" id="main-content"><section className="message-panel" role="alert"><h1>Could not load content studio</h1><button type="button" onClick={() => void refresh()}>Retry content studio</button></section></main>;
  const { products, items, packages } = state.data;
  return <main className="workbench-page" id="main-content"><header className="page-heading"><p className="eyebrow">Human-reviewed production</p><h1>Content studio</h1><p>Create, review, regenerate and export a local pending-publication package. This workbench never publishes automatically.</p></header>
    {notice ? <p className="action-notice" role="status">{notice}</p> : null}{actionError ? <p className="action-error" role="alert">{actionError}</p> : null}
    {products.length === 0 && items.length === 0 && packages.length === 0 ? <section className="message-panel" role="status"><h2>No products or content items yet</h2><p>Create a product from an evidence-backed opportunity before producing content.</p></section> : null}
    {products.length ? <section className="operator-panel"><div className="panel-heading"><h2>Products and materials</h2><p>{products.length} returned</p></div><div className="record-stack">{products.map(product => <ProductWorkbench key={product.id} product={product} addMaterial={addMaterial} createItem={createItem} run={run} pending={pending} />)}</div></section> : null}
    {items.length ? <section className="operator-panel"><div className="panel-heading"><h2>Review queue</h2><p>{items.length} items</p></div><div className="record-stack">{items.map(item => <ContentReview key={item.id} item={item} product={products.find(product => product.id === item.product_id)} mediaRuns={state.data.mediaRuns?.[item.id] ?? []} assessments={state.data.assessments ?? {}} generateImage={generateImage} analyzeImages={analyzeImages} reviewItem={reviewItem} regenerateItem={regenerateItem} exportItem={exportItem} run={run} pending={pending} />)}</div></section> : null}
    {packages.length ? <section className="operator-panel"><div className="panel-heading"><h2>Pending-publication packages</h2></div><ol className="fact-list">{packages.map(pkg => <li key={pkg.id}><strong>{pkg.status === "failed" ? "Package failed" : pkg.status === "ready" && pkg.availability === "available" ? "Package available" : `Package ${pkg.availability}`}</strong><code>{pkg.path}</code><span>{pkg.size_bytes} bytes · {pkg.sha256}</span></li>)}</ol></section> : null}
  </main>;
}

function ProductWorkbench({ product, addMaterial, createItem, run, pending }: { product: Product; addMaterial: Action; createItem: (payload: Record<string, unknown>) => Promise<unknown>; run: (action: () => Promise<unknown>, success: (value: unknown) => string) => Promise<void>; pending: boolean }) {
  const [material, setMaterial] = useState({ logical_name: "", path: "", media_type: "image/png", kind: "source" });
  const [draft, setDraft] = useState({ template_key: "list-v1", evidence: "", sources: "", images: "", fact: "" });
  const ids = (value: string) => value.split(",").map(item => item.trim()).filter(Boolean);
  return <article className="studio-record"><header><h3>{product.name}</h3><span>{product.materials.length} materials</span></header><p>{product.target_user}</p><ul className="compact-list">{product.materials.map(item => <li key={item.id}><code>{item.id}</code> · {item.logical_name} · <span className={`state state--${item.availability}`}>{item.availability}</span></li>)}</ul>
    <details><summary>Add existing material (manual)</summary><form className="action-form" onSubmit={event => { event.preventDefault(); void run(() => addMaterial(product.id, material), () => "Manual material accepted. Refresh to use the persisted version."); }}><p className="field-help">This imports an existing runtime file. It is not AI-generated.</p><label>Logical filename<input required value={material.logical_name} onChange={event => setMaterial({ ...material, logical_name: event.target.value })} /></label><label>Runtime-relative source path<input required value={material.path} onChange={event => setMaterial({ ...material, path: event.target.value })} /></label><label>Media type<input required value={material.media_type} onChange={event => setMaterial({ ...material, media_type: event.target.value })} /></label><label>Material kind<select value={material.kind} onChange={event => setMaterial({ ...material, kind: event.target.value })}><option value="source">Source</option><option value="output_image">Output image</option></select></label><button disabled={pending} type="submit">Add manual material</button></form></details>
    <details><summary>Create model draft</summary><form className="action-form" onSubmit={event => { event.preventDefault(); const evidence = ids(draft.evidence); const sourceIds = ids(draft.sources); const imageIds = ids(draft.images); void run(() => createItem({ product_id: product.id, opportunity_id: product.opportunity_id, template_key: draft.template_key, evidence_ids: evidence, material_ids: sourceIds, image_material_ids: imageIds, cover_material_id: imageIds[0], research_facts: [{ fact: draft.fact, evidence_ids: evidence }] }), () => "Content draft request completed. Refresh to inspect its persisted state."); }}><label>Template<select value={draft.template_key} onChange={event => setDraft({ ...draft, template_key: event.target.value })}><option value="list-v1">List</option><option value="problem-solution-v1">Problem / solution</option></select></label><label>Evidence IDs, comma-separated<input required value={draft.evidence} onChange={event => setDraft({ ...draft, evidence: event.target.value })} /></label><label>Source material IDs, comma-separated<input value={draft.sources} onChange={event => setDraft({ ...draft, sources: event.target.value })} /></label><label>Ordered image material IDs<input required value={draft.images} onChange={event => setDraft({ ...draft, images: event.target.value })} /></label><label>Research fact<textarea required value={draft.fact} onChange={event => setDraft({ ...draft, fact: event.target.value })} /></label><button disabled={pending} type="submit">Generate content draft</button></form></details>
  </article>;
}

function ContentReview({ item, product, mediaRuns, assessments, generateImage, analyzeImages, reviewItem, regenerateItem, exportItem, run, pending }: { item: ContentItem; product?: Product; mediaRuns: ContentMediaRun[]; assessments: Record<string, ContentMediaAssessment>; generateImage: GenerationAction; analyzeImages: AnalysisAction; reviewItem: Action; regenerateItem: Action; exportItem: Action; run: (action: () => Promise<unknown>, success: (value: unknown) => string) => Promise<void>; pending: boolean }) {
  const revision = item.current_revision;
  const [note, setNote] = useState("");
  const [checks, setChecks] = useState<Record<string, string>>({});
  if (!revision) return <article className="studio-record"><header><h3>Content item {item.id}</h3><span className={`state state--${item.status}`}>{item.status}</span></header><p>No current revision was persisted.</p></article>;
  const visuals = item.image_material_ids.map(material_id => ({ material_id, passed: true, observation: checks[material_id] ?? "" }));
  return <article className="studio-record"><header><div><h3>{revision.title}</h3><code>{item.id} · revision {revision.number}</code></div><span className={`state state--${item.status}`}>{item.status}</span></header><p className="draft-body">{revision.body}</p><p className="evidence-line">Sources: {revision.source_evidence_ids.join(", ")}</p>
    <MediaWorkbench item={item} product={product} runs={mediaRuns} assessments={assessments} generateImage={generateImage} analyzeImages={analyzeImages} run={run} pending={pending} />
    {item.status === "review" ? <><label>Review note<input aria-label={`Review note for ${revision.title}`} value={note} onChange={event => setNote(event.target.value)} /></label>{item.image_material_ids.map(id => <label key={id}>Visual check {id}<input aria-label={`Visual check for ${id}`} value={checks[id] ?? ""} onChange={event => setChecks(current => ({ ...current, [id]: event.target.value }))} /></label>)}</> : null}<div className="button-row">{item.status === "review" ? <><button disabled={pending} type="button" onClick={() => void run(() => reviewItem(item.id, { decision: "reject", expected_revision_id: revision.id, actor: "local-operator", note, visual_checks: [] }), () => "Rejection persisted. Persisted facts were reloaded.")}>Reject {revision.title}</button><button type="button" disabled={pending || !note || visuals.some(check => !check.observation)} onClick={() => void run(() => reviewItem(item.id, { decision: "approve", expected_revision_id: revision.id, actor: "local-operator", note, visual_checks: visuals }), () => "Approval persisted. Persisted facts were reloaded.")}>Approve {revision.title}</button></> : null}{item.status === "rejected" ? <button disabled={pending} type="button" onClick={() => void run(() => regenerateItem(item.id, { expected_revision_id: revision.id }), () => "Regeneration request completed. Persisted facts were reloaded.")}>Regenerate {revision.title}</button> : null}{item.status === "approved" ? <button disabled={pending} type="button" onClick={() => void run(() => exportItem(item.id, { expected_revision_id: revision.id }), value => `Package available: ${(value as { path?: string }).path ?? "path not returned"}`)}>Export {revision.title}</button> : null}</div><p className="boundary-note">Export creates a local pending-publication ZIP only. No account action is performed.</p></article>;
}

function MediaWorkbench({ item, product, runs, assessments, generateImage, analyzeImages, run, pending }: { item: ContentItem; product?: Product; runs: ContentMediaRun[]; assessments: Record<string, ContentMediaAssessment>; generateImage: GenerationAction; analyzeImages: AnalysisAction; run: (action: () => Promise<unknown>, success: (value: unknown) => string) => Promise<void>; pending: boolean }) {
  const revision = item.current_revision;
  const generatedIds = runs.filter(value => value.capability === "generate" && value.status === "succeeded" && value.output_material_id).map(value => value.output_material_id as string);
  const [selected, setSelected] = useState<string[]>([]);
  if (!revision) return null;
  const retry = (mediaRun: ContentMediaRun) => {
    if (mediaRun.capability === "generate" && mediaRun.plan_entry_id) {
      const planEntryId = mediaRun.plan_entry_id;
      return run(() => generateImage(item.id, { expected_revision_id: revision.id, image_plan_entry_id: planEntryId }), () => "Image generation retry queued. Persisted state was reloaded.");
    }
    return run(() => analyzeImages(item.id, { expected_revision_id: revision.id, material_ids: mediaRun.allowed_material_ids }), () => "Visual analysis retry queued. Persisted state was reloaded.");
  };
  return <section className="media-workbench" aria-labelledby={`media-${item.id}`}>
    <div className="media-heading"><div><h4 id={`media-${item.id}`}>AI image workflow</h4><p>Generation creates managed output images. Visual analysis is advice only.</p></div></div>
    <ol className="media-plan-list">{revision.image_plan.map(entry => {
      const open = runs.some(value => value.capability === "generate" && value.plan_entry_id === entry.material_id && (value.status === "queued" || value.status === "running"));
      return <li key={`${entry.page_number}-${entry.material_id}`}><div><strong>Page {entry.page_number} · {entry.role}</strong><p>{entry.headline} — {entry.visual_direction}</p></div><button type="button" disabled={pending || open} onClick={() => void run(() => generateImage(item.id, { expected_revision_id: revision.id, image_plan_entry_id: entry.material_id }), () => "Image generation queued. Persisted state was reloaded.")}>Generate image for page {entry.page_number}</button></li>;
    })}</ol>
    {generatedIds.length ? <fieldset><legend>Managed generated images for visual analysis</legend>{generatedIds.map(id => <label className="check-label" key={id}><input type="checkbox" checked={selected.includes(id)} onChange={event => setSelected(current => event.target.checked ? [...current, id] : current.filter(value => value !== id))} />{id}</label>)}<button type="button" disabled={pending || selected.length === 0} onClick={() => void run(() => analyzeImages(item.id, { expected_revision_id: revision.id, material_ids: selected }), () => "Visual analysis queued. Persisted state was reloaded.")}>Analyze selected images</button></fieldset> : <p className="panel-empty">No generated image is available yet.</p>}
    {runs.length ? <ol className="media-run-list">{runs.map(mediaRun => {
      const material = product?.materials.find(value => value.id === mediaRun.output_material_id);
      const assessment = assessments[mediaRun.id];
      return <li key={mediaRun.id}><header><div><strong>{mediaRun.capability === "generate" ? "Image generation" : "Visual analysis"}</strong><code>{mediaRun.id}</code></div><span className={`state state--${mediaRun.status}`}>{mediaRun.status}</span></header><dl><div><dt>Model</dt><dd>{mediaRun.model}</dd></div><div><dt>Duration</dt><dd>{mediaRun.duration_ms === null ? "not reported" : `${mediaRun.duration_ms} ms`}</dd></div><div><dt>Usage</dt><dd>{Object.entries(mediaRun.usage).map(([key, value]) => `${key}: ${value}`).join(", ") || "not reported"}</dd></div></dl>{material ? <p><code>{material.id}</code> · {material.availability} managed output_image · {material.logical_name}</p> : null}{mediaRun.error_category ? <p className="action-error">{mediaRun.error_category}: {mediaRun.error_detail ?? "Media run did not complete."}</p> : null}{(mediaRun.status === "failed" || mediaRun.status === "needs_human") ? <button type="button" disabled={pending} onClick={() => void retry(mediaRun)}>{mediaRun.capability === "generate" ? `Retry image generation for page ${revision.image_plan.find(value => value.material_id === mediaRun.plan_entry_id)?.page_number ?? "?"}` : "Retry visual analysis"}</button> : null}{assessment ? <aside className="visual-advice"><h5>AI visual advice — human review still required</h5><p>{assessment.assessment.summary}</p><p>Plan match: {assessment.assessment.plan_match ? "yes" : "no"} · Text: {assessment.assessment.text_readability}</p>{assessment.assessment.suggestions.length ? <ul>{assessment.assessment.suggestions.map(value => <li key={value}>{value}</li>)}</ul> : null}</aside> : null}</li>;
    })}</ol> : null}
  </section>;
}
