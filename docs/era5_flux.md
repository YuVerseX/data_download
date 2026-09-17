# ERA5 海表热通量

脚本：[download_era5_flux.py](../download_era5_flux.py)。从 CDS `reanalysis-era5-single-levels` 获取四个逐小时累计能量变量，可额外生成 UTC 日均通量。

## 默认配置

- 日期：省略时从 2001 年选至目录覆盖允许的最新完整最终 ERA5 年；也支持年份或起止日期。
- 区域：南海 `105 125 0 25`，顺序为 **西、东、南、北**。
- 网格：0.25°，请求边界需对齐网格；默认含边界，共 81 × 101 点。
- 输出目录：`era5_flux`；小时文件 `era5_flux_hourly_<开始日>_<结束日>.nc`，可选日均文件 `era5_flux_daily_<开始日>_<结束日>.nc`。

| 变量 | CDS 请求名 | 小时文件单位 |
| --- | --- | --- |
| `sshf` | `surface_sensible_heat_flux` | J m-2 |
| `slhf` | `surface_latent_heat_flux` | J m-2 |
| `ssr` | `surface_net_solar_radiation` | J m-2 |
| `str` | `surface_net_thermal_radiation` | J m-2 |

## 认证与用法

按 [README](../README.md) 安装依赖。正式下载需要 `requests`、`numpy`、`xarray`、`netCDF4`、`tqdm`、`cdsapi`；`-n` 只需要 `requests` 和 `tqdm`。

在 CDS 注册并接受数据集使用条款，按官方 API 配置说明设置 `%USERPROFILE%\.cdsapirc`，也可使用 `CDSAPI_URL` / `CDSAPI_KEY`。不要把密钥写入仓库或日志；代理设置见 [网络与代理](../README.md#网络与代理)。

```powershell
# 只查公开目录和计划，不提交 CDS 任务
python download_era5_flux.py 2020 --daily-mean -n

# 单日小区域试下载
python download_era5_flux.py --start-date 2020-02-29 --end-date 2020-02-29 --bbox 110 110.25 10 10.25 --daily-mean -o "F:\ERA5_FLUX_sample"

# 默认区域，指定年份
python download_era5_flux.py 2001-2020 --daily-mean -o "F:\ERA5_FLUX"
```

| 参数 | 作用 |
| --- | --- |
| 年份或 `--start-date`、`--end-date` | 二选一；日期首尾均包含 |
| `--bbox W E S N` | 经度 -180..180、纬度 -90..90，不跨日期变更线；边界为 0.25° 的整数倍 |
| `--daily-mean` | 另存日均通量；需要结束日次日的小时数据 |
| `--timeout` | 单次 HTTP 请求超时秒数，默认 60，不限制 CDS 排队时长 |
| `--retries` | 目录请求或分块任务总尝试次数，默认 3 |

脚本保守排除当月和最近三个完整日历月，并在文件包含 `expver` 时只接受 1。最终可选范围同时受目录覆盖限制，不会自动裁短超范围请求。

## 分块、校验与恢复

`-n` 只查询公开目录，不提交任务、不创建输出目录。正式任务串行，每个请求最多覆盖一个日历月，没有 `-j`。进度条表示完成块数，CDS 排队时显示任务状态；排队不计作可预测的下载进度。

下载先写 `.download.nc`，校验后重写为 `.writing.nc`，重新打开验证后改名。校验包括请求签名、完整时间轴、区域网格、变量、单位和数值；支持递增或递减纬度及等价的西经编码。日均还校验时间边界和处理信息。

完整匹配的文件重跑跳过；损坏或参数不同的正式文件保留并报错，没有 `--overwrite`。未完成块可能重新提交 CDS，任务 ID 不跨进程恢复，不承诺字节续传。只有可识别的临时网络错误及部分 HTTP 状态会触发脚本重试；认证、磁盘和内容校验错误停止。

同时预留原始下载、正在重写的文件和已完成文件空间；日均按月加载相邻小时块，大区域需考虑内存。同一输出目录不要并发运行多个实例。

## 日均计算与数据含义

四变量均向下为正。CDS 小时值是时间戳之前一小时的累计能量，单位 J m-2，不是瞬时通量。

对 UTC 日 D 使用 D01、D02、…、D23、D+1 00 共 24 个累计值，求和除以 86400 秒，得到 W m-2 的日均通量；不做相邻时间差分。脚本请求并保留结束日次日全天的小时数据。日均坐标为 D00，边界为 `[D00,D+1 00)`；任一小时缺测会传播为该像素日均缺测，缺少时间戳则报错。

原小时文件保留，日均更新单位和处理属性。不同符号约定、时间平均方式的数据联合使用前需转换并对齐；脚本不做海陆掩膜、插值或重网格。

- [CDS 数据集](https://cds.climate.copernicus.eu/datasets/reanalysis-era5-single-levels)。
- [CDS API 配置](https://cds.climate.copernicus.eu/how-to-api)。
- [ECMWF 累计量转换](https://confluence.ecmwf.int/plugins/viewsource/viewpagesrc.action?pageId=462888259)。
- [ERA5 数据说明](https://confluence.ecmwf.int/pages/viewpage.action?pageId=488277769)。
