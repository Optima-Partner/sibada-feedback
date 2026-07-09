#!/usr/bin/env python3
"""生成 sibada-medical 知识库「口播内容层」。

数据驱动 + 幂等 + 增量：
- 直接读 data/douyin/<region>/<slug>/{profile,videos,transcripts-corrected}.json
- slug→中文名从 crawl-summary*.json 自动读，无需改脚本
- 只入**已有 transcripts-corrected.json** 的账号；仅有元数据（未转写）的账号跳过并报告
- 纯机械忠实：description=标题、hashtags=标签、transcript=正文、metadata=互动/时间，零 LLM 归纳
- index.md 的口播层入口/状态两处由本脚本在 BEGIN/END marker 之间维护

用法：
  gen_content_layer.py --data <repo>/data/douyin --kb <KB工作clone路径>
"""
import argparse, json, os, glob, re, sys
from collections import Counter

REGION_CN = {"hangzhou": "杭州", "urumqi": "乌鲁木齐", "nanjing": "南京", "luoyang": "洛阳"}

# 选题聚合时剔除的非选题标签：通用科普 + 平台活动 + 医院名 + 节日问候 + 身份。
# 只影响"高频选题"聚合视图；raw_md 里每条视频完整原始标签一个不删。
GENERIC_TAGS = {
    "健康科普", "科普", "医学科普", "妇产科科普", "涨知识", "涨知识啦", "健康", "关注我", "医生",
    "健康科普计划", "健康科普破圈计划", "硬核健康科普行动", "医生科普", "医生日常", "抖医医生",
    "健康知识", "健康知识分享", "知识分享", "健康知识科普",
    "健康知识宝藏", "抖音健康知识宝藏", "抖音健康知识", "抖音健康知识分享", "抖音健康知识科普",
    "抖出健康知识宝藏", "都出健康知识宝藏", "抖出健康知识",
    "乌鲁木齐市妇幼保健院", "浙大邵逸夫医院", "邵逸夫医院",
    "马年大吉2026", "新年快乐", "祝大家新年快乐", "祝大家新年快乐心想事成万事如意",
    "医护工作者", "我的大白挂", "我的白大褂", "白大褂",
}
GREETING = re.compile(r"快乐|平安|喜乐|顺遂|安康|幸福|吉祥|如意|每一天|新的一年|好运|接好孕|接一切")


def date_of(iso): return iso[:10] if iso else "?"
def dur_str(ms):
    if not ms: return "—"
    s = int(ms/1000); return f"{s//60}:{s%60:02d}"


def build_name_map(data_root):
    """slug -> 中文名，从所有 crawl-summary*.json 汇总。"""
    m = {}
    for f in glob.glob(f"{data_root}/crawl-summary*.json"):
        for a in json.load(open(f)).get("accounts", []):
            if a.get("slug") and a.get("name"):
                m[a["slug"]] = a["name"]
    return m


def discover(data_root):
    """返回 (ready, pending)：ready=有转写可入库，pending=仅元数据未转写。"""
    ready, pending = [], []
    for prof in sorted(glob.glob(f"{data_root}/*/*/profile.json")):
        d = os.path.dirname(prof)
        region = os.path.basename(os.path.dirname(d))
        slug = os.path.basename(d)
        has_tr = os.path.exists(f"{d}/transcripts-corrected.json")
        has_v = os.path.exists(f"{d}/videos.json")
        (ready if (has_tr and has_v) else pending).append((region, slug, d))
    return ready, pending


def short_title(desc, transcript=""):
    if desc and desc.strip():
        line = re.split(r"\s+#", desc.strip().split("\n")[0])[0].strip() or desc.strip().split("\n")[0]
        if line: return line[:60]
    if transcript and transcript.strip():
        return "（无描述，口播首句）" + transcript.strip()[:24] + "…"
    return "（无描述）"


