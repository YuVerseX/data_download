# OSTIA REP 海表温度

脚本：[download_ostia_rep.py](../download_ostia_rep.py)。通过 Copernicus Marine Toolbox 按区域获取逐日 foundation SST，默认每月保存一个 NetCDF。

## 默认配置

- 产品：`SST_GLO_SST_L4_REP_OBSERVATIONS_010_011`。
- 数据集：`METOFFICE-GLO-SST-L4-REP-OBS-SST`。
- 起止日期必填，首尾均包含；脚本接受 1981-10-01 起日期，实际可用日期逐项核对远端时间坐标。
- 区域：西北太平洋 `100 180 0 60`，顺序为 **西、东、南、北**。
- 网格：0.05°；默认经度 100.025–179.975（1600 点）、纬度 0.025–59.975（1200 点）。
- 变量：`analysed_sst analysis_error`。
- 输出：当前工作目录中的 `OSTIA_REP_daily_<开始日>_<结束日>.nc`；按月分文件，数据仍为逐日。

## 认证与用法

按 [README](../README.md) 安装依赖，运行 `copernicusmarine login` 保存凭据。代理设置见 [网络与代理](../README.md#网络与代理)。

```powershell
# 单日预检查
python download_ostia_rep.py --start-date 2025-01-01 --end-date 2025-01-01 -n -o "F:\OSTIA_REP_NWP"

# 单日下载
python download_ostia_rep.py --start-date 2025-01-01 --end-date 2025-01-01 -o "F:\OSTIA_REP_NWP"

# 一年数据，默认逐月保存
python download_ostia_rep.py --start-date 2025-01-01 --end-date 2025-12-31 -o "F:\OSTIA_REP_NWP"

# 只获取 SST，自定义区域并按年保存
python download_ostia_rep.py --start-date 2001-01-01 --end-date 2001-12-31 --bbox 120 150 20 50 -v analysed_sst --chunk yearly -o "F:\OSTIA_REP_SST"
```

| 参数 | 作用 |
| --- | --- |
| `--bbox W E S N` | 经度 -180..180、纬度 -90..90，不跨日期变更线 |
| `-v/--variables` | 可选 `analysed_sst analysis_error sea_ice_fraction mask` |
| `--chunk monthly\|yearly` | 按月或年分文件，默认月；不改变时间分辨率 |
| `--dataset-version` | 固定目录版本 |
| `--compression 0..9` | 压缩级别，默认 1 |
| `--retries` | 每个文件总尝试次数，默认 3；不包含启动时的坐标查询 |
| `--overwrite`、`--stop-on-error`、`--no-progress` | 替换已有文件、失败后停止、关闭动态进度 |

区域按格点中心选择，不生成恰好位于请求边界的额外格点。脚本检查所有请求日期，缺日或超出覆盖会报错，不自动裁短。

## 预检查、空间与恢复

`-n` 联网读取远端坐标、变量和覆盖，估算待下载文件，不创建输出目录；已有文件仍会检查。正式任务按输出文件串行，没有 `-j`。服务端存储分块可能放大传输量，估算值以每次 `-n` 输出为准，不应按单个样本外推多年耗时。

下载前可用磁盘空间需达到已返回的文件大小估算的 1.2 倍。先写同目录 `.partial.nc`，检查逐日时间轴、经纬度、变量维度并分块读取全部数据，通过后写入请求及源信息，再替换正式文件。

重跑会校验并跳过完整匹配的文件。损坏、参数或记录的源信息不匹配时保留原文件并报告失败，显式 `--overwrite` 才替换。未完成文件重新请求，不支持字节续传；不要向同一输出目录并发运行多个实例。

## 数据含义与来源

- `analysed_sst`：foundation SST，单位 K；换算摄氏度用 `SST_C = SST_K - 273.15`。
- `analysis_error`：分析不确定性估计，单位 K，不是与真实温度的逐格点绝对误差。
- `sea_ice_fraction`：0–1 的海冰面积比例；`mask`：地表类别掩膜。
- foundation SST 旨在排除日间暖层影响，不等同于瞬时皮肤温度或普通日平均温度。
- 0.05° 是网格间距；L4 规则网格场不代表每个格点都有当天直接观测。

产品质量、版本历史及引用要求见官方资料：

- [产品页](https://data.marine.copernicus.eu/product/SST_GLO_SST_L4_REP_OBSERVATIONS_010_011/description)。
- DOI：[10.48670/moi-00168](https://doi.org/10.48670/moi-00168)。
- [用户手册](https://documentation.marine.copernicus.eu/PUM/CMEMS-SST-PUM-010-011.pdf)。
- [质量文档](https://documentation.marine.copernicus.eu/QUID/CMEMS-SST-QUID-010-011.pdf)。
- Worsfold et al. (2024), *Presenting a Long-Term, Reprocessed Dataset of Global Sea Surface Temperature Produced Using the OSTIA System*, DOI `10.3390/rs16183358`。
