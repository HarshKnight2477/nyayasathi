"""Answer questions about Indian statutes with a local model (Ollama), grounded in the real section text."""

import re

import ollama

import sources

MODEL = "qwen3:8b"
OPTIONS = {"temperature": 0.1, "num_ctx": 16384}


class LawAIError(Exception):
    pass


def _chat(messages, fmt=None, think=False):
    try:
        r = ollama.chat(model=MODEL, messages=messages, format=fmt, options=OPTIONS, think=think)
    except ollama.ResponseError as e:
        if "not found" in str(e).lower():
            raise LawAIError(f"Model {MODEL} isn't downloaded. Run: ollama pull {MODEL}") from e
        raise LawAIError(f"Ollama error: {e}") from e
    except Exception as e:  # connection refused etc.
        raise LawAIError("Can't reach Ollama. Start the Ollama app and try again.") from e
    return re.sub(r"<think>.*?</think>", "", r.message.content or "", flags=re.S).strip()


SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {"provisions": {"type": "array", "maxItems": 4, "items": {
        "type": "object",
        "properties": {"act": {"type": "string", "enum": list(sources.ACTS)},
                       "number": {"type": "string"}},
        "required": ["act", "number"]}}},
    "required": ["provisions"],
}


def _suggest_provisions(question):
    """When the question names no section, ask the model which provisions are relevant.
    Every suggestion is then looked up; only ones that exist are used, and the answer is built from their text."""
    import json
    raw = _chat([
        {"role": "system", "content": (
            "List the Indian statutory provisions most relevant to the question, as act + section/article number. "
            "Acts: IPC, BNS, CRPC, BNSS, IEA, BSA, CONSTITUTION (articles), CPC, CONTRACT, NI. "
            "For criminal law questions include both the old (IPC/CRPC/IEA) and new (BNS/BNSS/BSA) provision "
            "if you know them. Return at most 4.")},
        {"role": "user", "content": question}], fmt=SUGGEST_SCHEMA)
    try:
        items = json.loads(raw).get("provisions", [])
    except ValueError:
        return []
    return [(i["act"], re.sub(r"[^0-9A-Za-z]", "", i["number"]).upper()) for i in items if i.get("number")]


STOPWORDS = {"what", "which", "under", "section", "sections", "article", "explain", "about", "punishment",
             "offence", "offences", "there", "their", "shall", "with", "from", "that", "this", "does", "law",
             "india", "indian", "code", "act", "the", "and", "for", "who", "whom", "when", "where", "how", "is"}


def _content_words(text):
    return {w[:6] for w in re.findall(r"[a-z]{4,}", text.lower()) if w not in STOPWORDS}


def gather(question):
    refs = sources.find_references(question)
    suggested = False
    if not refs:
        refs, suggested = _suggest_provisions(question), True
    found, seen = [], set()

    def add(sec, note=""):
        if sec and (sec["act_key"], sec["num"]) not in seen and len(found) < 7:
            seen.add((sec["act_key"], sec["num"]))
            found.append({**sec, "note": note})

    words = _content_words(question)
    for key, num in refs:
        sec = sources.get_section(key, num)
        # Model-suggested provisions must share a keyword with the question, or they're dropped.
        if suggested and sec and not (words & _content_words(sec["heading"] + " " + sec["text"][:400])):
            continue
        add(sec)
        if sec and key in sources.NEW_LAW:
            for eq in sources.new_law_equivalents(key, num):
                sub = eq.get("old_subsections", {}).get(num)
                part = sources.subsection_text(eq["text"], sub) if sub else None
                if part:
                    # Give the model only the matching sub-section so it can't borrow another one's punishment.
                    add({**eq, "num": f"{eq['num']}({sub})", "text": f"{eq['heading']}\n{part}"},
                        f"{eq['act_key']} {eq['num']}({sub}) replaces {key} {num}")
                else:
                    add(eq, f"{eq['act_key']} {eq['num']} replaces {key} {num}")
        elif sec and key in sources.OLD_LAW:
            for eq in sources.old_law_equivalents(key, num):
                sub = sec.get("old_subsections", {}).get(eq["num"])
                add(eq, f"Old-law equivalent of {key} {num}" + (f"({sub})" if sub else ""))
    missing = [f"{k} {n}" for k, n in refs if (k, n) not in seen and not suggested]
    return found, missing, suggested


ANSWER_SYSTEM = """You are NyayaSathi, a study assistant for Indian law students.
Answer ONLY from the numbered SOURCES (the actual text of the provisions). Rules:
- Put the source number in square brackets after every sentence that states law, e.g. [1].
- Quote the exact words of the provision for definitions and punishments.
- If the sources don't answer part of the question, say "The provided provisions don't cover this" - never fill gaps from memory.
- Do not mention any case name, section or punishment that is not in the sources.
- Where a source says it replaces an old provision (IPC/CrPC/Evidence Act -> BNS/BNSS/BSA), name the exact
  sub-section given in the source header (e.g. "BNS 318(4)"), and take the punishment from THAT sub-section only.
  Notes like "[Note: similar to old IPC Section 420]" sit right after the sub-section they map.
  Old law applies to offences committed before 1 July 2024, new law from 1 July 2024.
- Text starting "Editorial Comment" or in [[double brackets]] is Indian Kanoon's commentary, not the statute:
  if you use it, say "Indian Kanoon's note says...".
- Format: a short direct answer first, then bullet points - Ingredients, Punishment, Old vs new law - but only
  the bullets that apply (no Punishment bullet for a non-penal provision). Under 250 words.
- Plain, simple English. This is not legal advice."""


def answer(question):
    found, missing, suggested = gather(question)
    if not found:
        msg = "I couldn't find that provision in the Acts I have"
        msg += f" ({', '.join(missing)})." if missing else "."
        msg += (" Try naming the section and Act, e.g. 'Section 420 IPC' or 'Article 21'. Acts covered: "
                + ", ".join(v[1] for v in sources.ACTS.values()) + ".")
        return {"answer": msg, "sources": [], "suggested": suggested, "missing": missing}

    blocks = []
    for i, s in enumerate(found, 1):
        blocks.append(f"[{i}] {s['act']} - {s['unit']} {s['num']}"
                      + (f" ({s['note']})" if s["note"] else "") + f"\n{s['text'][:3500]}")
    messages = [{"role": "system", "content": ANSWER_SYSTEM},
                {"role": "user", "content": "SOURCES:\n\n" + "\n\n".join(blocks) + f"\n\nQUESTION: {question}"}]
    text = _chat(messages)
    if not re.search(r"\[\d+\]", text):
        # Small models sometimes forget the markers; ask once more.
        text = _chat(messages + [{"role": "assistant", "content": text}, {"role": "user", "content": (
            "Rewrite your answer with the source number in square brackets, e.g. [1], after every "
            "sentence that states law. Keep the content the same and correct any punishment that does "
            "not match the quoted source text.")}])

    # Drop citation markers that point to sources that don't exist.
    text = re.sub(r"\[(\d+)\]", lambda m: m.group(0) if 1 <= int(m.group(1)) <= len(found) else "", text)
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", text)}
    return {
        "answer": text,
        "sources": [{"n": i, "title": f"{s['act']} - {s['unit']} {s['num']}", "heading": s["heading"],
                     "url": s["url"], "note": s["note"], "text": s["text"][:3500], "cited": i in cited}
                    for i, s in enumerate(found, 1)],
        "uncited_warning": not cited,
        "suggested": suggested,
        "missing": missing,
    }
