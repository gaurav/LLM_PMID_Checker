#!/usr/bin/env python3
"""
Ask TypeSafe's jev model whether SemMedDB sentences support each triple.

Sends each sampled triple to the TypeSafe System One API
(https://api.typesafe.ai/v1/systemone, docs at https://docs.typesafe.ai/api) as two Noul
questions -- one phrased as a question, one as a statement -- and records the returned
probabilities alongside this repo's own verdict, which is held out of the request.

Usage:
    python scripts/validate_with_typesafe.py --self-check
    python scripts/validate_with_typesafe.py --dry-run --limit 1
    python scripts/validate_with_typesafe.py --limit 1
    python scripts/validate_with_typesafe.py --limit 10
    python scripts/validate_with_typesafe.py

Requires TYPESAFE_API_KEY in the environment or in .env.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API_URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-latest"
TIMEOUT = 120
MAX_ATTEMPTS = 5
RETRYABLE_STATUS = {429, 500, 502, 503, 504, 529}

# Bump whenever the wording, criteria or state shape below change: it is part of the resume
# key, so without a bump a rerun would silently mix answers from two different prompts.
PROMPT_VERSION = 1

# Everything from "predicted" onward in the sample files is this repo's own LLM output and
# must never reach the API -- that is the answer we are asking for.
HELD_OUT_FIELDS = (
    "predicted",
    "support",
    "subject_mentioned",
    "object_mentioned",
    "supporting_sentences",
    "reasoning",
)

STATE_FIELDS = {
    "full": (
        "subject_curie",
        "subject_name",
        "predicate",
        "object_curie",
        "object_name",
        "PMID",
        "SemMedDB_sentences",
    ),
    "lean": ("subject_name", "predicate", "object_name", "SemMedDB_sentences"),
}

CRITERIA = {
    "true": "The sentences state, or directly imply, the asserted relationship between the subject and object",
    "false": "The sentences do not state the relationship, or state the opposite",
}

QUESTIONS = {
    "q_question": {
        "type": "noul",
        "instructions": (
            "Do the sentences in 'SemMedDB_sentences', drawn from scientific publications, "
            "support the assertion encoded by the triple 'subject_name' / 'predicate' "
            "(a Biolink Model predicate) / 'object_name'?"
        ),
        "criteria": CRITERIA,
    },
    "q_statement": {
        "type": "noul",
        "instructions": (
            "The sentences in 'SemMedDB_sentences', drawn from scientific publications, "
            "support the assertion encoded by the triple 'subject_name' / 'predicate' "
            "(a Biolink Model predicate) / 'object_name'."
        ),
        "criteria": CRITERIA,
    },
}

KEY_FIELDS = ("subject_curie", "predicate", "object_curie", "PMID")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

project_root = Path(__file__).resolve().parent.parent


def get_api_key() -> str:
    """TYPESAFE_API_KEY from the environment, else from .env (dotenv is not installed here)."""
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    source = "environment"
    if not key:
        env_file = project_root / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                name, _, value = line.partition("=")
                if name.strip() == "TYPESAFE_API_KEY":
                    key = value.strip().strip("\"'")
                    source = str(env_file)
                    break
    if not key:
        sys.exit("TYPESAFE_API_KEY is not set. Put it in .env or export it.")
    # An exported shell variable silently beats .env, so say which one won -- but never the key.
    logger.info("Using API key from %s (%d chars, ...%s)", source, len(key), key[-4:])
    return key


def variant_label(shape: str, extra_fields: list[str]) -> str:
    """Label that goes in the resume key, so extra fields can't collide with earlier runs."""
    return "+".join([shape] + extra_fields)


def build_payload(doc: dict, shape: str, extra_fields: list[str]) -> dict:
    fields = STATE_FIELDS[shape] + tuple(extra_fields)
    state = {f: doc[f] for f in fields if f in doc}
    return {"state": state, "model": MODEL, "questions": QUESTIONS}


def post_json(payload: dict, api_key: str) -> dict:
    """POST once, retrying transient failures with exponential backoff."""
    body = json.dumps(payload).encode("utf-8")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    for attempt in range(1, MAX_ATTEMPTS + 1):
        wait = 2**attempt
        try:
            request = Request(API_URL, data=body, headers=headers, method="POST")
            with urlopen(request, timeout=TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as e:  # must precede URLError -- HTTPError subclasses it
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code not in RETRYABLE_STATUS:
                logger.error("HTTP %s (not retryable): %s", e.code, detail)
                logger.error("Payload was: %s", json.dumps(payload)[:500])
                sys.exit(f"Giving up on HTTP {e.code}")
            retry_after = e.headers.get("Retry-After")
            if retry_after:
                try:
                    wait = float(retry_after)
                except ValueError:
                    pass
            logger.warning(
                "HTTP %s (attempt %d/%d), retrying in %.0fs: %s",
                e.code, attempt, MAX_ATTEMPTS, wait, detail,
            )
        except (URLError, TimeoutError, OSError) as e:
            logger.warning("Request error (attempt %d/%d): %s", attempt, MAX_ATTEMPTS, e)
        except json.JSONDecodeError as e:
            logger.warning("Non-JSON response (attempt %d/%d): %s", attempt, MAX_ATTEMPTS, e)

        if attempt < MAX_ATTEMPTS:
            time.sleep(wait)

    raise RuntimeError(f"Failed after {MAX_ATTEMPTS} attempts")


def parse_noul(response: dict, key: str) -> float:
    """Pull one probability out of the response, refusing anything that isn't one."""
    answer = (response.get("answers") or {}).get(key)
    if not isinstance(answer, dict) or "noul" not in answer:
        raise ValueError(f"no '{key}' answer in response: {json.dumps(response)[:300]}")
    value = float(answer["noul"])
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"'{key}' out of range: {value}")
    return value


