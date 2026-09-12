# ERA5 海表热通量

使用 CDS `reanalysis-era5-single-levels`、`product_type=reanalysis` 的最终 ERA5，区域 0.25° 经纬度网格；不是 ERA5-Land、月均产品或逐预报步的原始 MARS 累计。默认区域 105–125°E、0–25°N。2026-09-12 查询官方 STAC：1940-01-01 至 2026-09-05，目录更新时间 2026-09-11；默认完整年 2001–2025。脚本每次查询目录，不硬编码最新年份；为避开 ERA5T 保守排除最近三个完整日历月及当月，有 `expver` 时还必须为 1。目录覆盖不保证每个单独请求均成功。

| NetCDF 变量 | CDS 请求名 | 原始单位 |
|---|---|---|
| sshf | surface_sensible_heat_flux | J m-2 |
| slhf | surface_latent_heat_flux | J m-2 |
| ssr | surface_net_solar_radiation | J m-2 |
| str | surface_net_thermal_radiation | J m-2 |

四变量均为向下为正；海洋向大气散失热量为负，不擅自翻转符号。CDS 此小时产品的累计窗口是时间戳之前一小时，例如 D 日 00 UTC 对应前一天 23–24 UTC。原始逐小时累计能量保留 J m-2，逐小时采样不意味着瞬时通量。

`--daily-mean` 另存本地日均 W m-2：对 UTC 日 D，取 D01、D02、…、D23、D+1 00 的 24 个小时累计值，求和后除以 86400 秒，不做差分、不机械除以 3600。脚本额外请求结束日次日全天，原始数据也保留；日坐标为 D00，代表 [D00,D+1 00) 平均。任一小时缺值使该像素当天结果为 NaN；缺失时间戳则报错。合法海陆缺测不视为损坏，不插值填补。可将通量作为热收支约束的辅助量，不能直接当作温盐标签或独立实测。

## 认证与依赖

需要 `requests`、`numpy`、`xarray`、`netCDF4` 和 `cdsapi>=0.7.7`。先在 CDS 注册/登录并在数据集页面接受使用条款，再按官方 API 页面将个人配置写到用户目录 `.cdsapirc`（Windows `%USERPROFILE%\.cdsapirc`），也支持官方 `CDSAPI_URL` / `CDSAPI_KEY`。不要把配置、密钥加入仓库或粘贴到对话。

2026-09-12 检查的 `D:/DevEnvs/miniconda3/python.exe` 缺少 cdsapi，用户目录没有 `.cdsapirc`，也没有 CDS 环境变量；因此真实 CDS 下载和真实日均读取尚未验证。此次没有安装或升级环境，没有样本 NetCDF 下载到项目。公共目录查询和离线合成测试不等于真实下载成功。

## 命令

`--bbox` 顺序统一为 **西 东 南 北**，边界必须对齐 0.25°；内部转换为 CDS 的北西南东。日期范围首尾均包含，年份与日期参数二选一。

默认请求实际为 `area=[25,105,0,125]`，包括边界，经度81点、纬度101点。纬度递增或递减均接受，变量与坐标同步保留；拒绝乱序坐标和错误的坐标单位。西经区域若以0–360°返回，会连同变量转为请求的负经度顺序。此下载器没有海陆掩膜和重网格，和其他产品联合训练时仍需按坐标对齐；不要把数组下标直接视为相同地理位置。

默认选择完整年时，日均模式先为结束日之后的零点预留一天覆盖：如果可用数据只到某年12月31日，该年仍不能生成完整年日均，默认回退至上一完整年。显式指定的不可完成年份直接报错。

日均移除原累计能量的 `standard_name` 和 `GRIB_*` 属性，更新名称、单位和时间聚合说明；原变量属性以 `source_variable_attributes` JSON 保留，避免自动读CF/GRIB元数据时把通量误认为累计能量。小时文件记录 `downloaded_utc`，日均文件记录 `processed_utc`。日均请求签名处理版本为2；旧版本日均需换目录或移开后重算，小时文件仍可复用。

```powershell
# 默认从2001年至运行时核实的最新完整最终ERA5年；只查询公共目录
python download_era5_flux.py --daily-mean -n
# 认证完成后最小区域、一天日均验证（原始两天约3 KiB浮点payload，实际文件另含元数据）
python download_era5_flux.py --start-date 2020-02-29 --end-date 2020-02-29 --bbox 110 110.25 10 10.25 --daily-mean -o "E:\data_samples\era5_flux"
# 重跑同一命令检验有效文件跳过
python download_era5_flux.py --start-date 2020-02-29 --end-date 2020-02-29 --bbox 110 110.25 10 10.25 --daily-mean -o "E:\data_samples\era5_flux"
# 正式下载由用户自行执行，先替换路径占位符
python download_era5_flux.py --daily-mean -o "<正式数据目录>\ERA5_FLUX"
```

## 分块、校验与空间

请求最多一个日历月，临界日期独立分块。依次前台提交并等待 CDS，记录分块和尝试次数；`--retries` 为最大总尝试次数，默认 3、至少 1。网络异常或刚下载的文件校验不通过会重试当前块，已有正式文件的请求冲突不重试。没有后台进程或计划任务。无跨进程 CDS 任务ID恢复或字节续传保证，失败后重提当前分块；此前有效块通过内容和请求签名检查后跳过。已有正式文件请求不匹配或损坏会报错，需用户换目录或自行移开，不覆盖。

下载到 `.download.nc`，读取并校验四变量、能量单位、精确时间覆盖、0.25°区域坐标、维度与完整payload，然后写 `.writing.nc`，重新打开校验后同目录重命名。日均还校验 UTC `time_bounds`、处理公式、W m-2 单位及向下为正属性；坏日界文件不会被误判为完成。保留原科学属性并增加请求签名、来源、下载UTC时间、最终ERA5策略及处理依据。残留临时文件下次可以重新写入；不要同时向同一输出目录运行多个实例。未知异常只显示类型避免暴露认证信息，Ctrl+C 返回 130 并保留完成块。

默认区域 81×101 网格，按四变量 float32 计算：原始小时 payload 约 1.07 GiB/365天，31天约 92.9 MiB；日均另约 45.6 MiB/365天。2001–2025 原始约26.7 GiB、日均约1.11 GiB。CDS 服务端区域裁剪，网络不用拉全球文件，但实际传输/落盘受上游编码、压缩和属性影响，未用真实文件校准；float64可能翻倍。下载与重写会同时存在两个临时块，建议额外至少 0.4 GiB 磁盘及约1 GiB可用内存；日均按月读取相邻块。全球区域另需自行评估，当前估计只适用于默认区域。

历史离线测试覆盖闰日UTC边界、相邻零点缺失、NaN传播、单位/网格/ERA5T拒绝、原子提交、请求冲突、失败重试及重跑跳过。测试脚本已按用户要求删除。正式下载尚未启动。

## 官方依据

- [CDS 数据集](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels)
- [CDS STAC 实时覆盖](https://cds.climate.copernicus.eu/api/catalogue/v1/collections/reanalysis-era5-single-levels)
- [ECMWF 累计量转换表](https://confluence.ecmwf.int/plugins/viewsource/viewpagesrc.action?pageId=462888259)
- [ERA5 数据说明及 ERA5T](https://confluence.ecmwf.int/pages/viewpage.action?pageId=488277769)
- [CDS API 配置](https://cds.climate.copernicus.eu/how-to-api)
