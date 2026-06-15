# Hospital Patient Helpdesk Chatbot

End-to-end retrieval-augmented generation chatbot for hospital administrative support.

## Scope
The assistant answers questions about appointments, insurance guidance, departments,
schedules, and approved hospital policies using cited source documents. It is not a
diagnostic or treatment system.

## Build order
Implement the numbered modules from `01_load_documents.py` through
`22_error_monitor.py`, adding focused tests at each stage.

The `13_notebooks` folder contains matching notebooks from
`01_load_documents.ipynb` through `22_error_monitor.ipynb` for exploration,
validation, and documented experiments before code is promoted to modules.

## Demo dataset
`01_data/raw` contains a synthetic multi-source corpus covering PDFs, CSV files,
local website pages, SQLite tables, FAQs, a patient portal manual, and de-identified
support logs. Regenerate it with:

```bash
python 01_data/generate_demo_dataset.py
```

See `01_data/DATASET_README.md` and
`01_data/raw/provenance/source_manifest.csv` before using the data.

## Phase 1 notebook and module

`13_notebooks/01_load_documents.ipynb` is the interactive inspection layer. It
inventories the raw source tree, previews representative loaders, runs the
shared ingestion implementation, validates the manifest, and displays source
volume plots.

`03_ingestion/01_load_documents.py` is the reusable implementation layer. It
contains normalized schemas, PDF/CSV/JSON/JSONL/HTML/SQLite/text loaders,
fault-isolated discovery, validation, reporting, plots, output writing, and a
command-line interface for automation.

See `03_ingestion/01_load_documents.md` or the printable PDF for the complete
input inventory, normalized schema, code-section guide, outputs, and plot
interpretation.

Phase 1 artifacts use the same `01_` prefix as the workflow file:
`01_loaded_documents.json`, `01_ingestion_manifest.json`,
`01_source_inventory.csv`, `01_failed_documents.json`, and the two numbered
plots under `01_data/processed/plots/`.

## Phase 2 notebook and module

`13_notebooks/02_clean_documents.ipynb` is the interactive quality-review
layer. It explains each conservative cleaning rule, previews a PDF before and
after cleaning, runs the shared implementation, validates results, and displays
operation and character-reduction plots.

`03_ingestion/02_clean_documents.py` is the reusable implementation layer. It
contains atomic cleaning rules, schemas, validation, rejection tracking, audit
output, reports, plots, and a command-line interface for automation.

Phase 2 artifacts use the matching `02_` prefix:
`02_cleaned_documents.json`, `02_cleaning_report.json`,
`02_cleaning_audit.csv`, `02_rejected_documents.json`, and the two numbered
plots under `01_data/processed/plots/`.

See `03_ingestion/02_clean_documents.md` or the printable PDF for the complete
input/output contract, code-section walkthrough, plot interpretation, and
notebook-versus-module comparison.

## Phase 3 notebook and module

`13_notebooks/03_chunk_documents.ipynb` is the interactive analysis layer. It
explains the chunking parameters, previews splits, validates results, and
displays diagnostic plots. It imports the shared implementation instead of
maintaining a separate algorithm.

`03_ingestion/03_chunk_documents.py` is the reusable implementation layer. It
contains validated configuration, boundary-aware chunking, metadata handling,
quality checks, file output, plot generation, and a command-line interface for
automation and deployment.

All Phase 3 artifacts use the matching `03_` prefix: `03_text_chunks.json`,
`03_chunking_report.json`, `03_chunking_audit.csv`, `03_rejected_chunks.json`,
and the two diagnostic plots under `01_data/processed/plots/`.

See `03_ingestion/03_chunk_documents.md` or its printable PDF for the complete
input/output contract and code-section guide.

## Phase 4 notebook and module

`13_notebooks/04_create_metadata.ipynb` is the interactive review layer. It
loads the shared implementation, previews explicit and inferred metadata,
inspects coverage, and displays the field-coverage and department plots.

