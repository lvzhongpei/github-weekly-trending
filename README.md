# GitHub 本周热榜

每周一自动更新的 GitHub Trending 观察站，带一份可复现的数据档案。

- 站点：https://lvzhongpei.github.io/github-weekly-trending/
- 自动更新：GitHub Actions，每周一 08:00（UTC+8）
- 数据来源：GitHub Trending（weekly / daily / monthly）+ GitHub REST Search API

## 它是什么

首页由 `scripts/generate.py` 抓取当日数据生成，只包含**可复现的事实与规则化统计**，不含编辑判断：

| 区块 | 内容 |
|---|---|
| 周榜 | Trending 近 7 天新增 star，按增量重排，含总星、语言、一句话简介 |
| 新项目 | 7 天内创建且突破 100 星的仓库，按 star 排序 |
| 结构观察 | 按关键词归类，统计各赛道项目数 |
| 方法与免责 | 数据口径、仿冒判定规则、时效说明 |

人工撰写的深度报告放在 `archive/report-*.html`，与自动页分开，避免把评论当成数据。

## 目录

```
index.html                 最新一期（自动生成）
assets/style.css           全站样式
scripts/generate.py        生成器（仅标准库）
archive/                   归档：自动页 + 人工报告
data/history/*.json        每周快照，用于计算留存率与环比
data/latest.csv            当周原始数据
.github/workflows/weekly.yml
```

## 本地运行

```bash
GITHUB_TOKEN=<你的 token> python3 scripts/generate.py
```

`GITHUB_TOKEN` 只用于 GitHub Search API 的限额（无 token 为 60 次/小时）。生成结果写入 `index.html` 与 `archive/<日期>.html`。

无网络时可用缓存重渲染：

```bash
python3 scripts/generate.py --offline
```

## 仿冒簇判定

高星新仓库里存在批量伪造的引流仓库。生成器的判定条件是**同时满足**：

1. 同一创建日期
2. fork 数全部为 0
3. 星数落在该组中位数 ±5% 内
4. 该组规模 ≥ 5

判定为疑似仿冒的仓库不会出现在页面上，也不会生成链接。这是启发式规则，存在误伤可能；判断依据与样本在 `data/latest.csv` 的 `note` 列中标注。

## 免责

star 不等于质量。本站不对任何项目的安全性、合法性、可用性做背书；模拟器、静态重编译、破解类项目请自行评估法律风险。数据为抓取瞬间值，可能随后变化。
