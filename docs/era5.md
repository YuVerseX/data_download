# ERA5 单层数据

使用 `download_era5.py` 按月下载西北太平洋 `100–180°E、0–60°N` 的 ERA5 数据。先按 [CDS API 指南](https://cds.climate.copernicus.eu/how-to-api) 配置 `.cdsapirc` 并接受所用数据集的条款，再安装 [项目依赖](../README.md#安装与运行)。

```powershell
# 查看闰日期间的小区域下载计划
python download_era5.py hourly --start-date 2020-02-28 --end-date 2020-03-01 --bbox 110 110.25 10 10.25 -v u10 v10 msl -n

# 下载西北太平洋小时风和海平面气压，按月保存
python download_era5.py hourly 2023-2025 -v u10 v10 msl -o "E:\Data\NorthwestPacific\ERA5_hourly"

# 下载默认变量的 CDS 官方逐日平均
python download_era5.py cds-daily 2020 -o "E:\Data\NorthwestPacific\ERA5_daily"
```

| 子命令 | 下载内容 | 输出 |
| --- | --- | --- |
| `hourly` | 默认 `u10 v10 msl sshf slhf ssr str`。用 `-v` 自选这些变量及东西、南北向累计风应力 `ewss nsss` | 每个请求块一个 `era5_hourly_开始日_结束日.nc`，默认 `data/era5_hourly` |
| `cds-daily` | 官方日平均，默认 12 个变量。`--frequency` 控制 CDS 用 1、3 或 6 小时样本计算日平均，`--time-zone` 控制日界线 | 一变量保存 `.nc`，多变量保存 `.zip`，附请求 `.json`，默认 `data/era5_daily_forcing` |

日期首尾含当天，支持 `2020`、`2020-2022`、`2020,2022` 或显式起止日期。`--bbox` 按西、东、南、北输入，边界对齐 0.25°。`-n` 查询公开目录并查看计划和大致体积。正式运行时，同一请求已下载的月份校验后跳过。更换区域或变量时，建议使用新目录。`--overwrite` 可替换现有结果。脚本保守排除当月和最近三个完整月，避免混入 ERA5T。小时模式同时检查源文件中可用的 `expver`。

`hourly` 保留原始时间含义和单位。`u10`、`v10` 是逐小时风分量，`msl` 是海平面气压。四项热量是上一小时的累计能量（J/m²），累计风应力单位是 N·s/m²。计算 UTC 日均通量时，应使用当天 01 时至次日 00 时的 24 个累计值，除以 86400 秒。计算日最大风速和日均风速立方时，先求每小时的风速大小，再做日统计。 [ECMWF ERA5 数据说明](https://confluence.ecmwf.int/spaces/CKB/pages/76414402/ERA5+data+documentation)

旧命令 [小时热通量](era5_flux.md) 和 [官方日均强迫](era5_forcing.md) 继续支持原参数与文件命名。原小时热通量命令仍可通过 `--daily-mean` 生成 UTC 日均通量。
