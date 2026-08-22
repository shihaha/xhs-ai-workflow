import { useCallback, useEffect, useRef, useState } from "react";

import {
  createFinishedProductDossier,
  fetchFinishedProductDossiers,
  fetchKeywordPlan,
  type FinishedProductDossier,
  type FinishedProductDossierCreate,
  type KeywordPlan,
} from "../api/contentResearch";

type ResearchData = {
  dossiers: FinishedProductDossier[];
  keywordPlans: Record<string, KeywordPlan>;
};

type Draft = {
  product_key: string;
  name: string;
  version: string;
  target_user: string;
  core_need: string;
  deliverables: string;
  usage_instructions: string;
  faq: string;
  allowed_claims: string;
  forbidden_claims: string;
  source_index: string;
};

const EMPTY_DRAFT: Draft = {
  product_key: "",
  name: "",
  version: "",
  target_user: "",
  core_need: "",
  deliverables: "",
  usage_instructions: "",
  faq: "",
  allowed_claims: "",
  forbidden_claims: "",
  source_index: "",
};

const defaultLoad = async (): Promise<ResearchData> => {
  const dossiers = await fetchFinishedProductDossiers();
  const keywordPlans: Record<string, KeywordPlan> = {};
  await Promise.all(dossiers.map(async dossier => {
    keywordPlans[dossier.id] = await fetchKeywordPlan(dossier.id);
  }));
  return { dossiers, keywordPlans };
};

export interface ContentResearchPageProps {
  loadResearch?: () => Promise<ResearchData>;
  createDossier?: (payload: FinishedProductDossierCreate) => Promise<FinishedProductDossier>;
}

