# NyayaSathi

A free AI study assistant for Indian law students that runs on your own computer. Type in plain words and get the relevant laws and leading cases, taken from the source.

| You type | You get |
|---|---|
| `murder` | IPC 302, 307, 304 and their BNS equivalents, plus leading Supreme Court murder cases |
| `land acquisition` | Sections of the Land Acquisition Act 1894 and the 2013 Fair Compensation Act, plus key cases |
| `posco rape case` | POCSO sections 3, 4 and 6, IPC 376 → BNS 64, plus Aparna Bhat and Independent Thought |
| `cheque bounce` | NI Act 138, 139 and 118, plus Rangappa, Damodar Prabhu and Dashrath Rupsingh Rathod |
| `Section 420 IPC` | A plain-English explanation, plus the new law: BNS 318(4), same 7-year punishment |
| `Maneka Gandhi v Union of India (1978) 1 SCC 248` | The citation check: **verified**, **wrong for that case**, **belongs to a different case**, or **not found (possibly fabricated)** |

No API key is needed, nothing is paid, and your questions stay on your machine.

## How it stays accurate

The AI model never decides what the law is. It only does two things: it turns your words into search keywords, and it summarises statute text it has been given.

| Part | How |
|---|---|
| Finding laws | Indian Kanoon search across central Acts, ranked by how often courts cite each section |
| Statute text | Read from Indian Kanoon. The main codes (IPC, BNS, CrPC, BNSS, Evidence Act, BSA, Constitution, CPC, Contract Act, NI Act) are downloaded once and cached |
| Old → new criminal law | Indian Kanoon's "Similar to Section N from Old IPC/CrPC/IEA" notes, down to the sub-section, e.g. IPC 420 → BNS 318(4) |
| Finding cases | Supreme Court judgments on Indian Kanoon, ranked by how often later judgments cite them (landmark cases first) |
| Case citations | Read from each judgment's header and matched in code, so `(1978) 1 SCC 248` = `1978 SCC (1) 248`. No AI involved |
| Summaries | [Qwen3 8B](https://ollama.com/library/qwen3) running locally through [Ollama](https://ollama.com), told to use only the given text and to cite it as `[n]` |

Case holdings are deliberately **not** summarised by the AI. Each case shows the passage that matched your search, with a link to the full judgment.

## Setup

Requirements: Python 3.10+, about 6 GB of disk for the model, and ideally a GPU with 8 GB VRAM (a CPU works but is slower).

1. Install [Ollama](https://ollama.com/download) and download the model:
   ```bash
   ollama pull qwen3:8b
   ```
2. Install the Python packages:
   ```bash
   pip install -r requirements.txt
   ```
3. Start the app, then open http://localhost:8000:
   ```bash
   python app.py
   ```
   On Windows you can double-click `run.bat` instead.

The first search takes about a minute while the model loads. After that, a topic search takes 20–30 seconds and a section question about 10 seconds.

## Project structure

```
app.py           FastAPI server; one /api/search endpoint picks the mode
topic.py         plain-language topic -> relevant laws + leading cases
law_ai.py        explains a specific section from its text (Ollama)
sources.py       Indian Kanoon: Act parsing, old/new law mapping, search, case citations
static/index.html  the whole UI (no build step)
```

## Limits

- Search results can include loosely related sections. These are shown separately as "possibly less relevant".
- The old-to-new law mapping comes from Indian Kanoon's editorial notes, not an official gazette table, so double-check it for exams.
- "Not found" for a case can also mean it's very recent or only in a paid reporter. Common party names can match a different case, so check the court and date shown.
- Indian Kanoon rate-limits rapid requests, so the app spaces its requests out and caches pages it has fetched.
- This is a study aid, not legal advice. Always read the linked source before relying on it.

## Credits

- Legal data from [Indian Kanoon](https://indiankanoon.org). Please use it respectfully; the app follows its robots.txt and paces its requests.
- Local model: Qwen3 by Alibaba Cloud, served by Ollama.

## License

[MIT](LICENSE) © Harshvardhan Kumbharkar
