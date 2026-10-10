#!/usr/bin/env python3
"""筑紫野市の公式サイトから田中允議員の議会活動を取得し、ホームページを更新する。

- 一般質問: 筑紫野市議会インターネット議会中継の「田中 允 議員」ページ
- 委員会: 筑紫野市ホームページの委員会会議録ページ（PDF本文に議員名があるもの）

取得結果は data/activity.json に保存し、index.html の自動更新ブロックを書き換える。
GitHub Actions（.github/workflows/update-activity.yml）から定期実行する。
"""
import html
import json
import re
import subprocess
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "activity.json"
PAGES = [ROOT / "index.html", ROOT / "tanaka_website" / "index.html"]

STREAM = "https://chikushino-city.stream.jfit.co.jp"
SPEAKER_URL = STREAM + "/?tpl=speaker_result&speaker_id=110"
CITY = "https://www.city.chikushino.fukuoka.jp"
COMMITTEES = [
    ("建設環境常任委員会", "⌂", CITY + "/soshiki/1/23086.html"),
    ("予算審査常任委員会", "▤", CITY + "/soshiki/1/30311.html"),
]
NAME_RE = re.compile(r"田\s*中\s*允")           # 出席者欄（「田 中 允」）を含む
SPEECH_RE = re.compile(r"（田\s*中\s*允\s*君）")  # 発言者表記
MAX_RECORDS = 8  # 委員会ごとに表示する会議録の件数


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (tanaka-makoto-chikushino updater)"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def text_lines(page):
    page = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", page)
    page = re.sub(r'(?is)<a [^>]*href="([^"]*)"[^>]*>', r"\n@@LINK \1\n", page)
    t = html.unescape(re.sub(r"<[^>]+>", "\n", page))
    return [l.strip() for l in t.split("\n") if l.strip()]


def reiwa_key(session):
    m = re.match(r"令和(\d+)年第(\d+)回", session)
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def get_questions():
    lines = text_lines(fetch(SPEAKER_URL).decode("utf-8", "replace"))
    sessions, cur = [], None
    for l in lines:
        if re.fullmatch(r"令和\d+年第\d+回(定例会|臨時会)", l):
            cur = {"session": l, "date": "", "topics": [], "video": ""}
            sessions.append(cur)
        elif cur is None:
            continue
        elif re.fullmatch(r"\d+月\d+日", l) and not cur["date"]:
            cur["date"] = l
        elif re.match(r"^[0-9０-９]+）", l):
            cur["topics"].append(re.sub(r"^[0-9０-９]+）\s*", "", l))
        elif l.startswith("@@LINK ") and "play_vod" in l and not cur["video"]:
            cur["video"] = STREAM + l[len("@@LINK "):].replace("&amp;", "&")
    sessions = [s for s in sessions if s["topics"]]
    if not sessions:
        raise RuntimeError("一般質問を取得できませんでした（ページ構成が変わった可能性があります）")
    for s in sessions:
        s["year"] = reiwa_key(s["session"])[0]
    sessions.sort(key=lambda s: reiwa_key(s["session"]), reverse=True)
    return sessions


def pdf_status(url):
    """会議録PDFでの田中議員の関わり: "spoke"（発言あり）/ "attended"（出席）/ ""（記載なし）"""
    with tempfile.TemporaryDirectory() as d:
        pdf = Path(d) / "a.pdf"
        pdf.write_bytes(fetch(url))
        out = subprocess.run(["pdftotext", "-layout", str(pdf), "-"], capture_output=True, check=True)
    text = out.stdout.decode("utf-8", "replace")
    if SPEECH_RE.search(text):
        return "spoke"
    return "attended" if NAME_RE.search(text) else ""


def get_committees(checked):
    result = []
    for name, icon, page_url in COMMITTEES:
        lines = text_lines(fetch(page_url).decode("utf-8", "replace"))
        records, link = [], None
        for l in lines:
            if l.startswith("@@LINK "):
                link = l[len("@@LINK "):]
            elif link and link.endswith(".pdf") and "委員会" in l:
                url = link if link.startswith("http") else CITY + link
                label = re.sub(r"\s*\[PDFファイル／[^\]]*\]\s*$", "", l)
                records.append({"label": label, "url": url})
                link = None
        found = []
        for r in records:
            if len(found) >= MAX_RECORDS:
                break
            if r["url"] not in checked:
                try:
                    checked[r["url"]] = pdf_status(r["url"])
                except Exception as e:  # 1件の失敗で全体を止めない
                    print(f"skip {r['url']}: {e}", file=sys.stderr)
                    continue
            if checked[r["url"]]:
                found.append({**r, "spoke": checked[r["url"]] == "spoke"})
        result.append({"name": name, "icon": icon, "page": page_url, "records": found})
    return result