def gen(data_root, kb):
    name_map = build_name_map(data_root)
    ready, pending = discover(data_root)
    os.makedirs(f"{kb}/raw_md/口播文案", exist_ok=True)
    os.makedirs(f"{kb}/wiki/content/doctors", exist_ok=True)

    doctor_rows, global_tags = [], Counter()
    tot_v = tot_text = tot_note = tot_skip = tot_corrected = 0

    for region, slug, d in ready:
        p = json.load(open(f"{d}/profile.json"))
        vids = json.load(open(f"{d}/videos.json"))
        tr = json.load(open(f"{d}/transcripts-corrected.json"))
        name = name_map.get(slug, p.get("displayName", slug))
        region_cn = REGION_CN.get(region, region)
        bio_flat = re.sub(r"[\r\n]+", " ｜ ", p.get("bio", "")).strip()
        vids = sorted(vids, key=lambda v: v.get("createdAt", ""), reverse=True)
        dates = [date_of(v.get("createdAt")) for v in vids if v.get("createdAt")]
        span = f"{min(dates)} ~ {max(dates)}" if dates else "—"

        tagc = Counter()
        for v in vids:
            for h in (v.get("hashtags") or []):
                if h in GENERIC_TAGS or name in h or slug in h.lower() or GREETING.search(h): continue
                tagc[h] += 1; global_tags[h] += 1
        top = [f"{h}({c})" for h, c in tagc.most_common(12)]

        n_text = sum(1 for e in tr.values() if isinstance(e, dict) and e.get("text") and not e.get("note"))
        n_note = sum(1 for e in tr.values() if isinstance(e, dict) and e.get("note"))
        n_skip = sum(1 for e in tr.values() if isinstance(e, dict) and e.get("skipped"))
        tot_v += len(vids); tot_text += n_text; tot_note += n_note; tot_skip += n_skip
        tot_corrected += n_text + n_note

        # ---- raw_md source of truth ----
        os.makedirs(f"{kb}/raw_md/口播文案/{region}", exist_ok=True)
        R = [f"# 口播文案原文 — {region_cn}·{name}（抖音）\n"]
        R.append(f"> **来源链**：抖音账号「{p.get('displayName','')}」({p.get('totalVideos')} 条视频) "
                 f"→ `scout douyin`(TikHub) 抓取视频与元数据 → `gen asr`(Groq whisper-large-v3-turbo) 转写口播 "
                 f"→ 生殖医学术语表逐条校对（修同音字，不改写内容）。")
        R.append("> **性质**：ASR 转写文本，经术语校对后仍可能存在识别误差；**非临床/法规事实依据**，仅为该医生在公开平台口播中实际表达过的内容。")
        R.append(f"> **回溯**：每条含抖音视频 id，原视频与原始 ASR 见 `Optima-Partner/sibada-feedback` repo `data/douyin/{region}/{slug}/`。视频 CDN 地址为临时签名地址，不在此保存。\n")
        R.append(f"账号：{p.get('displayName')} · 粉丝 {p.get('followers'):,} · 视频 {p.get('totalVideos')} · 累计获赞 {p.get('totalLikes'):,} · 简介「{bio_flat}」\n")
        R.append("---\n")
        for v in vids:
            vid = str(v["id"]); ent = tr.get(vid, {})
            dd = date_of(v.get("createdAt"))
            tt = ent.get("text", "") if (ent.get("text") and not ent.get("note") and not ent.get("skipped")) else ""
            R.append(f"## [{dd}] {short_title(v.get('description',''), tt)}")
            R.append(f"<!-- video_id: {vid} -->")
            R.append(f"- 发布：{dd} ｜ 时长：{dur_str(v.get('duration'))} ｜ 点赞 {v.get('likes',0)} / 评论 {v.get('comments',0)} / 收藏 {v.get('collects',0)} / 分享 {v.get('shares',0)}")
            if v.get("description"): R.append(f"- 抖音描述：{v['description'].strip()}")
            tags = " ".join(f"#{h}" for h in (v.get("hashtags") or []))
            if tags: R.append(f"- 标签：{tags}")
            R.append("")
            if ent.get("skipped"):
                R.append(f"> ⚠ 图文帖 / 无口播视频（{ent['skipped']}），无转写文本。")
            elif ent.get("note"):
                R.append(f"> ⚠ 疑似无口播（{ent['note']}），以下文本为 ASR 对 BGM/字幕的识别，可能不准，入库请酌情过滤：")
                R.append(""); R.append(ent.get("text", "").strip())
            elif ent.get("text"):
                R.append("口播文字稿："); R.append(""); R.append(ent["text"].strip())
            else:
                R.append("> （无转写）")
            R.append("\n---\n")
        open(f"{kb}/raw_md/口播文案/{region}/{slug}.md", "w").write("\n".join(R))

        # ---- wiki 医生页 ----
        W = [f"# {region_cn}·{name} — 抖音口播内容", ""]
        W.append(f"> 口播内容层医生页。原文见 [`raw_md/口播文案/{region}/{slug}.md`](../../../raw_md/口播文案/{region}/{slug}.md)。"
                 f"本页为该医生公开抖音口播的**内容导航**（选题 / 发布量 / 互动），供内容创作与选题参考，**非临床事实依据**。")
        W += ["", "## 账号", "", "| 项 | 值 |", "|---|---|",
              f"| 医生 | {name} |",
              f"| 地区 / 机构 | {region_cn} ｜ {bio_flat} |",
              f"| 抖音账号 | {p.get('displayName','')} |",
              f"| 粉丝 / 获赞 | {p.get('followers'):,} ｜ {p.get('totalLikes'):,} |",
              f"| 视频数 | {p.get('totalVideos')} |",
              f"| 发布时间跨度 | {span} |",
              f"| 主页 | {p.get('profileUrl','')} |", ""]
        if top:
            W += ["## 高频选题标签", "",
                  "（按该医生视频的 hashtag 频次，已剔除账号名与「健康科普」等通用标签；括号内为条数）", "",
                  "- " + " · ".join(top), ""]
        W += ["## 视频选题一览", "",
              "按发布时间倒序。标题 = 医生抖音描述首句；点击「原文」跳 raw_md 对应条目（Ctrl+F 视频 id）。", "",
              "| 日期 | 选题（抖音描述） | 时长 | 赞/评/藏 | 口播原文 |", "|---|---|---|---|---|"]
        for v in vids:
            vid = str(v["id"]); ent = tr.get(vid, {})
            dd = date_of(v.get("createdAt"))
            tt = ent.get("text", "") if (ent.get("text") and not ent.get("note") and not ent.get("skipped")) else ""
            title = short_title(v.get("description", ""), tt).replace("|", "｜")
            flag = " 〔图文〕" if ent.get("skipped") else (" 〔疑无口播〕" if ent.get("note") else "")
            eng = f"{v.get('likes',0)}/{v.get('comments',0)}/{v.get('collects',0)}"
            W.append(f"| {dd} | {title}{flag} | {dur_str(v.get('duration'))} | {eng} | [原文](../../../raw_md/口播文案/{region}/{slug}.md) |")
        W.append("")
        open(f"{kb}/wiki/content/doctors/{slug}.md", "w").write("\n".join(W))

        doctor_rows.append(dict(region=region, region_cn=region_cn, name=name, slug=slug,
                                disp=p.get("displayName", ""), fol=p.get("followers", 0),
                                nv=p.get("totalVideos", 0), span=span,
                                themes=[h for h, _ in tagc.most_common(6)]))

    # ---- wiki/content/README.md ----
    write_readme(kb, doctor_rows, global_tags, tot_v, pending, name_map)
    # ---- index.md marker 区块 ----
    update_index(kb, doctor_rows, pending, name_map, tot_v, tot_text, tot_skip, tot_note, tot_corrected)

    return doctor_rows, pending, tot_v


