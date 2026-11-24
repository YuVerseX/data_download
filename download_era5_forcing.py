#!/usr/bin/env python3
"""Compatibility entry point for CDS ERA5 daily forcing downloads."""

from download_era5 import main as era5_main


def main(argv=None):
    return era5_main(argv, mode="cds-daily")


if __name__ == "__main__":
    raise SystemExit(main())
