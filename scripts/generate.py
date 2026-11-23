#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GitHub 本周热榜 —— 静态站点生成器

抓取 GitHub Trending（weekly / daily / monthly）与 Search API 的新建高星仓库，
生成 index.html、归档页与数据文件。只用标准库，便于在 GitHub Actions 里运行。

用法：
    GITHUB_TOKEN=<token> python3 scripts/generate.py
    python3 scripts/generate.py --offline   # 用上次缓存的数据重渲染（调试用）
"""

import csv
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHIVE_DIR = os.path.join(ROOT, "archive")
DATA_DIR = os.path.join(ROOT, "data")
HISTORY_DIR = os.path.join(DATA_DIR, "history")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 github-weekly-trending/1.0"
CST = timezone(timedelta(hours=8))

CATEGORIES = [
    ("Agent 与技能", ["skill", "agent", "mcp", "claude", "codex", "copilot", "cursor", "prompt"]),
    ("视频与图像", ["video", "ffmpeg", "clip", "render", "image", "photo", "camera"]),
    ("设计与前端", ["design", "css", "ui", "frontend", "figma", "component", "theme"]),
    ("模拟器与重编译", ["emulator", "recomp", "switch", "xbox", "playstation", "nintendo", "rom"]),
    ("系统与桌面", ["windows", "shell", "macos", "launcher", "desktop", "taskbar", "debloat"]),
    ("数据与基础设施", ["database", "observability", "metrics", "infra", "server", "postgres", "log"]),
]


# ---------------------------------------------------------------- 网络

def fetch(url, token=None, retries=3, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/json"})
    if token:
        req.add_header("Authorization", "Bearer " + token)
        req.add_header("Accept", "application/vnd.github+json")
    last = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 429) and i < retries - 1:
                time.sleep(5 * (i + 1))
                continue
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (i + 1))
    raise RuntimeError("fetch failed: %s (%s)" % (url, last))


# ---------------------------------------------------------------- 解析

def _num(pattern, text):
    m = re.search(pattern, text, re.S)
    return int(m.group(1).replace(",", "")) if m else 0


def _clean_desc(raw):
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^(?:Sponsor\s*)?Star\s*", "", text)
    text = re.sub(r"^\S+\s*/\s*\S+\s*", "", text)
    return text.strip()


def parse_trending(page, period):
    """period: weekly / daily / monthly"""
    rows = []
    for block in re.split(r"<article", page)[1:]:
        m = re.search(r'<h2[^>]*>.*?href="/([^"]+)"', block, re.S)
        if not m:
            continue
        name = m.group(1)
        desc = ""
        for pm in re.finditer(r"<p[^>]*>(.*?)</p>", block, re.S):
            cand = _clean_desc(pm.group(1))
            if "/" in pm.group(1) and len(cand) > 8:
                desc = cand
                break
        gain = re.search(r"([\d,]+)\s*stars (?:this|today)", block)
        lang = re.search(r'itemprop="programmingLanguage">([^<]+)</span>', block)
        rows.append({
            "name": name,
            "gain": int(gain.group(1).replace(",", "")) if gain else 0,
            "total": _num(r'href="/[^"]+/stargazers"[^>]*>.*?</svg>\s*([\d,]+)', block),
            "forks": _num(r'href="/[^"]+/forks"[^>]*>.*?</svg>\s*([\d,]+)', block),
            "lang": lang.group(1).strip() if lang else "-",
            "desc": desc,
            "period": period,
        })
    rows.sort(key=lambda x: -x["gain"])
    return rows


def fetch_new_repos(since, token):
    query = "created:>%s stars:>100" % since
    url = ("https://api.github.com/search/repositories?q=%s&sort=stars&order=desc&per_page=100"
           % urllib.parse.quote(query))
    payload = json.loads(fetch(url, token))
    items = []
    for it in payload.get("items", []):
        items.append({
            "name": it["full_name"],
            "stars": it["stargazers_count"],
            "forks": it["forks_count"],
            "lang": it.get("language") or "-",
            "created": (it.get("created_at") or "")[:10],
            "license": ((it.get("license") or {}).get("spdx_id")) or "-",
            "desc": (it.get("description") or "").replace("\n", " ").strip(),
            "url": it["html_url"],
        })
    return payload.get("total_count", len(items)), items


# ---------------------------------------------------------------- 分析

def detect_clusters(items, min_size=5):
    """同一创建日、零 fork、星数高度集中的仓库簇 —— 仿冒/刷星的典型形态。"""
    flagged = []
    by_date = {}
    for it in items:
        if it["forks"] == 0:
            by_date.setdefault(it["created"], []).append(it)
    for _date, group in by_date.items():
        if len(group) < min_size:
            continue
        stars = sorted(x["stars"] for x in group)
        median = stars[len(stars) // 2]
        tight = [x for x in group if abs(x["stars"] - median) <= max(10, median * 0.05)]
        if len(tight) >= min_size:
            flagged.extend(tight)
    names = {x["name"] for x in flagged}
    return flagged, names


def classify(item):
    hay = (item["name"] + " " + item.get("desc", "")).lower()
    for label, keys in CATEGORIES:
        if any(k in hay for k in keys):
            return label
    return "其他"


def previous_history(today):
    if not os.path.isdir(HISTORY_DIR):
        return None
    files = sorted(f for f in os.listdir(HISTORY_DIR) if f.endswith(".json") and f[:10] < today)
    if not files:
        return None
    try:
        with open(os.path.join(HISTORY_DIR, files[-1]), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- 渲染

def load_i18n():
    path = os.path.join(DATA_DIR, "i18n.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, str) and v.strip()}
    except Exception:  # noqa: BLE001
        return {}


def desc_cell(item, i18n):
    """中文优先；缺译时回退英文原文，并标记 fallback 以便弱化显示。"""
    en = (item.get("desc") or "").strip() or "—"
    zh = i18n.get(item["name"])
    if zh:
        return ('<span class="i18n zh">%s</span><span class="i18n en">%s</span>'
                % (esc(zh), esc(en[:160])))
    return ('<span class="i18n zh fallback">%s</span><span class="i18n en">%s</span>'
            % (esc(en[:160]), esc(en[:160])))


def esc(text):
    return html.escape(str(text), quote=True)


def pct(part, whole):
    return 0.0 if not whole else round(part * 100.0 / whole, 1)


def render(week, daily, monthly, new_total, new_items, flagged, prev, today, base="", home="./", i18n=None):
    i18n = i18n or {}
    weekly = week
    total_gain = sum(x["gain"] for x in weekly)
    top = weekly[0] if weekly else {"gain": 1, "name": "-"}
    denom = top["gain"] or 1

    prev_names = {x["name"] for x in (prev or {}).get("weekly", [])}
    cur_names = {x["name"] for x in weekly}
    retained = cur_names & prev_names
    new_faces = len(cur_names - prev_names) if prev_names else 0
    prev_total = (prev or {}).get("total_gain", 0)
    delta = None if not prev_total else round((total_gain - prev_total) * 100.0 / prev_total, 1)

    clean_new = [x for x in new_items if x["name"] not in {f["name"] for f in flagged}][:15]

    cats = {}
    for x in weekly + clean_new:
        cats[classify(x)] = cats.get(classify(x), 0) + 1
    cat_rows = sorted(cats.items(), key=lambda kv: -kv[1])

    langs = {}
    for x in weekly:
        langs[x["lang"]] = langs.get(x["lang"], 0) + 1
    lang_txt = " · ".join("%s %d" % (k, v) for k, v in sorted(langs.items(), key=lambda kv: -kv[1]))

    # ---- 表格行
    weekly_rows = []
    for i, x in enumerate(weekly, 1):
        weekly_rows.append(
            '<tr><td class="num" style="color:var(--muted)">%d</td>'
            '<td>%s</td>'
            '<td class="bar-cell"><div class="bar"><i style="width:%.1f%%"></i></div>'
            '<div class="micro" style="margin-top:5px">%s</div></td>'
            '<td class="r num">%s</td><td class="r micro hide-m">%s</td>'
            '<td class="desc hide-m">%s</td></tr>' % (
                i, _link(x["name"]), max(1.5, x["gain"] * 100.0 / denom),
                "{:,}".format(x["gain"]), "{:,}".format(x["total"]),
                esc(x["lang"]), desc_cell(x, i18n)))

    new_rows = []
    for i, x in enumerate(clean_new, 1):
        new_rows.append(
            '<tr><td class="num" style="color:var(--muted)">%02d</td><td>%s</td>'
            '<td class="r num">%s</td><td class="r num">%s</td>'
            '<td class="r micro hide-m">%s</td><td class="desc hide-m">%s</td></tr>' % (
                i, _link(x["name"]), "{:,}".format(x["stars"]), "{:,}".format(x["forks"]),
                esc(x["created"]), desc_cell(x, i18n)))

    cat_html = "".join(
        '<tr><td>%s</td><td class="bar-cell"><div class="bar"><i style="width:%.1f%%"></i></div></td>'
        '<td class="r num">%d</td></tr>' % (esc(k), v * 100.0 / max(1, max(cats.values())), v)
        for k, v in cat_rows)

    delta_txt = "—" if delta is None else ("%+.1f%%" % delta)
    delta_cls = "" if delta is None else ("up" if delta >= 0 else "dn")
    retention = "%d / %d" % (len(retained), len(prev_names)) if prev_names else "首次运行"

    sub_bits = ["本周 %d 个上榜项目合计新增 <b>%s</b> 星" % (len(weekly), "{:,}".format(total_gain))]
    if prev_names:
        sub_bits.append("其中 <b>%d</b> 个是新面孔" % new_faces)
    if flagged:
        sub_bits.append("同时在新仓库里检出 <b>%d</b> 个疑似仿冒簇" % len(flagged))

    archive_nav = '<a href="%sarchive/">归档</a>' % base

    return """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>GitHub 本周热榜 · {today}</title>
