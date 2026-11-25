# NOAA / PO.DAAC ACSPO 红外 L3S SST（v2.81）

脚本：[download_acspo_l3s.py](../download_acspo_l3s.py)。通过 NASA CMR 查询每日 granule 名，并走 PO.DAAC OPeNDAP（DAP4）服务端区域裁剪下载 `L3S_LEO_DY-STAR-v2.81`（0.02°，保留云洞），单日传输由全球 ~408 MB 压缩至区域 ~1–2 MB。

## 默认配置

- Collection：`C2805339147-POCLOUD`（`L3S_LEO_DY-STAR-v2.81`，2000-02-24 至今）。
- 区域：默认黑潮区 `137 160 23 41`（西、东、南、北）；首次运行时自动向 OPeNDAP 读取一次 `lat/lon` 轴确定精确切片索引（兼容升序/降序纬度）。
- 默认变量：`sea_surface_temperature sses_bias sses_standard_deviation quality_level l2p_flags`。
- 输出：`<output>/<YYYY>/ACSPO_L3S_<YYYYMMDD>.nc4`。

## 认证与用法

与 `download_mur_sst.py` 共用 NASA Earthdata Bearer token（默认读取 `%USERPROFILE%\.edl_token` 或环境变量 `EARTHDATA_TOKEN`）。token 有效期约 60 天，过期需前往 [Earthdata Login](https://urs.earthdata.nasa.gov) 重新生成。

```powershell
# 预检查（仅查公开 CMR 目录，不校验 token）
python download_acspo_l3s.py 2024 -n

# 正式下载 2016-2025 黑潮区
python download_acspo_l3s.py 2016-2025 --bbox 137 160 23 41 -j 4 -o "E:\Data\Kuroshio_OceanVar3D\ir_l3s_acspo"
```

## 恢复与校验

下载写 `.part` 后用 `h5py` 检查文件完整可读再替换正式 `.nc4`；重跑自动跳过已通过校验的日期。
