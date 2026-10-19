"""Read Indian statutes and case metadata from Indian Kanoon. No paid API.

Statutes: the full-act page of each Act is downloaded once, split into sections and cached on disk.
The new criminal laws (BNS, BNSS, BSA) carry Indian Kanoon's editorial notes "Similar to Section N
from Old IPC/CrPC/IEA", which give the old<->new mapping straight from the source.

Cases: Indian Kanoon search (allowed by its robots.txt) finds candidates; each judgment header
gives court, date and equivalent citations, which are used to verify a citation the user typed.
"""

import html
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib import robotparser

import httpx

IK = "https://indiankanoon.org"
HEADERS = {"User-Agent": "Mozilla/5.0 (NyayaSathi student prototype)"}
CACHE = Path(__file__).parent / "data"
CACHE.mkdir(exist_ok=True)

# key: (Indian Kanoon doc id of the full Act, display name, unit)
ACTS = {
    "IPC": (1569253, "Indian Penal Code, 1860", "Section"),
    "BNS": (149679501, "Bharatiya Nyaya Sanhita, 2023", "Section"),
    "CRPC": (445276, "Code of Criminal Procedure, 1973", "Section"),
    "BNSS": (91117739, "Bharatiya Nagarik Suraksha Sanhita, 2023", "Section"),
    "IEA": (1953529, "Indian Evidence Act, 1872", "Section"),
    "BSA": (70224818, "Bharatiya Sakshya Adhiniyam, 2023", "Section"),
    "CONSTITUTION": (237570, "Constitution of India", "Article"),
    "CPC": (161831507, "Code of Civil Procedure, 1908", "Section"),
    "CONTRACT": (171398, "Indian Contract Act, 1872", "Section"),
    "NI": (1132672, "Negotiable Instruments Act, 1881", "Section"),
}
NEW_LAW = {"IPC": "BNS", "CRPC": "BNSS", "IEA": "BSA"}
OLD_LAW = {v: k for k, v in NEW_LAW.items()}
OLD_LABEL = {"IPC": "IPC", "CRPC": "CRPC", "IEA": "IEA"}  # as written in IK remarks (case-insensitive)

ALIASES = {
    "IPC": ["ipc", "indian penal code", "penal code", "i.p.c"],
    "BNS": ["bns", "bharatiya nyaya sanhita", "nyaya sanhita"],
    "CRPC": ["crpc", "cr.p.c", "code of criminal procedure", "criminal procedure code"],
    "BNSS": ["bnss", "bharatiya nagarik suraksha sanhita", "nagarik suraksha sanhita"],
    "IEA": ["iea", "evidence act", "indian evidence act"],
    "BSA": ["bsa", "bharatiya sakshya adhiniyam", "sakshya adhiniyam"],
    "CONSTITUTION": ["constitution", "coi"],
    "CPC": ["cpc", "c.p.c", "code of civil procedure", "civil procedure code"],
    "CONTRACT": ["contract act", "indian contract act", "ica"],
    "NI": ["ni act", "negotiable instruments act", "n.i. act", "n.i act"],
}


def act_key(name):
    n = re.sub(r"[^a-z. ]", " ", (name or "").lower())
    n = re.sub(r"\s+", " ", n).strip()
    for key, names in ALIASES.items():
        if n in names or n.upper() == key:
            return key
    for key, names in ALIASES.items():
        if any(len(a) > 4 and a in n for a in names):
            return key
    return None


# ---------------------------------------------------------------- fetching

_robots = robotparser.RobotFileParser(IK + "/robots.txt")
_robots_loaded = False
_last_request = 0.0
_lock = threading.Lock()
_page_cache = {}


class RateLimited(Exception):
    pass


def _get(url, params=None):
    """Polite GET: obeys robots.txt and spaces requests out."""
    global _robots_loaded, _last_request
    if not _robots_loaded:
        try:
            _robots.read()
        except Exception:
            pass
        _robots_loaded = True
    full = str(httpx.URL(url, params=params))
    if _robots_loaded and _robots.last_checked and not _robots.can_fetch(HEADERS["User-Agent"], full):
        raise PermissionError(f"robots.txt disallows {full}")
    if full in _page_cache:
        return _page_cache[full]
    for attempt in range(3):
        with _lock:
            wait = 1.0 - (time.time() - _last_request)
            if wait > 0:
                time.sleep(wait)
            _last_request = time.time()
        r = httpx.get(url, params=params, headers=HEADERS, timeout=60, follow_redirects=True)
        if r.status_code == 429:
            retry = r.headers.get("retry-after", "")
            time.sleep(min(int(retry), 20) if retry.isdigit() else 5 * (attempt + 1))
            continue
        r.raise_for_status()
        page = r.content.decode("utf-8", errors="replace")
        if len(_page_cache) > 300:
            _page_cache.clear()
        _page_cache[full] = page
        return page
    raise RateLimited("Indian Kanoon is limiting requests right now. Wait a minute and try again.")