def load_done(output_path: Path) -> set:
    """Resume keys already in the output file, tolerating a torn final line."""
    done = set()
    if not output_path.exists():
        return done
    for line in output_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("Skipping unparseable line in %s", output_path)
            continue
        if "error" in record:
            continue
        done.add(
            tuple(record.get(f) for f in KEY_FIELDS)
            + (record.get("variant"), record.get("prompt_version"))
        )
    return done


def self_check() -> int:
    """Offline assertions. The first one is the one that matters."""
    docs = json.loads((project_root / "data" / "sample_100_triples.json").read_text())
    doc = docs[0]

    # Check the state's keys, not the serialized payload: the questions legitimately contain
    # the word "support", so a substring match over the whole payload cries wolf.
    for shape in ("full", "lean"):
        state = build_payload(doc, shape, [])["state"]
        leaked = sorted(set(state) & set(HELD_OUT_FIELDS))
        assert not leaked, f"{shape} state leaks this repo's own output: {leaked}"
        assert set(state) <= set(doc), f"{shape} state invented fields"

    assert len(build_payload(doc, "lean", [])["state"]) == 4
    assert len(build_payload(doc, "full", [])["state"]) == 7
    assert "supporting_sentences" in build_payload(doc, "lean", ["supporting_sentences"])["state"]
    assert variant_label("lean", ["supporting_sentences"]) == "lean+supporting_sentences"

    assert parse_noul({"answers": {"q_question": {"noul": 0.9}}}, "q_question") == 0.9
    for bad in ({"answers": {}}, {"answers": {"q_question": {}}}, {}):
        try:
            parse_noul(bad, "q_question")
        except ValueError:
            pass
        else:
            raise AssertionError(f"should have rejected {bad}")

    print("self-check passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--input", default="data/sample_100_triples.json")
    parser.add_argument("--output", default="data/typesafe_jev_results.jsonl")
    parser.add_argument("--limit", type=int, help="Only process the first N documents")
    parser.add_argument("--variants", default="full,lean")
    parser.add_argument("--extra-fields", default="", help="Comma-separated extra state fields")
    parser.add_argument("--delay", type=float, default=0.5, help="Seconds between calls")
    parser.add_argument("--dry-run", action="store_true", help="Print payloads, send nothing")
    parser.add_argument("--self-check", action="store_true", help="Offline assertions only")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    if args.self_check:
        return self_check()

    input_path = Path(args.input)
    if not input_path.is_absolute():
        input_path = project_root / input_path
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = project_root / output_path

    docs = json.loads(input_path.read_text())
    if args.limit:
        docs = docs[: args.limit]
    shapes = [s.strip() for s in args.variants.split(",") if s.strip()]
    extra_fields = [f.strip() for f in args.extra_fields.split(",") if f.strip()]

    if args.dry_run:
        for doc in docs:
            for shape in shapes:
                print(json.dumps(build_payload(doc, shape, extra_fields), indent=2))
        logger.info("Dry run: %d payload(s), nothing sent", len(docs) * len(shapes))
        return 0

    api_key = get_api_key()
    done = load_done(output_path)
    if done:
        logger.info("Resuming: %d (document, variant) results already in %s", len(done), output_path.name)

    calls = failures = input_tokens = output_tokens = 0
    started = time.time()

    try:
        with output_path.open("a") as out:
            for doc in docs:
                for shape in shapes:
                    label = variant_label(shape, extra_fields)
                    key = tuple(doc[f] for f in KEY_FIELDS) + (label, PROMPT_VERSION)
                    if key in done:
                        logger.debug("Already done: %s", key)
                        continue

                    record = {f: doc[f] for f in KEY_FIELDS}
                    record.update(
                        variant=label,
                        prompt_version=PROMPT_VERSION,
                        repo_support=doc.get("support"),
                        repo_predicted=doc.get("predicted"),
                        ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    )

                    response = post_json(build_payload(doc, shape, extra_fields), api_key)
                    calls += 1
                    logger.debug("Raw response: %s", json.dumps(response))
                    usage = response.get("usage") or {}
                    input_tokens += usage.get("input_tokens", 0)
                    output_tokens += usage.get("output_tokens", 0)

                    try:
                        record["q_question"] = parse_noul(response, "q_question")
                        record["q_statement"] = parse_noul(response, "q_statement")
                    except ValueError as e:
                        failures += 1
                        record["error"] = str(e)
                        logger.error("%s [%s]: %s", doc["PMID"], label, e)

                    record["model"] = response.get("model")
                    record["input_tokens"] = usage.get("input_tokens")
                    record["output_tokens"] = usage.get("output_tokens")

                    out.write(json.dumps(record) + "\n")
                    out.flush()

                    if "error" not in record:
                        logger.info(
                            "%s [%s] question=%.3f statement=%.3f (repo said %s)",
                            doc["PMID"], label, record["q_question"], record["q_statement"],
                            record["repo_support"],
                        )
                    time.sleep(args.delay)
    except KeyboardInterrupt:
        logger.warning("Interrupted -- everything completed so far is saved")

    logger.info("=" * 70)
    logger.info("SUMMARY")
    logger.info("  calls:         %d (%d failed)", calls, failures)
    logger.info("  input tokens:  %d", input_tokens)
    logger.info("  output tokens: %d", output_tokens)
    logger.info("  elapsed:       %.1fs", time.time() - started)
    logger.info("  results:       %s", output_path)
    logger.info("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
