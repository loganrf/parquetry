"""Smoke tests of the Qt UI (run headless with the offscreen platform)."""

import functools
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QMessageBox  # noqa: E402

from parquetry.config import AggregationConfig, ProcessingConfig, TimeRange  # noqa: E402
from parquetry.processing import load_plot_data  # noqa: E402
from parquetry.ui import workers  # noqa: E402
from parquetry.ui.app import create_app  # noqa: E402
from parquetry.ui.batch_dialog import BatchDialog  # noqa: E402
from parquetry.ui.export_dialog import ExportDialog, path_to_template  # noqa: E402
from parquetry.ui.main_window import MainWindow  # noqa: E402

from .conftest import START  # noqa: E402

EPOCH_START = START.timestamp() if START.tzinfo else (START - START.__class__(1970, 1, 1)).total_seconds()


def idle(qtbot, timeout=15000):
    qtbot.waitUntil(lambda: not workers._active, timeout=timeout)
    qtbot.wait(50)
    qtbot.waitUntil(lambda: not workers._active, timeout=timeout)


@pytest.fixture
def settings(tmp_path):
    s = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    s.setValue("browser/last_dir", str(tmp_path))
    return s


@pytest.fixture
def window(qtbot, settings, monkeypatch):
    create_app()
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)
    for name in ("warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, name, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))
    win = MainWindow(settings)
    qtbot.addWidget(win)
    win.resize(1200, 800)
    win.show()
    yield win
    idle(qtbot)


@pytest.fixture
def explorer(qtbot, window, telemetry):
    window.open_file(telemetry)
    idle(qtbot)
    assert window.stack.currentWidget() is window.explorer
    return window.explorer


def test_starts_with_file_browser(window, telemetry, qtbot):
    assert window.stack.currentWidget() is window.browser
    window.browser.set_directory(telemetry.parent)
    window.browser._preview(telemetry)
    idle(qtbot)
    assert window.browser.summary_labels["Rows"].text() == "600"
    assert window.browser.open_button.isEnabled()


def test_open_file_plots_default_parameters(explorer):
    assert explorer.params.x() == "time"
    assert explorer.params.selected_y() == ["speed", "temp"]
    assert [s.name for s in explorer.plot.data.series] == ["speed", "temp"]
    assert len(explorer.plot._plots) == 2  # stacked by default


def test_parameter_and_aggregation_changes_reload(explorer, qtbot):
    explorer.params.set_selected_y(["rpm"])
    explorer.aggregation.set_config(AggregationConfig(method="interval", every="10s", functions=["mean", "min", "max"]))
    explorer.update_plot()
    idle(qtbot)
    assert [s.name for s in explorer.plot.data.series] == ["rpm_mean", "rpm_min", "rpm_max"]
    assert len(explorer.plot.data.series[0].x) == 6
    explorer.act_stacked.setChecked(False)
    assert len(explorer.plot._plots) == 1


def test_ranges_absolute_and_relative(explorer):
    explorer.plot.add_range(EPOCH_START + 10, EPOCH_START + 20)
    assert explorer.ranges_panel.table.rowCount() == 1
    cfg = explorer.current_config()
    assert cfg.ranges[0].start == "2024-01-01T00:00:10" and cfg.ranges[0].end == "2024-01-01T00:00:20"
    explorer.ranges_panel.set_mode("relative")
    cfg = explorer.current_config()
    assert (cfg.ranges[0].start, cfg.ranges[0].end) == (10.0, 20.0)


def test_edit_range_in_table(explorer):
    explorer.plot.add_range(EPOCH_START + 10, EPOCH_START + 20)
    explorer.ranges_panel.table.item(0, 3).setText("2024-01-01 00:00:30")
    explorer.ranges_panel.table.item(0, 1).setText("climb")
    entry = explorer.plot.ranges()[0]
    assert entry.hi == pytest.approx(EPOCH_START + 30)
    assert entry.label == "climb"
    explorer.ranges_panel.table.item(0, 2).setText("not a time")  # rejected, old value restored
    assert explorer.plot.ranges()[0].lo == pytest.approx(EPOCH_START + 10)
    explorer.ranges_panel.removeRequested.emit(0)
    assert explorer.plot.ranges() == []


def test_shift_drag_selects_range(explorer, qtbot):
    view = explorer.plot.glw
    plot = explorer.plot._plots[0]
    rect = plot.getViewBox().sceneBoundingRect()
    start = view.mapFromScene(rect.left() + rect.width() * 0.25, rect.center().y())
    end = view.mapFromScene(rect.left() + rect.width() * 0.5, rect.center().y())
    explorer.act_select.setChecked(True)
    viewport = view.viewport()
    qtbot.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    for i in range(1, 11):
        qtbot.mouseMove(viewport, start + (end - start) * i / 10)
    qtbot.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
    ranges = explorer.plot.ranges()
    assert len(ranges) == 1
    assert ranges[0].hi > ranges[0].lo
    assert len(ranges[0].regions) == 2  # one region per stacked plot


def test_apply_config_with_relative_ranges(explorer, qtbot):
    cfg = ProcessingConfig(
        x="time",
        y=["rpm", "missing"],
        range_mode="relative",
        ranges=[TimeRange(5, 15, "a")],
        aggregation=AggregationConfig(method="every_nth", n=2),
    )
    warnings = explorer.apply_config(cfg)
    idle(qtbot)
    assert warnings and "missing" in warnings[0]
    assert explorer.params.selected_y() == ["rpm"]
    assert explorer.aggregation.config().method == "every_nth"
    [entry] = explorer.plot.ranges()
    assert (entry.lo - EPOCH_START, entry.hi - EPOCH_START, entry.label) == (pytest.approx(5), pytest.approx(15), "a")
    assert explorer.current_config().ranges[0].to_dict() == {"start": 5.0, "end": 15.0, "label": "a"}


def test_detail_loading(explorer, qtbot, monkeypatch):
    monkeypatch.setattr("parquetry.ui.explorer.load_plot_data", functools.partial(load_plot_data, max_points=100, envelope_buckets=10))
    explorer.update_plot()
    idle(qtbot)
    assert explorer.plot.data.reduced and explorer.act_detail.isEnabled()
    before = len(explorer.plot.data.series[0].x)
    explorer.plot.set_x_view(EPOCH_START + 10, EPOCH_START + 12, padding=0)
    explorer.load_detail()
    idle(qtbot)
    assert len(explorer.plot.data.series[0].x) > before


def test_export_dialog(explorer, qtbot, settings, tmp_path):
    explorer.plot.add_range(EPOCH_START + 10, EPOCH_START + 20)
    dialog = ExportDialog(explorer.info, explorer.current_config(), settings)
    qtbot.addWidget(dialog)
    qtbot.waitUntil(lambda: dialog.preview.toPlainText() != "", timeout=10000)
    assert dialog.preview.toPlainText().startswith("time,speed,temp")
    estimate = int(dialog.estimate_label.text().split()[1].replace(",", ""))
    assert 95 <= estimate <= 105  # proportional estimate of the 101 rows in the range
    dialog.sep_combo.setCurrentIndex(dialog.sep_combo.findData(";"))
    dialog.columns_table.item(1, 1).setText("velocity")
    out = tmp_path / "exports" / "telemetry_selection.csv"
    dialog.path_edit.setText(str(out))
    cfg = dialog.config()
    assert cfg.csv.separator == ";" and cfg.csv.rename == {"speed": "velocity"}
    assert cfg.output.path == str(out.parent / "{stem}_selection.csv")
    dialog._export()
    idle(qtbot)
    assert out.read_text().splitlines()[0] == "time;velocity;temp"
    assert len(out.read_text().splitlines()) == 102


def test_batch_dialog(explorer, qtbot, settings, telemetry, tmp_path):
    copy = tmp_path / "copy.parquet"
    copy.write_bytes(telemetry.read_bytes())
    dialog = BatchDialog(settings, explorer.current_config(), [telemetry, copy])
    qtbot.addWidget(dialog)
    dialog.output_edit.setText(str(tmp_path / "batch" / "{stem}.csv"))
    dialog._run()
    idle(qtbot)
    assert "2 succeeded, 0 failed" in dialog.log.toPlainText()
    assert (tmp_path / "batch" / "copy.csv").exists()


def test_path_to_template(tmp_path):
    from parquetry.processing import render_output_path

    source = tmp_path / "flight.parquet"
    assert path_to_template(tmp_path / "flight_export.csv", source) == "{stem}_export.csv"
    assert path_to_template(tmp_path / "out" / "x.csv", source) == str(tmp_path / "out" / "x.csv")
    # only a leading stem is generalised; the stem elsewhere in the name stays literal
    assert path_to_template(tmp_path / "data.csv", tmp_path / "a.parquet") == "data.csv"
    # literal braces survive the template round trip
    odd = tmp_path / "exports {2024}" / "flight {x}.csv"
    assert render_output_path(path_to_template(odd, source), source) == odd


def test_save_config_action(explorer, settings, tmp_path, monkeypatch):
    target = tmp_path / "saved.json"
    monkeypatch.setattr(
        "parquetry.ui.export_dialog.QFileDialog.getSaveFileName", staticmethod(lambda *a, **k: (str(target), ""))
    )
    explorer.act_save_config.trigger()
    saved = ProcessingConfig.load(target)
    assert saved.x == "time" and saved.y == ["speed", "temp"]
    assert saved.source.endswith("telemetry.parquet")


def test_mouse_readout(explorer, qtbot):
    plot = explorer.plot._plots[0]
    center = plot.getViewBox().sceneBoundingRect().center()
    explorer.plot._on_mouse_moved((center,))
    assert "2024-01-01" in explorer.plot.readout.text()
    assert "speed" in explorer.plot.readout.text()


def test_changing_x_clears_stale_plot_and_ranges(explorer, qtbot):
    from parquetry.dataset import ROW_INDEX

    explorer.plot.add_range(EPOCH_START + 10, EPOCH_START + 20)
    explorer.ranges_panel.set_mode("relative")
    explorer.params.set_x(ROW_INDEX)
    assert explorer.plot.ranges() == [] and explorer.plot.data is None  # nothing to select on until reloaded
    explorer.plot.add_range(10, 20)
    assert explorer.current_config().ranges[0].to_dict() == {"start": 10.0, "end": 20.0}
    idle(qtbot)
    qtbot.waitUntil(lambda: explorer.plot.data is not None and explorer.plot.data.x_kind == "numeric", timeout=10000)


def test_config_ranges_survive_interaction_during_load(explorer, qtbot):
    cfg = ProcessingConfig(x="time", y=["speed"], range_mode="relative", ranges=[TimeRange(5, 15)])
    explorer.apply_config(cfg)  # starts a load that resolves the ranges
    assert explorer.current_config().ranges[0].to_dict() == {"start": 5, "end": 15}  # before they are drawn
    explorer.params.set_selected_y(["speed", "rpm"])  # supersedes that load
    explorer.update_plot()
    idle(qtbot)
    assert len(explorer.plot.ranges()) == 1
    assert explorer.current_config().ranges[0].to_dict() == {"start": 5.0, "end": 15.0}


def test_pending_ranges_do_not_leak_into_next_file(window, explorer, qtbot, numeric_x):
    cfg = ProcessingConfig(x="time", y=["speed"], ranges=[TimeRange("2024-01-01T00:00:05", None)],
                           aggregation=AggregationConfig(method="interval", every="nonsense"))
    explorer.apply_config(cfg)  # invalid interval: the load never starts
    errors = []
    explorer.error.connect(lambda title, text: errors.append(text))
    window.open_file(numeric_x)
    idle(qtbot)
    assert errors == []
    assert explorer.info.path == numeric_x
    assert explorer.plot.ranges() == [] and explorer.plot.data.series


def test_every_nth_of_one_is_kept(explorer):
    explorer.aggregation.set_config(AggregationConfig(method="every_nth", n=1))
    assert explorer.aggregation.config().n == 1


def test_os_file_open_event(window, qtbot, telemetry):
    from PySide6.QtGui import QFileOpenEvent
    from PySide6.QtWidgets import QApplication

    from parquetry.ui.app import FileOpenHandler

    handler = FileOpenHandler(window)
    QApplication.instance().installEventFilter(handler)
    try:
        QApplication.sendEvent(QApplication.instance(), QFileOpenEvent(str(telemetry)))
        idle(qtbot)
        assert window.explorer.info is not None and window.explorer.info.path == telemetry
    finally:
        QApplication.instance().removeEventFilter(handler)
