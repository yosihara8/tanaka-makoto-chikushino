#!/usr/bin/env python3
"""田中允議員の過去の議会活動を筑紫野市の公式記録から収集し data/history.json に保存する。

- 本会議の発言（平成16年〜）: 筑紫野市議会 会議録検索システム
- 一般質問の題目（平成26年〜令和3年）: 筑紫野市ホームページの一般質問通告書（PDF）
  ※令和4年以降は scripts/update_activity.py が議会中継から取得する

会議録検索システムは短時間の連続アクセスを制限しているため、間隔を空けてアクセスする。
GitHub Actions（.github/workflows/build-history.yml）から手動で実行する。
"""
import html
import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "history.json"
RAW = ROOT / "data" / "history_raw.json"  # 取得した会議録の生データ（再処理用）
DBSR = "https://www.city.chikushino.fukuoka.dbsr.jp"
CITY = "https://www.city.chikushino.fukuoka.jp"
NOTICE_PAGE = CITY + "/soshiki/1/3958.html"
SPEAKER_ID = "31"  # 会議録検索システムでの「田中允」
WAIT = 20  # 会議録検索システムへのアクセス間隔（秒）
STREAM_FROM = (2022, 1)  # 令和4年以降は議会中継のデータを使う

ZEN = str.maketrans("０１２３４５６７８９", "0123456789")
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
_last = [0.0]


def get(url, data=None, polite=True):
    if polite:
        wait = WAIT - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
    body = urllib.parse.urlencode(data, doseq=True).encode() if data else None
    req = urllib.request.Request(url, data=body, headers={"User-Agent": "Mozilla/5.0 (tanaka-makoto-chikushino history)"})
    for attempt in range(4):
        try:
            with opener.open(req, timeout=120) as r:
                out = r.read()
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 3:
                raise
            time.sleep(60 * (attempt + 1))
    if polite:
        _last[0] = time.time()
    return out


def lines_of(page):
    page = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", page)
    page = re.sub(r'(?is)<a [^>]*href="([^"]*)"[^>]*>', r"\n@@LINK \1\n", page)
    t = html.unescape(re.sub(r"<[^>]+>", "\n", page))
    return [l.strip() for l in t.split("\n") if l.strip()]


def era_year(era, y):
    y = 1 if y in ("元", "") else int(y)
    return (1988 if era == "平成" else 2018) + y


def session_key(text):
    """「平成18年第４回定例会」などから (西暦, 回, 表記) を返す"""
    m = re.search(r"(平成|令和)(\d+|元)年第(\d+)回(定例会|臨時会)", text.translate(ZEN))
    if not m:
        return None
    era, y, n, kind = m.groups()
    return era_year(era, y), int(n), f"{era}{y}年第{n}回{kind}"


# ---------------------------------------------------------------- 会議録検索

def search_documents():
    top = get(DBSR + "/index.php/").decode("utf-8", "replace")
    action = re.search(r'action="(/index\.php/\d+\?Template=List)"', top).group(1)
    page = get(DBSR + action, {
        "optionsSpeakers": "on", "SpeakerName[]": SPEAKER_ID,
        "Cabinet[]": ["1", "2"], "Class[]": "3", "QueryType": "New", "Template": "List",
    }).decode("utf-8", "replace")
    sid = re.search(r"/index\.php/(\d+)\?Template=list", page).group(1)
    docs, n = {}, 1
    while True:
        page = get(f"{DBSR}/index.php/{sid}?Template=list&ListType=Text&ListOrder=ASC&Page={n}").decode("utf-8", "replace")
        cur = None
        for l in lines_of(page):
            m = re.match(r"@@LINK .*DocumentID=(\d+)$", l)
            if m and "VoiceType=all" in l:
                cur = {"id": int(m.group(1)), "title": "", "date": "", "speeches": []}
                continue
            if cur is None:
                continue
            if not cur["title"]:
                cur["title"] = l
                docs[cur["id"]] = cur
            elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", l) and not cur["date"]:
                cur["date"] = l
            elif l.startswith("...◯") and "田中" in l:
                m = re.match(r"\.\.\.◯([^（]*)（田中\s*允君）(?:〔登壇〕)?\s*(.*)", l)
                if m:
                    cur["speeches"].append({"role": m.group(1), "text": m.group(2).rstrip(".")})
        print(f"会議録一覧 {n}ページ目: 累計 {len(docs)} 文書", file=sys.stderr)
        if f"Page={n + 1}" not in page:
            break
        n += 1
    return sorted(docs.values(), key=lambda d: (d["date"], d["id"]))