`03_ingestion/04_create_metadata.py` is the reusable implementation layer. It
contains metadata schemas, department/category derivation, PDF page matching,
safety labels, validation, reporting, plotting, file writing, and a CLI for
automation. Inferred values always retain their method and confidence.

All Phase 4 artifacts use the matching `04_` prefix: `04_metadata.json`,
`04_enriched_chunks.json`, `04_metadata_report.json`, `04_metadata_audit.csv`,
`04_unresolved_metadata.json`, and both diagnostic plots.

See `03_ingestion/04_create_metadata.md` or the printable PDF for the complete
schema, input/output inventory, page-matching method, and code-section guide.

## Phase 5 notebook and module

`13_notebooks/05_create_embeddings.ipynb` is the interactive inspection layer.
It previews the Phase 4 input, demonstrates one vector, runs the shared module,
validates vector quality, and displays norm and cosine-similarity diagnostics.
Its project resolver works when Jupyter starts from the workspace root, project
root, or `13_notebooks` directory.

`04_embeddings/05_create_embeddings.py` is the reusable implementation layer.
It provides deterministic offline feature hashing, batching, L2 normalization,
checksums, validation, failure tracking, reports, plots, and a command-line
interface. The local baseline keeps approved hospital text on the machine.

All Phase 5 artifacts use the matching `05_` prefix: `05_embeddings.json`,
`05_embedding_manifest.json`, `05_embedding_report.json`,
`05_embedding_audit.csv`, `05_failed_embeddings.json`, and both diagnostic
plots under `01_data/processed/plots/`.

See `04_embeddings/05_create_embeddings.md` or its printable PDF for the full
embedding method, schema, input/output inventory, limitations, and comparison
between the notebook and production module.

## Phase 6 notebook and module

`13_notebooks/06_store_vector_index.ipynb` is the interactive validation layer.
It reviews input compatibility, builds the shared index, runs real cosine and
department-filtered queries, inspects the report, and displays composition plots.
Its project resolver works when Jupyter starts from the workspace root, project
root, or `13_notebooks` directory.

`04_embeddings/06_store_vector_index.py` is the reusable persistence layer. It
implements a portable SQLite exact-cosine backend with float32 vectors, atomic
rebuilds, checksum and integrity validation, metadata filtering, reports, plots,
and a command-line interface. ChromaDB or FAISS can later replace this adapter.

All Phase 6 artifacts use the matching `06_` prefix, including
`06_vector_index.sqlite3`, its manifest, report, audit, failure file, and both
diagnostic plots.

See `04_embeddings/06_store_vector_index.md` or its printable PDF for the full
backend rationale, stored schema, input/output inventory, query behavior, and
notebook-versus-module comparison.

## Phase 7 notebook and module

`13_notebooks/07_retriever.ipynb` is the interactive retrieval-review layer. It
validates index compatibility, demonstrates ranked evidence, explicit filters,
safety-routing labels, full test-set evaluation, and diagnostic plots. Its path
resolver works from the workspace root, project root, or `13_notebooks`.

`06_rag_pipeline/07_retriever.py` is the reusable retrieval layer. It uses the
Phase 5 query embedder and Phase 6 index, applies transparent category routing,
hybrid vector/lexical reranking, confidence labels, safety signals, reports,
plots, and CLI modes for one question or the bundled evaluation.

All Phase 7 artifacts use the matching `07_` prefix: retrieval results, report,
audit, failed-query file, top-score plot, and latency plot.

See `06_rag_pipeline/07_retriever.md` or its printable PDF for the full retrieval
flow, result schema, routing behavior, safety boundaries, and comparison between
the notebook and module.

## Phase 8 notebook and module

`13_notebooks/08_prompt_template.ipynb` is the interactive prompt-review layer.
It demonstrates grounded, insufficient-context, emergency, and unsafe-medical-
advice modes, previews citation-ready messages, validates all prompts, and shows
prompt-size and mode diagnostics. It runs from the workspace, project, or
`13_notebooks` directory.

