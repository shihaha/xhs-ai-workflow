from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from backend.app.features.content_research.benchmark_service import BenchmarkResearchService
from backend.app.features.xhs.service import CollectionFactNotFound, SearchNoteRead, SearchResultsRead
from backend.app.main import create_app
from backend.app.models.jobs import JobState
from backend.app.settings import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeXhsSearchService:
    def __init__(self, job_service) -> None:
        self.job_service = job_service
        self.results: dict[str, SearchResultsRead] = {}
        self.submissions: list[tuple[str, int, str]] = []

    def submit_search(self, keyword: str, expected_count: int):
        job = self.job_service.create(
            job_type="xhs_note_search",
            input_data={"keyword": keyword, "expected_count": expected_count},
            progress_total=expected_count,
            current_stage="xhs_collection_reserved",
        )
        self.submissions.append((keyword, expected_count, job.id))
        return job

    def get_search_results(self, job_id: str) -> SearchResultsRead:
        result = self.results.get(job_id)
        if result is None:
            raise CollectionFactNotFound(f"No trusted result for {job_id}")
        return result

    def complete(self, job_id: str, keyword: str, count: int, *, overlap: str | None = None) -> None:
        self.job_service.transition(job_id, JobState.running)
        self.job_service.transition(
            job_id,
            JobState.succeeded,
            progress_current=count,
            progress_total=count,
            current_stage="xhs_collection_succeeded",
        )
        items = [
            SearchNoteRead(
                note_id=(overlap if overlap is not None and index == 0 else f"{job_id}-{index}"),
                source_url=f"https://www.xiaohongshu.com/explore/{overlap if overlap is not None and index == 0 else f'{job_id}-{index}'}",
                title=f"note {index}",
                summary="benchmark summary",
                user_id=f"user-{index}",
            )
            for index in range(count)
        ]
        self.results[job_id] = SearchResultsRead(
            job_id=job_id,
            keyword=keyword,
            expected_count=count,
            succeeded_count=count,
            artifact_id=len(self.results) + 1,
            collected_at=datetime(2026, 8, 23, 0, 0, 0),
            items=items,
        )


def _app(tmp_path: Path):
    runtime = tmp_path / "runtime"
    app = create_app(
        Settings(runtime_dir=runtime, database_path=runtime / "workbench.sqlite3")
    )
    fake = FakeXhsSearchService(app.state.job_service)
    app.state.benchmark_research_service = BenchmarkResearchService(
        app.state.database, fake
    )
    return app, fake


async def _seed_product_and_keywords(client: httpx.AsyncClient) -> tuple[str, list[dict[str, object]]]:
    dossier = await client.post(
        "/api/v1/content-research/dossiers",
        json={
            "product_key": "seven-sins-test",
            "name": "七宗罪测试",
            "version": "1.0.0",
            "target_user": "希望了解性格倾向的用户",
            "core_need": "低门槛获得可理解的测试结果",
            "deliverables": ["测试题", "结果解释"],
            "usage_instructions": "作答后查看结果。",
            "faq": [],
            "allowed_claims": ["提供七宗罪主题的人格测试体验"],
            "forbidden_claims": ["用于疾病诊断"],
            "source_index": ["产品说明.md"],
            "uat_status": "passed",
        },
    )
    assert dossier.status_code == 201
    dossier_id = dossier.json()["id"]
    plan = await client.put(
        f"/api/v1/content-research/dossiers/{dossier_id}/keywords",
        json={
            "items": [
                {
                    "keyword": f"七宗罪测试-{index}",
                    "category": "main" if index <= 2 else "audience",
                    "expand": index == 1,
                    "scope": "benchmark",
                    "target_count": 5 + (index % 6),
                }
                for index in range(1, 11)
            ]
        },
    )
    assert plan.status_code == 200
    return dossier_id, plan.json()["items"]