GQ_HINT = re.compile(r"通告|一般質問|題目|項目|再質問|福祉の田中|質問してまいります|質問を行います")
TOPIC_RE = re.compile(
    r"(?:第?[0-9一二三四五六七八九十]+(?:題目|項目|点目)(?:め|目)?[の、は]?\s*|題目[、は]\s*|次に、|まず、|それでは、|最後に、|続きまして、)"
    r"「?([^。、「」（）]{4,48}?(?:について|の件|に向けて))")


def extract_topics(text):
    found = []
    for m in TOPIC_RE.finditer(text.translate(ZEN)):
        t = m.group(1).strip()
        t = re.sub(r"^(?:[0-9]+(?:項目め|題目|項目)?の|[0-9]+(?:項目め|題目|項目)|第[0-9]+(?:題目|項目)の|の|は|、|まず|また|最初に|最後に|再質問|今述べましたように)+", "", t)
        if (len(t) < 5 or re.search(r"再質問|御答弁|答弁|お尋ね|質問|ございます|させていただ|^第?[0-9]+(?:項目|題目)", t)
                or re.fullmatch(r"(?:今後の|成果と|実態と今後の)?(?:課題|方針)について", t)):
            continue
        if not any(t in f or f in t for f in found):
            found.append(t)
    return found[:8]


def classify(docs):
    """会議録の発言から会期ごとの活動をまとめる"""
    sessions = {}
    for d in docs:
        key = session_key(d["title"])
        if not key:
            continue
        year, num, name = key
        s = sessions.setdefault(name, {"session": name, "year": year, "num": num, "start": d["date"],
                                       "roles": [], "general_question": None, "proposals": [], "debates": [],
                                       "documents": []})
        s["documents"].append({"id": d["id"], "title": d["title"], "date": d["date"]})
        member = [sp for sp in d["speeches"] if re.search(r"\d+番$", sp["role"].translate(ZEN))]
        for sp in d["speeches"]:
            role = sp["role"].translate(ZEN)
            if not re.search(r"\d+番$", role) and role not in s["roles"]:
                s["roles"].append(role)
        for sp in member:
            t = sp["text"]
            m = re.search(r"(発議第[０-９\d]+号)(.+?)(?:について|の件)?、?(?:提出者|提案者)", t)
            if m:
                title = (m.group(1) + m.group(2)).translate(ZEN).strip("、 ")
                if title not in s["proposals"]:
                    s["proposals"].append(title)
            m = re.search(r"(.{2,60}?)(?:に対し|について|に)、?(賛成|反対)の立場", t)
            if m:
                s["debates"].append({"subject": m.group(1).translate(ZEN).strip("、 私は"), "stance": m.group(2)})
        score = sum(1 for sp in member if GQ_HINT.search(sp["text"]))
        if "臨時会" not in name and len(member) >= 3 and score and score > s.get("_gq_score", 0):
            s["_gq_score"] = score
            s["general_question"] = {"date": d["date"], "document": d["id"], "topics": []}
    for v in sessions.values():
        v.pop("_gq_score", None)
    return sessions


def fetch_question_text(doc_id):
    page = get(f"{DBSR}/index.php/?Template=view&VoiceType=all&DocumentID={doc_id}").decode("utf-8", "replace")
    text, mine = [], False
    for l in lines_of(page):
        if l.startswith("◯"):
            mine = "（田中" in l and "允君）" in l
        if mine:
            text.append(l)
    return "\n".join(text)


# ---------------------------------------------------------------- 一般質問通告書

def notice_pdfs():
    lines = lines_of(get(NOTICE_PAGE, polite=False).decode("utf-8", "replace"))
    out, link = {}, None
    for l in lines:
        if l.startswith("@@LINK "):
            link = l[7:]
        elif link and link.endswith(".pdf") and "一般質問通告" in l:
            key = session_key(re.sub(r"第(\d+)回\d+月定例会", r"第\1回定例会", l.translate(ZEN)))
            if key:
                out[key[2]] = CITY + link if link.startswith("/") else link
            link = None
    return out


