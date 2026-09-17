#!/usr/bin/env python3
"""
Ask TypeSafe's jev model whether SemMedDB sentences support each triple.

Sends each sampled triple to the TypeSafe System One API
(https://api.typesafe.ai/v1/systemone, docs at https://docs.typesafe.ai/api) as two Noul
questions -- one phrased as a question, one as a statement -- and records the returned
probabilities alongside this repo's own verdict, which is held out of the request.

Usage:
    python jev/validate_with_typesafe.py --self-check
    python jev/validate_with_typesafe.py --dry-run --limit 1
    python jev/validate_with_typesafe.py --limit 1
    python jev/validate_with_typesafe.py --limit 10
    python jev/validate_with_typesafe.py

Requires TYPESAFE_API_KEY in the environment or in .env.
"""

from __future__ import annotations

import argparse
import csv
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
#   v1: full and lean state shapes, two questions (question- and statement-phrased).
#   v2: one state shape (identifiers kept -- they disambiguate bare gene symbols), the
#       statement phrasing only (the two phrasings agreed to within 0.020 over 93 documents),
#       and criteria tightened against reading co-occurrence as support.
#   v3: v2 with the original criteria restored. The tightened wording shifted every
#       probability down ~0.047 without improving discrimination (AUC 0.908 -> 0.899, best
#       achievable accuracy 87.1% either way), so it was paying tokens to move the scale.
#       v3 vs v2 isolates the criteria; v3 vs v1 isolates dropping the second question.
PROMPT_VERSION = 3

# Probabilities in this band are reported as "maybe" rather than yes/no. Judgment call, not a
# fitted value: jev's probability does NOT reconstruct this repo's own maybe class (those 7
# documents scored anywhere from 0.16 to 0.92), so this is an abstain zone, nothing more.
# Applied when the CSV is written, so widening it costs a rewrite, not new API calls.
MAYBE_BAND = (0.4, 0.6)

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

STATE_FIELDS = (
    "subject_curie",
    "subject_name",
    "predicate",
    "object_curie",
    "object_name",
    "PMID",
    "SemMedDB_sentences",
)

CRITERIA = {
    "true": "The sentences state, or directly imply, the asserted relationship between the subject and object",
    "false": "The sentences do not state the relationship, or state the opposite",
}