def _pending_line(pending, name_map):
    """把未转写账号按地区汇总成一句 '南京 5 位、洛阳待补' 式文本。"""
    by_region = {}
    for region, slug, _ in pending:
        by_region.setdefault(REGION_CN.get(region, region), 0)
        by_region[region if False else REGION_CN.get(region, region)] += 1
    parts = [f"{r} {n} 位" for r, n in by_region.items()]
    return "、".join(parts) if parts else ""


def write_readme(kb, rows, global_tags, tot_v, pending, name_map):
    n_h = sum(1 for r in rows if r["region"] == "hangzhou")
    n_u = sum(1 for r in rows if r["region"] == "urumqi")
    pend = _pending_line(pending, name_map)
    M = ["# 口播内容层 — 医生科普内容", "",
         "本目录是知识库的**第三层：口播内容层**，与法规层、咨询层并列。", "",
         "- **来源**：斯巴达合作医生在**公开抖音账号**发布的科普短视频，经 `scout douyin` 抓取 → `gen asr` 转写 → 生殖医学术语校对。",
         "- **用途**：**内容创作与选题参考**——看医生们讲过哪些选题、某医生的表达风格、某话题大家怎么科普，辅助写口播 / 做数字人脚本。",
         f"- **规模**：{len(rows)} 位医生 / {tot_v} 条视频（杭州 {n_h} + 乌鲁木齐 {n_u}）。" + (f"{pend}待补。" if pend else ""), "",
         "> ⚠ **重要边界**：本层是医生在**社交平台面向大众**的口播表达，为传播做过简化，且是 ASR 转写（校对后仍可能有识别误差）。",
         "> **不得当作临床或法规事实依据**——查法规请用法规层，查临床事实请用咨询层。本层只回答"
         "「某医生 / 我们的医生在公开视频里**讲过**什么、怎么讲」，不回答「事实上**应该**怎样」。", "",
         "---", "", "## 医生页", "",
         "| 地区 | 医生 | 抖音账号 | 粉丝 | 视频 | 时间跨度 | 主要选题 |", "|---|---|---|---|---|---|---|"]
    for r in sorted(rows, key=lambda r: (r["region"], -r["nv"])):
        tstr = "、".join(r["themes"]) if r["themes"] else "—"
        M.append(f"| {r['region_cn']} | [{r['name']}](doctors/{r['slug']}.md) | {r['disp']} | {r['fol']:,} | {r['nv']} | {r['span']} | {tstr} |")
    M += ["", "## 跨医生高频选题标签", "",
          "（全部医生视频 hashtag 聚合，剔除账号名与通用标签；括号内为总条数。用于快速找「哪些选题被反复讲」）", "",
          " · ".join(f"`{h}`({c})" for h, c in global_tags.most_common(40)), "",
          "---", "", "## 原文与回溯", "",
          "- 每位医生的全部口播文字稿在 [`raw_md/口播文案/<地区>/<拼音>.md`](../../raw_md/口播文案/)，按发布时间倒序，每条带 `video_id`。",
          "- 原视频文件、原始 ASR、抓取脚本见 `Optima-Partner/sibada-feedback` repo（issue #10）`data/douyin/`。",
          "- 视频 CDN 地址为**临时签名地址会过期**，需要视频用 `scout douyin video-download <video_id>` 现取。", ""]
    open(f"{kb}/wiki/content/README.md", "w").write("\n".join(M))


