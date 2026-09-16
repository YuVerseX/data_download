# GLORYS12V1

脚本：[download_glorys.py](../download_glorys.py)

| 项目 | 值 |
| --- | --- |
| 数据集起点 | 1993-01-01；脚本仅处理下列支持年份 |
| 时间分辨率 | 日均 |
| 脚本可用范围 | 1993–2025，由脚本 `MIN_YEAR` / `MAX_YEAR` 限定 |
| 空间分辨率 | 1/12°（约 8 km），50 层 |
| 默认区域 | 南海 105–125°E, 0–25°N |
| 体积 | 随区域、深度、变量和压缩效果变化；`-n` 提供官方粗略估算 |

```powershell
python download_glorys.py 2020 -o D:\GLORYS
python download_glorys.py 2016-2020 -o D:\GLORYS
python download_glorys.py 2001,2003-2005 -o D:\GLORYS
```

年份写法和 SCSORA 一致，可用范围 1993–2025。
每年调用一次官方 `copernicusmarine.subset`，由 Toolbox 自动选择服务并按经纬度、
深度、变量和时间裁剪，直接写出单年 NetCDF。没有多年中间文件，也不做本地切分。
`--strategy yearly` 仅保留兼容；无需指定。旧的 `--strategy grouped` 已移除。

```powershell
# 西北太：先查看逐年计划和官方粗略估算
python download_glorys.py 2011-2016 --bbox 100 180 0 60 -o F:\GLORYS_NorthwestPacific -n
# 正式下载；失败时停止，重跑会跳过校验通过的年份
python download_glorys.py 2011-2016 --bbox 100 180 0 60 -o F:\GLORYS_NorthwestPacific --stop-on-error
```

不要同时运行两个进程写同一输出目录。不给 `-o` 就下到当前工作目录。
为兼容已有数据，文件名仍为 `GLORYS12V1_SCS_daily_年份.nc`，其中 SCS 不代表实际裁剪区域。
**不同区域或深度请使用不同目录**：已有文件校验只检查变量和时间轴，不检查区域及深度。

默认下南海（105–125°E, 0–25°N）全深度 5 个变量。改范围用 `--bbox W E S N`、
`-v/--variables`、`-z/-Z`，其余参数见 `--help`。

退出码：`0` 全部成功（含已完整跳过），`1` 有年份失败，`2` 参数错误，`130` Ctrl-C 中断。

## 数据源

Copernicus Marine Service，产品 `GLOBAL_MULTIYEAR_PHY_001_030`，
数据集 `cmems_mod_glo_phy_my_0.083deg_P1D-m`，版本 `202311`。

- 1/12°（约 8 km），50 层，日均；脚本不自动扩展支持年份
- 需要 CMEMS 账号：<https://data.marine.copernicus.eu/register>，
  然后 `copernicusmarine login` 一次，凭据存在 `~/.copernicusmarine/`
- 本脚本使用官方 `subset` 接口裁剪 ARCO 数据；不使用原始文件下载接口

### 变量

| 变量 | 含义 | 单位 | 维度 |
|---|---|---|---|
| `thetao` | 位温 | °C | 含 depth |
| `so` | 盐度 | 1e-3 | 含 depth |
| `uo` / `vo` | 流速东向 / 北向分量 | m/s | 含 depth |
| `zos` | 海表高度 | m | 二维 |
| `mlotst` | 混合层厚度 | m | 二维 |
| `bottomT` | 海底位温 | °C | 二维 |
| `siconc` `sithick` `usi` `vsi` | 海冰密集度 / 厚度 / 速度 | — | 二维 |

脚本默认只下 `thetao so uo vo zos`。**海冰那 4 个在南海全是缺测**，要了纯浪费。
`so` 的单位标 `1e-3` 是 CF 对实用盐度的写法，数值就是 33–35 那个量级，不用换算。

## 流量与空间

该数据集提供 `geoseries` 和 `timeseries`，二者的时间、空间分块不同。
Toolbox 根据每次单年请求自动选择服务。逐年下载不等于必然重复读取五六年数据，
但仍可能读取覆盖裁剪范围以外的存储块；实际网络传输量需测量。

`-n/--dry-run` 调用官方 `subset(dry_run=True)`，不下载数据正文：

- `file_size` 是输出文件大小估算，不能准确反映压缩后的大小。
- `data_transfer_size` 是传输量上限的粗略估算，不是实际网络流量。
- 数值及 MB 单位沿用官方 API，不自行换算后宣称为精确 GiB/TiB。
- 未返回的大小显示为“未知”，预估查询失败返回退出码 1。

正式下载直接调用单年 `subset`，不额外调用预估，也不以粗略预估设置硬性磁盘门槛。
每年开始时显示可用空间，完成时显示文件实际大小。
仍需容纳所有已完成年份及正在写入的一年；磁盘实际写满时，该年会报错。
若要重下已有年份，校验完成前旧文件仍保留，因此还需容纳该年的新文件。
缩小区域、减少变量或限定深度可减少请求的数据量。

官方说明：
- [subset 用法](https://toolbox-docs.marine.copernicus.eu/en/stable/usage/subset-usage.html)
- [返回值及估算含义](https://toolbox-docs.marine.copernicus.eu/en/stable/response-types.html)

## 恢复与校验

- 脚本不支持字节续传。重跑相同命令会跳过已通过校验的年份，未完成的年份重新请求。
- 当前年份直接写入输出目录的 `.glorys_tmp/`；验证通过后在同一磁盘改名为最终文件，
  不复制、不重新编码，不需要多年文件加逐年文件的双份空间。
- 校验检查所需变量存在、天数正确，以及完整、递增且无重复的逐日时间轴（含闰年）。
  这是结构检查，不是校验和检查，也不验证每个变量的全部数据块。
- 下载或校验失败时保留临时文件，并保留原有最终文件；下次请求覆盖对应单年临时文件。
  旧版遗留的 `_chunk_*.nc` 不会自动删除，清理前请自行确认其内容。
- `--stop-on-error` 在某一年失败后停止；不加则继续下一年。
- **脚本年份上限是 2025**。扩展前需核实源目录是否覆盖完整年。

## 数据集本身

- **1993 至 2021 年中是再分析段，之后是 interim 段**。模式、分辨率、同化方案相同，
  差别在同化的观测：再分析段用重处理的延迟模式观测，interim 段用近实时观测。
  CMEMS 已经把两段并进同一个 dataset ID，从数据里看不出接缝。
  **跨 2021 年中做趋势或气候态之前，先在接缝处画个区域平均时间序列看有没有跳变。**
  官方也没给出量化的两段技能差异，要确认得查 QUID。
- 引用：DOI [10.48670/moi-00021](https://doi.org/10.48670/moi-00021)，需注明
  Copernicus Marine Service。同行评议参考文献是 Lellouche et al. (2021),
  *The Copernicus Global 1/12° Oceanic and Sea Ice GLORYS12 Reanalysis*,
  Front. Earth Sci.
- 质量文档：[CMEMS-GLO-QUID-001-030](https://documentation.marine.copernicus.eu/QUID/CMEMS-GLO-QUID-001-030.pdf)
- 产品页：<https://data.marine.copernicus.eu/product/GLOBAL_MULTIYEAR_PHY_001_030/description>
