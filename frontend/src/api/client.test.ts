import { afterEach, describe, expect, it, vi } from "vitest";

import {
  fetchAccountNotes,
  fetchAccountProfile,
  fetchJob,
  fetchNoteSearchResults,
  startAccountCollection,
  startNoteSearch,
  fetchAccounts,
  startQianfanCollection,
  fetchContentMediaAssessment,
  fetchContentMediaRun,
  fetchContentMediaRuns,
  startContentImageAnalysis,
  startContentImageGeneration,
} from "./client";

afterEach(() => vi.unstubAllGlobals());

describe("paginated API reads", () => {
  it("loads account pages past the backend 100-row maximum", async () => {
    const first = Array.from({ length: 100 }, (_, index) => ({ user_id: `id-${index}` }));
    const second = [{ user_id: "id-100" }];
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => first })
      .mockResolvedValueOnce({ ok: true, json: async () => second });
    vi.stubGlobal("fetch", fetchMock);

    await expect(fetchAccounts()).resolves.toHaveLength(101);
    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/v1/radar/accounts?limit=100&offset=0", expect.any(Object));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/v1/radar/accounts?limit=100&offset=100", expect.any(Object));
  });
});

describe("content media API", () => {
  it("submits only trusted media identities and reads durable results", async () => {
    const run = { id: "run-1", status: "queued" };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => run })
      .mockResolvedValueOnce({ ok: true, json: async () => run })
      .mockResolvedValueOnce({ ok: true, json: async () => [run] })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ ...run, status: "succeeded" }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ run_id: "run-1", assessment: { plan_match: true } }) });
    vi.stubGlobal("fetch", fetchMock);

    await startContentImageGeneration("item-1", {
      expected_revision_id: "revision-1",
      image_plan_entry_id: "plan-entry-1",
    });
    await startContentImageAnalysis("item-1", {
      expected_revision_id: "revision-1",
      material_ids: ["material-1"],
    });
    await fetchContentMediaRuns("item-1");
    await fetchContentMediaRun("run-1");
    await fetchContentMediaAssessment("run-1");

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/v1/content-items/item-1/image-generations", expect.objectContaining({ method: "POST", body: '{"expected_revision_id":"revision-1","image_plan_entry_id":"plan-entry-1"}' }));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/v1/content-items/item-1/image-analyses", expect.objectContaining({ method: "POST", body: '{"expected_revision_id":"revision-1","material_ids":["material-1"]}' }));
    expect(fetchMock).toHaveBeenNthCalledWith(3, "/api/v1/content-items/item-1/media-runs", expect.any(Object));
    expect(fetchMock).toHaveBeenNthCalledWith(4, "/api/v1/content-media-runs/run-1", expect.any(Object));
    expect(fetchMock).toHaveBeenNthCalledWith(5, "/api/v1/content-media-runs/run-1/assessment", expect.any(Object));
  });
});

describe("Qianfan collection API", () => {
  it("posts only the strict expected count to the controlled start route", async () => {
    const response = { collection_id: "collection-1", scopes: [] };
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => response });
    vi.stubGlobal("fetch", fetchMock);

    await expect(startQianfanCollection({ expected_count_per_scope: 20 })).resolves.toEqual(response);
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/radar/qianfan-collections", expect.objectContaining({ method: "POST", body: '{"expected_count_per_scope":20}' }));
  });
});

describe("XHS account and note collection API", () => {
  it("uses only the strict Task 3 account collection and public read routes", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ job_id: "job-account", status: "queued" }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ user_id: "u-1" }) })
      .mockResolvedValueOnce({ ok: true, json: async () => [] })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ id: "job-account" }) });
    vi.stubGlobal("fetch", fetchMock);

    await startAccountCollection("u-1", { sample_limit: 10 });
    await fetchAccountProfile("u-1");
    await fetchAccountNotes("u-1");
    await fetchJob("job-account");

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/v1/accounts/u-1/collections", expect.objectContaining({ method: "POST", body: '{"sample_limit":10}' }));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/v1/accounts/u-1/profile", expect.any(Object));
    expect(fetchMock).toHaveBeenNthCalledWith(3, "/api/v1/accounts/u-1/notes", expect.any(Object));
    expect(fetchMock).toHaveBeenNthCalledWith(4, "/api/v1/jobs/job-account", expect.any(Object));
  });

  it("uses the strict search collection route and reads results only by returned job id", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ job_id: "job-search", status: "queued" }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ job_id: "job-search", items: [] }) });
    vi.stubGlobal("fetch", fetchMock);

    await startNoteSearch({ keyword: "露营收纳", expected_count: 1 });
    await fetchNoteSearchResults("job-search");

    expect(fetchMock).toHaveBeenNthCalledWith(1, "/api/v1/notes/search-collections", expect.objectContaining({ method: "POST", body: '{"keyword":"露营收纳","expected_count":1}' }));
    expect(fetchMock).toHaveBeenNthCalledWith(2, "/api/v1/note-search-results?job_id=job-search", expect.any(Object));
  });
});
