# Agent Wiki Lab

A local knowledge base designed to replace Confluence Cloud and Rovo MCP.

I built this project to investigate where AI agents go wrong when answering questions about internal documentation. I implemented a wiki, tried several retrieval methods, defined requirements for the agent’s answers that the client could check, and compared the results with Confluence Cloud through Atlassian’s official MCP server. One failure stood out. An agent can find the right passage, quote it accurately, and still answer by repeating the user’s question.

![Revision 2 of the Orbit guide showing the new EU timeout and a link to the decision](docs/wiki-revision.png)

In this demo, an agent changed the EU timeout for a service called Orbit from 90 to 45 seconds. A reader can open the previous revision, follow the link to the decision behind the change, and check that the adjacent section on escalation is still intact. After cloning the repository, open `demo/index.html` in a browser or run the wiki using the quick start below. [Demo](demo/) · [Compare the revisions](demo/p/orbit-guide/)

## How the wiki works

| Scenario | Implementation |
|---|---|
| Two editors change the same section | SQLite stores revisions and edit history. Each write checks the revision the editor started from. Retrying a request with the same ID applies the edit only once. [Storage](wiki.py) |
| An agent proposes an edit | The MCP server exposes tools based on the agent’s role. Each draft records its author, the source behind the change, and a diff. Another editor can approve or reject it. [MCP tools](wiki_mcp.py) · [Review workflow](review.py) |
| A reader checks a source | The web interface and the MCP server read from the same store and enforce access controls. Importing HTML, Markdown, plain text, or PDF preserves the original file and its checksum. [Web interface](site_view.py) · [Importer](materials.py) |
| An engineer chooses a retrieval method | The benchmark compares SQLite FTS5, lexical search with XERJ, multilingual-E5 embeddings, and hybrid retrieval using reciprocal rank fusion (RRF). Separate experiments cover full text and short document previews. [Retrieval benchmark](benchmark.py) · [Full-text evaluation](fulltext_eval.py) |
| An engineer investigates a wrong answer | Both systems use the same client, which logs searches, reads, submission attempts, and timings. A separate control gives the model preselected excerpts to see whether the error persists when the evidence is already in context. [Agent client](rovo_read_pilot.py) · [Control experiment](evidence_control.py) |

## Latest benchmark results

**Document retrieval.** Both systems received 64 English questions from EnterpriseRAG-Bench against a shared set of 512 pages. XERJ returned at least one reference page in its top five results for **56/64** questions. Rovo’s semantic `search` tool did so for **52/64**. The paired 95% bootstrap confidence interval for the difference in Hit@5 ranged from −1.56 to +14.06 percentage points and included zero. XERJ ran locally. Rovo was accessed over the network, with site search scoped to the Confluence app. [All 64 pairs, timings, and settings](results/rovo-2026-11-22/paired-search.json)

**Agent answers.** For 16 questions, both systems used the same local Qwen3-4B model and the same client. Each attempt allowed up to five searches, five reads, and 180 seconds. To pass, an answer had to address the entire question, support its claims, and provide working links. In the latest client, the model first breaks the question into parts, then supplies a source and a short verbatim quote for each claim.

| Across 16 tasks | XERJ | Confluence + Rovo |
|---|---:|---:|
| Complete answers with verified sources | 1 | 0 |
| Answers submitted before the deadline | 6 | 6 |
| Timeouts | 10 | 10 |
| Total searches / reads | 16 / 37 | 16 / 50 |
| Median task time, including timeouts | 180 s | 180 s |

[Answers and quotations](results/agent-policy-v3-2026-11-23/paired/blind.json) · [Grades](results/agent-policy-v3-2026-11-23/paired/agent-grades.json)

**Control with preselected sources.** On eight questions that required multiple documents, three configurations fully answered the same **3/8** questions. The configurations were Qwen3-4B with the baseline prompt, Qwen3-4B prompted to address each part of the question, and Qwen3-8B with that same per-part prompt. For example, one evidence packet contained four incident reports that met the criteria. The three answers were “two,” “three,” and “six,” respectively. The 8B trace shows that the model counted separate excerpts from the same report as separate incidents. Each configuration ran once per question. [Analysis and limitations](POLICY-AND-EVIDENCE.md) · [8B answers](results/agent-policy-v3-2026-11-23/oracle-8b/answers.json)

## Quick start

The demo uses local sample data and requires only Python 3.12 with SQLite FTS5. Run these commands from the repository root.

```sh
python3.12 app.py init
python3.12 app.py serve --engine sqlite
```

Open the wiki in your browser. The server listens on the loopback interface. [Local wiki](http://127.0.0.1:8765/)

To connect a model through MCP, install the dependencies and configure your MCP client to run `.venv/bin/python` with the arguments `wiki_mcp.py --role reader`. If the client starts in a different working directory, use absolute paths for both the interpreter and the script.

```sh
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -m unittest -v
```

## Data and reproducibility

EnterpriseRAG-Bench is a public corpus of synthetic enterprise documents. XERJ indexed **511,962 documents** locally from the full release. For a load test, I added synthetic policy documents to bring the corpus to **40 GB** of source text across **1,657,141 documents**. The paired comparison with Confluence used 512 shared pages. [40 GB results](results/retry-2026-11-22/xerj-40gb.json) · [Experiment history](EXPERIMENTS.md)

[How to reproduce the benchmarks](REPRODUCE.md) · [Full report on the latest comparison](POLICY-AND-EVIDENCE.md) · [Dataset release](https://github.com/onyx-dot-app/EnterpriseRAG-Bench/releases/tag/v1.0.0)

The project code is licensed under MIT, © 2026 Faso367. The EnterpriseRAG-Bench data and excerpts are licensed under MIT, © 2026 Onyx. Models and binaries are downloaded separately. [Project license](LICENSE) · [Dataset license](LICENSE-EnterpriseRAG-Bench.txt)
