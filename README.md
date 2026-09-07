# data_download

各类科研数据集的下载脚本，一个数据源一个脚本。

## 脚本清单

| 脚本                                      | 数据集                        | 时间范围                                       | 时间分辨率 | 单文件体积                 | 说明                              |
| ----------------------------------------- | ----------------------------- | ---------------------------------------------- | ---------- | -------------------------- | --------------------------------- |
| [download_scsora.py](download_scsora.py)   | SCSORA 再分析                 | 2001–2024                                     | 逐日       | ~172 GiB（每年一个文件）   | [docs/scsora.md](docs/scsora.md)   |
| [download_mur_sst.py](download_mur_sst.py) | MUR L4 海表温度（默认南海）   | 2002-06-01 至今                                | 逐日       | ~4 MiB（每天一个文件）     | [docs/mur_sst.md](docs/mur_sst.md) |
| [download_argo.py](download_argo.py)       | Argo 剖面（默认南海）         | 全球 1997 至今；**南海实际只到 2022-04** | 离散剖面   | ~20 KiB（每剖面一个文件）  | [docs/argo.md](docs/argo.md)       |
| [download_glorys.py](download_glorys.py)   | GLORYS12V1 再分析（默认南海） | 1993–2025                                     | 日均       | ~12.7 GB（南海全深度单年） | [docs/glorys.md](docs/glorys.md)   |

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
下之前务必先跑 `-n` 看流量。它也**不支持断点续传**——中断了当前那一块要重来，
但已下好的年份会跳过。详见 [docs/glorys.md](docs/glorys.md)。

## 备注

- 数据文件不入库，已在 [.gitignore](.gitignore) 排除
- 第三方依赖记在 [requirements.txt](requirements.txt)，注明所属脚本
- 数据源的细节和坑写进 `docs/<脚本名>.md`
