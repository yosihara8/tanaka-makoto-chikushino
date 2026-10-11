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


HISTORY = ROOT / "data" / "history.json"


def wareki(year):
    if year >= 2019:
        return "令和元年" if year == 2019 else f"令和{year - 2018}年"
    return f"平成{year - 1988}年"


def short_date(d):
    """2026-06-22 / 6月22日 → 6月22日"""
    m = re.match(r"\d{4}-(\d{2})-(\d{2})", d or "")
    return f"{int(m.group(1))}月{int(m.group(2))}日" if m else (d or "")


def link(url, text, cls=""):
    c = f' class="{cls}"' if cls else ""
    return f'<a{c} href="{esc(url)}" target="_blank" rel="noopener">{esc(text)} ↗</a>'


def merge_sessions(questions, history):
    """会議録・通告書（過去分）と議会中継（令和4年〜）の記録を会期ごとにまとめる"""
    base = history.get("minutes_base", "")
    sessions = {}
    for h in history.get("sessions", []):
        s = dict(h)
        s["minutes"] = [{"label": f'{d["title"].split("（")[-1].split("）")[0]}（{short_date(d["date"])}）', "url": base + str(d["id"])}
                        for d in h.get("documents", [])]
        gq = h.get("general_question")
        if gq:
            src = {"notice": gq.get("notice"), "minutes": base + str(gq["document"])}
            s["gq"] = {"date": short_date(gq["date"]), "topics": gq.get("topics", []), "source": gq.get("source", "minutes"),
                       "links": [(src["minutes"], "会議録")] + ([(gq["notice"], "通告書（PDF）")] if gq.get("notice") else []),
                       "topic_url": gq.get("notice") or src["minutes"]}
        sessions[s["session"]] = s
    for q in questions:
        s = sessions.setdefault(q["session"], {"session": q["session"], "year": 2018 + q["year"],
                                               "num": reiwa_key(q["session"])[1], "roles": [], "proposals": [],
                                               "debates": [], "minutes": []})
        old = s.get("gq") or {}
        s["gq"] = {"date": q["date"], "topics": q["topics"], "source": "stream", "topic_url": q["video"] or SPEAKER_URL,
                   "links": [(q["video"] or SPEAKER_URL, "録画を見る")] + [l for l in old.get("links", []) if l[1] == "会議録"]}
    return sorted(sessions.values(), key=lambda s: (s["year"], s["num"]), reverse=True)


ROLE_ORDER = ["議長", "副議長"]


def role_label(r):
    return r.replace("常任委員長", "常任委員会 委員長").replace("特別委員長", "特別委員会 委員長")


def summarize_roles(sessions):
    years = {}
    for s in sessions:
        for r in s.get("roles", []):
            if "委員長" in r or r in ROLE_ORDER:
                years.setdefault(r, set()).add(s["year"])
    out = []
    for r, ys in years.items():
        ys = sorted(ys)
        spans, a = [], ys[0]
        for prev, y in zip(ys, ys[1:] + [None]):
            if y != prev + 1:
                spans.append(wareki(a) if a == prev else f"{wareki(a)}〜{wareki(prev)}")
                a = y
        out.append((ROLE_ORDER.index(r) if r in ROLE_ORDER else 9, ys[0], role_label(r), "、".join(spans)))
    return [(r, span) for _, _, r, span in sorted(out)]


def year_roles(sessions, y):
    return sorted({role_label(r) for s in sessions if s["year"] == y for r in s.get("roles", [])
                   if "委員長" in r or r in ROLE_ORDER})


def render_entry(s):
    """年表の1会期分（一般質問の題目と出典）"""
    head = re.sub(r"^(平成|令和)(\d+|元)年", "", s["session"])
    gq = s["gq"]
    srcs = " ".join(link(u, t, "src") for u, t in gq["links"])
    if gq["topics"]:
        items = "".join(f"<li>{esc(t)}</li>" for t in gq["topics"])
        body = f'<ul class="topics">{items}</ul>'
        if gq["source"] == "minutes":
            body += '<p class="dim">題目は会議録の発言から抜粋</p>'
    else:
        body = '<p class="dim">質問の内容は会議録でご覧いただけます。</p>'
    return (f'<div class="entry"><div class="entry-h"><b>{esc(head)}</b><span>{esc(gq["date"])}</span>'
            f'<span class="srcs">{srcs}</span></div>{body}</div>')


