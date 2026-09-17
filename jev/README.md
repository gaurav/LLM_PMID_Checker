# Validating SemMedDB triples with TypeSafe's Jev

An experiment in getting a **second opinion** on this repo's triple evaluations from a model
that is not a text-generating LLM. Self-contained: the script, the input sample, the raw
results and the findings all live in this directory.

This work is unlikely to be merged into the parent repo — it is here because the SemMedDB data
and this repo's own verdicts are here. If you are arriving from the pull request and want to
reuse any of it, [Findings](#findings) is the part worth taking.

## Start here: `typesafe_jev_v3.csv`

**[`typesafe_jev_v3.csv`](typesafe_jev_v3.csv) is the file to look at** — 100 rows, one per
triple, from prompt version 3 (the configuration we settled on: one statement-phrased Noul,
full state, cheapest of the four at 503 input tokens per call). The other result files hold all
400 calls across all four configurations, which is only interesting if you care how we got
here.

| Column | |
|---|---|
| `subject_curie`, `subject_name`, `predicate`, `object_curie`, `object_name`, `PMID` | the triple and the publication it was drawn from |
| `repo_support` | **this repo's** verdict: `yes` / `no` / `maybe`, from its gpt-oss pipeline reading the whole abstract |
| `jev_support` | **Jev's** verdict: `yes` / `no` / `maybe`, thresholded from `q_statement` at the 0.4–0.6 band |
| `q_statement` | the raw probability, 0–1, that the sentences support the triple — the actual model output, and what you want if you would rather pick your own threshold |
| `variant`, `prompt_version` | which configuration produced the row (all `base` / `3` in this file) |
| `elapsed_s` | round trip for that one query |
| `error` | non-empty only if the answer could not be parsed (empty throughout here) |

How the two systems line up across the 100:

| repo \ Jev | yes | maybe | no |
|---|---|---|---|
| **yes** | 47 | 3 | 4 |
| **maybe** | 3 | 3 | 1 |
| **no** | 7 | 7 | 25 |

75 outright agreements, 11 outright conflicts, 14 where one of the two abstains. Neither column
is ground truth — see [Caveats](#caveats).

## What Jev is

[Jev](https://docs.typesafe.ai) is TypeSafe's "System One" model. Unlike a chat LLM, it does
not generate text. You give it some **state** (a string, object or array) plus **typed
questions**, and it returns typed answers:

| Question type | Answer |
|---|---|
| **Noul** | the probability, 0–1, that the answer to a yes/no question is *yes* |
| **Choice** | a distribution over options you define, plus a `confidence` |
| **Score** | a rating against ordered levels you define, plus a `confidence` |

The whole API is one endpoint, `POST https://api.typesafe.ai/v1/systemone`, with a bearer
token. A request and its response:

```json
{"state": {"subject_name": "Leonurine", "predicate": "biolink:located_in", "...": "..."},
 "model": "jev-latest",
 "questions": {"q_statement": {"type": "noul",
                               "instructions": "The sentences ... support the assertion ...",
                               "criteria": {"true": "...", "false": "..."}}}}
```
```json
{"model": "jev-1.13.0",
 "answers": {"q_statement": {"type": "noul", "noul": 0.04}},
 "usage": {"input_tokens": 599, "output_tokens": 38}}
```

A calibrated number instead of prose is the appeal here: this repo's pipeline emits
`yes`/`no`/`maybe` plus free-text reasoning, and comparing two prose judgments is harder than
comparing a verdict to a probability. Note that **Noul answers carry no `confidence` field** —
only Choice and Score do.

## What we asked it

For each triple we send the SemMedDB sentences and the triple itself, and ask one Noul:

> The sentences in `SemMedDB_sentences`, drawn from scientific publications, support the
> assertion encoded by the triple `subject_name` / `predicate` (a Biolink Model predicate) /
> `object_name`.

**Everything from `predicted` onward in the input is this repo's own LLM output and is
stripped before sending** — otherwise we would be showing Jev the answer we are asking it for.
`--self-check` asserts this offline, because a leak here would not look like a bug: it would
look like Jev is excellent.

The state is the identifying fields plus the sentences:

```json
{"subject_curie": "NCBIGene:1154", "subject_name": "CISH", "predicate": "biolink:interacts_with",
 "object_curie": "UMLS:C0005456", "object_name": "Binding Sites", "PMID": "PMID:8157659",
 "SemMedDB_sentences": "..."}
```

Note this is a **weaker evidence base than this repo's own pipeline uses**: `main.py` reads the
entire PubMed abstract, while Jev sees only the SemMedDB sentences. Agreement numbers below
should be read with that in mind — we are not comparing two models on equal footing.

## Running it

```bash
python jev/validate_with_typesafe.py --self-check          # offline assertions, no network
python jev/validate_with_typesafe.py --dry-run --limit 1   # print the exact payload, send nothing
python jev/validate_with_typesafe.py --limit 10            # 10 documents
python jev/validate_with_typesafe.py                       # all 100
```

Needs `TYPESAFE_API_KEY` in `.env` or the environment (see `env.default`). Stdlib only — no
`typesafe-sdk`, no `requests`, and deliberately no import of `src/config.py`, so it runs in a
bare interpreter without the repo's conda environment.

| Flag | Default | |
|---|---|---|
| `--input` | `jev/sample_100_triples.json` | |
| `--output` | `jev/typesafe_jev_results.jsonl` | CSV view written alongside |
| `--limit` | all | first N documents |
| `--extra-fields` | *(none)* | extra state fields, e.g. `supporting_sentences` |
| `--maybe-band` | `0.4,0.6` | probabilities reported as `maybe` |
| `--delay` | `0.5` | seconds between calls |

Runs are **resumable and append-only**. The resume key is
`(subject_curie, predicate, object_curie, PMID, variant, prompt_version)`, so re-running costs
nothing for work already done, and changing the prompt means bumping `PROMPT_VERSION` rather
than silently mixing two prompts in one file.

### Files here

| File | |
|---|---|
| `validate_with_typesafe.py` | the script |
| `sample_100_triples.json` | 100 triples sampled uniformly over the 1,571,772 distinct triples in `results_with_names.parquet`, one random PMID row each |
| **`typesafe_jev_v3.csv`** | **the results to use** — 100 rows, one per triple, prompt v3 only |
| `typesafe_jev_results.jsonl` | raw results, all 400 calls, one line per (document, prompt version) — the full record, including per-call token counts |
| `typesafe_jev_results.csv` | the same 400 calls as CSV, rewritten from the JSONL after every run |

The v3 file is a filter of the 400-row CSV, so it can be regenerated at any time:

```bash
python3 -c "
import csv
src=list(csv.DictReader(open('jev/typesafe_jev_results.csv')))
rows=[r for r in src if r['prompt_version']=='3']
w=csv.DictWriter(open('jev/typesafe_jev_v3.csv','w',newline=''),fieldnames=src[0].keys())
w.writeheader(); w.writerows(rows)"
```

The sample was drawn with:

```sql
CREATE TEMP TABLE picked AS
  SELECT * FROM (SELECT DISTINCT subject_curie, predicate, object_curie
                 FROM 'results/results_with_names.parquet') USING SAMPLE 10000 ROWS;
SELECT DISTINCT ON (r.subject_curie, r.predicate, r.object_curie) r.*
  FROM 'results/results_with_names.parquet' r JOIN picked p USING (subject_curie, predicate, object_curie)
  ORDER BY r.subject_curie, r.predicate, r.object_curie, random();
```

## Findings

100 documents, 400 calls across four prompt configurations, ~230k tokens total. This repo's
verdicts on the sample: 54 `yes`, 39 `no`, 7 `maybe`.

### Agreement is ~87%, and the prompt is not the lever

| | questions | criteria | state | acc@0.5 | AUC | best acc | at threshold |
|---|---|---|---|---|---|---|---|
| v1 | 2 (question + statement) | original | full | 87.1% | 0.908 | 87.1% | 0.57 |
| v2 | 1 (statement) | tightened | full | 84.9% | 0.899 | 87.1% | 0.43 |
| v3 | 1 (statement) | original | full | 82.8% | 0.898 | 86.0% | 0.59 |

*(n=93, excluding this repo's 7 `maybe` documents.)*

Four prompt variations — two phrasings, two criteria wordings, two state shapes, one or two
questions — all land at **AUC 0.898–0.908** and **best achievable accuracy 86–87%**. The
`acc@0.5` spread is mostly an artifact of where 0.5 falls: a handful of documents sit near the
boundary and a 0.01 nudge flips them. **~87% agreement looks like a ceiling for this setup**,
and it is not going to be moved by rewording the question.

### What we tried, and what each thing actually did

**Question phrasing (interrogative vs declarative): null.** Over 93 documents the two
phrasings agreed to within 0.020 (median 0.010, max 0.11), one discordant verdict, McNemar
p=1.000. The docs suggest testing both; for this task it does not matter. Asking one question
instead of two changed probabilities by **+0.005 on average** (median 0.000, 87/100 documents
within 0.05) — so the second question was pure cost.

**Tightened criteria: a recalibration, not an improvement.** Adding "mentioning both the
subject and the object is not enough" to the `false` criterion shifted *every* probability down
by ~0.05 (−0.055 on `yes` documents, −0.033 on `no` documents) without improving
discrimination. Restoring the original wording recovered exactly that (+0.052). It fixed 2
false positives and broke 4 true positives — at a fixed 0.5 cutoff that reads as a regression,
but AUC and best-achievable accuracy were unchanged. **It moved the scale, not the ranking.**

**Dropping the identifiers: actively harmful, against expectation.** We A/B'd a "lean" state
(names + sentences only) against the full state, expecting the CURIEs and PMID to be noise —
`jev-1.13`'s documented weak spots include raw identifiers and accuracy loss from irrelevant
context. Over 100 documents the two agreed closely (mean |full − lean| = 0.066) and neither was
significantly better (McNemar p=0.625), **but the two largest divergences were both bare gene
symbols**:

| PMID | triple | this repo | full | lean |
|---|---|---|---|---|
| 8157659 | `NCBIGene:1154` CISH `interacts_with` Binding Sites | no | **0.03** | 0.66 |
| 18292807 | `NCBIGene:11261` CHP1 `affects` NADPH Oxidase | no | **0.15** | 0.67 |

The CURIE is what identifies "CISH" as a gene rather than an abbreviation or an ordinary word.
**Keep the identifiers.** For biomedical KG work this generalises beyond Jev: a bare gene
symbol is genuinely ambiguous, and the CURIE is cheap disambiguation.

### Errors are asymmetric: it over-reads co-occurrence

```
this repo = yes  →  Jev yes 50   Jev no   4
this repo = no   →  Jev yes  8   Jev no  31
```

Eight false positives to four false negatives, and the false positives share a shape — the
sentence puts both entities in one frame without asserting the relation between them:

- *Yohimbine `interacts_with` Prazosin* — the sentence studies both drugs and explicitly looks
  "for evidence of interaction", but between noradrenergic and serotonergic **systems**, not
  between the two drugs. Jev: 0.69.
- *ATPases `has_part` Proteins* — the sentence says "proteins of the P-type adenosine
  triphosphatases", which is an **is-a**, not a **has-part**. Jev: 0.68.

Both are exactly the distinction SemMedDB itself gets wrong, so they are the cases that matter.
Naming the failure in the criteria did not fix it (above).

### The probability does not reconstruct a three-class verdict

Mean probability by this repo's verdict looks encouraging — `yes` 0.769, `no` 0.299, `maybe`
0.581, neatly in between — but **that middle number is an averaging artifact**. The 7 `maybe`
documents individually scored `[0.16, 0.43, 0.48, 0.51, 0.69, 0.88, 0.92]`. A 0.4–0.6 band
catches 3 of them.

A `maybe` band is still worth having, as an **abstain zone** rather than a reconstruction of
this repo's third class. At 0.4–0.6 it abstains on 12 of 100 documents and accuracy on the 82
confident ones rises to **89.0%**. The band is applied when the CSV is written, not stored, so
rebanding is a rewrite rather than new API calls.

### Cost and speed

| | |
|---|---|
| Input tokens per call | 503 (one question) – 571 (two questions) |
| Output tokens per call | 21 for one question, 38 for two — flat, regardless of input size |
| Query round trip | mean 0.37s, median 0.36s, max 0.94s |
| Whole 100-document run | ~52k tokens, ~90s sequential at `--delay 0.5` |
| All four configurations | ~230k tokens, well under $1 |

Sequential calls with a 0.5s delay never hit a rate limit; we never saw a 429.

The per-response `usage` figures reconcile exactly with the TypeSafe console: at four separate
readings, the console equalled our cumulative input+output plus one 597-token call made before
this work started, with zero drift. So the console is accurate in real time — useful if you
want to watch a long run — and **its counter includes output tokens**, though whether output is
actually priced is invisible at cent granularity. Output was 5.1% of our tokens regardless.

Extrapolating to the 10,000-triple sample: input tokens fit **457 fixed + 0.258 per sentence
character**, and that sample's sentences are the same size as this one's (mean 184 chars vs
176), giving ~504 input + 21 output per document, **~5.25M tokens** and roughly **$0.15–0.40**.
The binding cost is wall clock — 2.4h sequential at `--delay 0.5`, 1.0h at `--delay 0` — so
concurrency or question-batching needs deciding before that run, and neither is tested here.

## If you want to push this further

In rough order of expected value:

1. **Give it the abstract, not just the sentences.** Jev is working from less evidence than
   `main.py` is. This is the biggest asymmetry in the comparison and the most likely source of
   the remaining ~13%.
2. **Try a `Choice` question** with `supports` / `contradicts` / `says_nothing`, which is what
   TypeSafe's own [citation-checking
   cookbook](https://docs.typesafe.ai/cookbooks/citation_check.md) uses. It maps onto
   `yes`/`no`/`maybe` directly and, unlike Noul, returns a `confidence`.
3. **Batch questions per call.** TypeSafe report batching many questions into one call is
   ~12x cheaper; at 26.7M triples that is the difference that matters, not the wording.
4. **Adjudicate the disagreements by hand.** We have been treating this repo's verdicts as
   ground truth. They are not — they are another model's output. Twelve of the 93 non-`maybe`
   documents disagree, a small enough pile to read properly, and it is the only way to learn
   which system is actually right.

## Caveats

- **n=100.** Differences of a few points here are noise; the McNemar p-values above say so.
- **The reference labels are not ground truth**, they are this repo's gpt-oss output.
- The sample is uniform over *distinct triples*, one random PMID each — not uniform over the
  26.7M triple-PMID rows, so predicate frequencies here do not match the full dataset.
- `elapsed_s` is blank for the first 10 documents of v1, which predate that column.
- Results were collected against `jev-1.13.0` (the `jev-latest` alias) in September 2026.
