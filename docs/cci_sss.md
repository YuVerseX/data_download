# ESA CCI 海表盐度

2026-09-12 复查：本次验证样本及测试脚本已按用户要求删除，下文路径、体积和测试结果仅为历史验证记录。默认区域实际格点中心为经度105.125..124.875（80点）、纬度0.125..24.875（100点），全部位于请求框内。默认计划在选定最新完整年后严格检查整个区间，区间内部任何缺日/缺年都会报错，不能静默跳过；质量标识和观测数还校验无量纲、整数及产品有效范围。

`download_cci_sss.py` 下载官方 CEDA 发布的 ESA CCI SSS **v5.5、L4、GLOBALv5.5/7days**。2026-09-12 实查目录最高发布版本为 v05.5。目录年份为 2010–2023，但逐日文件实际从 **2010-01-09 至 2023-12-30**，2023 缺 12 月 31 日，因此此产品的最新完整日历年为 **2022**。程序每次执行重新读取目录并核查逐日文件，不把目录年份直接当完整年，也不补造文件。

默认无日期参数与 `--all-available` 均选择产品起始日至服务端最新完整年；本次核查为 2010-01-09 至 2022-12-31。明确请求 2010 全年或 2023 全年会因缺日期报错。若科研需要 2023 已有部分，可另指定截止 2023-12-30。

## 变量与科学语义

- `sss`：盐度；`sss_random_error`：随机误差，源单位均为 `0.001`，保留数值与原单位，不再乘除 1000。
- `noutliers`：格点内异常观测数；`total_nobs`：时间窗口内 SSS 观测数。
- `pct_var`：产品未解释的 SSS 变率百分比，单位 `%`。
- `sss_qc`、`lsc_qc`、`isc_qc`：盐度质量、陆地污染、海冰污染标识；实际属性规定 0=Good、1=Bad。

八个变量全部保存。海陆缺测 NaN 保留，不当作坏文件；不自动删除质量不良点，便于研究时按用途建立掩膜。有效观测支持数不能解释成每日独立观测数。

源全球规则网格 720×1440，格点中心间隔 0.25°，经度 -179.875 至 179.875、纬度 -89.875 至 89.875。源属性标明有效空间分辨率 **50 km**；0.25°重采样不提高实际分辨率。产品是 **7 日滑动平均、每日一个采样**，不是每日独立观测。本地不额外平均、插值或伪造逐日观测。多卫星来源 SMOS、Aquarius、SMAP，未新增其他盐度数据源。

2023-01-01 样本 time=2023-01-01 00:00:00；源 `time_coverage_duration=P7D`、`time_coverage_resolution=P1D`。源 start/end 却写 `20221228T000000Z` 和 `20230104T235959Z`，与 P7D 的精确边界存在不一致；本脚本原样保留，不能据此自行推算或修正精确权重。源 `id` 后缀写 fv5.3，而下载目录、文件名及 `product_version` 均为 5.5；保留该源属性并用独立溯源属性记录真实URL和版本。

## 使用

依赖 requests、numpy、xarray、netCDF4、pydap。CEDA 此目录为公开访问，本次不需要账号。pydap 使用 requests 网络配置，解决本地 netCDF4/libcurl 直连 OPeNDAP I/O 失败。不会自动回退全球下载。

```powershell
# 只查询小型 XML 目录，不下载场数据；输出当前完整年覆盖与估算
python download_cci_sss.py --all-available --dry-run
# 小样本；bbox 始终 WEST EAST SOUTH NORTH
python download_cci_sss.py --start-date 2023-01-01 --end-date 2023-01-01 --bbox 110 111 10 11 --out data/validation/cci_sss
# 正式命令由用户自行执行；执行时重新检查完整年覆盖
python download_cci_sss.py --all-available --bbox 105 125 0 25 --out "<正式输出目录>/cci_sss"
# 或选择完整历史年份
python download_cci_sss.py 2011-2022 --bbox 105 125 0 25 --out "<正式输出目录>/cci_sss"
```

支持 `--years 2011-2022`、`-n/--dry-run`、`-o/--output-dir/--out`、`--retries`（含首次在内总尝试次数，默认 3）、`--timeout`（每次网络请求秒数）。不跨经度180°，经度范围 -180..180。

OPeNDAP 服务端按区域切片，只传八个变量的区域数组、坐标和元数据，不拉全球原文件。每个日期单独写 `.nc.part`，完整校验变量、单位、坐标、精确时间、时间窗口、版本及全部数据可读性后同目录原子改名。有效文件重跑跳过；既有请求或 bbox 不同会拒绝覆盖，请换输出目录。中断后单日重试，不支持字节续传；每次只处理一个临时文件。源科学属性、下载UTC时间、来源URL、请求和处理方式保存在输出属性。

## 验证与成本

2026-09-12 已通过：小样本 dry-run、2023-01-01 的110–111°E/10–11°N服务端裁剪真实下载、八变量读取、重跑 SKIP；4×4格点盐度均值约32.64625。文件为 `data/validation/cci_sss/2023/ESACCI-SEASURFACESALINITY-L4-SSS-GLOBAL-MERGED_OI_7DAY_RUNNINGMEAN_DAILY_0.25deg-20230101-fv5.5.nc`，**36,196 bytes**。另有离线恢复、原子提交、请求冲突、无穷值/单位/时间错误及海陆NaN测试。

默认区域100×80=8,000格点；网络 DAP2 数组约224,000 bytes/日（整数填充使线传近似28 bytes/格点），还需坐标、XML、DDS/DAS和HTTP开销。2010-01-09至2022-12-31共4,740文件，区域数值传输约1.0 GiB，实际总网络量更高；落盘约0.8–1.2 GiB为估算，未做全量实测。每文件开销在小样本占比很高，不能用4×4文件大小按面积直接外推。单日临时区域文件约0.2 MB，加内存中的区域数据；不需要缓存数十GiB全球文件。dry-run 同时列出原全球压缩文件目录体积作对照，目录大小有舍入误差。

本次未启动正式或后台下载。

## 官方依据

- CEDA 数据版本目录：<https://data.cci.ceda.ac.uk/thredds/catalog/esacci/sea_surface_salinity/data/catalog.html>
- v5.5 7days 年份目录：<https://data.cci.ceda.ac.uk/thredds/catalog/esacci/sea_surface_salinity/data/v05.5/GLOBALv5.5/7days/catalog.html>
- 实际样本DDS/DAS元数据：<https://data.cci.ceda.ac.uk/thredds/dodsC/esacci/sea_surface_salinity/data/v05.5/GLOBALv5.5/7days/2023/ESACCI-SEASURFACESALINITY-L4-SSS-GLOBAL-MERGED_OI_7DAY_RUNNINGMEAN_DAILY_0.25deg-20230101-fv5.5.nc.das>
- ESA发布说明：<https://climate.esa.int/en/news-events/Sea-Surface-Salinity-Record-Extended/>
