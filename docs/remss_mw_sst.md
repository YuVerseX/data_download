# REMSS 微波 L3U SST（AMSR2 v8.2 & GMI v8.2a，PO.DAAC 镜像）

脚本：[download_remss_mw_sst.py](../download_remss_mw_sst.py)。从 NASA PO.DAAC GHRSST 镜像下载透云微波 L3U SST（`AMSR2-REMSS-L3U-v8.2` 与 `GMI-REMSS-L3U-v8.2a`，0.25°），因单日全球文件仅 ~4–5 MB，脚本下载后在本地即时裁剪至 `--bbox` 并保存轻量压缩 NetCDF4（~0.15 MB/日）。

## 默认配置

- Collections：
  - `amsr2`：`C2600786104-POCLOUD`（`AMSR2-REMSS-L3U-v8.2`，2012-07 起）；
  - `gmi`：`C2036877762-POCLOUD`（`GMI-REMSS-L3U-v8.2a`，2014-03 起）。
- 区域：默认黑潮区外扩 2° `136 161 22 42`（西、东、南、北，给微波足迹卷积留足边界）。
- 输出：`<output>/<sensor>/<YYYY>/<SENSOR>_L3U_<YYYYMMDD>.nc`。

## 认证与用法

复用与 `download_mur_sst.py` 相同的 NASA Earthdata Bearer token（`%USERPROFILE%\.edl_token`），无需单独注册 REMSS 账号。

```powershell
# 预检查（仅查公开 CMR 目录，不校验 token）
python download_remss_mw_sst.py 2024 -n

# 正式下载 2016-2025 AMSR2 与 GMI
python download_remss_mw_sst.py 2016-2025 --bbox 136 161 22 42 -j 4 -o "E:\Data\Kuroshio_OceanVar3D\mw_l3u_remss"
```

## 恢复与校验

裁剪后的文件校验含 `sea_surface_temperature` 变量并原子落盘，重跑自动跳过已完成日期。
