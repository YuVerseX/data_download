# ERA5 海表热通量

交互终端显示下载分块及日均生成进度条；CDS 排队时显示真实任务状态，无法预估排队百分比。重定向日志时自动关闭动态条。

从 CDS `reanalysis-era5-single-levels` 下载最终 ERA5 的四个逐小时累计能量变量，服务端裁剪至指定区域。默认区域为 105–125°E、0–25°N，输出 0.25° 网格。

| NetCDF 变量 | CDS 请求名 | 小时文件单位 |
|---|---|---|
| sshf | surface_sensible_heat_flux | J m-2 |
| slhf | surface_latent_heat_flux | J m-2 |
| ssr | surface_net_solar_radiation | J m-2 |
| str | surface_net_thermal_radiation | J m-2 |

四变量均向下为正，海洋向大气散失热量为负。此 CDS 小时产品的累计窗口为时间戳之前一小时；小时累计能量不是瞬时通量。

`--daily-mean` 另外保存 UTC 日均通量，单位 W m-2。对日 D 使用 D01、D02、…、D23、D+1 00 的 24 个小时累计值，求和除以 86400 秒，不做相邻时刻差分。结束日次日全天也会请求并保留在小时文件中。日坐标为 D00，并记录 `[D00,D+1 00)` 的时间边界；任一小时像素缺值会传播为日均 NaN，缺少时间戳则报错。

## 准备与运行

在运行脚本的同一个终端安装依赖：

```powershell
python -m pip install -r requirements.txt
```

需要 `requests`、`numpy`、`xarray`、`netCDF4`、`tqdm` 和 `cdsapi>=0.7.7`。脚本在查询目录前检查运行所需依赖；`--dry-run` 只需要 `requests` 和 `tqdm`。

在 CDS 注册并在数据集页面接受使用条款，按官方 API 配置页将个人配置写入 `%USERPROFILE%\.cdsapirc`；也支持 `CDSAPI_URL` / `CDSAPI_KEY`。不要把密钥、认证配置加入仓库或粘贴到日志。

```powershell
# 只查公开目录、显示计划，不提交 CDS 任务
python download_era5_flux.py 2001-2025 --daily-mean --dry-run

# 先验证一天的小区域下载、日均处理与落盘
python download_era5_flux.py --start-date 2020-02-29 --end-date 2020-02-29 --bbox 110 110.25 10 10.25 --daily-mean -o "F:\ERA5_FLUX_sample"

# 完整区域；重复同一命令会校验并跳过已完成文件
python download_era5_flux.py 2001-2025 --bbox 105 125 0 25 --daily-mean -o "F:\ERA5_FLUX"
```

日期首尾均包含，年份与日期参数二选一。省略年份时查询目录并选择从 2001 年起可用的完整年。为避开 ERA5T，保守排除当月及最近三个完整日历月；文件有 `expver` 时只接受 1。日均必须额外覆盖结束日次日，目录覆盖不代表每项请求一定成功。

`--bbox` 顺序为 **西 东 南 北**，边界必须对齐 0.25°，不支持跨日期变更线区域。默认网格包括边界，共 81×101 点。接受递增或递减纬度；需要时将西经区域从 0–360° 转换为负经度并同步重排变量。脚本不做海陆掩膜、插值或重网格。

## 进度、失败与恢复

每个 CDS 请求最多一个日历月。日志显示请求日期、尝试次数、CDS 任务状态以及最终 `Saved` 路径。CDS 排队和计算可能较久；`--timeout 60` 控制单次 HTTP 请求超时，不限制任务排队时长。

`--retries 3` 表示脚本最多尝试三次当前目录请求或分块任务。只重试可识别的暂时网络错误及 HTTP 408/425/429/500/502/503/504；HTTP 401/403、缺依赖、磁盘错误或内容校验失败立即停止，避免无意义地重提任务。日志给出安全的原因分类，不输出可能带密钥的原始异常文本。CDS 客户端也可能有自身轮询或下载行为。

`Interrupted` 表示 Python 收到中断（常见于 Ctrl+C 或终端停止操作），本身不是网络错误。重跑同一命令会检查完成文件的请求签名、时间轴、区域坐标、变量、单位和数值；通过后跳过。任务 ID 不跨进程恢复，不承诺字节续传，未完成块可能重新提交 CDS。

下载先写 `.download.nc`，校验后写 `.writing.nc`，重新打开验证后重命名为正式文件。残留临时文件下次可重写；已有正式文件损坏或请求不匹配会停止，需换目录或自行移开，脚本不覆盖。不要同时向同一输出目录运行多个实例。

日均文件还验证时间边界、处理公式与向下为正属性。原累计能量的 `standard_name` 和 `GRIB_*` 不再用于日均；原属性以 `source_variable_attributes` JSON 保留。日均处理签名版本为 2，小时为 1；旧版日均需换目录重算，兼容的小时文件可以复用。

默认区域按四变量 float32 估算：小时数据约 1.07 GiB/365 天，31 天约 92.9 MiB；日均另约 45.6 MiB/365 天。实际传输及磁盘占用受压缩、编码、属性影响。下载、校验和重写会同时占用临时空间；日均按月加载相邻小时块。更大区域需另行评估空间和内存。

## 官方依据

- [CDS 数据集](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels)
- [CDS STAC 覆盖](https://cds.climate.copernicus.eu/api/catalogue/v1/collections/reanalysis-era5-single-levels)
- [ECMWF 累计量转换](https://confluence.ecmwf.int/plugins/viewsource/viewpagesrc.action?pageId=462888259)
- [ERA5 数据说明及 ERA5T](https://confluence.ecmwf.int/pages/viewpage.action?pageId=488277769)
- [CDS API 配置](https://cds.climate.copernicus.eu/how-to-api)
