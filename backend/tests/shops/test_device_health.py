from pathlib import Path

from backend.app.adapters.android_device import (
    DEFAULT_SELECTOR_PROFILE_VERSION,
    AndroidDeviceAdapter,
)


class _FakeAdbDevice:
    def __init__(self, serial: str, state: str = "device") -> None:
        self.serial = serial
        self._state = state

    def get_state(self) -> str:
        return self._state


class _FakeAdbClient:
    def __init__(self, devices: list[_FakeAdbDevice]) -> None:
        self._devices = devices

    def device_list(self) -> list[_FakeAdbDevice]:
        return self._devices


class _FakeU2Device:
    def __init__(self, package: str, activity: str) -> None:
        self._foreground = {"package": package, "activity": activity, "pid": 17}

    def app_current(self) -> dict[str, object]:
        return dict(self._foreground)


def _adapter(
    tmp_path: Path,
    *,
    devices: list[_FakeAdbDevice],
    foreground_package: str = "com.xingin.xhs",
) -> AndroidDeviceAdapter:
    return AndroidDeviceAdapter(
        runtime_dir=tmp_path / "runtime",
        adb_client_factory=lambda: _FakeAdbClient(devices),
        u2_connector=lambda _: _FakeU2Device(
            foreground_package, "com.xingin.xhs.activity.ShopDetail"
        ),
        executable_resolver=lambda _: "C:/Android/platform-tools/adb.exe",
    )


def test_device_health_reports_disconnected_without_connecting_uiautomator(
    tmp_path: Path,
) -> None:
    """No ADB device is unavailable fact, never an inferred healthy device."""
    connected = False

    def connect(_: str) -> _FakeU2Device:
        nonlocal connected
        connected = True
        return _FakeU2Device("com.xingin.xhs", "ShopDetail")

    adapter = AndroidDeviceAdapter(
        runtime_dir=tmp_path / "runtime",
        adb_client_factory=lambda: _FakeAdbClient([]),
        u2_connector=connect,
        executable_resolver=lambda _: "C:/Android/platform-tools/adb.exe",
    )

    health = adapter.health()

    assert health.status == "unavailable"
    assert health.device_id is None
    assert health.detail == "device_disconnected"
    assert health.raw_evidence["devices"] == []
    assert health.raw_evidence["selector_profile_version"] == (
        DEFAULT_SELECTOR_PROFILE_VERSION
    )
    assert connected is False


def test_device_health_reports_offline_selected_device_as_disconnected(
    tmp_path: Path,
) -> None:
    """An ADB row in offline state is still disconnected for collection purposes."""
    health = _adapter(
        tmp_path, devices=[_FakeAdbDevice("phone-1", state="offline")]
    ).health()

    assert health.status == "unavailable"
    assert health.device_id == "phone-1"
    assert health.detail == "device_disconnected"
    assert health.raw_evidence["devices"] == [
        {"device_id": "phone-1", "state": "offline"}
    ]


def test_device_health_reports_wrong_foreground_app_for_human_action(
    tmp_path: Path,
) -> None:
    """A connected phone in another app must not be declared ready for XHS collection."""
    health = _adapter(
        tmp_path,
        devices=[_FakeAdbDevice("phone-1")],
        foreground_package="com.example.other",
    ).health()

    assert health.status == "needs_human"
    assert health.device_id == "phone-1"
    assert health.detail == "wrong_foreground_app"
    assert health.raw_evidence["foreground"]["package"] == "com.example.other"
    assert health.raw_evidence["expected_package"] == "com.xingin.xhs"


def test_device_health_reports_selected_profile_version_when_available(
    tmp_path: Path,
) -> None:
    """A ready-device fact is bound to the selector profile that was actually checked."""
    health = _adapter(tmp_path, devices=[_FakeAdbDevice("phone-1")]).health()

    assert health.status == "available"
    assert health.device_id == "phone-1"
    assert health.detail == "ready"
    assert health.raw_evidence["selector_profile_version"] == (
        DEFAULT_SELECTOR_PROFILE_VERSION
    )
