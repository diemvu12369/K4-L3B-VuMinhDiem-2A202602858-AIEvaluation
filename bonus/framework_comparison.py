"""Exercise 3.4 (bonus) — RAGAS vs DeepEval on the same 20 benchmark traces.

Both frameworks score exactly the same input that the lab heuristics scored:
question, recorded actual answer, retrieved contexts (artifacts/actual_answers.json)
and the golden expected answer (golden_dataset.json). Both use the same judge
model so differences come from the frameworks' prompts and scoring logic.

Run from the repo root (needs bonus/requirements-bonus.txt installed):

    python bonus/framework_comparison.py

Configuration comes from .env / environment (never hard-coded):
    OPENAI_API_KEY   key for the OpenAI-compatible endpoint
    OPENAI_BASE_URL  optional, e.g. https://openrouter.ai/api/v1
    OPENAI_MODEL     judge model, e.g. gpt-4o-mini or openai/gpt-4o-mini
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import warnings
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
os.environ.setdefault("RAGAS_DO_NOT_TRACK", "true")
warnings.filterwarnings("ignore")

API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
BASE_URL = os.getenv("OPENAI_BASE_URL") or None
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
EMBED_MODEL = "openai/text-embedding-3-small" if BASE_URL else "text-embedding-3-small"

MAX_TOKENS = 1024

METRICS = ("faithfulness", "answer_relevancy", "context_recall", "context_precision")


def load_cases() -> list[dict[str, Any]]:
    golden = json.loads((ROOT / "golden_dataset.json").read_text(encoding="utf-8"))
    actual = json.loads((ROOT / "artifacts/actual_answers.json").read_text(encoding="utf-8"))
    bench = json.loads((ROOT / "artifacts/benchmark_results.json").read_text(encoding="utf-8"))
    gold_by_id = {p["id"]: p for p in golden["qa_pairs"]}
    lab_by_id = {r["id"]: r for r in bench["results"]}
    cases = []
    for record in actual["answers"]:
        gold = gold_by_id[record["id"]]
        lab = lab_by_id[record["id"]]
        cases.append({
            "id": record["id"],
            "question": gold["question"],
            "answer": record["actual_answer"],
            "contexts": [c["text"] for c in record["retrieved_contexts"]],
            "reference": gold["expected_answer"],
            "lab": {
                "faithfulness": lab["faithfulness"],
                "answer_relevancy": lab["relevance"],
                "context_recall": lab["context_recall"],
                "context_precision": lab["context_precision"],
                "passed": lab["passed"],
            },
        })
    return cases


# --------------------------------------------------------------------------- RAGAS

def run_ragas(cases: list[dict[str, Any]]) -> tuple[list[dict[str, float | None]], float]:
    from langchain_openai import ChatOpenAI, OpenAIEmbeddings
    from ragas import EvaluationDataset, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import (
        Faithfulness,
        LLMContextPrecisionWithReference,
        LLMContextRecall,
        ResponseRelevancy,
    )
    from ragas.run_config import RunConfig

    llm = LangchainLLMWrapper(
        ChatOpenAI(model=MODEL, api_key=API_KEY, base_url=BASE_URL, temperature=0,
                   max_tokens=MAX_TOKENS)
    )
    embeddings = LangchainEmbeddingsWrapper(
        OpenAIEmbeddings(model=EMBED_MODEL, api_key=API_KEY, base_url=BASE_URL,
                         check_embedding_ctx_length=False)
    )
    dataset = EvaluationDataset.from_list([
        {
            "user_input": c["question"],
            "response": c["answer"],
            "retrieved_contexts": c["contexts"],
            "reference": c["reference"],
        }
        for c in cases
    ])
    started = time.perf_counter()
    result = evaluate(
        dataset,
        metrics=[
            Faithfulness(),
            ResponseRelevancy(),
            LLMContextRecall(),
            LLMContextPrecisionWithReference(),
        ],
        llm=llm,
        embeddings=embeddings,
        run_config=RunConfig(max_workers=4, timeout=180),
        show_progress=True,
    )
    elapsed = time.perf_counter() - started
    frame = result.to_pandas()
    column = {
        "faithfulness": "faithfulness",
        "answer_relevancy": "answer_relevancy",
        "context_recall": "context_recall",
        "context_precision": "llm_context_precision_with_reference",
    }
    rows = []
    for _, row in frame.iterrows():
        rows.append({m: _num(row.get(col)) for m, col in column.items()})
    return rows, elapsed


# ------------------------------------------------------------------------ DeepEval

def _make_deepeval_model():
    from deepeval.models import DeepEvalBaseLLM
    from openai import AsyncOpenAI, OpenAI

    class CompatJudge(DeepEvalBaseLLM):
        """DeepEval judge backed by any OpenAI-compatible endpoint."""

        def __init__(self) -> None:
            self.client = OpenAI(api_key=API_KEY, base_url=BASE_URL)
            self.aclient = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL)
            super().__init__(MODEL)

        def load_model(self):
            return self.client

        def _request(self, prompt: str, schema: Any) -> dict[str, Any]:
            kwargs: dict[str, Any] = {
                "model": MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                # Without a cap the endpoint reserves the model's max output
                # (16k tokens) per request, which exhausts small credit balances.
                "max_tokens": MAX_TOKENS,
            }
            if schema is not None:
                kwargs["response_format"] = {"type": "json_object"}
            return kwargs

        @staticmethod
        def _parse(text: str, schema: Any):
            if schema is None:
                return text
            match = re.search(r"\{.*\}", text, re.DOTALL)
            return schema.model_validate_json(match.group(0) if match else text)

        def generate(self, prompt: str, schema: Any = None):
            response = self.client.chat.completions.create(**self._request(prompt, schema))
            return self._parse(response.choices[0].message.content or "", schema)

        async def a_generate(self, prompt: str, schema: Any = None):
            response = await self.aclient.chat.completions.create(**self._request(prompt, schema))
            return self._parse(response.choices[0].message.content or "", schema)

        def get_model_name(self) -> str:
            return f"{MODEL} (OpenAI-compatible)"

    return CompatJudge()


def run_deepeval(cases: list[dict[str, Any]]) -> tuple[list[dict[str, float | None]], float]:
    from deepeval.metrics import (
        AnswerRelevancyMetric,
        ContextualPrecisionMetric,
        ContextualRecallMetric,
        FaithfulnessMetric,
    )
    from deepeval.test_case import LLMTestCase

    judge = _make_deepeval_model()
    factories = {
        "faithfulness": FaithfulnessMetric,
        "answer_relevancy": AnswerRelevancyMetric,
        "context_recall": ContextualRecallMetric,
        "context_precision": ContextualPrecisionMetric,
    }
    rows = []
    started = time.perf_counter()
    for index, case in enumerate(cases, start=1):
        test_case = LLMTestCase(
            input=case["question"],
            actual_output=case["answer"],
            expected_output=case["reference"],
            retrieval_context=case["contexts"],
        )
        row: dict[str, float | None] = {}
        for name, factory in factories.items():
            metric = factory(model=judge, threshold=0.5, async_mode=False, include_reason=False)
            try:
                metric.measure(test_case)
                row[name] = _num(metric.score)
            except Exception as exc:  # keep the run going; record the gap
                print(f"  DeepEval {name} failed on {case['id']}: {exc}", file=sys.stderr)
                row[name] = None
        rows.append(row)
        print(f"DeepEval {index:02d}/{len(cases)} {case['id']} done", flush=True)
    return rows, time.perf_counter() - started


# --------------------------------------------------------------------------- utils

def _num(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if number != number else round(number, 4)  # NaN -> None


def _mean(values: list[float | None]) -> float | None:
    present = [v for v in values if v is not None]
    return round(sum(present) / len(present), 4) if present else None


def _pearson(xs: list[float | None], ys: list[float | None]) -> float | None:
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    if len(pairs) < 3:
        return None
    mx = sum(x for x, _ in pairs) / len(pairs)
    my = sum(y for _, y in pairs) / len(pairs)
    cov = sum((x - mx) * (y - my) for x, y in pairs)
    vx = sum((x - mx) ** 2 for x, _ in pairs)
    vy = sum((y - my) ** 2 for _, y in pairs)
    return round(cov / (vx * vy) ** 0.5, 3) if vx and vy else None


def main() -> int:
    if not API_KEY:
        print("ERROR: OPENAI_API_KEY is missing (.env)", file=sys.stderr)
        return 2
    cases = load_cases()
    print(f"Judge model: {MODEL} | base_url: {BASE_URL or 'OpenAI default'} | cases: {len(cases)}")

    ragas_rows, ragas_seconds = run_ragas(cases)
    deepeval_rows, deepeval_seconds = run_deepeval(cases)

    per_case = []
    for case, r_row, d_row in zip(cases, ragas_rows, deepeval_rows):
        per_case.append({"id": case["id"], "lab": case["lab"], "ragas": r_row, "deepeval": d_row})

    summary: dict[str, Any] = {}
    for metric in METRICS:
        lab = [c["lab"][metric] for c in per_case]
        rag = [c["ragas"][metric] for c in per_case]
        dee = [c["deepeval"][metric] for c in per_case]
        summary[metric] = {
            "missing": {"ragas": rag.count(None), "deepeval": dee.count(None)},
            "lab_mean": _mean(lab),
            "ragas_mean": _mean(rag),
            "deepeval_mean": _mean(dee),
            "pearson_ragas_deepeval": _pearson(rag, dee),
            "pearson_lab_ragas": _pearson(lab, rag),
            "pearson_lab_deepeval": _pearson(lab, dee),
        }

    output = {
        "judge_model": MODEL,
        "base_url": BASE_URL,
        "embedding_model_ragas": EMBED_MODEL,
        "runtime_seconds": {"ragas": round(ragas_seconds, 1), "deepeval": round(deepeval_seconds, 1)},
        "summary": summary,
        "cases": per_case,
    }
    out_path = ROOT / "artifacts/framework_comparison.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print("\n| Metric | Lab heuristic | RAGAS | DeepEval | r(RAGAS, DeepEval) |")
    print("|---|---:|---:|---:|---:|")
    for metric, s in summary.items():
        print(f"| {metric} | {s['lab_mean']} | {s['ragas_mean']} | {s['deepeval_mean']} | "
              f"{s['pearson_ragas_deepeval']} |")
    print(f"\nRuntime: RAGAS {ragas_seconds:.0f}s, DeepEval {deepeval_seconds:.0f}s")
    print(f"Saved: {out_path}")
    missing = sum(s["missing"]["ragas"] + s["missing"]["deepeval"] for s in summary.values())
    if missing:
        print(f"WARNING: {missing} metric scores are missing (API errors/NaN); "
              "means and correlations above are NOT comparable.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
