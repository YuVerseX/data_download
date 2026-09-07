# data_download

各类科研数据集的下载脚本，一个数据源一个脚本。

## 脚本清单

| 脚本 | 数据集 | 单文件体积 | 说明 |
| --- | --- | --- | --- |
| [download_scsora.py](download_scsora.py) | SCSORA 逐日再分析 2001–2024 | ~172 GiB | [docs/scsora.md](docs/scsora.md) |

## 用法

```bash
pip install -r requirements.txt

cd D:\数据目录
python F:\Code\data_download\download_scsora.py 2001
```

下载到**当前工作目录**，中断后重跑同样的命令续传。

## 备注

- 数据文件不入库，已在 [.gitignore](.gitignore) 排除
- 第三方依赖记在 [requirements.txt](requirements.txt)，注明所属脚本
- 数据源的细节和坑写进 `docs/<脚本名>.md`