@pytest.mark.anyio
async def test_full_collection_requires_trusted_two_note_probe(tmp_path: Path) -> None:
    app, fake = _app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        dossier_id, keywords = await _seed_product_and_keywords(client)
        keyword = keywords[0]

        blocked = await client.post(
            f"/api/v1/content-research/dossiers/{dossier_id}/benchmark-searches",
            json={"keyword_item_id": keyword["id"], "stage": "full"},
        )
        assert blocked.status_code == 409
        assert fake.submissions == []

        probe = await client.post(
            f"/api/v1/content-research/dossiers/{dossier_id}/benchmark-searches",
            json={"keyword_item_id": keyword["id"], "stage": "probe"},
        )
        assert probe.status_code == 202
        assert probe.json()["expected_count"] == 2
        assert probe.json()["job_state"] == "queued"
        probe_job = probe.json()["xhs_job_id"]

        still_blocked = await client.post(
            f"/api/v1/content-research/dossiers/{dossier_id}/benchmark-searches",
            json={"keyword_item_id": keyword["id"], "stage": "full"},
        )
        assert still_blocked.status_code == 409

        fake.complete(probe_job, keyword["keyword"], 2)
        full = await client.post(
            f"/api/v1/content-research/dossiers/{dossier_id}/benchmark-searches",
            json={"keyword_item_id": keyword["id"], "stage": "full"},
        )

    assert full.status_code == 202
    assert full.json()["expected_count"] == keyword["target_count"]
    assert fake.submissions == [
        (keyword["keyword"], 2, probe_job),
        (keyword["keyword"], keyword["target_count"], full.json()["xhs_job_id"]),
    ]


@pytest.mark.anyio
async def test_overview_reads_only_trusted_full_results_and_deduplicates_notes(
    tmp_path: Path,
) -> None:
    app, fake = _app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        dossier_id, keywords = await _seed_product_and_keywords(client)
        full_jobs: list[tuple[dict[str, object], str]] = []
        for keyword in keywords[:2]:
            probe = await client.post(
                f"/api/v1/content-research/dossiers/{dossier_id}/benchmark-searches",
                json={"keyword_item_id": keyword["id"], "stage": "probe"},
            )
            fake.complete(probe.json()["xhs_job_id"], keyword["keyword"], 2)
            full = await client.post(
                f"/api/v1/content-research/dossiers/{dossier_id}/benchmark-searches",
                json={"keyword_item_id": keyword["id"], "stage": "full"},
            )
            full_jobs.append((keyword, full.json()["xhs_job_id"]))

        first_keyword, first_job = full_jobs[0]
        second_keyword, second_job = full_jobs[1]
        fake.complete(first_job, first_keyword["keyword"], first_keyword["target_count"], overlap="shared-note")
        fake.complete(second_job, second_keyword["keyword"], second_keyword["target_count"], overlap="shared-note")

        overview = await client.get(
            f"/api/v1/content-research/dossiers/{dossier_id}/benchmarks"
        )

    assert overview.status_code == 200
    body = overview.json()
    expected_unique = first_keyword["target_count"] + second_keyword["target_count"] - 1
    assert body["unique_full_note_count"] == expected_unique
    shared = next(item for item in body["unique_full_notes"] if item["note_id"] == "shared-note")
    assert set(shared["source_keywords"]) == {
        first_keyword["keyword"], second_keyword["keyword"]
    }
    assert len(shared["source_search_ids"]) == 2
    assert all(
        search["result_status"] == "trusted"
        for search in body["searches"]
        if search["stage"] == "full"
    )


@pytest.mark.anyio
async def test_old_keyword_run_cannot_start_new_benchmark_collection(tmp_path: Path) -> None:
    app, _fake = _app(tmp_path)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        dossier_id, old_keywords = await _seed_product_and_keywords(client)
        replacement = await client.put(
            f"/api/v1/content-research/dossiers/{dossier_id}/keywords",
            json={
                "items": [
                    {
                        "keyword": f"新关键词-{index}",
                        "category": "main" if index == 1 else "scenario",
                        "expand": index == 1,
                        "scope": "benchmark",
                        "target_count": 5,
                    }
                    for index in range(1, 11)
                ]
            },
        )
        assert replacement.status_code == 200

        response = await client.post(
            f"/api/v1/content-research/dossiers/{dossier_id}/benchmark-searches",
            json={"keyword_item_id": old_keywords[0]["id"], "stage": "probe"},
        )

    assert response.status_code == 409
    assert "current keyword-plan run" in response.json()["detail"]
