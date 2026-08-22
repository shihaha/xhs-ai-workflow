import { useCallback, useEffect, useRef, useState } from "react";

import {
  createFinishedProductDossier,
  fetchBenchmarkOverview,
  fetchFinishedProductDossiers,
  fetchKeywordPlan,
  generateKeywordPlan as postGenerateKeywordPlan,
  startBenchmarkSearch as postStartBenchmarkSearch,
  type BenchmarkOverview,
  type BenchmarkSearch,
  type FinishedProductDossier,
  type FinishedProductDossierCreate,
  type KeywordPlan,
  type KeywordPlanItem,
} from "../api/contentResearch";

type ResearchData = {
  dossiers: FinishedProductDossier[];
  keywordPlans: Record<string, KeywordPlan>;
  benchmarkOverviews?: Record<string, BenchmarkOverview>;
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
  const benchmarkOverviews: Record<string, BenchmarkOverview> = {};
  await Promise.all(dossiers.map(async dossier => {
    const [plan, benchmarks] = await Promise.all([
      fetchKeywordPlan(dossier.id),
      fetchBenchmarkOverview(dossier.id),
    ]);
    keywordPlans[dossier.id] = plan;
    benchmarkOverviews[dossier.id] = benchmarks;
  }));
  return { dossiers, keywordPlans, benchmarkOverviews };
};

export interface ContentResearchPageProps {
  loadResearch?: () => Promise<ResearchData>;
  createDossier?: (payload: FinishedProductDossierCreate) => Promise<FinishedProductDossier>;
  generateKeywordPlan?: (dossierId: string) => Promise<KeywordPlan>;
  startBenchmarkSearch?: (
    dossierId: string,
    keywordItemId: string,
    stage: "probe" | "full",
  ) => Promise<BenchmarkSearch>;
}