def _replace_marker(text, key, new_body):
    b, e = f"<!-- douyin:{key}:begin -->", f"<!-- douyin:{key}:end -->"
    pat = re.compile(re.escape(b) + r".*?" + re.escape(e), re.S)
    if not pat.search(text):
        sys.stderr.write(f"[warn] index.md 缺 marker {key}，跳过该区块更新\n")
        return text
    return pat.sub(b + "\n" + new_body + "\n" + e, text)


def update_index(kb, rows, pending, name_map, tot_v, tot_text, tot_skip, tot_note, tot_corrected):
    idx_path = f"{kb}/index.md"
    if not os.path.exists(idx_path): return
    txt = open(idx_path).read()

    # 区块 A：医生页入口（按地区分组）
    by_region = {}
    for r in sorted(rows, key=lambda r: (r["region"], -r["nv"])):
        by_region.setdefault(r["region_cn"], []).append(r)
    lines = [f"### 医生页（[wiki/content/doctors/](wiki/content/doctors/)，{len(rows)} 位）", ""]
    for region_cn, rs in by_region.items():
        items = " · ".join(f"[{r['name']}](wiki/content/doctors/{r['slug']}.md)（{r['nv']} 条）" for r in rs)
        lines.append(f"- **{region_cn}**：{items}")
    pend = _pending_line(pending, name_map)
    lines += ["", "口播文字稿原文在 [`raw_md/口播文案/`](raw_md/口播文案/)（按地区/医生分文件，每条带 `video_id`）。"
              + (f"{pend}待补（先抓取→转写→校对再入库）。" if pend else "")]
    txt = _replace_marker(txt, "doctors", "\n".join(lines))

    # 区块 B：状态表行
    # tot_text=正常口播(862)、tot_note=疑似无口播(40)、tot_skip=图文帖(11)。
    # 「转写」= 有文本的条目 = 862+40=902；「有效/术语校对」= 正常口播 = 862（note 是幻觉不计校对）。
    n_transcribed = tot_text + tot_note
    n_valid = tot_text
    n_h = sum(1 for r in rows if r["region"] == "hangzhou")
    n_u = sum(1 for r in rows if r["region"] == "urumqi")
    pend = _pending_line(pending, name_map)
    strows = [
        f"| raw_md 口播文案 | ✅ {len(rows)} 位医生（杭州 {n_h} + 乌鲁木齐 {n_u}），{tot_v} 条视频，{n_transcribed} 条口播文字稿 + {tot_skip} 图文帖标记 |",
        f"| wiki/content 医生页 | ✅ {len(rows)} 页 + README（选题聚合 + 医生索引） |",
        f"| 术语校对 | ✅ {n_valid} 条有效口播经生殖医学术语校对（原始 ASR 保留在 sibada-feedback repo） |",
        f"| 幻觉/无口播标记 | ✅ {tot_note} 条疑似无口播 + {tot_skip} 图文帖已标 `⚠`，引用时排除 |",
        f"| 待补 | ⏳ {pend + '（先抓取→转写→校对再入库）' if pend else '无'} |",
    ]
    txt = _replace_marker(txt, "status", "\n".join(strows))
    open(idx_path, "w").write(txt)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True, help="data/douyin 目录")
    ap.add_argument("--kb", required=True, help="KB 工作 clone 路径")
    a = ap.parse_args()
    rows, pending, tot_v = gen(a.data, a.kb)
    print(f"入库 {len(rows)} 位医生 / {tot_v} 条视频")
    if pending:
        print(f"跳过（仅元数据、未转写）{len(pending)} 个账号：")
        for region, slug, _ in pending:
            print(f"  - {REGION_CN.get(region, region)}/{slug}")
