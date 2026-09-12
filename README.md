# data_download

各类科研数据集的下载脚本，一个数据源一个脚本。

## 脚本清单

| 脚本                                      | 数据集                        | 时间范围                                       | 时间分辨率 | 单文件体积                 | 说明                              |
| ----------------------------------------- | ----------------------------- | ---------------------------------------------- | ---------- | -------------------------- | --------------------------------- |
| [download_scsora.py](download_scsora.py)   | SCSORA 再分析                 | 2001–2024                                     | 逐日       | ~172 GiB（每年一个文件）   | [docs/scsora.md](docs/scsora.md)   |
| [download_mur_sst.py](download_mur_sst.py) | MUR L4 海表温度（默认南海）   | 2002-06-01 至今                                | 逐日       | ~4 MiB（每天一个文件）     | [docs/mur_sst.md](docs/mur_sst.md) |
| [download_argo.py](download_argo.py)       | Argo 剖面（默认南海）         | 全球 1997 至今；**南海实际只到 2022-04** | 离散剖面   | ~20 KiB（每剖面一个文件）  | [docs/argo.md](docs/argo.md)       |
| [download_glorys.py](download_glorys.py)   | GLORYS12V1 再分析（默认南海） | 1993–2025                                     | 日均       | ~12.7 GB（南海全深度单年） | [docs/glorys.md](docs/glorys.md)   |
| [download_duacs.py](download_duacs.py) | DUACS L4 海平面与地转流（默认南海） | 默认 2001 至最新完整年（本次为 2025） | 逐日 | ~93 MiB（南海七变量单年，样本值） | [docs/duacs.md](docs/duacs.md) |

## 新增海表输入与热通量

新增下载器共用 `--bbox W E S N`（西、东、南、北），默认 `105 125 0 25`；支持 `-n/--dry-run`、`-o/--output-dir`、年份选择或 `--start-date` / `--end-date`。默认起点为 2001 年与产品实际起点的较晚者，截止时间按运行时官方目录核查的完整年选择，不补造缺年。

| 脚本 | 用途 | 产品与时间语义 | 文档 |
| --- | --- | --- | --- |
| [download_cci_sss.py](download_cci_sss.py) | 海表盐度输入 | ESA CCI SSS v5.5，7 日平均、逐日采样，0.25°网格；有效空间分辨率约 50 km | [docs/cci_sss.md](docs/cci_sss.md) |
| [download_ccmp.py](download_ccmp.py) | 海表风矢量输入 | RSS CCMP v3.1，0.25°，UTC 00/06/12/18 四个分析时次；可另存本地日均 | [docs/ccmp.md](docs/ccmp.md) |
| [download_era5_flux.py](download_era5_flux.py) | 热收支约束辅助变量 | ERA5 单层逐小时四项热通量累计量；可另存按 UTC 日积分转换的 W/m² 日均 | [docs/era5_flux.md](docs/era5_flux.md) |

CCI 的逐日输出不是每日独立观测。CCMP 使用卫星观测与 ERA5 背景场融合，不能因平台不同就认为独立。ERA5 原始累计量与本地日均通量分开保存。SSS 是海表盐度；DUACS 的 SLA 是海平面异常，不包含 SSS。海陆缺测与质量标识按上游语义保留。

本次仅开发和小样本验证，**正式下载尚未启动**。测试数据、测试脚本和汇总验证报告已按要求删除。三个下载器的命令、认证前置条件及空间估算见上表各产品文档。既有脚本不代表对应全时段数据已经下载完成；上表 Argo 截至 2022 年的历史表述仅来自一次区域索引筛选，不能推广为整个南海此后没有观测。新增下载器默认区域与 GLORYS、DUACS 一致；CCI/CCMP与ERA5格点中心不同，建模前需显式对齐。MUR的bbox为逗号分隔的西、南、东、北；Argo为逗号分隔的西、东、南、北，且默认区域仍是105–121E、2–25N，请查看各自帮助。

## 用法

```powershell
conda create -n data_download python=3.12
conda activate data_download
pip install -r requirements.txt

python F:\Code\data_download\download_scsora.py 2001 -o "D:\数据目录"
python F:\Code\data_download\download_mur_sst.py 2020-2024 -o "D:\数据目录"
```

环境只需建一次，之后每次用前 `conda activate data_download` 即可。

年份支持单个 `2001`、区间 `2001-2005` 或组合 `2001,2003-2005`；`-o` 指定输出目录，
省略则下到当前工作目录。中断后重跑同样的命令续传。

各脚本的参数不一样，`--help` 或对应的 `docs/<脚本名>.md` 里有。

**MUR SST 需要先配好 Earthdata Login 的 token**（存到 `%USERPROFILE%\.edl_token`），
见 [docs/mur_sst.md](docs/mur_sst.md#认证)。token 等同账号凭据，不要入库。

**GLORYS12V1 需要 CMEMS 账号**，跑一次 `copernicusmarine login` 即可。
它的网络流量远大于落盘体积（南海全深度单年 12.7 GB 数据要拉 247 GiB），
默认逐年直下，不再本地切分；`--strategy grouped` 可合并请求以省流量。
下之前务必先跑 `-n` 看流量。它也**不支持断点续传**——中断了当前请求要重来，
但已下好的年份会跳过。详见 [docs/glorys.md](docs/glorys.md)。

## 备注

**DUACS 同样使用 CMEMS 账号**，默认逐年下载。先运行 `python download_duacs.py 2001-2025 -n` 预估流量；去掉 `-n` 并加 `-o "F:\DUACS"` 开始下载。完整年文件校验后跳过，未完成年份重下。

- 数据文件不入库，已在 [.gitignore](.gitignore) 排除
- 第三方依赖记在 [requirements.txt](requirements.txt)，注明所属脚本
- 数据源的细节和坑写进 `docs/<脚本名>.md`
