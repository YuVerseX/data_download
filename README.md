# data_download

科研数据下载脚本，一个数据源一个脚本。命令在仓库根目录运行，具体参数见对应文档或 `python <脚本名> --help`。

## 脚本清单

| 脚本 | 数据集 | 脚本支持的时间选择 | 时间含义 | 文档 |
| --- | --- | --- | --- | --- |
| [download_scsora.py](download_scsora.py) | SCSORA 再分析 | 2001–2024 | 逐日，按年保存 | [SCSORA](docs/scsora.md) |
| [download_mur_sst.py](download_mur_sst.py) | MUR L4 海表温度 | 2002-06-01 起；默认当年 | 逐日 | [MUR SST](docs/mur_sst.md) |
| [download_argo.py](download_argo.py) | Argo 剖面 | 按索引、区域和年份筛选 | 离散剖面 | [Argo](docs/argo.md) |
| [download_glorys.py](download_glorys.py) | GLORYS12V1 再分析 | 1993–2025 | 日均，按年保存 | [GLORYS](docs/glorys.md) |
| [download_duacs.py](download_duacs.py) | DUACS L4 海平面与地转流 | 默认 2001 起至目录最新完整年 | 逐日，按年保存 | [DUACS](docs/duacs.md) |
| [download_cci_sss.py](download_cci_sss.py) | ESA CCI SSS v5.5 海表盐度 | 年份或日期区间；逐日检查源目录 | **七天滑动平均，逐日采样** | [CCI SSS](docs/cci_sss.md) |
| [download_ccmp.py](download_ccmp.py) | RSS CCMP v3.1 海表风场 | 默认 2001 起至目录最新完整年 | 每天四个分析时次，可另存日均 | [CCMP](docs/ccmp.md) |
| [download_era5_flux.py](download_era5_flux.py) | ERA5 海表热通量 | 默认 2001 起至可用的完整最终 ERA5 年 | 小时累计能量，可另存日均通量 | [ERA5](docs/era5_flux.md) |

时间选择不代表所选区域每天都有有效观测。输出大小、网络流量和数据覆盖限制见各产品文档。

## 安装与运行

```powershell
conda create -n data_download python=3.12
conda activate data_download
python -m pip install -r requirements.txt

# 查看计划
python download_cci_sss.py --start-date 2011-01-01 --end-date 2023-12-30 -n

# 下载；输出路径按需修改
python download_cci_sss.py --start-date 2011-01-01 --end-date 2023-12-30 --bbox 105 125 0 25 --out "F:\cci_sss"
python download_ccmp.py 2001-latest --bbox 105 125 0 25 --daily-mean -o "F:\CCMP"
```

环境只需创建一次。后续激活 `data_download` 后运行；`-n/--dry-run` 查看计划，`-o` 指定输出目录。首次使用建议先下载一个日期或最小分块，确认认证、下载与保存成功。

## 认证

| 数据源 | 准备 |
| --- | --- |
| MUR SST | Earthdata Login token，默认读取 `%USERPROFILE%\.edl_token`，见 [认证说明](docs/mur_sst.md#认证) |
| GLORYS、DUACS | Copernicus Marine 账号，运行 `copernicusmarine login` |
| ERA5 | CDS 账号、数据集条款及 `.cdsapirc`，见 [ERA5 文档](docs/era5_flux.md) |
| SCSORA、Argo、CCI、CCMP | 脚本使用公开下载入口；使用许可与引用见各文档 |

## 区域与恢复

- GLORYS、DUACS、CCI、CCMP、ERA5 使用空格分隔的 `--bbox 西 东 南 北`，默认 `105 125 0 25`；允许的经度范围以各脚本为准。
- MUR 使用逗号分隔的 `--bbox 西,南,东,北`；Argo 使用 `--bbox 西,东,南,北`，默认 `105,121,2,25`。
- 不同产品的格点中心与时间平均方式不同，联合使用前需按坐标和时间对齐。
- 中断后重跑相同命令。SCSORA 支持字节续传；其余脚本跳过已完成文件，未完成文件或分块重新下载。具体校验方式见各文档。
- 网络流量可能远大于最终输出：CCMP 先下载全球日文件再裁剪，GLORYS、DUACS 受服务端分块影响。长时段下载前先查看计划和空间需求。

数据及凭据不入库，忽略规则见 [.gitignore](.gitignore)，依赖见 [requirements.txt](requirements.txt)。