def _text(fragment):
    fragment = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", fragment, flags=re.S | re.I)
    fragment = re.sub(r"<(br|/p|/div|/h\d|/li|/tr|/section)[^>]*>", "\n", fragment, flags=re.I)
    fragment = re.sub(r'<span class="akn-(p|num)"', "\n<span", fragment)
    fragment = html.unescape(re.sub(r"<[^>]+>", " ", fragment)).replace("�", "")
    fragment = re.sub(r"[ \t\xa0]+", " ", fragment)
    return re.sub(r"\s*\n\s*", "\n", fragment).strip()


# ---------------------------------------------------------------- statutes

def _parse_act(key):
    doc_id, name, unit = ACTS[key]
    page = _get(f"{IK}/doc/{doc_id}/")
    # Normal markup is <section class="akn-section" id="section_N">; a few sections (e.g. BNS 318)
    # are mis-tagged as <section class="akn-chapter"><h2>Chapter 318. Cheating.</h2>.
    # Inserted sections (e.g. IEA 65A, 65B) sometimes sit inside the previous section as a bold line.
    starts = list(re.finditer(
        r'<section class="akn-section" id="section_(?P<id>[^"]+)"'
        r'|<section class="akn-chapter"><h2>Chapter (?P<id2>\d+[A-Z]*)\.\s'
        r'|<span class="akn-p"><b class="akn-b">(?P<id3>\d+[A-Z]+)\.\s', page))
    sections = {}
    for i, m in enumerate(starts):
        chunk = page[m.start(): starts[i + 1].start() if i + 1 < len(starts) else m.start() + 60000]
        raw_id = m.group("id") or m.group("id2") or m.group("id3")
        num = raw_id.lstrip("0") or "0"
        if num.upper() in sections:  # keep the first (proper) occurrence
            continue
        link = re.search(r'<a href="/doc/(\d+)/">', chunk)
        heading = (re.search(r"<h3>(.*?)</h3>", chunk, re.S) or re.search(r"<h2>Chapter (.*?)</h2>", chunk, re.S)
                   or re.search(r'<b class="akn-b">(.*?)</b>', chunk, re.S))
        similar = []
        for r in re.findall(r'<b class="akn-b">Similar to (.*?) from Old (\w+)</b>', chunk):
            refs = re.sub(r"(\d+)\s*\(([A-Za-z])\)", r"\1\2", r[0])  # "65(B)" -> "65B"
            similar += [s.upper() for s in re.findall(r"\d+[A-Za-z]*", refs)]
        # Keep IK's mapping notes inline (they follow the sub-section they map), in a readable form,
        # and record which sub-section each old provision corresponds to.
        text = re.sub(r"\[\s*Similar to (.*?) from Old (\w+)\s*-?\s*(?:Also Refer)?\s*\]",
                      lambda n: f"[Note: similar to old {n.group(2).upper()} {n.group(1)}]", _text(chunk))
        old_subs = {}
        notes = list(re.finditer(r"\[Note: similar to old \w+ (.*?)\]", text))
        # One note at the end maps the whole section; several notes each follow the sub-section they map.
        for note in notes if len(notes) > 1 else []:
            subs = re.findall(r"(?:^|\n)\s*\((\d+[A-Za-z]?)\)", text[:note.start()])
            refs = re.sub(r"(\d+)\s*\(([A-Za-z])\)", r"\1\2", note.group(1))
            for old in re.findall(r"\d+[A-Za-z]*", refs):
                if subs:
                    old_subs.setdefault(old.upper(), subs[-1])
        sections[num.upper()] = {
            "num": num.upper(),
            "heading": _text(heading.group(1)).rstrip(" .-—") + "." if heading else "",
            "text": text[:8000],
            "url": f"{IK}/doc/{link.group(1)}/" if link and m.group("id") else f"{IK}/doc/{doc_id}/",
            "old_sections": list(dict.fromkeys(similar)),
            "old_subsections": old_subs,
        }
    return {"key": key, "name": name, "unit": unit, "act_url": f"{IK}/doc/{doc_id}/", "sections": sections}


