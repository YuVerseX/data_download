# ESA CCI 海表盐度

脚本：[download_cci_sss.py](../download_cci_sss.py)。获取 ESA CCI SSS v5.5 的 `GLOBALv5.5/7days` 区域数据。**七天滑动平均、逐日采样，不是单日均值。**

## 默认配置

- 日期：支持年份或起止日期；省略时从目录中实际起点（不早于 2001 年）选至最新完整日历年，并检查中间缺日。
- 区域：南海 `105 125 0 25`，顺序为 **西、东、南、北**。
- 网格：0.25°；默认经度 105.125–124.875（80 点）、纬度 0.125–24.875（100 点）。
- 变量：`sss sss_random_error noutliers total_nobs pct_var sss_qc lsc_qc isc_qc`，固定下载八个变量。
- 输出：默认 `data/cci_sss/YYYY/<源文件名>.nc`。

无需账号，按 [README](../README.md) 安装依赖。正式下载需要 `requests`、`numpy`、`xarray`、`netCDF4`、`pydap` 和 `tqdm`；`-n` 不需要 `netCDF4`、`pydap`。

## 用法

```powershell
# 查询单日计划，不下载数据正文
python download_cci_sss.py --start-date 2011-01-01 --end-date 2011-01-01 -n -o "F:\cci_sss"

# 下载同一天
python download_cci_sss.py --start-date 2011-01-01 --end-date 2011-01-01 -o "F:\cci_sss"

# 指定完整年份，或用日期参数请求部分年份
python download_cci_sss.py 2011-2012 --bbox 105 125 0 25 -o "F:\cci_sss"

# 发现实际起点与最新完整年，只查看计划
python download_cci_sss.py --all-available -n
```

日期首尾均包含。位置年份、`--years`、起止日期和 `--all-available` 互斥。年份请求要求该年每一天都存在，目录少一天也会报错；部分年份请用实际可用的起止日期。

| 参数 | 作用 |
| --- | --- |
| `--bbox W E S N` | 经度 -180..180、纬度 -90..90，不跨日期变更线，区域内至少有一个格点 |
| `-o/--output-dir/--out` | 输出目录，默认 `data/cci_sss` |
| `--timeout` | 每次网络请求超时秒数，默认 120 |
| `--retries` | 包含首次在内的总尝试次数，默认 3 |
| `--proxy` | 接受 `direct` 或 HTTP(S) 代理地址；现有实现可能被环境代理覆盖 |

固定代理或直连时按 [README](../README.md#网络与代理) 设置终端环境，不单独依赖 `--proxy`。

## 预检查、校验与恢复

`-n` 查询目录并核对日期和计划，不下载 NetCDF、不创建数据输出目录。正式任务按日串行，没有 `-j`。

一天的八个变量合并成一个 DAP2 区域请求，另请求一次属性，不下载全球原件。先写 `.nc.part`，重新打开检查时间、坐标、变量、单位、版本、质量标识及数据可读性，通过后改名。源科学属性、缺测和质量标识保留，不做额外时间平均或插值。

重跑校验通过的文件会跳过；损坏或请求不匹配时停止，脚本没有 `--overwrite`。未完成日从头下载，不支持字节续传。临时网络错误可以重试；缺依赖、校验失败和磁盘错误不重复下载。同一输出目录不要并发运行多个实例。

## 数据含义与来源

盐度及误差单位 `0.001` 保持源数值，不额外乘除 1000；质量标识 0=Good、1=Bad。缺测质量标识不视为 Good。部分整数变量未显式声明缺测值，脚本会识别 NetCDF 默认缺测码并在输出中记录。

保留 `P7D` 时间窗口和 `P1D` 采样间隔。网格间距不等于有效分辨率，连续日期的滑动平均包含重叠观测。

- [CEDA 版本目录](https://data.cci.ceda.ac.uk/thredds/catalog/esacci/sea_surface_salinity/data/catalog.html)。
- [v5.5 七天产品目录](https://data.cci.ceda.ac.uk/thredds/catalog/esacci/sea_surface_salinity/data/v05.5/GLOBALv5.5/7days/catalog.html)。
- [ESA 产品发布说明](https://climate.esa.int/en/news-events/Sea-Surface-Salinity-Record-Extended/)。
