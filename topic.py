"""Plain-language topic search: "murder", "land acquisition", "posco rape case" -> relevant laws + cases.

1. The local model turns the user's words into 1-3 short legal search phrases (fixing spellings like
   "posco" -> "POCSO"). Short phrases work far better on Indian Kanoon than full sentences.
2. Laws: Indian Kanoon search restricted to statutes; section-level hits are merged and ranked by how often
   courts cite them. Old criminal-law sections get their BNS/BNSS/BSA equivalent added.
3. Cases: Indian Kanoon search restricted to the Supreme Court, ranked by "cited by" (landmark cases first);
   each judgment header gives the real reporter citations.
4. The model writes a short overview from the fetched text only, citing [n].
"""

import json
import math
import re

import law_ai
import sources

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string"},
        "law_queries": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "case_queries": {"type": "array", "items": {"type": "string"}, "maxItems": 2},
    },
    "required": ["topic", "law_queries", "case_queries"],
}

PLAN_SYSTEM = """You turn a law student's plain words into Indian Kanoon search keywords.
Return JSON:
- topic: the legal topic in 2-6 words, spelled correctly (e.g. "posco" -> "POCSO", "dowery" -> "dowry").
- law_queries: 1-3 SHORT keyword phrases (1-4 words each) to find the statute sections, using words that appear
  in the law itself. Include the key offence/concept word. For crimes, add a phrase for the punishment section.
  Examples: "murder" -> ["murder", "culpable homicide"]; "land acquisition" -> ["land acquisition",
  "land acquisition compensation"]; "posco rape case" -> ["penetrative sexual assault", "aggravated sexual
  assault child", "rape"]; "cheque bounce" -> ["dishonour of cheque negotiable instruments"];
  "dowry harassment" -> ["dowry death", "husband or relative subjecting woman to cruelty", "dowry prohibition"].
  When you know the main Act, put its short name in one phrase (e.g. "negotiable instruments", "dowry prohibition").
  If an older Act was replaced by a newer one, include a phrase from the NEW Act's name, e.g. land acquisition
  -> "fair compensation land acquisition" (2013 Act replaced the 1894 Act).
- case_queries: 1-2 SHORT phrases (2-4 words) to find leading court judgments on the topic, e.g.
  ["POCSO rape"], ["land acquisition compensation"], ["dishonour of cheque"].
Only keywords - no section numbers you are unsure of, no explanations."""


def plan(text):
    raw = law_ai._chat([{"role": "system", "content": PLAN_SYSTEM}, {"role": "user", "content": text}],
                       fmt=PLAN_SCHEMA)
    try:
        p = json.loads(raw)
    except ValueError:
        p = {}
    clean = lambda qs: [re.sub(r"[^\w\s\-]", " ", q).strip() for q in qs if q and q.strip()][:3]
    words = re.sub(r"\b(case|cases|law|laws|section|sections|about|what|is|the|of|for|on)\b", " ", text, flags=re.I)
    fallback = [re.sub(r"\s+", " ", words).strip() or text]
    return {"topic": p.get("topic") or text,
            "law_queries": clean(p.get("law_queries", [])) or fallback,
            "case_queries": clean(p.get("case_queries", []))[:2] or fallback}


SECTION_TITLE = re.compile(r"^(Section|Article|Rule|Order)\s+([\w.()-]+)\s+in\s+(?:The\s+)?(.+)$", re.I)


def find_laws(queries, limit=6):
    hits, acts = {}, {}

    def collect(results, qi):
        for rank, r in enumerate(results[:10]):
            central = r["source"].startswith("Union of India")
            m = SECTION_TITLE.match(r["title"])
            if not m:
                # A whole Act matched (e.g. "The Negotiable Instruments Act, 1881"): remember it.
                if central and r["source"].endswith("Act") and r["cited_by"] >= 300:
                    acts.setdefault(r["title"], r["cited_by"])
                continue
            if r["doc_id"] in hits:
                continue
            # Most-cited central provisions first; earlier queries and higher search rank break ties.
            score = math.log10(r["cited_by"] + 1) * 2 + (2 if central else -3) - rank * 0.15 - qi * 0.5
            if m.group(1).lower() == "rule" or "Rules" in m.group(3) or "Regulations" in m.group(3):
                score -= 3
            hits[r["doc_id"]] = {**r, "unit": m.group(1).title(), "num": m.group(2), "act": m.group(3).strip(),
                                 "score": score}

    for qi, q in enumerate(queries):
        collect(sources.search_results(f"{q} doctypes: laws"), qi)
    # Search inside heavily cited Acts that matched as a whole but whose sections didn't show up.
    have = {h["act"].lower() for h in hits.values() if h["score"] > 4}
    for act, _ in sorted(acts.items(), key=lambda a: -a[1])[:2]:
        name = re.sub(r"^The\s+", "", act)
        if not any(name.lower() in a for a in have):
            collect(sources.search_results(f"{queries[0]} {name} doctypes: laws"), 0)
    # At most 3 sections per Act, so a second relevant Act (e.g. the 2013 land law) isn't crowded out.
    ranked, per_act = [], {}
    pool = sorted(hits.values(), key=lambda h: -h["score"])
    if sum(h["score"] >= 4 for h in pool) >= 2:  # drop obscure state rules when strong central hits exist
        pool = [h for h in pool if h["score"] >= 1]
    for h in pool:
        if per_act.get(h["act"], 0) < 3 and len(ranked) < limit:
            per_act[h["act"]] = per_act.get(h["act"], 0) + 1
            ranked.append(h)

    laws, seen = [], set()
    for h in ranked:
        key = sources.act_key(h["act"].split(",")[0])
        sec = sources.get_section(key, h["num"]) if key else None
        if sec:
            item = {"title": f"{sec['act']} - {sec['unit']} {sec['num']}", "heading": sec["heading"],
                    "url": sec["url"], "text": sec["text"][:3000], "note": "", "cited_by": h["cited_by"]}
        else:
            try:
                text = sources.read_section_doc(h["doc_id"])
            except Exception:
                text = h["snippet"]
            heading = next((l for l in text.split("\n")[1:3] if l.strip()), "")
            item = {"title": h["title"], "heading": heading, "url": f"{sources.IK}/doc/{h['doc_id']}/",
                    "text": text, "note": "", "cited_by": h["cited_by"]}
        if item["title"] in seen:
            continue
        seen.add(item["title"])
        laws.append(item)
        # Old criminal law -> add the new-law equivalent from the cached mapping.
        if key in sources.NEW_LAW:
            for eq in sources.new_law_equivalents(key, h["num"].upper())[:1]:
                sub = eq.get("old_subsections", {}).get(h["num"].upper())
                part = sources.subsection_text(eq["text"], sub) if sub else None
                t = f"{eq['act']} - Section {eq['num']}" + (f"({sub})" if sub else "")
                if t not in seen:
                    seen.add(t)
                    laws.append({"title": t, "heading": eq["heading"], "url": eq["url"],
                                 "text": f"{eq['heading']}\n{part}" if part else eq["text"][:3000],
                                 "note": f"New law replacing {key} {h['num']} (from 1 July 2024)", "cited_by": None})
    return laws[:limit + 3]


