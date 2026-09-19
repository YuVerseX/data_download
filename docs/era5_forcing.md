# ERA5 逐日大气强迫下载

`download_era5_forcing.py` 从 CDS 的官方
`derived-era5-single-levels-daily-statistics` 数据集下载逐日平均 ERA5 单层变量。
默认范围为西北太平洋 `100–180°E, 0–60°N`，空间分辨率为 ERA5 原生 `0.25°`。

## 默认变量

| CDS 变量 | 含义 | 单位 |
|---|---|---|
| `10m_u_component_of_wind` | 10 m 纬向风 | m s-1 |
| `10m_v_component_of_wind` | 10 m 经向风 | m s-1 |
| `2m_temperature` | 2 m 气温 | K |
| `2m_dewpoint_temperature` | 2 m 露点温度 | K |
| `mean_sea_level_pressure` | 海平面气压 | Pa |
| `mean_total_precipitation_rate` | 总降水率 | kg m-2 s-1 |
| `mean_evaporation_rate` | 蒸发率 | kg m-2 s-1 |
| `mean_surface_sensible_heat_flux` | 感热通量 | W m-2 |
| `mean_surface_latent_heat_flux` | 潜热通量 | W m-2 |
| `mean_surface_net_short_wave_radiation_flux` | 地表净短波辐射 | W m-2 |
| `mean_surface_net_long_wave_radiation_flux` | 地表净长波辐射 | W m-2 |
| `sea_ice_cover` | 海冰覆盖率 | 0–1 |

露点温度和气压可用于估算比湿，但由日平均露点和日平均气压得到的比湿不等于逐小时
计算比湿后再取日平均。若训练目标要求严格的日平均比湿，应下载逐小时露点和气压，先
计算逐小时比湿再聚合。脚本选用 ERA5 的平均率变量，而不是累计量，避免把 `J m-2`
或 `m` 的逐小时累计值直接平均后误当作通量或降水率。

## 准备

1. 注册并登录 [Copernicus Climate Data Store](https://cds.climate.copernicus.eu/)。
2. 在数据集页面接受许可条款。
3. 按 CDS API 页面配置用户目录下的 `.cdsapirc`。
4. 安装依赖：`pip install -r requirements.txt`。

## 命令

先检查 1993–2024 年的请求，不提交下载任务：

```powershell
python .\download_era5_forcing.py 1993-2024 -n
```

正式下载：

```powershell
python .\download_era5_forcing.py 1993-2024 -o E:\data\ERA5_daily_forcing
```

上述默认范围和 12 个变量在压缩前约为 `40.4 GiB` 的 float32 数据；实际 ZIP 大小取决于
各变量的 NetCDF 压缩率。脚本会拆成 384 个逐月请求，单月失败时不影响已经完成的月份。

指定精确日期：

```powershell
python .\download_era5_forcing.py `
  --start-date 1993-01-01 --end-date 2024-12-31 `
  -o E:\data\ERA5_daily_forcing
```

默认按 UTC 日界线，对 ERA5 的 24 个逐小时样本计算日平均。若训练标签按北京时间定义，
可加 `--time-zone utc+08:00`。改变日界线后会改变日平均对应的日期窗口，不应与 UTC
日产品混用。

本任务使用的 1993–2024 年已经是最终 ERA5。目录中靠近当前日期的数据可能来自 ERA5T，
之后会被最终 ERA5 替换。为保证长期可复现，脚本会保守排除当前月和最近三个完整月份，
不会把“目录已发布”直接理解为“数据已经冻结”；侧车文件同时记录精确请求。

## 输出与续传

多变量请求每个月输出一个 ZIP，例如：

```text
ERA5_daily_forcing_1993-01-01_1993-01-31.zip
ERA5_daily_forcing_1993-01-01_1993-01-31.zip.json
```

只选择一个变量时 CDS 返回裸 NetCDF，脚本相应保存为 `.nc` 和 `.nc.json`，而不是给
NetCDF 文件使用错误的 `.zip` 扩展名。

CDS 的该数据集会把多变量请求的 NetCDF 打包为 ZIP，但不承诺每个变量各占一个文件。脚本保留官方
原始结构，并在完成后检查：

- ZIP 是否完整且至少包含一个 NetCDF；
- 所有 NetCDF 是否包含请求变量对应的预期短名；
- 每个 NetCDF 是否包含可解码的 `time` 或 `valid_time`；
- 日期序列是否与请求覆盖的每一天完全一致；
- 经纬度坐标是否完整覆盖请求的 0.25° 网格。

`.json` 侧车文件记录精确请求。再次运行相同命令会校验并跳过完整文件。已有文件损坏、
侧车缺失或请求参数改变时，脚本默认保留现场并报错；确认可以替换后使用 `--overwrite`
重新下载，或改用另一个输出目录。

## 符号约定

ERA5 地表湍流热通量采用向下为正的约定。海洋模型常用向海洋增热为正时可直接沿用，
但具体训练代码仍应根据目标数据的约定统一符号。ERA5 蒸发通常表现为负值，表示水从
地表进入大气；计算海洋淡水通量 `P-E` 前必须先统一降水和蒸发的正方向。

官方资料：

- [ERA5 daily statistics dataset](https://cds.climate.copernicus.eu/datasets/derived-era5-single-levels-daily-statistics)
- [ERA5 parameter listings](https://confluence.ecmwf.int/pages/viewpage.action?pageId=239349050)