<meta name="description" content="每周一自动更新的 GitHub Trending 榜单与新建高星仓库统计。">
<link rel="stylesheet" href="{base}assets/style.css">
</head>
<body>
<header class="site-head"><div class="site-head-in"><a class="brand" href="{home}"><b>GITHUB WEEKLY</b><span>auto-generated</span></a>
<nav>{archive_nav}<a href="https://github.com/lvzhongpei/github-weekly-trending" target="_blank" rel="noopener">源码</a><button class="lang-switch" id="lang-switch" type="button" aria-label="切换中英文">EN</button></nav></div></header>
<main class="wrap">
<div class="masthead">
  <div class="kicker">Weekly Brief · {today} · Auto-generated</div>
  <h1>GitHub 本周热榜</h1>
  <p class="sub">{sub}</p>
  <div class="kpi">
    <div><span class="n">{total_gain}</span><span class="l">周榜新增星</span><span class="d {delta_cls}">{delta} vs 上期</span></div>
    <div><span class="n">{n_weekly}</span><span class="l">上榜项目</span><span class="d">留存 {retention}</span></div>
    <div><span class="n">{new_total}</span><span class="l">7 天新建 &gt;100★</span><span class="d">全部为本周新建仓库</span></div>
    <div><span class="n">{n_flag}</span><span class="l">疑似仿冒簇</span><span class="d {flag_cls}">{flag_note}</span></div>
  </div>
  <p class="hint" style="font-family:var(--mono);font-size:11.5px;color:var(--muted);margin:16px 0 0">
    本页由 GitHub Actions 自动生成，仅含数据与规则化统计；人工撰写的深度分析见归档中的「月报 / 周报」。</p>
  <nav class="toc"><a href="#s1">01 周榜</a><a href="#s2">02 新项目</a><a href="#s3">03 结构观察</a><a href="#s4">04 方法与免责</a></nav>