`06_rag_pipeline/08_prompt_template.py` is the reusable prompt-construction
layer. It loads YAML policy, budgets sources and context, creates citations,
delimits retrieved text as untrusted evidence, applies healthcare safety modes,
validates prompts, and writes reports, audits, failures, and plots.

All Phase 8 artifacts use the matching `08_` prefix: prompt bundles, report,
audit, failed-prompt file, token-estimate plot, and prompt-mode plot.

See `06_rag_pipeline/08_prompt_template.md` or its printable PDF for the prompt
schema, mode rules, injection defenses, output inventory, and notebook-versus-
module comparison.

## Phase 9 notebook and module

`13_notebooks/09_llm_client.ipynb` is the interactive provider-validation
layer. It imports the shared module, inspects the Phase 8 contract, demonstrates
grounded and safety-routed responses, runs all prompts, validates outputs, and
displays numbered latency and response-length plots. Its examples use the free,
private, deterministic offline provider.

`06_rag_pipeline/09_llm_client.py` is the reusable model-access layer. It
supports offline validation, OpenAI, Gemini, Anthropic Claude, and local Ollama;
normalizes provider responses; applies bounded retries; validates citations and
safety routes; protects credentials; writes reports and audits; and exposes a
CLI for automation. Hosted providers require explicit model configuration.

All Phase 9 artifacts use the matching `09_` prefix: normalized responses,
report, audit, failed-request file, latency plot, and output-length plot.

See `06_rag_pipeline/09_llm_client.md` or its printable PDF for provider setup,
the complete input/output contract, safety validation, code-section guide, and
notebook-versus-module comparison.

## Phase 10 notebook and module

`13_notebooks/10_rag_chain.ipynb` is the interactive end-to-end validation
layer. It imports the shared chain, traces one cited answer, demonstrates
emergency and unsafe-medical-advice routing, runs all sample questions, validates
the final contracts, and displays numbered stage-latency and answer-mode plots.

`06_rag_pipeline/10_rag_chain.py` is the reusable orchestration layer. It
combines the Phase 7 retriever, Phase 8 prompt builder, and Phase 9 LLM client;
preserves source provenance; measures each stage; validates grounding and safety;
isolates per-question failures; writes artifacts; and provides single-question
and batch command-line modes.

All Phase 10 artifacts use the matching `10_` prefix: final RAG answers, report,
audit, failed-answer file, stage-latency plot, and answer-mode plot.

See `06_rag_pipeline/10_rag_chain.md` or its printable PDF for the complete
execution flow, final answer schema, input/output inventory, safety boundaries,
code-section guide, and notebook-versus-module comparison.

## Phase 11 notebook and module

`13_notebooks/11_safety_guardrails.ipynb` is the interactive final-safety
review layer. It demonstrates unchanged safe answers, emergency and medical-
advice overrides, all adversarial rule tests, artifact assertions, and numbered
action and rule-coverage plots.

`06_rag_pipeline/11_safety_guardrails.py` is the reusable policy-enforcement
layer. It applies deterministic emergency, diagnosis, dosage, prompt-injection,
grounding, and sensitive-data rules; uses approved replacement messages; records
auditable actions and risk labels; validates protected outputs; runs adversarial
tests; and provides batch CLI automation.

All Phase 11 artifacts use the matching `11_` prefix: guarded answers, report,
audit, adversarial-test results, failed-check file, action plot, and rule-trigger
plot. Real pytest coverage is provided in `12_tests/test_guardrails.py`.

See `06_rag_pipeline/11_safety_guardrails.md` or its printable PDF for the full
rule catalog, execution order, protected schema, inputs and outputs, limitations,
test coverage, and notebook-versus-module comparison.

## Phase 12 notebook and module

`13_notebooks/12_api_main.ipynb` is the interactive backend walkthrough. It
initializes the same chat service used by the API, validates health and request
normalization, demonstrates grounded and emergency `/chat` behavior, checks the
FastAPI route contract when dependencies are installed, runs all synthetic
requests, validates artifacts, and displays numbered latency and safety-action
plots.

