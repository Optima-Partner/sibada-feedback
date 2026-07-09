# 抖音口播内容 → sibada-medical 知识库入库管线（issue #10）

把 `data/douyin/` 里医生抖音口播文字稿，结构化增量入甲方生产知识库 `sibada-medical`
的**「口播内容层」**（第三层，与法规层/咨询层并列）。

## 一条命令

```bash
bash scripts/ingest-kb/ingest.sh          # 正式入库
bash scripts/ingest-kb/ingest.sh --dry-run # 只生成+校验+看 diff，不提交
```

它会：clone 甲方 KB bare repo → 生成口播内容层 → 死链校验 → commit → push →
拉平所有 per-user clone + 修属主。**幂等**（同数据重跑 = nothing to ingest），
**增量**（新账号转写好放进 `data/douyin/` 直接重跑，只加新账号、老的不动）。

## 关键约束：只入「已转写」的账号

管线只处理有 `transcripts-corrected.json` 的账号；**仅有元数据（profile+videos，未转写）
的账号会被自动跳过并列出**。所以入库前，新账号必须先走完上游流水线：

```
scout douyin 抓取 → 下载视频 → gen asr 转写 → 生殖医学术语校对 → transcripts-corrected.json
```

例：南京 5 位医生目前只有元数据，跑 ingest 会跳过它们；等它们转写校对完成，重跑即自动纳入。

## 产物结构（生成到 KB）

- `raw_md/口播文案/<地区>/<拼音>.md` — source of truth，每位医生全部口播文字稿**一字不改**，
  每条带 `video_id` + 发布日期 + 抖音描述 + 标签 + 互动数 + 来源链
- `wiki/content/README.md` — 层总览 + 跨医生选题聚合 + 医生索引
- `wiki/content/doctors/<拼音>.md` — 每位医生选题/互动导航页
- `index.md` — 口播层入口 + 状态（脚本在 `<!-- douyin:*:begin/end -->` marker 之间维护）

## 忠实与边界（写进 AGENTS.md 硬规则 #9）

- **口播文字稿一字不改照录**（纯机械结构化，零 LLM 归纳/总结医学结论）
- 40 条 ASR 幻觉（BGM/字幕）+ 图文帖已标 `⚠`，引用时排除
- 该层**非临床/法规事实依据**，只回答「某医生讲过什么」，不进法规/临床查询链路、
  与前两层不交叉引用（防污染医学库可信度）

## 文件

- `gen_content_layer.py` — 生成器（数据驱动：账号从 `data/douyin/` 发现，中文名从
  `crawl-summary*.json` 读；可独立跑 `--data <dir> --kb <clone>` 只生成不提交）
- `ingest.sh` — 一条命令驱动（含 clone/push/拉平；可配 `PROD_SSH`/`BARE`/`DATA_DIR` 等 env）

## 配置（env override）

| env | 默认 | 说明 |
|---|---|---|
| `PROD_SSH` | `sibada-prod` | 甲方 ECS 的 ssh alias |
| `BARE` | `/mnt/nas/sibada/_shared/kb/sibada-kb.git` | 甲方 NAS KB canonical bare repo |
| `DATA_DIR` | `<repo>/data/douyin` | 数据源 |

> 注：甲方 KB 与 GitHub 源 repo `sibada-assisted-reproduction-cn` 已分叉，本管线只写甲方
> 生产 NAS，不碰 GitHub 源与 stage。
