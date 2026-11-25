# CMEMS 沿轨 1 Hz L3 SLA（008_062）

脚本：[download_cmems_sla_l3.py](../download_cmems_sla_l3.py)。通过 Copernicus Marine Toolbox 按卫星与年份拉取 `SEALEVEL_GLO_PHY_L3_MY_008_062` 沿轨 1 Hz 海面高度异常，下载后即时按 `--bbox` 裁剪，合并为每年每星一个轻量 `Parquet` 文件并自动清理全球临时文件。

## 默认配置

- 产品：`SEALEVEL_GLO_PHY_L3_MY_008_062`（`cmems_obs-sl_glo_phy-ssh_my_<mission>-l3-duacs_PT1S`）。
- 卫星：默认覆盖 `2016–2025` 全部活跃任务（`alg c2 c2n h2a h2ag h2b j2 j2g j2n j3 j3n s3a s3b s6a-lr swon`），包含可留出作独立检验的 `c2n`/`alg`。
- 区域：默认黑潮区 `137 160 23 41`（西、东、南、北）。
- 输出：`SLA_L3_<mission>_<YYYY>.parquet`（zstd 压缩）。

## 用法

需先运行 `copernicusmarine login` 保存凭据。

```powershell
# 预检查 2024 年单星计划（不下载）
python download_cmems_sla_l3.py 2024 --missions c2n s3a -n

# 下载 2016-2025 黑潮区全部沿轨 L3 SLA
python download_cmems_sla_l3.py 2016-2025 --bbox 137 160 23 41 -o "E:\Data\Kuroshio_OceanVar3D\sla_l3_alongtrack"
```

## 恢复与校验

已存在的 `Parquet` 重跑时验证核心列可读后自动跳过；加 `--overwrite` 可重下。远端某星在某年无轨时记为 `empty` 不报错。