`07_backend/12_api_main.py` is the reusable backend layer. It defines the
framework-independent request contract, long-lived `ChatService`, FastAPI app
factory, `/`, `/health`, and `/chat` routes, response models, no-store security
headers, sanitized errors, offline API-service evaluation, artifacts, plots, and
server CLI. FastAPI, Pydantic, Uvicorn, and pytest are listed in requirements but
are not installed in the current interpreter.

All Phase 12 artifacts use the matching `12_` prefix: API responses, report,
audit, failed-request file, request-latency plot, and safety-action plot. API
tests are provided in `12_tests/test_api.py`.

See `07_backend/12_api_main.md` or its printable PDF for the route contract,
request and response schemas, input/output inventory, dependency note,
deployment guidance, code-section guide, and notebook-versus-module comparison.

## Phase 13 notebook and module

`13_notebooks/13_request_schema.ipynb` is the interactive request-contract
review layer. It demonstrates valid normalization, invalid payload rejection,
optional Pydantic model behavior, full sample-plus-adversarial validation,
artifact assertions, and numbered question-length and validation-outcome plots.

`07_backend/13_request_schema.py` is the reusable request-validation layer. It
defines the canonical `/chat` input contract, normalizes whitespace, validates
question length, optional filters, session ID, language, channel, urgency,
unknown fields, and sensitive identifier patterns, and can create a Pydantic
model when that dependency is installed.

All Phase 13 artifacts use the matching `13_` prefix: validation results, report,
audit, failed-check file, question-length plot, and validation-outcome plot.
Request-schema tests are provided in `12_tests/test_request_schema.py`.

See `07_backend/13_request_schema.md` or its printable PDF for the request field
rules, input/output inventory, validation flow, dependency behavior, test
coverage, and notebook-versus-module comparison.

## Phase 14 notebook and module

`13_notebooks/14_response_schema.ipynb` is the interactive response-contract
review layer. It validates a real API response, demonstrates invalid response
cases, checks optional Pydantic model behavior, runs real-plus-adversarial
validation, asserts generated artifacts, and displays numbered answer-length and
validation-outcome plots.

`07_backend/14_response_schema.py` is the reusable response-validation layer. It
defines the canonical `/chat` output contract with answer text, citations,
sources, retrieval confidence, safety flag, guardrail action, risk level,
provider, model, latency, and timestamp; validates citation-to-source integrity
and safety-flag consistency; and can create a Pydantic model when installed.

All Phase 14 artifacts use the matching `14_` prefix: validation results, report,
audit, failed-check file, answer-length plot, and validation-outcome plot.
Response-schema tests are provided in `12_tests/test_response_schema.py`.

See `07_backend/14_response_schema.md` or its printable PDF for the response
field rules, source schema, input/output inventory, validation flow, dependency
behavior, test coverage, and notebook-versus-module comparison.

## Phase 15 notebook and module

`13_notebooks/15_streamlit_app.ipynb` is the interactive UI-validation layer. It
checks source badges, safety banners, markdown rendering, backend service calls,
a grounded answer, an emergency override, generated artifacts, and numbered
response-mode and safety-action plots without launching a web server.

`08_app/15_streamlit_app.py` is the reusable Streamlit interface. It renders the
patient-facing chat UI, example questions, emergency warning, source badges,
guardrail status, optional feedback, and transcript download while using the same
Phase 12 `ChatService` as the API. Running it with Python generates deterministic
Phase 15 artifacts.

All Phase 15 artifacts use the matching `15_` prefix: transcript, session report,
UI audit, failed-turn file, response-mode plot, and safety-action plot. UI helper
tests are provided in `12_tests/test_streamlit_app.py`.

See `08_app/15_streamlit_app.md` or its printable PDF for the UI features,
input/output inventory, run commands, helper-function guide, safety notes, and
notebook-versus-module comparison.

