import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchAccounts } from "./client";

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