export function ContentResearchPage({
  loadResearch = defaultLoad,
  createDossier = createFinishedProductDossier,
}: ContentResearchPageProps) {
  const [state, setState] = useState<
    | { kind: "loading" }
    | { kind: "error" }
    | { kind: "ready"; data: ResearchData }
  >({ kind: "loading" });
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [pending, setPending] = useState(false);
  const pendingRef = useRef(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      setState({ kind: "ready", data: await loadResearch() });
    } catch {
      setState({ kind: "error" });
    }
  }, [loadResearch]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const submit = async () => {
    if (pendingRef.current) return;
    pendingRef.current = true;
    setPending(true);
    setActionError(null);
    setNotice(null);
    try {
      const payload: FinishedProductDossierCreate = {
        product_key: draft.product_key.trim(),
        name: draft.name.trim(),
        version: draft.version.trim(),
        target_user: draft.target_user.trim(),
        core_need: draft.core_need.trim(),
        deliverables: lines(draft.deliverables),
        usage_instructions: draft.usage_instructions.trim(),
        faq: faqLines(draft.faq),
        allowed_claims: lines(draft.allowed_claims),
        forbidden_claims: lines(draft.forbidden_claims),
        source_index: lines(draft.source_index),
        uat_status: "passed",
      };
      const created = await createDossier(payload);
      setNotice(`已接入成品资料：${created.name} ${created.version}`);
      setDraft(EMPTY_DRAFT);
      setState({ kind: "ready", data: await loadResearch() });
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "成品资料接入失败。" );
    } finally {
      pendingRef.current = false;
      setPending(false);
    }
  };

  if (state.kind === "loading") {
    return <main className="workbench-page" id="main-content"><section className="loading-panel"><p role="status">正在加载成品资料</p></section></main>;
  }
  if (state.kind === "error") {
    return <main className="workbench-page" id="main-content"><section className="message-panel" role="alert"><h1>无法加载成品资料</h1><p>内容研究接口没有返回有效结果。</p><button type="button" onClick={() => void refresh()}>重新加载</button></section></main>;
  }

  return (
    <main className="workbench-page" id="main-content">
      <header className="page-heading">
        <p className="eyebrow">Phase D · Finished Product Input</p>
        <h1>成品资料</h1>
        <p>这里是内容系统入口，只接收已经制作完成并经过人工 UAT 的产品。产品研究与产品制作不在本工作台自动化。</p>
      </header>

      {notice ? <p className="action-notice" role="status">{notice}</p> : null}
      {actionError ? <p className="action-error" role="alert">{actionError}</p> : null}

      <section className="operator-panel">
        <div className="panel-heading"><h2>已接入产品</h2><p>{state.data.dossiers.length} 个版本</p></div>
        {state.data.dossiers.length === 0 ? <p className="panel-empty">还没有完成 UAT 的成品资料。</p> : (
          <div className="record-stack">
            {state.data.dossiers.map(dossier => (
              <article className="studio-record" key={dossier.id}>
                <header>
                  <div><h3>{dossier.name}</h3><code>{dossier.product_key} · {dossier.version}</code></div>
                  <span className="state state--succeeded">UAT 已通过</span>
                </header>
                <p><strong>目标用户：</strong>{dossier.target_user}</p>
                <p><strong>核心需求：</strong>{dossier.core_need}</p>
                <p><strong>交付物：</strong>{dossier.deliverables.join("；")}</p>
                <p><strong>可用 claims：</strong>{dossier.allowed_claims.join("；")}</p>
                <p><strong>关键词网络：</strong>{state.data.keywordPlans[dossier.id]?.count ?? 0} / 10–20</p>
              </article>
            ))}
          </div>
        )}
      </section>

      <section className="operator-panel">
        <div className="panel-heading"><h2>接入一个已完成产品</h2><p>不会创建或修改产品本体</p></div>
        <form className="action-form" onSubmit={event => { event.preventDefault(); void submit(); }}>
          <label>稳定产品标识<input required pattern="[A-Za-z0-9._-]+" value={draft.product_key} onChange={event => setDraft({ ...draft, product_key: event.target.value })} placeholder="例如 seven-sins-test" /></label>
          <label>产品名称<input required value={draft.name} onChange={event => setDraft({ ...draft, name: event.target.value })} /></label>
          <label>产品版本<input required value={draft.version} onChange={event => setDraft({ ...draft, version: event.target.value })} placeholder="例如 1.0.0" /></label>
          <label>目标用户<textarea required value={draft.target_user} onChange={event => setDraft({ ...draft, target_user: event.target.value })} /></label>
          <label>核心需求<textarea required value={draft.core_need} onChange={event => setDraft({ ...draft, core_need: event.target.value })} /></label>
          <label>实际交付物（每行一项）<textarea required value={draft.deliverables} onChange={event => setDraft({ ...draft, deliverables: event.target.value })} /></label>
          <label>使用方式<textarea required value={draft.usage_instructions} onChange={event => setDraft({ ...draft, usage_instructions: event.target.value })} /></label>
          <label>FAQ（每行“问题 | 答案”）<textarea value={draft.faq} onChange={event => setDraft({ ...draft, faq: event.target.value })} /></label>
          <label>允许表达的产品 claims（每行一项）<textarea required value={draft.allowed_claims} onChange={event => setDraft({ ...draft, allowed_claims: event.target.value })} /></label>
          <label>禁止表达的 claims（每行一项）<textarea value={draft.forbidden_claims} onChange={event => setDraft({ ...draft, forbidden_claims: event.target.value })} /></label>
          <label>原始资料索引（每行一个真实文件或资料名）<textarea required value={draft.source_index} onChange={event => setDraft({ ...draft, source_index: event.target.value })} /></label>
          <p className="boundary-note">提交即表示该版本产品本体已经完成并通过人工 UAT；这里不会替你研究产品或制作产品。</p>
          <button disabled={pending} type="submit">接入成品资料</button>
        </form>
      </section>
    </main>
  );
}

function lines(value: string): string[] {
  return value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
}

function faqLines(value: string): Array<{ question: string; answer: string }> {
  return lines(value).map(row => {
    const separator = row.indexOf("|");
    if (separator < 1 || separator === row.length - 1) {
      throw new Error("FAQ 每一行都必须使用“问题 | 答案”格式。" );
    }
    return {
      question: row.slice(0, separator).trim(),
      answer: row.slice(separator + 1).trim(),
    };
  });
}