def esc(s):
    return html.escape(s, quote=True)


def render_session(s):
    v = esc(s["video"] or SPEAKER_URL)
    head = f'{esc(s["session"])}（{esc(s["date"])}）' if s["date"] else esc(s["session"])
    items = "".join(f'<li><a href="{v}" target="_blank" rel="noopener">{esc(t)} ↗</a></li>' for t in s["topics"])
    return (f'<h4 class="session">{head} <a class="session-src" href="{v}" target="_blank" rel="noopener">録画を見る ↗</a></h4>'
            f'<ul class="topic-list">{items}</ul>')


def render_questions(questions):
    years = sorted({s["year"] for s in questions}, reverse=True)
    latest = years[0]
    tabs = "".join(
        f'<button class="tab" role="tab" aria-selected="{"true" if y == latest else "false"}" data-year="{y}">令和{y}年</button>'
        for y in years)
    panel = "".join(render_session(s) for s in questions if s["year"] == latest)
    by_year = {str(y): "".join(render_session(s) for s in questions if s["year"] == y) for y in years}
    data = json.dumps(by_year, ensure_ascii=False).replace("</", "<\\/")
    return (f'<div class="tabs" role="tablist" aria-label="年度別一般質問">{tabs}</div>'
            f'<div class="question-panel" role="tabpanel" aria-live="polite"><h3 id="questionTitle">令和{latest}年｜一般質問</h3>'
            f'<div id="topicList">{panel}</div>'
            '<p class="note">筑紫野市議会インターネット議会中継に公開されている一般質問の題目です。各項目をクリックすると、その日の録画が開きます。</p></div>'
            f'<script type="application/json" id="questionData">{data}</script>')


def render_committees(committees):
    cards = []
    for c in committees:
        items = "".join(
            f'<li><a href="{esc(r["url"])}" target="_blank" rel="noopener">{esc(r["label"])} ↗</a>'
            + ('<span class="badge">発言あり</span>' if r.get("spoke") else "") + "</li>"
            for r in c["records"])
        if not items:
            items = f'<li><a href="{esc(c["page"])}" target="_blank" rel="noopener">会議録一覧（筑紫野市ホームページ） ↗</a></li>'
        cards.append(f'<article class="card"><h3><span class="icon" aria-hidden="true">{c["icon"]}</span>{esc(c["name"])}</h3><ul>{items}</ul></article>')
    return f'<div class="two-col">{"".join(cards)}</div>'


def replace_block(page, name, content):
    pat = re.compile(rf"(<!--AUTO:{name}-->).*?(<!--/AUTO:{name}-->)", re.S)
    if not pat.search(page):
        raise RuntimeError(f"index.html に AUTO:{name} の目印がありません")
    return pat.sub(lambda m: m.group(1) + content + m.group(2), page)


def main():
    old = json.loads(DATA.read_text(encoding="utf-8")) if DATA.exists() else {}
    checked = old.get("checked_pdfs", {})
    questions = get_questions()
    committees = get_committees(checked)
    now = datetime.now(timezone(timedelta(hours=9)))
    # 月に1回は記録を更新する（60日間変更のないリポジトリでは定期実行が止まるため）
    new = {"questions": questions, "committees": committees, "checked_pdfs": checked,
           "checked_month": now.strftime("%Y-%m")}
    if all(new[k] == old.get(k) for k in new):
        print("変更なし")
        return
    changed = any(new[k] != old.get(k) for k in ("questions", "committees"))
    new["updated"] = now.strftime("%Y-%m-%d") if changed or "updated" not in old else old["updated"]
    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps(new, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for p in PAGES:
        s = p.read_text(encoding="utf-8")
        s = replace_block(s, "QUESTIONS", render_questions(questions))
        s = replace_block(s, "COMMITTEES", render_committees(committees))
        s = re.sub(r"(<!--AUTO:UPDATED-->).*?(<!--/AUTO:UPDATED-->)", lambda m: m.group(1) + new["updated"] + m.group(2), s)
        p.write_text(s, encoding="utf-8")
    print(f"更新しました: 一般質問 {len(questions)} 回分 / 会議録 {sum(len(c['records']) for c in committees)} 件")


if __name__ == "__main__":
    main()
