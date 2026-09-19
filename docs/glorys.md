# GLORYS12V1

脚本：[download_glorys.py](../download_glorys.py)。通过 Copernicus Marine Toolbox 按区域、深度和变量获取逐日再分析。支持直接按年请求、按月下载后合并年度 NetCDF，以及直接保留月文件。

## 默认配置

- 数据集：`cmems_mod_glo_phy_my_0.083deg_P1D-m`；脚本未固定目录版本。
- 年份必填，支持 1993–2025，由脚本常量限制，不随源站自动扩展。
- 区域：南海 `105 125 0 25`，`--bbox` 顺序为 **西、东、南、北**。
- 网格：1/12°，50 个垂向层；默认获取全深度。
- 变量：`thetao so uo vo zos`。
- 输出：默认在当前工作目录生成 `GLORYS12V1_SCS_daily_YYYY.nc`，压缩级别 1；`monthly-files` 改为 `YYYY/GLORYS12V1_SCS_daily_YYYYMM.nc`。文件名中的 `SCS` 不随区域参数改变。

## 认证与用法

按 [README](../README.md) 安装依赖，运行 `copernicusmarine login` 保存账号凭据。代理设置统一见 [网络与代理](../README.md#网络与代理)。

```powershell
# 查询一个小区域的年度计划和估算
python download_glorys.py 2020 --bbox 110 111 10 11 -v zos -n -o "D:\GLORYS_sample"

# 下载同一请求
python download_glorys.py 2020 --bbox 110 111 10 11 -v zos -o "D:\GLORYS_sample"

# 默认区域、全深度
python download_glorys.py 2020 -o "D:\GLORYS"

# 西北太平洋：直接保留月文件，不执行耗时的年度重压缩
python download_glorys.py 2011-2016 --strategy monthly-files -j 2 --retries 3 --bbox 100 180 0 60 -o "F:\GLORYS_NorthwestPacific" --stop-on-error

# 如确实需要单个年度文件，下载月分片后再合并
python download_glorys.py 2011-2016 --strategy monthly -j 2 --retries 3 --bbox 100 180 0 60 -o "F:\GLORYS_NorthwestPacific" --stop-on-error
```

年份支持 `2001`、`2001-2005`、`2001,2003-2005`。

| 参数 | 作用 |
| --- | --- |
| `-v/--variables` | 空格分隔的变量列表 |
| `-z/--min-depth`、`-Z/--max-depth` | 深度下限、上限，单位 m；默认不限 |
| `--compression 0..9` | 压缩级别，默认 1，0 为不压缩 |
| `--overwrite` | 重建已有年度文件；`monthly-files` 重下已有月文件并采用当前压缩级别；`monthly` 仍复用有效月分片后重建年度文件 |
| `--stop-on-error` | 一个年份失败后停止；默认继续下一年 |
| `--strategy yearly` | 默认模式；每年发起一次请求，直接保存年度文件 |
| `--strategy monthly` | 每月独立请求，全部完成后在本地合并为年度文件 |
| `--strategy monthly-files` | 每月独立请求并直接保存在 `YYYY/` 目录，不生成年度合并文件 |
| `-j/--workers` | 两种月度模式的并发数，默认 2；建议不要超过 4 |
| `--retries` | 月度模式每月最多尝试次数，默认 3，包含首次 |
| `--keep-monthly` | 年度合并校验成功后仍保留月分片 |

## 预检查与空间

`-n` 会创建输出目录，并调用 `subset(dry_run=True)` 联网查询待下载年份的估算，不下载数据正文。`file_size` 是输出大小估算，`data_transfer_size` 是粗略传输量估算；二者均不等于实际测得的大小，未返回的值显示为未知。

正式下载默认按年串行。两种月度策略都会把每个年份拆成 12 个独立请求，默认同时下载 2 个月。`monthly` 会在一年下载完后串行执行本地合并；`monthly-files` 直接保留月文件，不做解压和重压缩，适合大区域、全深度请求。交互式终端会显示一条按月份推进的聚合进度条，并统计新下载、缓存复用和失败数量；它表示已完成月份数，不是单个月份的字节传输百分比。并发过高可能触发服务端限流，也会增加网络和磁盘争用，建议使用 `-j 2`，不建议超过 4。

年度合并需要解压月分片并重新压缩全部数据，无法直接拼接已有 zlib 数据块；对于外置 USB 磁盘上的大区域文件可能耗时很长。`monthly` 合并期间通常需同时容纳月分片和年度临时文件，约为两个年度文件的空间；`monthly-files` 不需要这份额外空间。每完成并验证一个年度合并就会尝试清理其月分片，除非指定 `--keep-monthly`；Windows 文件锁导致清理失败时会明确告警。

## 校验与恢复

年度模式先写 `.glorys_tmp/` 中的年度文件，验证后替换正式文件。`monthly` 把已完成分片保存在 `.glorys_tmp/monthly/<请求哈希>/YYYY/`，12 个月完整后生成年度文件。`monthly-files` 也先在该缓存目录下载，验证后再原子移入输出目录的 `YYYY/` 子目录，因此不会在用户可见目录中清理 Toolbox 临时文件；首次切换时会验证并原子迁移旧 `monthly` 缓存，不会重新下载。重跑会跳过完整月份，只补缺失或损坏月份。单个月份不支持字节续传。

月文件无需先物理合并即可按连续时间轴读取：

```python
import xarray as xr

ds = xr.open_mfdataset(
    r"F:\GLORYS_NorthwestPacific\2015\*.nc",
    combine="by_coords",
    chunks="auto",
)
```

正式年度文件会检查所需变量以及完整、递增、无重复的逐日时间轴；月文件模式会逐月执行同样的时间轴和变量检查。月度合并还要求各分片的非时间坐标完全一致，不会静默取坐标并集。若合并因 NetCDF/HDF 数据块读取错误而失败，脚本会逐月扫描数据变量、坐标和辅助变量，并删除确认损坏的分片，下次运行只补下这些月份。

按 `Ctrl+C` 后，脚本会取消尚未开始的月份并停止后续重试；已经进入 Toolbox 的请求无法安全强制终止，脚本会等待这些请求结束或报错后再以退出码 130 返回。

**已有文件检查不核对区域、深度，也不逐块读取全部变量。不同区域或深度必须使用不同输出目录。** 不要同时运行多个实例写同一输出目录。

退出码：`0` 成功或跳过，`1` 有年份失败或估算失败，`2` 参数错误，`130` 中断。

## 数据含义与来源

| 变量 | 含义 | 单位 |
| --- | --- | --- |
| `thetao` | 海水位温 | °C |
| `so` | 盐度 | `1e-3`，读取时保留源数值与单位 |
| `uo`、`vo` | 东向、北向流速 | m/s |
| `zos` | 海表高度 | m |

其他变量可通过 `-v` 请求，是否可用以源数据集为准。网格间距不等于独立有效分辨率，产品处理历史和质量说明见官方资料。

- [产品页](https://data.marine.copernicus.eu/product/GLOBAL_MULTIYEAR_PHY_001_030/description)。
- DOI：[10.48670/moi-00021](https://doi.org/10.48670/moi-00021)。
- [质量文档](https://documentation.marine.copernicus.eu/QUID/CMEMS-GLO-QUID-001-030.pdf)。
- [Toolbox subset](https://toolbox-docs.marine.copernicus.eu/en/stable/usage/subset-usage.html)。
