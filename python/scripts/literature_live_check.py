"""Live answer-contract check against a running Scholar Assistant API.

This is the manual companion to the automated layers: it drives the real service
with a real model and prints what the answer contract produced, so a reviewer can
see the evidence chain instead of trusting a summary.

    python scripts/literature_live_check.py --provider fixture
    python scripts/literature_live_check.py --provider arxiv \
        --query 'all:"retrieval augmented generation"' --max-results 2 \
        --question "What retrieval or evaluation protocol do these papers use?"

It needs a server that is already running (``python api.py``) and a configured
model.  Exit codes: 0 answered, 1 insufficient, 2 transport or service failure.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.net_env import normalize_proxy_env  # noqa: E402

normalize_proxy_env()


def _post(client, path: str, payload: dict[str, Any]):
    return client.post(path, json=payload)


def _create_project(client, location: Path, name: str) -> str:
    response = _post(
        client,
        "/api/project/create",
        {
            "name": name,
            "location": str(location),
            "template_id": "research_paper",
            "init_git": False,
        },
    )
    if response.status_code != 200:
        raise SystemExit(f"project create failed: {response.status_code} {response.text[:200]}")
    return str(response.json()["project_path"])


def _search_and_index(
    client, project: str, provider: str, query: str, question: str, limit: int
) -> list[str]:
    search = _post(
        client,
        "/api/literature/search",
        {
            "provider": provider,
            "plan": {
                "research_question": question,
                "suggested_query": query,
                "generation_method": "user",
                "generation_model": None,
                "generation_config": {"source": "literature_live_check"},
            },
            "query": {
                "query": query,
                "page": 1,
                "page_size": limit,
                "sort_by": "relevance",
                "sort_order": "descending",
                "filters": {"year_from": None, "year_to": None, "categories": []},
            },
        },
    )
    if search.status_code != 200:
        raise SystemExit(f"search failed: {search.status_code} {search.text[:200]}")
    execution = search.json()
    records = execution["page"]["records"]
    print(f"search: {execution['page']['result_mode']} mode, {len(records)} record(s)")
    if not records:
        return []

    imported = _post(
        client,
        "/api/literature/import",
        {
            "project_path": project,
            "search_execution_id": execution["search_execution_id"],
            "paper_ids": [record["paper_id"] for record in records],
        },
    )
    if imported.status_code != 200:
        raise SystemExit(f"import failed: {imported.status_code} {imported.text[:200]}")
    source_ids = [item["source_id"] for item in imported.json()["results"]]
    print(f"imported: {len(source_ids)} source(s)")

    indexed: list[str] = []
    source_by_paper = {
        str(record["paper_id"]): str(item["source_id"])
        for record, item in zip(records, imported.json()["results"], strict=False)
    }

    for source_id in source_ids:
        acquired = _post(
            client,
            "/api/literature/fulltext",
            {"project_path": project, "source_id": source_id, "force": False},
        )
        body = acquired.json()
        if acquired.status_code != 200:
            detail = body.get("detail") if isinstance(body, dict) else None
            reason = detail.get("code") if isinstance(detail, dict) else None
            print(f"  acquire {source_id}: {reason or acquired.status_code}")
            continue
        result = _post(
            client, "/api/literature/index", {"project_path": project, "source_id": source_id}
        ).json()
        print(f"  index {source_id}: {result.get('status')} ({result.get('page_count')} pages)")
        indexed.append(source_id)

    if not indexed and provider == "fixture":
        # The offline corpus never declares open full text, so the check attaches
        # its synthetic PDFs the same way the fixed demo script does.
        indexed = _attach_corpus_pdfs(client, project, source_by_paper)
    return indexed


def _attach_corpus_pdfs(client, project: str, source_by_paper: dict[str, str]) -> list[str]:
    from src.literature.demo_corpus import DemoCorpusError, load_demo_corpus, materialize_pdf

    try:
        corpus = load_demo_corpus()
    except DemoCorpusError as exc:
        print(f"  attach: demo corpus unavailable ({exc.code})")
        return []

    wanted = {record.paper_id for record in corpus.records_to_index()}
    workspace = Path(tempfile.mkdtemp(prefix="literature-live-check-pdf-"))
    indexed: list[str] = []
    for paper_id, source_id in source_by_paper.items():
        if paper_id not in wanted:
            continue
        pdf_path = materialize_pdf(corpus, paper_id, workspace / f"{paper_id}.pdf")
        with pdf_path.open("rb") as stream:
            attached = client.post(
                "/api/project/sources/import",
                data={"project_path": project, "source_id": source_id},
                files={"file": (pdf_path.name, stream, "application/pdf")},
            )
        if attached.status_code != 200:
            print(f"  attach {source_id}: {attached.status_code}")
            continue
        result = _post(
            client, "/api/literature/index", {"project_path": project, "source_id": source_id}
        ).json()
        print(f"  attach+index {source_id}: {result.get('status')}")
        indexed.append(source_id)
    return indexed


def _print_answer(payload: dict[str, Any]) -> None:
    print(f"\nstatus: {payload['status']}")
    if payload.get("insufficient_reason"):
        print(f"insufficient_reason: {payload['insufficient_reason']}")
    print(f"retrieved_chunk_count: {payload.get('retrieved_chunk_count')}")
    claims = payload.get("claims") or []
    if claims:
        model = claims[0]
        print(
            "model: "
            f"{model.get('model_provider')}/{model.get('model_name')} "
            f"config_hash={str(model.get('model_config_hash'))[:16]}"
        )
    print(f"\nclaims ({len(claims)}):")
    for index, claim in enumerate(claims, start=1):
        print(f"  {index}. [{claim['evidence_status']}] {claim['text'][:110]}")
        print(f"     cites {claim['evidence_ids']}")
    print(f"\nevidence ({len(payload.get('evidence') or [])}):")
    for item in payload.get("evidence") or []:
        span = item.get("span") or {}
        quote = (span.get("exact_quote") or "").replace("\n", " ")
        print(f"  - p.{span.get('page_start')} {str(item.get('title'))[:46]} :: {quote[:70]}")
    rejected = payload.get("rejected_claims") or []
    print(f"\nrejected_claims ({len(rejected)}):")
    for claim in rejected:
        print(f"  - {claim['reason']}: {claim['text'][:80]}")
    unresolved = payload.get("unresolved") or []
    print(f"unresolved ({len(unresolved)}): {[item.get('reason') for item in unresolved]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Live answer-contract check")
    parser.add_argument("--base-url", default="http://127.0.0.1:18088")
    parser.add_argument("--provider", default="fixture", choices=["fixture", "arxiv"])
    parser.add_argument("--query", default='all:"evidence traceable question answering"')
    parser.add_argument(
        "--question",
        default="What evaluation protocol do the demo papers use for evidence checking?",
    )
    parser.add_argument("--max-results", type=int, default=2)
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args(argv)

    import httpx

    root = Path(tempfile.mkdtemp(prefix="literature-live-check-"))
    with httpx.Client(base_url=args.base_url, timeout=600.0) as client:
        project = _create_project(client, root, "Live Check")
        source_ids = _search_and_index(
            client, project, args.provider, args.query, args.question, args.max_results
        )
        if not source_ids:
            print("\nno indexed source: the answer step cannot run")
            return 1
        answer = _post(
            client,
            "/api/literature/answer",
            {
                "project_path": project,
                "question": args.question,
                "source_ids": source_ids,
                "top_k": args.top_k,
            },
        )
        print(f"\nanswer http: {answer.status_code}")
        if answer.status_code != 200:
            print(json.dumps(answer.json(), ensure_ascii=False, indent=2))
            return 2
        payload = answer.json()
        _print_answer(payload)
    return 0 if payload["status"] == "answered" else 1


if __name__ == "__main__":
    raise SystemExit(main())