def render_history(sessions):
    years = sorted({s["year"] for s in sessions}, reverse=True)
    rows = []
    for y in years:
        items = [s for s in sessions if s["year"] == y]
        notes = []
        roles = year_roles(sessions, y)
        if roles:
            notes.append(f'<p class="yr-note role">{esc("・".join(roles))}</p>')
        props = [p for s in items for p in s.get("proposals", [])]
        if props:
            notes.append('<p class="yr-note">議案の提出：' + "、".join(esc(p) for p in props) + "</p>")
        body = "".join(render_entry(s) for s in items if s.get("gq"))
        if not body:
            body = '<p class="dim">一般質問の記録はありません。</p>'
        rows.append(f'<section class="yr" id="y{y}"><h3><span class="ad">{y}</span><span class="jp">{wareki(y)}</span></h3>'
                    f'<div class="yr-body">{"".join(notes)}{body}</div></section>')
    rows.append('<section class="yr"><h3><span class="ad">1991–2003</span><span class="jp">平成3年〜平成15年</span></h3>'
                '<div class="yr-body"><p class="dim">この期間の会議録はインターネットでは公開されていません。'
                '筑紫野市議会事務局（議会図書室）で閲覧できます。</p></div></section>')
    return "".join(rows)


def render_latest(sessions):
    s = next((s for s in sessions if s.get("gq") and s["gq"]["topics"]), None)
    if not s:
        return ""
    items = "".join(f"<li>{link(s['gq']['topic_url'], t)}</li>" for t in s["gq"]["topics"])
    return f'<h3>最新の一般質問｜{esc(s["session"])}（{esc(s["gq"]["date"])}）</h3><ol>{items}</ol>'


def render_stats(sessions, history):
    n_gq = sum(1 for s in sessions if s.get("gq"))
    first = min((s["year"] for s in sessions if s.get("gq")), default="")
    return (f'<div><b>9</b><span>当選回数</span></div>'
            f'<div><b>{n_gq}</b><span>一般質問（{first}年〜）</span></div>'
            f'<div><b>{history.get("documents", 0)}</b><span>本会議で発言した日</span></div>')


def render_roles(sessions):
    return "".join(f"<tr><th>{esc(r)}</th><td>{esc(span)}</td></tr>" for r, span in summarize_roles(sessions))


def render_committees(committees):
    blocks = []
    for c in committees:
        items = "".join(
            f'<li>{link(r["url"], r["label"])}' + ('<em>発言あり</em>' if r.get("spoke") else "") + "</li>"
            for r in c["records"])
        if not items:
            items = f'<li>{link(c["page"], "会議録一覧（筑紫野市ホームページ）")}</li>'
        blocks.append(f'<div class="com"><h3>{esc(c["name"])}</h3><ul>{items}</ul></div>')
    return "".join(blocks)


def replace_block(page, name, content):
    pat = re.compile(rf"(<!--AUTO:{name}-->).*?(<!--/AUTO:{name}-->)", re.S)
    if not pat.search(page):
        raise RuntimeError(f"index.html に AUTO:{name} の目印がありません")
    return pat.sub(lambda m: m.group(1) + content + m.group(2), page)


def render_pages(activity):
    history = json.loads(HISTORY.read_text(encoding="utf-8")) if HISTORY.exists() else {}
    sessions = merge_sessions(activity["questions"], history)
    for p in PAGES:
        s = p.read_text(encoding="utf-8")
        s = replace_block(s, "HISTORY", render_history(sessions))
        s = replace_block(s, "STATS", render_stats(sessions, history))
        s = replace_block(s, "ROLES", render_roles(sessions))
        s = replace_block(s, "LATEST", render_latest(sessions))
        s = replace_block(s, "COMMITTEES", render_committees(activity["committees"]))
        s = replace_block(s, "UPDATED", activity.get("updated", ""))
        p.write_text(s, encoding="utf-8")


def main():
    if "--render-only" in sys.argv:  # 保存済みのデータからページだけを作り直す
        render_pages(json.loads(DATA.read_text(encoding="utf-8")))
        return
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
    render_pages(new)
    print(f"更新しました: 一般質問 {len(questions)} 回分 / 会議録 {sum(len(c['records']) for c in committees)} 件")


if __name__ == "__main__":
    main()