## Phase 16 notebook and module

`13_notebooks/16_create_test_set.ipynb` is the interactive test-set review
layer. It inspects the approved seed questions, previews enriched test cases,
runs the shared Phase 16 implementation, validates generated artifacts, and
displays numbered category and safety-class plots.

`09_evaluation/16_create_test_set.py` is the reusable evaluation-data creation
layer. It reads approved sample patient questions, attaches expected answers,
expected sources, safety class, expected answer mode, guardrail action, matching
terms, avoid terms, and review tags. It also validates duplicate questions,
missing fields, and sensitive-data patterns before writing downstream evaluation
artifacts.

All Phase 16 artifacts use the matching `16_` prefix: test-set CSV, structured
JSON, report, audit CSV, failed-case file, category plot, and safety-class plot.

See `09_evaluation/16_create_test_set.md` or its printable PDF for the complete
input/output inventory, code-section guide, safety notes, and
notebook-versus-module comparison.

## Phase 17 notebook and module

`13_notebooks/17_evaluate_retrieval.ipynb` is the interactive retrieval-quality
review layer. It inspects the Phase 16 test set, runs the shared Phase 17
evaluator, validates generated artifacts, and displays numbered pass-rate,
top-score, and latency plots.

`09_evaluation/17_evaluate_retrieval.py` is the reusable retrieval-evaluation
layer. It calls the real Phase 7 retriever against the Phase 6 vector index,
then records expected-source hits, expected-category hits, safety-routing hits,
confidence labels, latency, misses, and runtime failures.

All Phase 17 artifacts use the matching `17_` prefix: retrieval results, report,
audit CSV, failed-query file, misses file, pass-rate plot, score plot, and
latency plot.

See `09_evaluation/17_evaluate_retrieval.md` or its printable PDF for the full
retrieval-evaluation contract, scoring rules, input/output inventory, plot
guide, and notebook-versus-module comparison.

## Phase 18 notebook and module

`13_notebooks/18_evaluate_answers.ipynb` is the interactive answer-quality
review layer. It inspects the Phase 16 expectations and Phase 10 generated
answers, runs the shared Phase 18 evaluator, validates generated artifacts, and
displays numbered answer-score, dimension-score, and mode-comparison plots.

`09_evaluation/18_evaluate_answers.py` is the reusable answer-evaluation layer.
It scores answer correctness, citation/source grounding, safety behavior,
hallucination-risk terms, expected-versus-actual mode, and low-score answers for
review.

All Phase 18 artifacts use the matching `18_` prefix: answer-evaluation
results, report, audit CSV, failed-evaluation file, low-score answer file,
overall-score plot, dimension-score plot, and mode-comparison plot.

See `09_evaluation/18_evaluate_answers.md` or its printable PDF for the complete
answer-evaluation contract, scoring rules, input/output inventory, plot guide,
and notebook-versus-module comparison.

## Phase 19 notebook and module

`13_notebooks/19_feedback_logger.ipynb` is the interactive feedback-log review
layer. It inspects the Phase 15 Streamlit transcript, runs the shared Phase 19
logger, validates generated feedback artifacts, and displays numbered rating
and reason-tag plots.

`09_evaluation/19_feedback_logger.py` is the reusable feedback-logging layer. It
validates thumbs-up/down ratings, checks reason tags, sanitizes free-text
comments for private identifiers, records safe response metadata, writes JSON
and JSONL logs, and summarizes feedback for quality review.

All Phase 19 artifacts use the matching `19_` prefix: feedback JSON, feedback
JSONL, report, audit CSV, failed-record file, rating plot, and reason-tag plot.

See `09_evaluation/19_feedback_logger.md` or its printable PDF for the feedback
schema, privacy rules, input/output inventory, plot guide, and
notebook-versus-module comparison.

## Privacy and safety
Use synthetic or properly authorized data during development. Redact personal and health
information from logs, ground answers in approved documents, and route emergencies to
local emergency services and qualified clinicians.
