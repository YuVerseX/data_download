# data_download

各类科研数据集的下载脚本，一个数据源一个脚本。

## 脚本清单

| 脚本                                    | 数据集                       | 单文件体积 | 说明                            |
| --------------------------------------- | ---------------------------- | ---------- | ------------------------------- |
| [download_scsora.py](download_scsora.py) | SCSORA 逐日再分析 2001–2024 | ~172 GiB   | [docs/scsora.md](docs/scsora.md) |

## 用法

```powershell
conda create -n data_download python=3.12
conda activate data_download
pip install -r requirements.txt

python F:\Code\data_download\download_scsora.py 2001 -o "D:\数据目录"
```

环境只需建一次，之后每次用前 `conda activate data_download` 即可。

年份支持单个 `2001`、区间 `2001-2005` 或组合 `2001,2003-2005`；`-o` 指定输出目录，
省略则下到当前工作目录。中断后重跑同样的命令续传。

## 备注

- 数据文件不入库，已在 [.gitignore](.gitignore) 排除
- 第三方依赖记在 [requirements.txt](requirements.txt)，注明所属脚本
- 数据源的细节和坑写进 `docs/<脚本名>.md`