def find_cases(queries, limit=5):
    hits = {}
    for qi, q in enumerate(queries):
        for rank, r in enumerate(sources.search_results(f"{q} doctypes: supremecourt")[:10]):
            if r["doc_id"] not in hits:
                r["score"] = math.log10(r["cited_by"] + 1) * 2 - rank * 0.2 - qi * 0.5
                hits[r["doc_id"]] = r
    ranked = sorted(hits.values(), key=lambda h: -h["score"])[:limit]
    cases = []
    for h in ranked:
        try:
            c = sources.read_case(h["doc_id"])
        except Exception:
            continue
        c["cited_by"] = h["cited_by"]
        c["snippet"] = h["snippet"]
        c["typed_matches"] = []
        cases.append(c)
    return cases


OVERVIEW_SYSTEM = """You are NyayaSathi, a study assistant for Indian law students.
Using ONLY the numbered SOURCES (statute text), write a short overview of the topic:
1. One or two plain-English sentences on what the law says about the topic.
2. "**Key provisions**": a bullet per RELEVANT provision - Act + section, what it covers in a few words, and the
   punishment if it is in the text - ending with its source number like [3]. Leave out sources that are not about
   the topic (search results can include unrelated sections).
   If a source says it replaces an old provision, mention both (old law before 1 July 2024, new law after).
   If two Acts cover the same ground and one is older (e.g. Land Acquisition Act 1894 and the 2013 Act), say the
   newer Act now applies, but only if both are in the sources.
Rules: write source numbers exactly as [3]. Never mention a section or punishment that is not in the sources.
Do not discuss court cases. Under 220 words. Not legal advice."""


def search_topic(text):
    p = plan(text)
    laws = find_laws(p["law_queries"])
    # Explicit section references in the text ("murder 302") are added first.
    for key, num in sources.find_references(text):
        sec = sources.get_section(key, num)
        if sec:
            t = f"{sec['act']} - {sec['unit']} {sec['num']}"
            if all(l["title"] != t for l in laws):
                laws.insert(0, {"title": t, "heading": sec["heading"], "url": sec["url"],
                                "text": sec["text"][:3000], "note": "", "cited_by": None})
    cases = find_cases(p["case_queries"])

    # The model only summarises statute text. Cases are listed as found (title, court, real citations,
    # matching passage) - an 8B model can't be trusted to state holdings.
    for i, s in enumerate(laws, 1):
        s["n"] = i
    overview = ""
    if laws:
        blocks = "\n\n".join(f"[{s['n']}] {s['title']}" + (f" - {s['note']}" if s["note"] else "")
                             + f"\n{s['text'][:1500]}" for s in laws)
        overview = law_ai._chat([{"role": "system", "content": OVERVIEW_SYSTEM},
                                 {"role": "user", "content": f"SOURCES:\n\n{blocks}\n\nTOPIC: {p['topic']}"}])
        overview = re.sub(r"\[\s*(?:n|source\s*)?(\d+)\s*\]", r"[\1]", overview, flags=re.I)
        overview = re.sub(r"\[(\d+)\]", lambda m: m.group(0) if 1 <= int(m.group(1)) <= len(laws) else "", overview)
    cited = {int(n) for n in re.findall(r"\[(\d+)\]", overview)}
    for s in laws:
        s["cited"] = s["n"] in cited
    return {"mode": "topic", "topic": p["topic"], "queries": p, "overview": overview, "laws": laws, "cases": cases}
