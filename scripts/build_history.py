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


GQ_HINT = re.compile(r"通告|一般質問|題目|質問してまいります|質問を行います|質問いたします|お尋ねいたします")
TOPIC_RE = re.compile(
    r"(?:第?[0-9一二三四五六七八九十]+(?:題目|項目|点目)(?:め|目)?[の、は]?\s*|題目[、は]\s*|次に、|まず、|それでは、|最後に、|続きまして、)"
    r"「?([^。、「」（）]{4,48}?(?:について|の件|に向けて))")


def extract_topics(text):
    found = []
    for m in TOPIC_RE.finditer(text.translate(ZEN)):
        t = m.group(1).strip()
        t = re.sub(r"^(?:の|は|、|まず|再質問)+", "", t)
        if re.search(r"再質問|御答弁|答弁|お尋ね|質問|ございます|させていただ", t):
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
        if (len(member) >= 3 and any(GQ_HINT.search(sp["text"]) for sp in member[:2])
                and not s["general_question"]):
            s["general_question"] = {"date": d["date"], "document": d["id"], "topics": []}
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


def notice_topics(url):
    """通告書PDFの表から田中議員の「質問題目」欄を取り出す"""
    pages = pdf_words(url)
    col = None
    for words in pages:
        heads = {w[4]: w for w in words}
        t = next((w for w in words if "題目" in w[4] or w[4] in ("質問事項", "件名")), None)
        g = next((w for w in words if "要旨" in w[4] or "内容" in w[4]), None)
        if t and g and t[0] < g[0]:
            col = (t[0] - 25, g[0] - 5)
            break
    if not col:
        return []
    cells, active = [], False
    for words in pages:
        name_rows = sorted({round(w[1]) for w in words if w[0] < col[0] and re.fullmatch(r"[一-鿿]{1,3}", w[4])})
        tanaka = [w for w in words if w[4] in ("田中", "田") and w[0] < col[0]]
        start = min((w[1] for w in tanaka), default=None)
        if start is None and not active:
            continue
        top = start - 30 if start is not None else 0
        # 次の議員の行（田中議員より下にある氏名欄の語）で区切る
        others = [w[1] for w in words if w[0] < col[0] - 5 and w[1] > (start or 0) + 40
                  and re.fullmatch(r"[一-鿿]{1,4}", w[4]) and w[4] not in ("田中", "允", "田", "中")]
        bottom = min(others, default=1e9)
        cells += sorted([w for w in words if col[0] <= w[0] < col[1] and top <= w[1] < bottom], key=lambda w: (round(w[1] / 4), w[0]))
        active = bottom == 1e9
        if not active:
            break
    # 行ごとに連結し、番号で題目を区切る
    rows, cur_y = [], None
    for w in cells:
        if cur_y is None or abs(w[1] - cur_y) > 4:
            rows.append([])
            cur_y = w[1]
        rows[-1].append(w[4])
    topics = []
    for r in rows:
        line = "".join(r).translate(ZEN)
        m = re.match(r"^([0-9]{1,2})[\.．\s]?(.*)", line)
        if m and m.group(2):
            topics.append(m.group(2))
        elif topics:
            topics[-1] += line
        elif line:
            topics.append(line)
    return [t.strip() for t in topics if len(t.strip()) >= 3][:8]


# ---------------------------------------------------------------- main

def main():
    docs = search_documents()
    sessions = classify(docs)
    notices = notice_pdfs()
    print(f"通告書PDF: {len(notices)} 件", file=sys.stderr)
    for name, s in sorted(sessions.items(), key=lambda kv: (kv[1]["year"], kv[1]["num"])):
        gq = s["general_question"]
        if not gq or s["year"] >= STREAM_FROM[0]:
            continue
        if name in notices:
            try:
                gq["topics"] = notice_topics(notices[name])
                gq["source"] = "notice"
                gq["notice"] = notices[name]
            except Exception as e:
                print(f"通告書の読み取り失敗 {name}: {e}", file=sys.stderr)
        if not gq["topics"]:
            gq["topics"] = extract_topics(fetch_question_text(gq["document"]))
            gq["source"] = "minutes"
        print(f"{name}: {gq['source']} {gq['topics']}", file=sys.stderr)
    data = {"sessions": sorted(sessions.values(), key=lambda s: (s["year"], s["num"])),
            "minutes_base": DBSR + "/index.php/?Template=view&VoiceType=all&DocumentID=",
            "documents": len(docs)}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"保存しました: {len(data['sessions'])} 会期 / {len(docs)} 文書", file=sys.stderr)


if __name__ == "__main__":
    main()
