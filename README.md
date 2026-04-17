# sk_automation

这是一个用于存放多个自动化流程的仓库。

当前已整理好的模块：

- `radar_pool_gen/`

后续可以继续在仓库根目录下增加其他自动化文件夹，例如：

- `xxx_automation/`
- `yyy_pipeline/`

## 当前结构

```text
sk_automation/
├─ README.md
├─ .gitignore
└─ radar_pool_gen/
   ├─ README.md
   ├─ .env.example
   ├─ scan_product_feeds.py
   ├─ clean_seed_products.py
   ├─ relay_gemini_scan_yesterday_entries.py
   ├─ kimi_build_radar_pools.py
   ├─ build_radar_pool_table.py
   └─ run_radar_pool_gen.py
```

## 使用方式

如果你要运行当前这个模块，进入对应目录：

```powershell
cd .\radar_pool_gen
python .\run_radar_pool_gen.py
```

每个模块可以有自己独立的：

- `README.md`
- `.env.example`
- `output/`
- 脚本和配置文件

这样后面继续扩展时，仓库结构会比较清晰。