QUESTIONS = {
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

# Readable fields copied onto each record so the CSV view needs no lookup back to the sample.
NAME_FIELDS = ("subject_name", "object_name")

# The CSV is a view over the JSONL: what the other LLM decided, what jev decided, how long it
# took. The JSONL keeps the variant, so any slice (lean only, full only, ...) can be re-derived
# later without re-calling the API.
CSV_COLUMNS = (
    "subject_curie",
    "subject_name",
    "predicate",
    "object_curie",
    "object_name",
    "PMID",
    "repo_support",
    "jev_support",
    "q_statement",
    "variant",
    "prompt_version",
    "elapsed_s",
    "error",
)

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


def variant_label(extra_fields: list[str]) -> str:
    """Label that goes in the resume key, so extra fields can't collide with earlier runs."""
    return "+".join(["base"] + extra_fields)


def build_payload(doc: dict, extra_fields: list[str]) -> dict:
    fields = STATE_FIELDS + tuple(extra_fields)
    state = {f: doc[f] for f in fields if f in doc}
    return {"state": state, "model": MODEL, "questions": QUESTIONS}


def classify(probability, band: tuple[float, float]) -> str:
    """yes / maybe / no, the same three classes this repo's own pipeline reports."""
    if probability is None or probability == "":
        return ""
    if band[0] <= probability <= band[1]:
        return "maybe"
    return "yes" if probability > band[1] else "no"


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


def write_csv(jsonl_path: Path, csv_path: Path, band: tuple[float, float]) -> int:
    """Rewrite the CSV view from the whole JSONL. Cheap, so just redo it after every run."""
    rows = []
    for line in jsonl_path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        row = {c: record.get(c, "") for c in CSV_COLUMNS}
        # Derived here, not stored: rebanding is then a CSV rewrite, never a new API call.
        row["jev_support"] = classify(record.get("q_statement"), band)
        rows.append(row)
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


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
    docs = json.loads((project_root / "jev" / "sample_100_triples.json").read_text())
    doc = docs[0]

    # Check the state's keys, not the serialized payload: the questions legitimately contain
    # the word "support", so a substring match over the whole payload cries wolf.
    state = build_payload(doc, [])["state"]
    leaked = sorted(set(state) & set(HELD_OUT_FIELDS))
    assert not leaked, f"state leaks this repo's own output: {leaked}"
    assert set(state) <= set(doc), "state invented fields"
    assert len(state) == 7

    assert "supporting_sentences" in build_payload(doc, ["supporting_sentences"])["state"]
    assert variant_label(["supporting_sentences"]) == "base+supporting_sentences"

    assert parse_noul({"answers": {"q_statement": {"noul": 0.9}}}, "q_statement") == 0.9
    for bad in ({"answers": {}}, {"answers": {"q_statement": {}}}, {}):
        try:
            parse_noul(bad, "q_statement")
        except ValueError:
            pass
        else:
            raise AssertionError(f"should have rejected {bad}")

    # Band edges are inclusive on both sides, so neither 0.4 nor 0.6 may read as a verdict.
    assert [classify(p, (0.4, 0.6)) for p in (0.0, 0.39, 0.4, 0.5, 0.6, 0.61, 1.0)] == [
        "no", "no", "maybe", "maybe", "maybe", "yes", "yes"
    ]
    assert classify(None, (0.4, 0.6)) == ""

    print("self-check passed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--input", default="jev/sample_100_triples.json")
    parser.add_argument("--output", default="jev/typesafe_jev_results.jsonl")
    parser.add_argument("--csv", help="CSV view of the results (default: --output with .csv)")
    parser.add_argument("--limit", type=int, help="Only process the first N documents")
    parser.add_argument("--extra-fields", default="", help="Comma-separated extra state fields")
    parser.add_argument(
        "--maybe-band",
        default=",".join(str(b) for b in MAYBE_BAND),
        help="lo,hi probability band reported as 'maybe' (applied when writing the CSV)",
    )
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
    extra_fields = [f.strip() for f in args.extra_fields.split(",") if f.strip()]
    band = tuple(float(b) for b in args.maybe_band.split(","))
    if len(band) != 2 or not 0 <= band[0] <= band[1] <= 1:
        sys.exit(f"--maybe-band must be lo,hi within 0..1, got {args.maybe_band}")

    if args.dry_run:
        for doc in docs:
            print(json.dumps(build_payload(doc, extra_fields), indent=2))
        logger.info("Dry run: %d payload(s), nothing sent", len(docs))
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
                label = variant_label(extra_fields)
                key = tuple(doc[f] for f in KEY_FIELDS) + (label, PROMPT_VERSION)
                if key in done:
                    logger.debug("Already done: %s", key)
                    continue

                record = {f: doc[f] for f in KEY_FIELDS}
                record.update({f: doc.get(f) for f in NAME_FIELDS})
                record.update(
                    variant=label,
                    prompt_version=PROMPT_VERSION,
                    repo_support=doc.get("support"),
                    repo_predicted=doc.get("predicted"),
                    ts=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                )

                # Round trip only: request out, response back, including any retry waits.
                # Excludes reading the sample, writing results and the inter-call delay.
                query_started = time.monotonic()
                response = post_json(build_payload(doc, extra_fields), api_key)
                record["elapsed_s"] = round(time.monotonic() - query_started, 3)
                calls += 1
                logger.debug("Raw response: %s", json.dumps(response))
                usage = response.get("usage") or {}
                input_tokens += usage.get("input_tokens", 0)
                output_tokens += usage.get("output_tokens", 0)

                try:
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
                        "%s %.3f -> %-5s in %.2fs (repo said %s)",
                        doc["PMID"], record["q_statement"],
                        classify(record["q_statement"], band), record["elapsed_s"],
                        record["repo_support"],
                    )
                time.sleep(args.delay)
    except KeyboardInterrupt:
        logger.warning("Interrupted -- everything completed so far is saved")

    csv_path = Path(args.csv) if args.csv else output_path.with_suffix(".csv")
    if not csv_path.is_absolute():
        csv_path = project_root / csv_path
    csv_rows = write_csv(output_path, csv_path, band) if output_path.exists() else 0

    logger.info("=" * 70)
    logger.info("SUMMARY")
    logger.info("  calls:         %d (%d failed)", calls, failures)
    logger.info("  input tokens:  %d", input_tokens)
    logger.info("  output tokens: %d", output_tokens)
    logger.info("  elapsed:       %.1fs", time.time() - started)
    logger.info("  results:       %s", output_path)
    logger.info("  csv view:      %s (%d rows)", csv_path, csv_rows)
    logger.info("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())
