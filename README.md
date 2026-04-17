# Radar Pool Gen

这是一个适合公开上传的 `radar_pool` 生成流程副本。

仓库中包含以下 5 个步骤的脚本：

1. 扫描产品 feed
2. 清洗候选产品
3. 使用 relay Gemini 扫描单个产品
4. 使用 Kimi 做初筛打分
5. 生成最终的 Markdown 雷达表

## 文件说明

- `scan_product_feeds.py`
- `clean_seed_products.py`
- `relay_gemini_scan_yesterday_entries.py`
- `kimi_build_radar_pools.py`
- `build_radar_pool_table.py`
- `run_radar_pool_gen.py`

## 运行要求

- Python 3.10+
- 可以访问 feed 和模型 API 的网络环境

当前脚本只依赖 Python 标准库。

## 环境变量

运行完整流程前，先设置这些环境变量：

```powershell
$env:RELAY_BASE_URL="https://your-relay.example.com/v1"
$env:RELAY_API_KEY="your-relay-api-key"
$env:KIMI_BASE_URL="https://api.moonshot.cn/v1"
$env:KIMI_API_KEY="your-kimi-api-key"
```

也可以参考 `.env.example` 自行配置本地环境。

## 运行方式

在当前目录执行：

```powershell
python .\run_radar_pool_gen.py
```

常见变体：

```powershell
python .\run_radar_pool_gen.py --overwrite-scan --overwrite-score
python .\run_radar_pool_gen.py --feed-retries 4 --feed-retry-delay 3
python .\run_radar_pool_gen.py --skip-feed-scan --skip-clean
```

## 输出目录

运行产物会写入 `output/` 下的时间戳目录，例如：

```text
output/output_YYYYMMDD-HHMMSS/
```

`output/` 已被 Git 忽略，不包含在这个公开版副本中。

## GitHub 上传说明

- 运行产物已通过 `.gitignore` 排除
- 本地环境文件已通过 `.gitignore` 排除
- 副本中的脚本不再包含硬编码 API Key

## 建议的 Git 初始化命令

```powershell
git init
git add .
git commit -m "Initial public-ready radar pool pipeline"
```