</div>

<section id="s1">
  <div class="sec-head"><span class="sec-num">01</span><h2>本周榜单</h2></div>
  <p class="lede">GitHub Trending 近 7 天新增 star，已按新增量重排。条形满格为 {top_gain}。</p>
  <div class="tbl-scroll" style="overflow-x:auto">
  <table>
    <thead><tr><th style="width:26px">#</th><th style="width:290px">项目</th><th class="bar-cell">周增星</th>
    <th class="r" style="width:82px">总星</th><th class="r hide-m" style="width:96px">语言</th><th class="hide-m">一句话</th></tr></thead>
    <tbody>{weekly_rows}</tbody>
  </table>
  </div>
  <p class="note">语言分布：{lang_txt}。抓取时间 {stamp}。</p>
</section>

<section id="s2">
  <div class="sec-head"><span class="sec-num">02</span><h2>本周新建 · 高星仓库</h2></div>
  <p class="lede">{since} 之后创建、7 天内突破 100 星的仓库共 {new_total} 个，下表为剔除疑似仿冒簇后的前 15 名。</p>
  <div class="tbl-scroll" style="overflow-x:auto">
  <table>
    <thead><tr><th style="width:34px">#</th><th style="width:300px">项目</th><th class="r" style="width:82px">Stars</th>
    <th class="r" style="width:76px">Forks</th><th class="r hide-m" style="width:92px">创建</th><th class="hide-m">一句话</th></tr></thead>
    <tbody>{new_rows}</tbody>
  </table>
  </div>
  <p class="note">已排除 {n_flag} 个疑似仿冒仓库（判定规则见 04 节）。这些仓库不生成链接。</p>
</section>

