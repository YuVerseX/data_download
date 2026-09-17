# Copernicus Marine 海表盐度与密度

脚本：[download_cmems_sss.py](../download_cmems_sss.py)。通过 Toolbox 获取产品 `MULTIOBS_GLO_PHY_S_SURFACE_MYNRT_015_013` 的区域日或月数据。

## 默认配置

- 起止日期必填，首尾均包含，使用 `YYYY-MM-DD`。
- 区域：西北太平洋 `100 180 0 60`，顺序为 **西、东、南、北**；0.125° 表层网格。
- 频率：`--frequency daily`；`monthly` 使用独立月产品。
- 变量：`sos sos_error dos dos_error sea_ice_fraction`。
- 来源：`--source auto` 以脚本固定的 2024-01-01 为分界，之前用 MY、自该日起用 NRT，跨界拆分；并非动态寻找 MY/NRT 边界。
- 输出：当前工作目录，按源和年份分文件，名称为 `CMEMS_SSS_SSD_<源>_<频率>_<开始日>_<结束日>.nc`。

| 频率 | MY 数据集 | NRT 数据集 |
| --- | --- | --- |
| 日 | `cmems_obs-mob_glo_phy-sss_my_multi_P1D` | `cmems_obs-mob_glo_phy-sss_nrt_multi_P1D` |
| 月 | `cmems_obs-mob_glo_phy-sss_my_multi_P1M` | `cmems_obs-mob_glo_phy-sss_nrt_multi_P1M` |

## 认证与用法

按 [README](../README.md) 安装依赖，运行 `copernicusmarine login` 保存凭据。代理设置见 [网络与代理](../README.md#网络与代理)。

```powershell
# 单日预检查
python download_cmems_sss.py --start-date 2024-01-01 --end-date 2024-01-01 -n -o "D:\CMEMS_SSS"

# 单日下载默认区域和变量
python download_cmems_sss.py --start-date 2024-01-01 --end-date 2024-01-01 -o "D:\CMEMS_SSS"

# 年度盐度与误差，使用独立目录
python download_cmems_sss.py --start-date 2001-01-01 --end-date 2001-12-31 -v sos sos_error -o "D:\CMEMS_SSS_salinity"

# 月产品：包含 2001 年 1 月至 12 月的时间戳
python download_cmems_sss.py --start-date 2001-01-01 --end-date 2001-12-01 --frequency monthly -o "D:\CMEMS_SSS_monthly"
```

| 参数 | 作用 |
| --- | --- |
| `--bbox W E S N` | 经度 -180..180、纬度 -90..90，不跨日期变更线 |
| `-v/--variables` | 从默认五变量中选择 |
| `--source auto\|my\|nrt` | 选择来源，仍会核对源站覆盖；NRT 请求不能早于脚本分界日 |
| `--dataset-version` | 固定目录版本；跨 MY/NRT 的请求需拆开固定 |
| `--compression 0..9` | 压缩级别，默认 1 |
| `--retries` | 每个文件总尝试次数，默认 3；不包含启动时的坐标查询 |
| `--overwrite`、`--stop-on-error`、`--no-progress` | 替换已有文件、失败后停止、关闭动态进度 |

月产品只选择区间内每月 1 日的时间戳，起止日期建议使用月初；每个按年拆分的区间都需包含月时间戳，否则报错。区域按格点中心选择，默认经度为 100.0625–179.9375（640 点）、纬度为 0.0625–59.9375（480 点）；运行时打印实际边界。

## 预检查、校验与恢复

`-n` 联网读取坐标、逐项核对请求时间并估算待下载文件，不创建输出目录；已有文件仍会检查。正式任务按源和年份串行，没有 `-j`。下载前的可用磁盘空间需达到已返回的文件大小估算的 1.2 倍；传输量可能明显大于输出文件。

先写同目录 `.partial.nc`，检查时间轴、经纬度、变量维度并分块读取全部数据，再写入请求参数并替换正式文件。完整匹配的文件跳过，损坏或参数不匹配的文件保留并报告失败；使用 `--overwrite` 才替换。未完成文件重新请求，不支持字节续传；不要向同一输出目录并发运行多个实例。

未指定 `--dataset-version` 时，已有文件检查不会比较源站自动解析的产品版本。需要固定版本的数据集应显式指定版本并使用独立目录。

## 数据含义与来源

`sos` 为海表盐度，`sos_error` 为其误差；`dos` 为海表密度（kg/m³），`dos_error` 为密度误差；`sea_ice_fraction` 为海冰面积百分比。保留源数值、单位和缺测属性，月产品不是脚本对日产品临时求平均。

- [产品下载页](https://data.marine.copernicus.eu/product/MULTIOBS_GLO_PHY_S_SURFACE_MYNRT_015_013/download)。
- [用户手册](https://documentation.marine.copernicus.eu/PUM/CMEMS-MOB-PUM-015-013.pdf)。
- [Toolbox subset](https://toolbox-docs.marine.copernicus.eu/en/stable/usage/subset-usage.html)。
