#!/usr/bin/env python3
"""Compatibility entry point for the ERA5 hourly heat flux download."""

from download_era5 import main as era5_main


def main(argv=None):
    return era5_main(argv, mode="flux")


if __name__ == "__main__":
    raise SystemExit(main())