def load_act(key):
    path = CACHE / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    act = _parse_act(key)
    path.write_text(json.dumps(act, ensure_ascii=False), encoding="utf-8")
    return act


def get_section(key, num):
    act = load_act(key)
    sec = act["sections"].get(str(num).upper().replace(" ", ""))
    if not sec:
        return None
    return {**sec, "act": act["name"], "act_key": key, "unit": act["unit"]}


def subsection_text(text, sub):
    """Text of sub-section (sub) only, up to the next sub-section or mapping note."""
    m = re.search(rf"(?:^|\n)\s*\({re.escape(sub)}\)", text)
    if not m:
        return None
    rest = text[m.start():]
    end = re.search(r"\n\s*\(\d+[A-Za-z]?\)|\[Note:", rest[4:])
    return rest[: end.start() + 4 if end else len(rest)].strip()


def new_law_equivalents(old_key, num):
    """IPC 420 -> BNS sections whose IK note says 'Similar to Section 420 from Old IPC'."""
    new_key = NEW_LAW.get(old_key)
    if not new_key:
        return []
    act = load_act(new_key)
    num = str(num).upper()
    return [get_section(new_key, s["num"]) for s in act["sections"].values() if num in s["old_sections"]]


def old_law_equivalents(new_key, num):
    old_key = OLD_LAW.get(new_key)
    sec = get_section(new_key, num)
    if not old_key or not sec:
        return []
    return [x for x in (get_section(old_key, n) for n in sec["old_sections"]) if x]


REF_RE = re.compile(
    r"\b(?:(?P<unit>sections?|secs?\.?|s\.|u/s\.?|articles?|art\.?)\s*(?P<num>\d+[A-Z]?)(?:\s*\(\w+\))*"
    r"|(?P<act2>IPC|BNSS|BNS|CrPC|CRPC|BSA|IEA|CPC)\s*(?:section\s*|s\.\s*)?(?P<num2>\d+[A-Z]?)\b)",
    re.I)


def _act_after(text):
    """Longest run of following words that names an Act: 'of the Evidence Act, 1872' -> IEA."""
    words = re.sub(r"^\s*(?:of\s+)?(?:the\s+)?", "", text, flags=re.I).split()[:6]
    for n in range(len(words), 0, -1):
        key = act_key(" ".join(words[:n]).strip(",.?;:()"))
        if key:
            return key
    return None


def find_references(question):
    """Regex pass for things like 'Section 420 IPC', 'IPC 420', 'Article 21', 's. 438 CrPC'."""
    refs = []
    for m in REF_RE.finditer(question):
        if m.group("act2"):
            key, num = act_key(m.group("act2")), m.group("num2")
        else:
            num = m.group("num")
            key = _act_after(question[m.end():m.end() + 60])
            if not key and m.group("unit").lower().startswith("art"):
                key = "CONSTITUTION"
        if key:
            refs.append((key, num.upper()))
    return list(dict.fromkeys(refs))


# ---------------------------------------------------------------- cases

CITE_RE = re.compile(
    r"\(\d{4}\)\s*\d+\s*SCC\s*(?:\(Cri\)\s*)?\d+|\d{4}\s*SCC\s*\(\d+\)\s*\d+|\d{4}\s*\(\d+\)\s*SCC\s*\d+|"
    r"AIR\s*\d{4}\s*(?:SC|Supreme Court|[A-Z][A-Za-z]+)\s*\d+|\d{4}\s*AIR\s*\d+|"
    r"\[\d{4}\]\s*\d+\s*SCR\s*\d+|\d{4}\s*SCR\s*\(\d+\)\s*\d+|\(\d{4}\)\s*\d+\s*SCR\s*\d+|"
    r"\d{4}\s*SCC\s*OnLine\s*[A-Za-z]+\s*\d+|\d{4}\s*INSC\s*\d+|\(\d{4}\)\s*\d+\s*SCALE\s*\d+",
    re.I)


