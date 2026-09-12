# ESA CCI 海表盐度

下载 CEDA 的 ESA CCI SSS v5.5、L4、GLOBALv5.5/7days。**七天滑动平均，每天采样一次，不是单日均值。** 保留源数值、单位、缺测和质量标识，不插值、不额外平均。

## 运行

在运行脚本的同一个 Python 环境安装依赖：

```powershell
python -m pip install "requests>=2.31" "numpy>=1.26" "xarray>=2024.10" "netCDF4>=1.6" "pydap>=3.5" "tqdm>=4.66"

# 先下载一天，检查出现 SAVED
python download_cci_sss.py --start-date 2011-01-01 --end-date 2011-01-01 --bbox 105 125 0 25 --out "F:\cci_sss"

# 下载指定覆盖；相同目录下完整文件会校验后跳过
python download_cci_sss.py --start-date 2011-01-01 --end-date 2023-12-30 --bbox 105 125 0 25 --out "F:\cci_sss"
```

2026-09-12 核对的 v5.5 目录，2023 年仅到 12 月 30 日。使用 `2011-2023` 会严格要求 2023-12-31，因缺日停止；不会悄悄跳过。日期首尾包含，`--years`/位置年份与日期参数二选一。无日期参数或 `--all-available` 从产品实际起点选到最新完整日历年，仍检查内部所有日期。

`-n/--dry-run` 只检查目录和计划，不下载数据。`--timeout 120` 设置每次网络请求超时秒数；`--retries 3` 为包括首次在内的总尝试次数。临时网络错误重试，缺依赖、数据校验和磁盘错误直接给出原因。

默认使用环境代理；`--proxy direct` 禁用显式代理，或 `--proxy http://127.0.0.1:10808` 指定代理。TUN 模式仍可能接管直连流量。

## 数据与保存

交互终端显示目录检查与逐日完成进度条（完成数、耗时、预计剩余时间）；重定向日志时自动关闭动态条。完成数包含校验后跳过的已有文件。

- `--bbox` 顺序为西、东、南、北，经度 -180..180，不跨日期变更线。按格点中心裁剪，不补造边界。
- 默认区域为经度 105.125..124.875（80 点）、纬度 0.125..24.875（100 点）。网格间距 0.25°，源有效分辨率约 50 km。
- 保存 `sss`、`sss_random_error`、`noutliers`、`total_nobs`、`pct_var`、`sss_qc`、`lsc_qc`、`isc_qc`。盐度及误差单位 `0.001` 保持原样，不乘除 1000；质量标识 0=Good、1=Bad。
- 部分整数变量没有显式 `_FillValue`，实际使用 NetCDF 默认缺测码（int16 为 -32767，int8 为 -127）。脚本将这些码解码为缺测并在输出明确记录；其他非法负数仍报错。不会把缺测质量标识变成 Good。
- 一天的八个变量合并为一个 DAP2 区域请求，另请求一次属性，不下载全球文件。日志显示日期进度、收到的字节、保存结果，不再逐条打印底层 URL。
- 每日保存到年份目录。先写 `.nc.part`，重开验证时间、坐标、变量、单位、版本与数据后改名；已有文件不匹配或损坏时停止，避免覆盖。
- 重跑校验并跳过完整日文件；未完成的一天从头下载。不要同时向同一目录运行多个实例。

源 `P7D` / `P1D` 时间语义及科学属性保留。个别文件的源 start/end 或 id 与产品描述不一致，脚本不擅自修正，另记录实际来源和请求。

2011-01-01 至 2023-12-30 共 4747 个日期，默认区域数值传输约 1 GiB，加上目录、坐标和属性开销。实际大小随编码变化；不需要缓存约 24 GiB 全球原件。

## 官方依据

- CEDA 数据版本目录：[https://data.cci.ceda.ac.uk/thredds/catalog/esacci/sea_surface_salinity/data/catalog.html](https://data.cci.ceda.ac.uk/thredds/catalog/esacci/sea_surface_salinity/data/catalog.html)
- v5.5 7days 年份目录：[https://data.cci.ceda.ac.uk/thredds/catalog/esacci/sea_surface_salinity/data/v05.5/GLOBALv5.5/7days/catalog.html](https://data.cci.ceda.ac.uk/thredds/catalog/esacci/sea_surface_salinity/data/v05.5/GLOBALv5.5/7days/catalog.html)
- 实际样本DDS/DAS元数据：[https://data.cci.ceda.ac.uk/thredds/dodsC/esacci/sea_surface_salinity/data/v05.5/GLOBALv5.5/7days/2023/ESACCI-SEASURFACESALINITY-L4-SSS-GLOBAL-MERGED_OI_7DAY_RUNNINGMEAN_DAILY_0.25deg-20230101-fv5.5.nc.das](https://data.cci.ceda.ac.uk/thredds/dodsC/esacci/sea_surface_salinity/data/v05.5/GLOBALv5.5/7days/2023/ESACCI-SEASURFACESALINITY-L4-SSS-GLOBAL-MERGED_OI_7DAY_RUNNINGMEAN_DAILY_0.25deg-20230101-fv5.5.nc.das)
- ESA发布说明：[https://climate.esa.int/en/news-events/Sea-Surface-Salinity-Record-Extended/](https://climate.esa.int/en/news-events/Sea-Surface-Salinity-Record-Extended/)