def pdf_words(url):
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "a.pdf"
        p.write_bytes(get(url, polite=False))
        xml = subprocess.run(["pdftotext", "-bbox", str(p), "-"], capture_output=True, check=True).stdout.decode("utf-8", "replace")
    pages = []
    for pg in re.findall(r"(?s)<page[^>]*>(.*?)</page>", xml):
        words = [(float(a), float(b), float(c), float(e), html.unescape(w))
                 for a, b, c, e, w in re.findall(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>', pg)]
        pages.append(words)
    return pages


RANK_RE = re.compile(r"^\d+[（(]\d+番[)）]$")


def notice_topics(url):
    """通告書PDFの表から田中議員の「質問題目」欄を取り出す

    表は「順位(議席番号)・氏名｜番号・質問題目｜(1)質問項目」の列で構成される。
    題目欄は番号「1」「2」…の右側から、質問項目の「(1)」の左側までの範囲。
    """
    pages = pdf_words(url)
    picked, started = [], False
    for words in pages:
        left = [w for w in words if w[0] < 110]
        ranks = sorted(w[1] for w in left if RANK_RE.match(w[4].translate(ZEN)))
        items = [w[0] for w in words if re.fullmatch(r"[（(]\d+[)）]", w[4].translate(ZEN))]
        if not items:
            continue
        item_x = min(items)
        head = max((w[1] for w in words if w[4] in ("題", "題目", "質問題目")), default=0)
        if not started:
            name = next((w for w in left if w[4] == "田中" and any(v[4] == "允" and abs(v[1] - w[1]) < 3 for v in left)), None)
            if not name:
                continue
            top = max((y for y in ranks if y <= name[1] + 1), default=name[1] - 12) - 2
            started = True
        else:
            top = head + 5
        bottom = min((y for y in ranks if y > top + 4), default=1e9)
        picked += [w for w in words if 100 <= w[0] < item_x - 3 and top <= w[1] < bottom - 2
                   and w[4] not in ("田中", "允", "田", "中")]
        if bottom < 1e9:
            break
    picked.sort(key=lambda w: (round(w[1]), w[0]))
    num_x = min((w[0] for w in picked if re.fullmatch(r"\d{1,2}", w[4].translate(ZEN))), default=None)
    topics = []
    for w in picked:
        t = w[4].translate(ZEN)
        if num_x is not None and re.fullmatch(r"\d{1,2}", t) and abs(w[0] - num_x) < 6:
            topics.append("")
        elif topics:
            topics[-1] += t
        else:
            topics.append(t)
    return [t.strip() for t in topics if len(t.strip()) >= 3][:8]


# ---------------------------------------------------------------- main

def main():
    raw = json.loads(RAW.read_text(encoding="utf-8")) if RAW.exists() else {}
    if raw and "--full" not in sys.argv:  # 保存済みの会議録データを使う（--full で会議録から取得し直す）
        docs = raw["docs"]
    else:
        docs = search_documents()
    texts = raw.get("texts", {})
    sessions = classify(docs)
    notices = notice_pdfs()
    print(f"通告書PDF: {len(notices)} 件", file=sys.stderr)
    # 通告書がある会期（平成26年〜）は、通告書に名前があるかで一般質問の有無を判断する
    for name, url in notices.items():
        s = sessions.get(name)
        if not s or s["year"] >= STREAM_FROM[0]:
            continue
        try:
            topics = notice_topics(url)
        except Exception as e:
            print(f"通告書の読み取り失敗 {name}: {e}", file=sys.stderr)
            continue
        if topics:
            gq = s["general_question"] or {"date": "", "document": max(s["documents"], key=lambda d: d["date"])["id"], "topics": []}
            gq.update({"topics": topics, "notice": url, "source": "notice"})
            s["general_question"] = gq
        elif s["general_question"]:
            print(f"通告書から題目を読み取れず（会議録で補う）: {name}", file=sys.stderr)
    for name, s in sorted(sessions.items(), key=lambda kv: (kv[1]["year"], kv[1]["num"])):
        gq = s["general_question"]
        if not gq or s["year"] >= STREAM_FROM[0]:
            continue
        if not gq["topics"] and gq.get("source") != "notice":
            key = str(gq["document"])
            if key not in texts:
                texts[key] = fetch_question_text(gq["document"])
            gq["topics"] = extract_topics(texts[key])
            gq["source"] = "minutes"
        print(f"{name}: {gq['source']} {gq['topics']}", file=sys.stderr)
    RAW.parent.mkdir(exist_ok=True)
    RAW.write_text(json.dumps({"docs": docs, "texts": texts}, ensure_ascii=False) + "\n", encoding="utf-8")
    data = {"sessions": sorted(sessions.values(), key=lambda s: (s["year"], s["num"])),
            "minutes_base": DBSR + "/index.php/?Template=view&VoiceType=all&DocumentID=",
            "documents": len(docs)}
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"保存しました: {len(data['sessions'])} 会期 / {len(docs)} 文書", file=sys.stderr)


if __name__ == "__main__":
    main()