def cite_key(cite):
    """'(1978) 1 SCC 248' == '1978 SCC (1) 248'; 'AIR 1978 SC 597' == '1978 AIR 597'."""
    c = re.sub(r"\s+", " ", cite.upper())
    reporter = next((r for r in ("SCC ONLINE", "SCC", "AIR", "SCR", "INSC", "SCALE") if r in c), "")
    return reporter, tuple(sorted(re.findall(r"\d+", c)))


STOP = {"v", "vs", "versus", "the", "of", "and", "anr", "ors", "others", "another", "union", "india", "state",
        "mrs", "mr", "ltd", "pvt", "co", "on", "in", "uoi", "&"}


def _name_words(s):
    s = CITE_RE.sub(" ", s)
    s = re.sub(r" on \d{1,2} \w+,? \d{4}.*$", "", s)
    # "U.P." -> "UP", "K.S.Puttaswamy" -> "KS Puttaswamy", "Govt.Of" -> "Govt Of"
    s = re.sub(r"(?<![A-Za-z])((?:[A-Za-z]\.\s?){2,})", lambda m: m.group(1).replace(".", "").replace(" ", "") + " ", s)
    s = s.replace(".", " ")
    return {w for w in re.findall(r"[a-z]+", s.lower()) if w not in STOP and len(w) > 1}


def _search_ik(query):
    page = _get(f"{IK}/search/", params={"formInput": query})
    out = []
    for doc_id, title in re.findall(r'<a href="/(?:doc|docfragment)/(\d+)/[^"]*"[^>]*>(.*?)</a>', page, re.S):
        title = _text(title)
        if title and title not in ("Full Document", "Entire Act") and doc_id not in [o[0] for o in out]:
            out.append((doc_id, title))
    return out


def search_results(query):
    """Indian Kanoon search results with title, court/source, cited-by count and matching snippet."""
    page = _get(f"{IK}/search/", params={"formInput": query})
    out = []
    for art in re.findall(r'<article class="result".*?</article>', page, re.S):
        t = re.search(r'<a href="/(?:doc|docfragment)/(\d+)/[^"]*"[^>]*>(.*?)</a>', art, re.S)
        if not t:
            continue
        src = re.search(r'class="docsource">(.*?)</span>', art, re.S)
        cited = re.search(r"Cited by (\d+)", art)
        head = re.search(r'class="headline">(.*?)</div>', art, re.S)
        out.append({"doc_id": t.group(1), "title": _text(t.group(2)), "source": _text(src.group(1)) if src else "",
                    "cited_by": int(cited.group(1)) if cited else 0,
                    "snippet": re.sub(r"\s+", " ", _text(head.group(1)))[:400] if head else ""})
    return out


def read_section_doc(doc_id, max_chars=3500):
    """A single-section page (any Act): title line + provision text."""
    page = _get(f"{IK}/doc/{doc_id}/")
    i = page.find('class="doc_title"')
    seg = page[i:i + 200000]
    end = seg.find("<footer")
    text = _text(seg[:end] if end > 0 else seg)
    text = re.sub(r'^class="doc_title">', "", text)
    return text[:max_chars]


def read_case(doc_id):
    page = _get(f"{IK}/doc/{doc_id}/")
    title = _text(m.group(1)) if (m := re.search(r'<h2 class="doc_title">(.*?)</h2>', page, re.S)) else ""
    court = _text(m.group(1)) if (m := re.search(r'class="docsource_main">(.*?)</h\d>', page, re.S)) else ""
    start = page.find('<div class="judgments">')
    head = _text(page[start:start + 30000]) if start >= 0 else ""
    equiv = _text(m.group(1)) if (m := re.search(r'class="doc_citations">(.*?)</h\d>', page, re.S)) else ""
    equiv = re.sub(r"^Equivalent citations:\s*", "", equiv) or (
        m.group(1).strip() if (m := re.search(r"Equivalent citations:\s*(.+?)\n", head)) else "")
    block = re.sub(r"\s+", " ", m.group(1)) if (m := re.search(r"CITATION:\s*(.+?)CITATOR INFO", head, re.S)) else ""
    bench = m.group(1).strip() if (m := re.search(r"Bench:\s*(.+?)\n", head)) else ""
    date = m.group(1) if (m := re.search(r" on (\d{1,2} \w+,? \d{4})\s*$", title)) else ""
    pieces = [p.strip() for p in re.split(r",\s*", equiv) if p.strip()]
    pieces += [p.group(0) for p in CITE_RE.finditer(block)]
    cites = list(dict.fromkeys(re.sub(r"\s+", " ", p) for p in pieces))
    return {"doc_id": doc_id, "url": f"{IK}/doc/{doc_id}/", "title": title, "court": court, "date": date,
            "bench": bench, "citations": cites, "opening": head[:1500]}


