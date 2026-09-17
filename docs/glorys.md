# GLORYS12V1

脚本：[download_glorys.py](../download_glorys.py)。通过 Copernicus Marine Toolbox 按区域、深度、变量和年份获取逐日再分析，每年保存一个 NetCDF。

## 默认配置

- 数据集：`cmems_mod_glo_phy_my_0.083deg_P1D-m`；脚本未固定目录版本。
- 年份必填，支持 1993–2025，由脚本常量限制，不随源站自动扩展。
- 区域：南海 `105 125 0 25`，`--bbox` 顺序为 **西、东、南、北**。
- 网格：1/12°，50 个垂向层；默认获取全深度。
- 变量：`thetao so uo vo zos`。
- 输出：当前工作目录中的 `GLORYS12V1_SCS_daily_YYYY.nc`，压缩级别 1。文件名中的 `SCS` 不随区域参数改变。

## 认证与用法

按 [README](../README.md) 安装依赖，运行 `copernicusmarine login` 保存账号凭据。代理设置统一见 [网络与代理](../README.md#网络与代理)。

```powershell
# 查询一个小区域的年度计划和估算
python download_glorys.py 2020 --bbox 110 111 10 11 -v zos -n -o "D:\GLORYS_sample"

# 下载同一请求
python download_glorys.py 2020 --bbox 110 111 10 11 -v zos -o "D:\GLORYS_sample"

# 默认区域、全深度
python download_glorys.py 2020 -o "D:\GLORYS"

# 西北太平洋：明确指定区域，失败后停止
python download_glorys.py 2011-2016 --bbox 100 180 0 60 -o "F:\GLORYS_NorthwestPacific" --stop-on-error
```

年份支持 `2001`、`2001-2005`、`2001,2003-2005`。

| 参数 | 作用 |
| --- | --- |
| `-v/--variables` | 空格分隔的变量列表 |
| `-z/--min-depth`、`-Z/--max-depth` | 深度下限、上限，单位 m；默认不限 |
| `--compression 0..9` | 压缩级别，默认 1，0 为不压缩 |
| `--overwrite` | 重下已有年份 |
| `--stop-on-error` | 一个年份失败后停止；默认继续下一年 |
| `--strategy yearly` | 唯一策略，也是默认值，无需指定 |

## 预检查与空间

`-n` 会创建输出目录，并调用 `subset(dry_run=True)` 联网查询待下载年份的估算，不下载数据正文。`file_size` 是输出大小估算，`data_transfer_size` 是粗略传输量估算；二者均不等于实际测得的大小，未返回的值显示为未知。

正式下载按年串行，没有 `-j`，也没有脚本级自动重试。Toolbox 选择底层服务和存储块；小区域请求仍可能读取边界以外的块。需容纳已完成年份和当前临时文件；重下已有文件时，旧文件在新文件校验完成前保留。

## 校验与恢复

先写 `.glorys_tmp/` 中的年度文件，验证后替换正式文件。重跑会检查所需变量及完整、递增、无重复的逐日时间轴；检查通过则跳过，检查失败则重新下载。不支持字节续传，未完成年份重新请求。

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
