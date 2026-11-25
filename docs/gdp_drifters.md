# NOAA GDP 15 m 逐小时漂流浮标（v2.01）

脚本：[download_gdp_drifters.py](../download_gdp_drifters.py)。从公开 NOAA AOML ERDDAP（`drifter_hourly_qc`）按月分块拉取区域内表层 15 m 漂流浮标轨迹与流速，自动根据 `drogue_lost_date` 计算布尔列 `drogue_status`，按年保存为轻量 `Parquet`。

## 默认配置

- 数据源：`https://erddap.aoml.noaa.gov/gdp/erddap/tabledap/drifter_hourly_qc.csvp`（免登录）。
- 区域：默认黑潮区 `137 160 23 41`（西、东、南、北）。
- 过滤：默认仅保留带水帆（`drogue_status == True`）样本；加 `--all-drifters` 可保留全部样本。
- 输出：`GDP_hourly_<YYYY>.parquet`。

## 用法

```powershell
# 预检查接口连通性与请求 URL
python download_gdp_drifters.py 2022,2024-2025 -n

# 下载 2022 与 2024-2025 黑潮区带水帆漂流浮标
python download_gdp_drifters.py 2022,2024-2025 --bbox 137 160 23 41 -o "E:\Data\Kuroshio_OceanVar3D\gdp_drifters"
```

## 恢复与校验

按年落盘，重跑自动校验并跳过已有年份；加 `--overwrite` 强制重下。