def _parties_match(query_names, title):
    """True when the case title contains the parties typed (all of a short name, most of a long one)."""
    def norm(s):  # common abbreviations in Indian case titles
        s = re.sub(r"\bU\.?\s?O\.?\s?I\b\.?", "Union of India", s, flags=re.I)
        return re.sub(r"\bGovt\b\.?", "Government", s, flags=re.I)

    split = lambda s: re.split(r"\s+(?:v|vs|versus)\.?\s+", s, maxsplit=1, flags=re.I)
    q_sides = split(norm(CITE_RE.sub(" ", query_names)))
    t_sides = split(norm(re.sub(r" on \d{1,2} \w+,? \d{4}.*$", "", title)))
    if len(q_sides) == 1:  # no "v" typed: names can be anywhere in the title
        q_words = _name_words(q_sides[0])
        return bool(q_words) and len(q_words & _name_words(title)) / len(q_words) >= 0.75

    def words(s):
        """Party-name words; for generic parties ("Union of India", "State of") use every word, strictly."""
        w = _name_words(s)
        return (w, False) if w else ({x for x in re.findall(r"[a-z]{2,}", s.lower()) if x not in ("of", "the", "and")}, True)

    def all_words(s):
        return words(s)[0] | {x for x in re.findall(r"[a-z]{2,}", s.lower())}

    def fits(t_order):
        for i, q_side in enumerate(q_sides[:2]):
            side, generic = words(q_side)
            if not side:
                continue
            t_side = all_words(t_order[i]) if i < len(t_order) else set()
            # Petitioner names must match closely; named respondents are often abbreviated (Govt of UP).
            need = 1.0 if generic or (i == 0 and len(side) <= 2) else (0.66 if i == 0 else 0.5)
            if len(side & t_side) / len(side) < need:
                return False
        return True

    # Appeals often swap the parties ("State of X v Y" is listed as "Y vs State of X").
    return fits(t_sides) or (len(t_sides) == 2 and fits(t_sides[::-1]))


def _with_matches(cases, typed):
    for c in cases:
        keys = {cite_key(x) for x in c["citations"]}
        c["typed_matches"] = [t for t in typed if cite_key(t) in keys]
    return cases


def case_lookup(query):
    """Find the case, list its citations, and check any citation typed in the query."""
    typed = list(dict.fromkeys(re.sub(r"\s+", " ", m.group(0)).strip() for m in CITE_RE.finditer(query)))
    names = _name_words(query)

    by_name = []
    if names:
        hits = _search_ik(" ".join(sorted(names, key=query.lower().find)))
        by_name = [doc_id for doc_id, title in hits if _parties_match(query, title)][:3]
    by_cite = []
    for t in typed:
        by_cite += [doc_id for doc_id, _ in _search_ik(f'"{t}"')[:3]]

    with ThreadPoolExecutor(max_workers=3) as pool:
        cases = _with_matches([c for c in pool.map(_safe_case, by_name) if c], typed)
        cases.sort(key=lambda c: -len(c["typed_matches"]))
        # Who does the typed citation actually belong to?
        owners = []
        if typed and not (cases and cases[0]["typed_matches"]):
            ids = [d for d in dict.fromkeys(by_cite) if d not in by_name][:4]
            owners = [c for c in _with_matches([c for c in pool.map(_safe_case, ids) if c], typed)
                      if c["typed_matches"]]

    if not names and owners:  # citation-only query
        cases, owners = owners, []
    if cases and typed and cases[0]["typed_matches"]:
        verdict = "verified" if len(cases[0]["typed_matches"]) == len(typed) else "partly_verified"
    elif cases and typed:
        verdict = "citation_mismatch"  # case exists, citation doesn't match it
    elif cases:
        verdict = "found"
    elif owners:
        verdict = "name_mismatch"  # citation exists but belongs to a different case
    else:
        verdict = "not_found"
    return {"query": query, "typed_citations": typed, "verdict": verdict, "cases": cases,
            "citation_belongs_to": owners}


def _safe_case(doc_id):
    try:
        return read_case(doc_id)
    except Exception:
        return None
