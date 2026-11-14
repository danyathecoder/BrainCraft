"""eeg.gpype_bridge: pipeline wiring with a stub g.Pype, plus one real
g.Pype -> LSL -> EEGStream round trip (skipped without the gpype group)."""

from __future__ import annotations

import sys
import time

import pytest

from eeg import gpype_bridge as bridge


class StubGp:
    """Records which nodes are created and how they are connected."""

    def __init__(self):
        self.made: list[tuple[str, dict]] = []
        self.links: list[tuple[str, str]] = []
        gp = self

        class Node:
            def __init__(self, **kw):
                self.kind = type(self).__name__
                gp.made.append((self.kind, kw))

            def __repr__(self):
                return self.kind

        for name in ("HybridBlack", "Generator", "LSLSender", "CsvWriter",
                     "ChannelSelector", "Bandpass", "TimeSeriesScope"):
            setattr(self, name, type(name, (Node,), {}))

        class Pipeline:
            def connect(self, a, b):
                gp.links.append((a.kind, b.kind))

        self.Pipeline = Pipeline

    def kwargs(self, kind):
        return next(kw for k, kw in self.made if k == kind)


def build(argv, gp=None):
    gp = gp or StubGp()
    _, scope = bridge.build(gp, bridge.parse_args(argv))
    return gp, scope


def test_headset_streams_all_17_channels_in_unicorn_layout():
    gp, _ = build(["--headless"])
    hb = gp.kwargs("HybridBlack")
    assert hb["include_accel"] and hb["include_gyro"] and hb["include_aux"]
    assert hb["enable_oscar"] is False and hb["test_signal"] is False
    assert gp.kwargs("LSLSender")["stream_name"] == bridge.DEFAULT_NAME
    assert gp.links == [("HybridBlack", "LSLSender")]


def test_options_reach_the_nodes(tmp_path):
    gp, _ = build(["--headless", "--oscar", "--test-signal", "--serial", "UN-1",
                   "--name", "Other", "--record", str(tmp_path / "raw" / "s1")])
    hb = gp.kwargs("HybridBlack")
    assert hb["enable_oscar"] and hb["test_signal"] and hb["serial"] == "UN-1"
    assert gp.kwargs("LSLSender")["stream_name"] == "Other"
    assert gp.kwargs("CsvWriter")["file_name"].endswith("s1.csv")
    assert (tmp_path / "raw").is_dir()
    assert ("HybridBlack", "CsvWriter") in gp.links


def test_scope_shows_only_eeg_and_lsl_gets_raw_data():
    gp, scope = build([])
    assert scope is not None
    assert gp.kwargs("ChannelSelector")["indices"] == list(range(8))
    assert gp.links == [
        ("HybridBlack", "LSLSender"),
        ("HybridBlack", "ChannelSelector"),
        ("ChannelSelector", "Bandpass"),
        ("Bandpass", "TimeSeriesScope"),
    ]


def test_fake_uses_generator_with_17_channels():
    gp, _ = build(["--fake", "--no-scope"])
    assert gp.kwargs("Generator")["channel_count"] == bridge.N_DEVICE_CHANNELS
    assert not any(k == "HybridBlack" for k, _ in gp.made)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only guard")
def test_headless_headset_refused_on_macos():
    with pytest.raises(SystemExit, match="macOS"):
        bridge.main(["--headless"])


def test_real_gpype_round_trip():
    gp = pytest.importorskip("gpype")
    from eeg.stream import EEGStream

    name = f"BrainCraft-test-{time.time_ns()}"
    p, _ = bridge.build(gp, bridge.parse_args(["--fake", "--headless", "--name", name]))
    p.start()
    try:
        with EEGStream("lsl", name=name, timeout=8) as s:
            assert s.layout.eeg == tuple(range(8)) and s.layout.gyro == (11, 12, 13)
            assert s.wait_for_samples(250, timeout=8)
            assert abs(s.window(1.0)).max() > 0
    finally:
        p.stop()
        p.close()
