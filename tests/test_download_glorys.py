from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import copernicusmarine
import numpy as np
import xarray as xr

import download_glorys as glorys


class GlorysDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.options = glorys.Options(
            bbox=(100.0, 180.0, 0.0, 60.0),
            variables=["zos"],
            min_depth=None,
            max_depth=None,
            compression=1,
        )

    def write_period(self, path: Path, start: date, end: date,
                     value: float = 1.0,
                     longitudes: list[float] | None = None) -> None:
        longitudes = longitudes or [120.0, 121.0]
        times = glorys.expected_dates(start, end)
        dataset = xr.Dataset(
            {
                "zos": (
                    ("time", "latitude", "longitude"),
                    np.full((len(times), 1, len(longitudes)), value, dtype="float32"),
                )
            },
            coords={
                "time": times,
                "latitude": [10.0],
                "longitude": longitudes,
            },
        )
        dataset.to_netcdf(path, engine="netcdf4")

    def test_month_bounds_handles_leap_year(self) -> None:
        self.assertEqual(
            glorys.month_bounds(2000, 2),
            (date(2000, 2, 1), date(2000, 2, 29)),
        )
        self.assertEqual(
            glorys.month_bounds(2001, 2),
            (date(2001, 2, 1), date(2001, 2, 28)),
        )

    def test_request_cache_key_isolates_request_parameters(self) -> None:
        same_data_different_compression = glorys.Options(
            bbox=self.options.bbox,
            variables=["zos"],
            min_depth=None,
            max_depth=None,
            compression=9,
        )
        other_bbox = glorys.Options(
            bbox=(101.0, 180.0, 0.0, 60.0),
            variables=["zos"],
            min_depth=None,
            max_depth=None,
            compression=1,
        )
        self.assertEqual(
            glorys.request_cache_key(self.options),
            glorys.request_cache_key(same_data_different_compression),
        )
        self.assertNotEqual(
            glorys.request_cache_key(self.options),
            glorys.request_cache_key(other_bbox),
        )

    def test_verify_period_rejects_incomplete_time_axis(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "partial.nc"
            self.write_period(path, date(2001, 1, 1), date(2001, 1, 30))
            problem = glorys.verify_period(
                path, date(2001, 1, 1), date(2001, 1, 31), ["zos"])
        self.assertIn("完整、递增且无重复", problem or "")

    def test_monthly_files_requires_months_even_if_year_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output_dir = Path(raw)
            annual = output_dir / glorys.FILENAME.format(year=2001)
            self.write_period(annual, date(2001, 1, 1), date(2001, 12, 31))

            todo, skipped = glorys.pending_years(
                [2001], output_dir, self.options, overwrite=False,
                strategy="monthly-files")

        self.assertEqual(todo, [2001])
        self.assertEqual(skipped, [])

    def test_download_month_reuses_valid_part(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parts_dir = Path(raw)
            start, end = glorys.month_bounds(2001, 1)
            part = parts_dir / glorys.PART_FILENAME.format(year=2001, month=1)
            self.write_period(part, start, end)
            with mock.patch.object(
                    copernicusmarine, "subset",
                    side_effect=AssertionError("不应重新下载有效分片")):
                result = glorys.download_month(
                    2001, 1, parts_dir, self.options, retries=3, workers=2)
        self.assertEqual(result, (1, "exists", ""))

    def test_download_month_retries_after_file_lock_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            parts_dir = Path(raw)
            start, end = glorys.month_bounds(2001, 1)
            calls = 0

            def subset(**kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    error = PermissionError(
                        glorys.errno.EACCES, "另一个程序正在使用此文件")
                    error.winerror = 32
                    raise error
                target = Path(kwargs["output_directory"]) / kwargs["output_filename"]
                self.write_period(target, start, end)
                return SimpleNamespace(status="000", message="ok")

            with (mock.patch.object(copernicusmarine, "subset", side_effect=subset),
                  mock.patch.object(glorys.time, "sleep")):
                result = glorys.download_month(
                    2001, 1, parts_dir, self.options, retries=3, workers=2)

        self.assertEqual(calls, 2)
        self.assertEqual(result, (1, "done", ""))

    def test_windows_error_classification(self) -> None:
        for winerror in (32, 33):
            with self.subTest(winerror=winerror):
                error = PermissionError(glorys.errno.EACCES, "文件被占用")
                error.winerror = winerror
                self.assertFalse(glorys.is_nonretryable_local_error(error))

        access_denied = PermissionError(glorys.errno.EACCES, "拒绝访问")
        access_denied.winerror = 5
        self.assertTrue(glorys.is_nonretryable_local_error(access_denied))

    def test_download_month_retries_locked_final_publish(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            staging_dir = root / "staging"
            final_dir = root / "2001"
            staging_dir.mkdir()
            final_dir.mkdir()
            start, end = glorys.month_bounds(2001, 1)
            subset_calls = 0
            replace_calls = 0
            original_replace = Path.replace

            def subset(**kwargs):
                nonlocal subset_calls
                subset_calls += 1
                target = Path(kwargs["output_directory"]) / kwargs["output_filename"]
                self.write_period(target, start, end)
                return SimpleNamespace(status="000", message="ok")

            def replace(source, target):
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls == 1:
                    error = PermissionError(glorys.errno.EACCES, "拒绝访问")
                    error.winerror = 5
                    raise error
                return original_replace(source, target)

            with (mock.patch.object(copernicusmarine, "subset", side_effect=subset),
                  mock.patch.object(Path, "replace", autospec=True, side_effect=replace),
                  mock.patch.object(glorys.time, "sleep")):
                result = glorys.download_month(
                    2001, 1, staging_dir, self.options, retries=3, workers=1,
                    final_dir=final_dir)

        self.assertEqual(result, (1, "done", ""))
        self.assertEqual(subset_calls, 1)
        self.assertEqual(replace_calls, 2)

    def test_download_month_does_not_retry_access_denied(self) -> None:
        error = PermissionError(glorys.errno.EACCES, "拒绝访问")
        error.winerror = 5
        with tempfile.TemporaryDirectory() as raw:
            with (mock.patch.object(
                    copernicusmarine, "subset", side_effect=error) as subset,
                  mock.patch.object(glorys.time, "sleep") as sleep):
                result = glorys.download_month(
                    2001, 1, Path(raw), self.options, retries=3, workers=2)

        self.assertEqual(subset.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(result[1], "failed")
        self.assertIn("拒绝访问", result[2])

    def test_download_month_does_not_retry_disk_full(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            with (mock.patch.object(
                    copernicusmarine, "subset",
                    side_effect=OSError(glorys.errno.ENOSPC, "磁盘空间不足")) as subset,
                  mock.patch.object(glorys.time, "sleep") as sleep):
                result = glorys.download_month(
                    2001, 1, Path(raw), self.options, retries=3, workers=2)

        self.assertEqual(subset.call_count, 1)
        sleep.assert_not_called()
        self.assertEqual(result[1], "failed")
        self.assertIn("磁盘空间不足", result[2])

    def test_download_month_stages_output_and_preserves_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            staging_dir = root / "staging"
            final_dir = root / "2001"
            staging_dir.mkdir()
            final_dir.mkdir()
            name = glorys.PART_FILENAME.format(year=2001, month=1)
            sidecar = final_dir / f"{name}.sha256"
            sidecar.write_text("keep", encoding="ascii")
            start, end = glorys.month_bounds(2001, 1)

            def subset(**kwargs):
                target = Path(kwargs["output_directory"]) / kwargs["output_filename"]
                self.assertEqual(target.parent, staging_dir)
                self.write_period(target, start, end)
                return SimpleNamespace(status="000", message="ok")

            with mock.patch.object(copernicusmarine, "subset", side_effect=subset):
                result = glorys.download_month(
                    2001, 1, staging_dir, self.options, retries=1, workers=1,
                    final_dir=final_dir)
            sidecar_value = sidecar.read_text(encoding="ascii")
            final_exists = (final_dir / name).exists()

        self.assertEqual(result, (1, "done", ""))
        self.assertEqual(sidecar_value, "keep")
        self.assertTrue(final_exists)

    def test_download_month_force_replaces_valid_final(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            staging_dir = root / "staging"
            final_dir = root / "2001"
            staging_dir.mkdir()
            final_dir.mkdir()
            start, end = glorys.month_bounds(2001, 1)
            final = final_dir / glorys.PART_FILENAME.format(year=2001, month=1)
            self.write_period(final, start, end, value=1.0)

            def subset(**kwargs):
                target = Path(kwargs["output_directory"]) / kwargs["output_filename"]
                self.write_period(target, start, end, value=2.0)
                return SimpleNamespace(status="000", message="ok")

            with mock.patch.object(copernicusmarine, "subset", side_effect=subset) as call:
                result = glorys.download_month(
                    2001, 1, staging_dir, self.options, retries=1, workers=1,
                    final_dir=final_dir, force=True)

            with xr.open_dataset(final) as dataset:
                value = float(dataset["zos"][0, 0, 0])

        self.assertEqual(result, (1, "done", ""))
        self.assertEqual(call.call_count, 1)
        self.assertEqual(value, 2.0)

    def test_download_month_honors_preexisting_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cancelled = glorys.threading.Event()
            cancelled.set()
            with mock.patch.object(
                    copernicusmarine, "subset",
                    side_effect=AssertionError("取消后不应开始下载")):
                result = glorys.download_month(
                    2001, 1, Path(raw), self.options, retries=3, workers=2,
                    cancel_event=cancelled)

        self.assertEqual(result, (1, "failed", "已取消"))

    def test_download_monthly_year_updates_aggregate_progress(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            output_dir = root / "output"
            tmp_dir = output_dir / glorys.TMP_DIRNAME
            output_dir.mkdir()
            tmp_dir.mkdir()
            progress = mock.MagicMock()
            progress.disable = False

            def finish_merge(year, parts, target, merge_tmp_dir, options):
                target.touch()
                return None

            with (mock.patch("tqdm.auto.tqdm", return_value=progress) as tqdm_mock,
                  mock.patch.object(
                      glorys, "download_month",
                      side_effect=lambda year, month, *_: (month, "done", "")),
                  mock.patch.object(
                      glorys, "merge_months", side_effect=finish_merge)):
                outcome = glorys.download_monthly_year(
                    2001, output_dir, tmp_dir, self.options, workers=3,
                    retries=3, keep_monthly=True, label="[1/1]")

        self.assertEqual(outcome.status, "done")
        tqdm_mock.assert_called_once_with(
            total=12,
            desc="[1/1] 2001",
            unit="月",
            dynamic_ncols=True,
            disable=not glorys.sys.stderr.isatty(),
            file=glorys.sys.stderr,
        )
        self.assertEqual(progress.update.call_count, 12)
        progress.close.assert_called_once_with()

    def test_monthly_files_promotes_cache_without_merging(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            output_dir = Path(raw) / "output"
            tmp_dir = output_dir / glorys.TMP_DIRNAME
            cache_parts_dir = (
                tmp_dir / glorys.PARTS_DIRNAME
                / glorys.request_cache_key(self.options) / "2001"
            )
            cache_parts_dir.mkdir(parents=True)
            for month in range(1, 13):
                start, end = glorys.month_bounds(2001, month)
                part = cache_parts_dir / glorys.PART_FILENAME.format(
                    year=2001, month=month)
                self.write_period(part, start, end, float(month))

            existing = [(month, "exists", "") for month in range(1, 13)]
            with (mock.patch.object(
                      glorys, "run_month_downloads", return_value=existing),
                  mock.patch.object(glorys, "merge_months") as merge_mock):
                outcome = glorys.download_month_files_year(
                    2001, output_dir, tmp_dir, self.options,
                    workers=2, retries=3, overwrite=False, label="[1/1]")

            final_dir = output_dir / "2001"
            self.assertEqual(outcome.status, "done")
            self.assertEqual(len(list(final_dir.glob("*.nc"))), 12)
            self.assertFalse(cache_parts_dir.exists())
            todo, skipped = glorys.pending_years(
                [2001], output_dir, self.options, overwrite=False,
                strategy="monthly-files")
            self.assertEqual(todo, [])
            self.assertEqual([item.status for item in skipped], ["exists"])
            merge_mock.assert_not_called()

    def test_merge_months_rejects_mismatched_coordinates(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            parts: list[Path] = []
            for month in range(1, 13):
                start, end = glorys.month_bounds(2001, month)
                part = root / glorys.PART_FILENAME.format(year=2001, month=month)
                longitudes = [120.0, 122.0] if month == 2 else [120.0, 121.0]
                self.write_period(part, start, end, longitudes=longitudes)
                parts.append(part)

            target = root / glorys.FILENAME.format(year=2001)
            problem = glorys.merge_months(2001, parts, target, root, self.options)

            self.assertIsNotNone(problem)
            self.assertFalse(target.exists())

    def test_merge_read_error_deletes_only_damaged_parts(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            good = root / "good.nc"
            damaged = root / "damaged.nc"
            good.touch()
            damaged.touch()
            target = root / glorys.FILENAME.format(year=2001)
            with (mock.patch.object(
                      xr, "open_mfdataset", side_effect=RuntimeError("NetCDF: HDF error")),
                  mock.patch.object(
                      glorys, "verify_data_readable", side_effect=[None, "HDF error"])):
                problem = glorys.merge_months(
                    2001, [good, damaged], target, root, self.options)

            self.assertIn("已删除损坏分片", problem or "")
            self.assertTrue(good.exists())
            self.assertFalse(damaged.exists())

    def test_merge_months_builds_valid_leap_year(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            parts: list[Path] = []
            for month in range(1, 13):
                start, end = glorys.month_bounds(2000, month)
                part = root / glorys.PART_FILENAME.format(year=2000, month=month)
                self.write_period(part, start, end, float(month))
                parts.append(part)

            target = root / glorys.FILENAME.format(year=2000)
            problem = glorys.merge_months(2000, parts, target, root, self.options)

            self.assertIsNone(problem)
            self.assertIsNone(glorys.verify(target, 2000, ["zos"]))
            with xr.open_dataset(target) as dataset:
                self.assertEqual(dataset.sizes["time"], 366)
                self.assertEqual(float(dataset["zos"][0, 0, 0]), 1.0)
                self.assertEqual(float(dataset["zos"][-1, 0, 0]), 12.0)


if __name__ == "__main__":
    unittest.main()
