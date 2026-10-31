import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from deviation_meter.ui import MainWindow  # noqa: E402


def test_unkey_ptt_treats_false_readback_as_success(monkeypatch) -> None:
    calls: list[bool] = []

    class FakeRigctldClient:
        def __init__(self, host: str, port: int) -> None:
            del host, port

        def set_ptt(self, enabled: bool) -> bool:
            calls.append(enabled)
            return False

    monkeypatch.setattr("deviation_meter.ui.RigctldClient", FakeRigctldClient)
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    try:
        window._ptt_keyed = True
        window._ptt_timer.start(1_000)
        assert window._unkey_ptt() is True
        assert calls == [False]
        assert window._ptt_keyed is False
    finally:
        window.close()
        app.processEvents()


def test_auto_tab_switches_to_direwolf_source(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    original_generator = window.auto_generator_combo.currentData()
    original_path = window.direwolf_config_edit.text()
    original_tone = window.direwolf_tone_combo.currentData()
    original_source = window.source_combo.currentData()
    try:
        window.source_combo.setCurrentIndex(window.source_combo.findData("live"))
        window.auto_generator_combo.setCurrentIndex(
            window.auto_generator_combo.findData("direwolf")
        )
        app.processEvents()
        assert window.direwolf_bar.isHidden() is False
        assert window.auto_start_button.isEnabled() is False

        config = tmp_path / "direwolf.conf"
        config.write_text(
            "ADEVICE digirig-ts2000\nACHANNELS 1\nMODEM 1200\n",
            encoding="utf-8",
        )
        window.direwolf_config_edit.setText(str(config))
        window._refresh_direwolf_info()
        assert window._direwolf_profile_error is None
        assert "1.2 kHz" in window.direwolf_info_label.text()
        assert window.auto_start_button.isEnabled() is True
    finally:
        window.auto_generator_combo.setCurrentIndex(
            window.auto_generator_combo.findData(original_generator)
        )
        window.direwolf_config_edit.setText(original_path)
        window.direwolf_tone_combo.setCurrentIndex(
            window.direwolf_tone_combo.findData(original_tone)
        )
        window.source_combo.setCurrentIndex(
            window.source_combo.findData(original_source)
        )
        window.close()
        app.processEvents()


def test_auto_tab_switches_to_xara_source() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    original_generator = window.auto_generator_combo.currentData()
    original_source = window.source_combo.currentData()
    original_host = window.xara_host_edit.text()
    original_port = window.xara_port_spin.value()
    try:
        window.source_combo.setCurrentIndex(window.source_combo.findData("live"))
        window.auto_generator_combo.setCurrentIndex(
            window.auto_generator_combo.findData("xara")
        )
        app.processEvents()
        assert window.xara_bar.isHidden() is False
        assert window.direwolf_bar.isHidden() is True
        assert window.auto_tone_spin.value() == 1.5
        assert window.auto_tone_spin.isEnabled() is False
        assert window.auto_start_button.isEnabled() is True
        assert "127.0.0.1:8300" in window.xara_info_label.text()
    finally:
        window.auto_generator_combo.setCurrentIndex(
            window.auto_generator_combo.findData(original_generator)
        )
        window.source_combo.setCurrentIndex(
            window.source_combo.findData(original_source)
        )
        window.xara_host_edit.setText(original_host)
        window.xara_port_spin.setValue(original_port)
        window.close()
        app.processEvents()


def test_auto_tab_switches_to_target_method() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    original_method = window.auto_method_combo.currentData()
    original_source = window.source_combo.currentData()
    try:
        window.source_combo.setCurrentIndex(window.source_combo.findData("live"))
        window.auto_method_combo.setCurrentIndex(
            window.auto_method_combo.findData("target")
        )
        app.processEvents()
        assert window.auto_null_combo.isHidden() is True
        assert window.auto_target_spin.isHidden() is False
        assert window.auto_tolerance_spin.isHidden() is False
        assert window.auto_start_button.isEnabled() is True
        assert window.profile_combo.findData("generic-fm") >= 0
    finally:
        window.auto_method_combo.setCurrentIndex(
            window.auto_method_combo.findData(original_method)
        )
        window.source_combo.setCurrentIndex(
            window.source_combo.findData(original_source)
        )
        window.close()
        app.processEvents()


def test_main_window_builds_expected_tabs_and_meter() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    try:
        assert window.tabs.count() == 2
        assert window.tabs.tabText(0) == "Measure"
        assert window.tabs.tabText(1) == "Auto-Calibrate"
        assert window.frequency_edit.text() == "443.457000"
        assert window.meter.minimumWidth() >= 480
        assert window.worker_is_running() is False

        window.resize(1280, 720)
        app.processEvents()
        small_font = window.digital.value_label.font().pixelSize()
        small_scale = window._ui_scale
        window.resize(1900, 1000)
        app.processEvents()
        large_font = window.digital.value_label.font().pixelSize()
        assert window._ui_scale > small_scale
        assert large_font > small_font

        window.mode_combo.setCurrentIndex(window.mode_combo.findData("bessel"))
        assert window.bessel_bar.isHidden() is False
        assert window.profile_combo.isHidden() is True
        window.mode_combo.setCurrentIndex(window.mode_combo.findData("digital"))
        assert window.bessel_bar.isHidden() is True
        assert window.profile_combo.isHidden() is False

        assert window.auto_start_button.isHidden() is False
        assert window.auto_cancel_button.isEnabled() is False
        assert window.auto_save_button.isEnabled() is False
        window.source_combo.setCurrentIndex(
            window.source_combo.findData("simulator")
        )
        app.processEvents()
        assert window.auto_start_button.isEnabled() is True
        window.source_combo.setCurrentIndex(window.source_combo.findData("live"))
        app.processEvents()
        assert window.auto_start_button.isEnabled() is True
        window.source_combo.setCurrentIndex(
            window.source_combo.findData("simulator")
        )
        app.processEvents()
    finally:
        window.close()
        app.processEvents()