export function ContentResearchPage({
  loadResearch = defaultLoad,
  createDossier = createFinishedProductDossier,
  generateKeywordPlan = postGenerateKeywordPlan,
  startBenchmarkSearch = postStartBenchmarkSearch,
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

  const runAction = async (action: () => Promise<string>) => {
    if (pendingRef.current) return;
    pendingRef.current = true;
    setPending(true);
    setActionError(null);
    setNotice(null);
    try {
      setNotice(await action());
      setState({ kind: "ready", data: await loadResearch() });
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "操作失败。" );
    } finally {
      pendingRef.current = false;
      setPending(false);
    }
  };

  const submit = async () => {
    await runAction(async () => {
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
      setDraft(EMPTY_DRAFT);
      return `已接入成品资料：${created.name} ${created.version}`;
    });
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
            {state.data.dossiers.map(dossier => {
              const plan = state.data.keywordPlans[dossier.id];
              const benchmarks = state.data.benchmarkOverviews?.[dossier.id];
              return (
                <article className="studio-record" key={dossier.id}>
                  <header>
                    <div><h3>{dossier.name}</h3><code>{dossier.product_key} · {dossier.version}</code></div>
                    <span className="state state--succeeded">UAT 已通过</span>
                  </header>
                  <p><strong>目标用户：</strong>{dossier.target_user}</p>
                  <p><strong>核心需求：</strong>{dossier.core_need}</p>
                  <p><strong>交付物：</strong>{dossier.deliverables.join("；")}</p>
                  <p><strong>可用 claims：</strong>{dossier.allowed_claims.join("；")}</p>
                  <div className="button-row">
                    <button
                      disabled={pending}
                      type="button"
                      onClick={() => void runAction(async () => {
                        const generated = await generateKeywordPlan(dossier.id);
                        return `已生成关键词网络：${dossier.name} · ${generated.count} 个词`;
                      })}
                    >
                      {plan?.count ? "重新生成关键词网络" : "AI 生成关键词网络"}
                    </button>
                  </div>
                  <KeywordPlanView
                    plan={plan}
                    benchmarks={benchmarks}
                    pending={pending}
                    onStart={(keywordItemId, stage) => void runAction(async () => {
                      const search = await startBenchmarkSearch(dossier.id, keywordItemId, stage);
                      const label = stage === "probe" ? "测试采集" : "完整采集";
                      return `${label}已进入任务队列：${search.keyword} · ${search.expected_count} 篇`;
                    })}
                  />
                </article>
              );
            })}
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

function KeywordPlanView({
  plan,
  benchmarks,
  pending,
  onStart,
}: {
  plan?: KeywordPlan;
  benchmarks?: BenchmarkOverview;
  pending: boolean;
  onStart: (keywordItemId: string, stage: "probe" | "full") => void;
}) {
  if (!plan?.run_id || plan.count === 0) {
    return <p className="field-help">还没有关键词网络。教程要求每个成品建立 10–20 个找对标用的关键词。</p>;
  }
  return (
    <section aria-label="关键词网络">
      <p className="field-help">
        最新 Run：{plan.source === "ai" ? "AI" : "人工"} · {plan.count} 个词
        {plan.model ? ` · ${plan.model}` : ""}
        {plan.prompt_version ? ` · ${plan.prompt_version}` : ""}
        {benchmarks ? ` · 去重后完整对标 ${benchmarks.unique_full_note_count} 篇` : ""}
      </p>
      <div className="table-scroll">
        <table>
          <thead><tr><th>顺序</th><th>关键词</th><th>类型</th><th>扩展</th><th>目标</th><th>对标采集</th></tr></thead>
          <tbody>
            {plan.items.map(item => (
              <tr key={item.id}>
                <td>{item.position}</td>
                <td>{item.keyword}</td>
                <td>{item.category}</td>
                <td>{item.expand ? "是" : "否"}</td>
                <td>{item.target_count}</td>
                <td><BenchmarkAction item={item} overview={benchmarks} pending={pending} onStart={onStart} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function BenchmarkAction({
  item,
  overview,
  pending,
  onStart,
}: {
  item: KeywordPlanItem;
  overview?: BenchmarkOverview;
  pending: boolean;
  onStart: (keywordItemId: string, stage: "probe" | "full") => void;
}) {
  const probe = latestSearch(overview, item.id, "probe");
  const full = latestSearch(overview, item.id, "full");

  if (full?.result_status === "trusted") {
    return <span className="state state--succeeded">已完成 {full.succeeded_count}/{full.expected_count}</span>;
  }
  if (full) {
    if (full.job_state === "failed" || full.job_state === "cancelled") {
      return <button disabled={pending} type="button" onClick={() => onStart(item.id, "full")}>重试完整采集</button>;
    }
    if (full.job_state === "needs_human") {
      return <span className="state state--needs_human">完整采集需人工处理</span>;
    }
    if (full.job_state === "succeeded" && full.result_status === "untrusted") {
      return <span className="state state--failed">完整采集证据不可信</span>;
    }
    return <span className={`state state--${full.job_state}`}>完整采集 {full.progress_current}/{full.expected_count}</span>;
  }

  if (probe?.result_status === "trusted") {
    return <button disabled={pending} type="button" onClick={() => onStart(item.id, "full")}>采集 {item.target_count} 篇</button>;
  }
  if (probe) {
    if (probe.job_state === "failed" || probe.job_state === "cancelled") {
      return <button disabled={pending} type="button" onClick={() => onStart(item.id, "probe")}>重试测试采集</button>;
    }
    if (probe.job_state === "needs_human") {
      return <span className="state state--needs_human">测试采集需人工处理</span>;
    }
    if (probe.job_state === "succeeded" && probe.result_status === "untrusted") {
      return <span className="state state--failed">测试证据不可信</span>;
    }
    return <span className={`state state--${probe.job_state}`}>测试采集 {probe.progress_current}/2</span>;
  }

  return <button disabled={pending} type="button" onClick={() => onStart(item.id, "probe")}>测试采集 2 篇</button>;
}

function latestSearch(
  overview: BenchmarkOverview | undefined,
  keywordItemId: string,
  stage: "probe" | "full",
): BenchmarkSearch | undefined {
  return overview?.searches
    .filter(search => search.keyword_item_id === keywordItemId && search.stage === stage)
    .sort((a, b) => b.attempt - a.attempt)[0];
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
