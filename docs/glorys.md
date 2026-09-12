# GLORYS12V1

脚本：[download_glorys.py](../download_glorys.py)

| 项目 | 值 |
| --- | --- |
| 数据集起点 | 1993-01-01；脚本仅处理下列支持年份 |
| 时间分辨率 | 日均 |
| 脚本可用范围 | 1993–2025，由脚本 `MIN_YEAR` / `MAX_YEAR` 限定 |
| 空间分辨率 | 1/12°（约 8 km），50 层 |
| 默认区域 | 南海 105–125°E, 0–25°N |
| 体积 | 落盘 12.7 GB/年（南海全深度 5 变量）；**网络流量另算，见「流量放大」** |

```powershell
python download_glorys.py 2020 -o D:\GLORYS
python download_glorys.py 2016-2020 -o D:\GLORYS
python download_glorys.py 2001,2003-2005 -o D:\GLORYS
```

年份写法和 SCSORA 一致，可用范围 1993–2025。
默认 `--strategy yearly`：每年单独请求，下载后直接校验并改名，不做本地切分。
网络读取量更大，但避免多年文件的二次读写和压缩；实际总耗时取决于网络和磁盘性能。
需要节省网络读取量时，使用 `--strategy grouped` 合并请求后按年切分。

```powershell
python download_glorys.py 2001-2025 -o F:\GLORYS --strategy yearly
python download_glorys.py 2001-2025 -o F:\GLORYS --strategy grouped
```

不要同时运行两个进程写同一输出目录。
不给 `-o` 就下到当前工作目录。**下之前先跑 `-n` 看流量**，理由见下面「流量放大」。

默认下南海（105–125°E, 0–25°N）全深度 5 个变量。改范围用 `--bbox W E S N`、
`-v/--variables`、`-z/-Z`，其余参数见 `--help`。

退出码：`0` 全部成功（含已完整跳过），`1` 有年份失败，`2` 参数错误，`130` Ctrl-C 中断。

## 数据源

Copernicus Marine Service，产品 `GLOBAL_MULTIYEAR_PHY_001_030`，
数据集 `cmems_mod_glo_phy_my_0.083deg_P1D-m`，版本 `202311`。

- 1/12°（约 8 km），50 层，日均；脚本不自动扩展支持年份
- 需要 CMEMS 账号：<https://data.marine.copernicus.eu/register>，
  然后 `copernicusmarine login` 一次，凭据存在 `~/.copernicusmarine/`
- 走 `copernicusmarine` toolbox 从 ARCO(zarr) 裁剪，**没有可直接 GET 的整年文件**

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

ARCO 在时间维上分块存储，一块约 5.75 年。裁剪时块必须整块拉下来再切，
所以**网络读取量可能远大于落盘体积**。dry-run 返回的 `data_transfer_size` 是估算值，
不等于网卡实测流量，也不能直接按比例推算耗时。

南海全深度 5 变量的历史样本和工具箱网络估算（实际以 `-n` 为准）：

| 请求 | 落盘样本 | 网络读取估算 | 比例 |
|---|---|---|---|
| 2020 单年 | 12.7 GB | 247.5 GiB | 20× |
| 2016–2020 一次请求 | 61.8 GB | 247.5 GiB | 4× |
| 2016–2020 拆成 5 次 | 61.8 GB | 1237 GiB | 20× |

`--strategy grouped` 按块分组，减少同一远端块的重复读取，但会增加本地切分成本。
默认逐年模式只输出单年文件，仍可能读取覆盖多年的远端块，并不意味着网络只拉单年。

### 块边界年

实测边界年（这些年横跨两块，单独请求也要付两块流量）：

```
1998  2004  2010  2015  2021
```

间隔 6/6/5/6 年——块跨度不是整年，边界落在年中。脚本里是
`CHUNK_BOUNDARY_YEARS` 常量。数据集版本或分块变化时需重新检查，不能直接沿用边界。

重测办法：对单变量逐年 dry-run，流量是基准值 2 倍的就是边界年。

```powershell
copernicusmarine subset -i cmems_mod_glo_phy_my_0.083deg_P1D-m -v thetao `
  -t 2027-01-01 -T 2027-12-31 -x 105 -X 125 -y 0 -Y 25 `
  --dry-run -r data_transfer_size --log-level QUIET
```

### 其他省流量的办法

- 限深度：`-z 0 -Z 1000` 省 29%，只要表层 `-z 0 -Z 1` 省 96%
- 缩小 bbox

## 恢复与校验

- **没有断点续传**。toolbox 是整块下完才落盘，中断了整块重来。已校验通过的年文件
  会被跳过，所以重跑同样的命令不会重下已完成的年，但失败的那一块要从头再来。
- **没有校验和**，字节数又因压缩不可预测。脚本改成打开文件验 time 维：
  长度等于该年天数、首末日期对、变量齐全。校验不过的落成 `.bad` 不覆盖好文件。
- **磁盘峰值**。仅 grouped 模式下，多年块要先落一个完整的中间文件再切分，峰值约是最终体积的 2 倍。
  南海全深度一个 5 年块峰值约 120 GB。装不下就一次只给一个年份。
  中间文件放在输出目录下的 `.glorys_tmp/`，正常结束会自己清掉。
- **切分要保留原始打包编码**。toolbox 下来的变量是 int16 + `scale_factor` 打包的，
  xarray 读进来解码成 float64，直接写回去文件会大三四倍。脚本里 `transfer_encoding()`
  负责把 dtype 和标度抄回去——改切分逻辑时别把这个丢了。
- **脚本年份上限是 2025**。扩展前需核实源目录是否覆盖完整年，不能只修改上限绕过校验。

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