<section id="s3">
  <div class="sec-head"><span class="sec-num">03</span><h2>结构观察</h2></div>
  <p class="lede">按仓库名与描述做关键词归类，只反映「这批项目在做什么」，不代表质量评价。</p>
  <table style="max-width:560px">
    <thead><tr><th>类别</th><th class="bar-cell">占比</th><th class="r" style="width:60px">数量</th></tr></thead>
    <tbody>{cat_html}</tbody>
  </table>
  <div class="callout">
    <b>本期三个数字。</b>周榜合计新增 {total_gain} 星（较上期 {delta}）；与上期榜单重叠 {retention}；
    新建高星仓库 {new_total} 个，其中疑似仿冒 {n_flag} 个。这三项每周都会重算，可直接横向比较。
  </div>
</section>

<section id="s4">
  <div class="sec-head"><span class="sec-num">04</span><h2>方法与免责</h2></div>
  <p class="lede">本页是数据页，不是评论页。以下规则决定了它会说什么、不会说什么。</p>
  <table>
    <thead><tr><th style="width:150px">项目</th><th>说明</th></tr></thead>
    <tbody>
      <tr><td class="micro">数据来源</td><td class="desc">github.com/trending（weekly / daily / monthly）与 GitHub REST Search API，均为公开数据。</td></tr>
      <tr><td class="micro">star 口径</td><td class="desc">「周增星」为 GitHub 页面披露值，不提供逐日曲线，无法二次核验；「总星」为抓取瞬间值。</td></tr>
      <tr><td class="micro">仿冒判定</td><td class="desc">同一创建日、fork 为 0、星数落在中位数 ±5% 内的仓库达到 5 个及以上，判定为疑似仿冒簇。这是启发式规则，可能误伤真实项目。</td></tr>
      <tr><td class="micro">类别</td><td class="desc">按名称与描述的关键词归类，一个项目只归一类，优先级见源码 CATEGORIES。</td></tr>
      <tr><td class="micro">简介语言</td><td class="desc">中文简介来自仓库内 <code>data/i18n.json</code>，由人工维护，不是机器翻译；没有条目时回退 GitHub 英文原文。右上角可切换中英文。</td></tr>
      <tr><td class="micro">时效</td><td class="desc">所有数字为抓取瞬间值。同一周内重复抓取会出现小幅差异（例如仿冒簇数量会随刷星持续增长而增加），这是数据在变，不是计算错误。</td></tr>
      <tr><td class="micro">免责</td><td class="desc">star 不等于质量。本页不对任何项目的安全性、合法性、可用性做背书；模拟器、重编译、破解类项目请自行评估法律风险。</td></tr>
    </tbody>
  </table>
</section>
</main>
<footer><span>GitHub 本周热榜 · {today}</span><span>由 GitHub Actions 每周一自动生成 · 数据抓取 {stamp}</span></footer>
<script src="{base}assets/lang.js" defer></script>
</body>
</html>
""".format(
        today=today, archive_nav=archive_nav, base=base, home=home, sub="；".join(sub_bits) + "。",
        total_gain="{:,}".format(total_gain), delta=delta_txt, delta_cls=delta_cls,
        n_weekly=len(weekly), retention=retention, new_total="{:,}".format(new_total),
        n_flag=len(flagged), flag_cls=("dn" if flagged else ""),
        flag_note=("同日创建 · 零 fork" if flagged else "本期未检出"),
        top_gain="{:,}".format(top["gain"]), weekly_rows="".join(weekly_rows),
        new_rows="".join(new_rows), since=(datetime.strptime(today, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d"),
        cat_html=cat_html, lang_txt=esc(lang_txt),
        stamp=datetime.now(CST).strftime("%Y-%m-%d %H:%M CST"),
    )


def _link(name):
    owner, _, repo = name.partition("/")
    return ('<a class="proj" href="https://github.com/%s" target="_blank" rel="noopener">%s'
            '<span class="owner">/%s</span></a>') % (esc(name), esc(owner), esc(repo))


# ---------------------------------------------------------------- 归档

def rebuild_archive_index(today):
    items = []
    if os.path.isdir(ARCHIVE_DIR):
        for fn in os.listdir(ARCHIVE_DIR):
            if not fn.endswith(".html") or fn == "index.html":
                continue
            path = os.path.join(ARCHIVE_DIR, fn)
            try:
                with open(path, encoding="utf-8") as fh:
                    head = fh.read(4000)
            except Exception:  # noqa: BLE001
                head = ""
            m = re.search(r"<title>(.*?)</title>", head, re.S)
            title = html.unescape(m.group(1)).strip() if m else fn
            dm = re.search(r"(\d{4}-\d{2}-\d{2})", fn)
            date = dm.group(1) if dm else "0000-00-00"
            is_report = fn.startswith("report-")
            items.append((date, is_report, fn, title))
    # 按日期倒序；同一天人工报告排在自动页之前
    items.sort(key=lambda x: (x[0], 1 if x[1] else 0), reverse=True)
    rows = "".join(
        '<li><a href="%s">%s</a><span class="meta">%s · %s</span></li>' % (
            esc(fn), esc(title), esc(date), "人工报告" if is_report else "自动数据页")
        for date, is_report, fn, title in items)
    page = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>归档 · GitHub 本周热榜</title><link rel="stylesheet" href="../assets/style.css"></head>
<body><header class="site-head"><div class="site-head-in"><a class="brand" href="../"><b>GITHUB WEEKLY</b><span>archive</span></a>
<nav><a href="../">最新一期</a></nav></div></header>
<main class="wrap"><div class="masthead"><div class="kicker">Archive · %s</div><h1>归档</h1>
<p class="sub">按时间倒序。含每周自动生成的数据页与人工撰写的深度报告。</p></div>
<section style="border-top:none;margin-top:0;padding-top:10px"><ul class="archive-list">%s</ul></section>
</main><footer><span>GitHub 本周热榜 · 归档</span><span>共 %d 期</span></footer></body></html>
""" % (today, rows or "<li>暂无归档</li>", len(items))
    with open(os.path.join(ARCHIVE_DIR, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(page)
    return len(items)


# ---------------------------------------------------------------- 主流程

def main():
    offline = "--offline" in sys.argv
    token = os.environ.get("GITHUB_TOKEN", "").strip() or None
    today = datetime.now(CST).strftime("%Y-%m-%d")
    os.makedirs(HISTORY_DIR, exist_ok=True)

    cache = os.path.join(DATA_DIR, "last_fetch.json")
    if offline and os.path.exists(cache):
        with open(cache, encoding="utf-8") as fh:
            snap = json.load(fh)
    else:
        week = parse_trending(fetch("https://github.com/trending?since=weekly"), "weekly")
        daily = parse_trending(fetch("https://github.com/trending?since=daily"), "daily")
        monthly = parse_trending(fetch("https://github.com/trending?since=monthly"), "monthly")
        since = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
        new_total, new_items = fetch_new_repos(since, token)
        snap = {"weekly": week, "daily": daily, "monthly": monthly,
                "new_total": new_total, "new_items": new_items, "since": since, "date": today}
        with open(cache, "w", encoding="utf-8") as fh:
            json.dump(snap, fh, ensure_ascii=False)

    week, daily, monthly = snap["weekly"], snap["daily"], snap["monthly"]
    new_total, new_items = snap["new_total"], snap["new_items"]
    flagged, flagged_names = detect_clusters(new_items)
    prev = previous_history(snap.get("date", today))
    i18n = load_i18n()

    day = snap.get("date", today)
    page = render(week, daily, monthly, new_total, new_items, flagged, prev, day, i18n=i18n)
    with open(os.path.join(ROOT, "index.html"), "w", encoding="utf-8") as fh:
        fh.write(page)
    arch = render(week, daily, monthly, new_total, new_items, flagged, prev, day, base="../", home="../", i18n=i18n)
    with open(os.path.join(ARCHIVE_DIR, "%s.html" % day), "w", encoding="utf-8") as fh:
        fh.write(arch)

    # 数据存档
    with open(os.path.join(HISTORY_DIR, "%s.json" % today), "w", encoding="utf-8") as fh:
        json.dump({"date": today, "weekly": week, "daily": daily, "monthly": monthly,
                   "total_gain": sum(x["gain"] for x in week),
                   "flagged": [f["name"] for f in flagged],
                   "new_total": new_total}, fh, ensure_ascii=False, indent=1)

    with open(os.path.join(DATA_DIR, "latest.csv"), "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["dataset", "rank", "repo", "stars_period", "total_stars", "forks",
                    "language", "created", "license", "url", "note"])
        for i, x in enumerate(week, 1):
            w.writerow(["weekly", i, x["name"], x["gain"], x["total"], x["forks"], x["lang"],
                        "", "", "https://github.com/" + x["name"], x["desc"][:180]])
        for i, x in enumerate(new_items, 1):
            note = ("【疑似仿冒引流仓库，勿下载】" if x["name"] in flagged_names else "") + x["desc"][:180]
            w.writerow(["new", i, x["name"], x["stars"], x["stars"], x["forks"], x["lang"],
                        x["created"], x["license"], x["url"], note])

    n_arch = rebuild_archive_index(today)
    print("generated index.html: weekly=%d new=%d flagged=%d archive=%d total_gain=%d"
          % (len(week), len(new_items), len(flagged), n_arch, sum(x["gain"] for x in week)))


if __name__ == "__main__":
    main()
