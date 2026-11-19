"""VSS 路径清单（M1）：全部名称来自实测 169 路径 JSON（12_dynamic_vehicle_state.json），
经 scripts/gen_paths.py 提取核对，无一猜测。

档位（方案 §6.3）：
  T1    实时全量批（awake 态 60s）
  T1loc 行驶定位微批（10s）
  T1chg 充电采样（30s，路径含于 T1，供采样器挑用）
  T2    配置档（1h）
  T0    休眠轻探测（3 路径）
"""

from __future__ import annotations

# ---------------------------------------------------------------- 状态判定核心
P_ONLINE_5G = "Vehicle.ConnectManager.ConnectStatus.5G"
P_ONLINE_XCU = "Vehicle.ConnectManager.ConnectStatus.xcu"
P_ONLINE_HUF = "Vehicle.ConnectManager.ConnectStatus.hu-f"
P_LOCATION = "Vehicle.Location.CurrentLocationInfo"   # {lat,lon,spd,dir,alt,hdop,sat,utc,v}
P_TRAVEL_STATUS = "Vehicle.Cabin.TravelStatus"        # 行驶状态
P_TRIP_TOTAL = "Vehicle.Carcenter.Trip.Total"          # 里程计数器（accMileage 等）
P_CHARGE_STATUS = "Vehicle.Powertrain.Battery.ChargeStatus"
P_SOC = "Vehicle.Powertrain.Battery.ResidueBattery"

# ---------------------------------------------------------------- T1loc（10s 定位微批）
T1_LOC_PATHS = [P_LOCATION, P_TRAVEL_STATUS, P_TRIP_TOTAL]

# ---------------------------------------------------------------- T0（休眠轻探测）
T0_PROBE_PATHS = [P_ONLINE_5G, P_ONLINE_XCU, P_ONLINE_HUF]

# ---------------------------------------------------------------- T1chg（充电采样挑选）
T1_CHARGE_PATHS = [
    P_SOC,
    P_CHARGE_STATUS,
    "Vehicle.Powertrain.Battery.CLTCChargePower",
    "Vehicle.Powertrain.Battery.WLTCChargePower",
    "Vehicle.Powertrain.Battery.ACChargeCurrent",
    "Vehicle.Powertrain.Battery.ACChargeVoltage",
    "Vehicle.Powertrain.Battery.OGCChargeCurrent",
    "Vehicle.Powertrain.Battery.OGCChargeVoltage",
    "Vehicle.Powertrain.Battery.ChargeSurplusTime",
    "Vehicle.Powertrain.Battery.DCChrgngGunActuSts",        # 直流枪连接
    "Vehicle.Powertrain.Battery.ACChgrActualConnSts",      # 交流枪连接
]

# ---------------------------------------------------------------- T1（实时全量批，awake 60s）
T1_PATHS: list[str] = [
    # 电池/充电
    *T1_CHARGE_PATHS,
    "Vehicle.Powertrain.Battery.ChargeFaults",
    "Vehicle.Powertrain.Battery.DischargeStatus",
    "Vehicle.Powertrain.ChargingPile.ChargingLimit",
    "Vehicle.Powertrain.ChargingPile.OGCType",
    "Vehicle.Powertrain.ChargingPile.ScheduledCharging.State",
    "Vehicle.Powertrain.ChargingPile.ScheduledCharging.Switch",
    # 在线/位置/里程/行驶
    *T0_PROBE_PATHS,
    P_LOCATION,
    P_TRAVEL_STATUS,
    P_TRIP_TOTAL,
    # 门/锁/窗/后视镜/充电口盖
    "Vehicle.Body.DoorLockStatus.MainDoor",
    "Vehicle.Body.DoorLockStatus.CopilotDoor",
    "Vehicle.Body.DoorLockStatus.BackLeftDoor",
    "Vehicle.Body.DoorLockStatus.BackRightDoor",
    "Vehicle.Body.DoorLockStatus.TrunkDoor",
    "Vehicle.Body.DoorSwitchStatus.MainDoor",
    "Vehicle.Body.DoorSwitchStatus.CopilotDoor",
    "Vehicle.Body.DoorSwitchStatus.BackLeftDoor",
    "Vehicle.Body.DoorSwitchStatus.BackRightDoor",
    "Vehicle.Body.DoorSwitchStatus.TrunkDoor",
    "Vehicle.Body.DoorSwitchStatus.ChrgPorLidSts",
    "Vehicle.Body.WindowPosition.MainWindow",
    "Vehicle.Body.WindowPosition.CopilotWindow",
    "Vehicle.Body.WindowPosition.BackLeftWindow",
    "Vehicle.Body.WindowPosition.BackRightWindow",
    "Vehicle.Body.RearMirro.LRearMirro",
    "Vehicle.Body.RearMirro.RRearMirro",
    "Vehicle.Body.RearMirro.LHeatSts",
    "Vehicle.Body.RearMirro.RHeatSts",
    # 空调/温控/座椅/冰箱
    "Vehicle.Cabin.AC.SetTemp",
    "Vehicle.Cabin.AC.FrtACIncarTemp",
    "Vehicle.Cabin.AC.WindMode",
    "Vehicle.Cabin.AC.ExSpeedStatus",
    "Vehicle.Cabin.AC.DefrostModeStatus",
    "Vehicle.VehInfo.CarControl.Hvac.DspOtsdTemp",        # 车外温度
    "Vehicle.Cabin.Seat.FLSeatHeatState",
    "Vehicle.Cabin.Seat.FRSeatHeatState",
    "Vehicle.Cabin.Seat.SLSeatHeatState",
    "Vehicle.Cabin.Seat.FLSeatVentilationState",
    "Vehicle.Cabin.Seat.FRSeatVentilationState",
    "Vehicle.Cabin.Fridge.DlyTmRemain",
    # 胎压/胎温（实测名 RL/RR）
    "Vehicle.Chassis.Tire.FLTirePressure",
    "Vehicle.Chassis.Tire.FRTirePressure",
    "Vehicle.Chassis.Tire.RLTirePressure",
    "Vehicle.Chassis.Tire.RRTirePressure",
    "Vehicle.Chassis.Tire.FLTireTemp",
    "Vehicle.Chassis.Tire.FRTireTemp",
    "Vehicle.Chassis.Tire.RLTireTemp",
    "Vehicle.Chassis.Tire.RRTireTemp",
    "Vehicle.Chassis.Tire.TPMSSysSts",
    # 哨兵/低电量/续航
    "Vehicle.Sentry.SentinelStatus",
    "Vehicle.Cabin.LowBatteryMode",
    "Vehicle.Cabin.CLTC.PureElecEnduranceMileInd",
    "Vehicle.Cabin.CLTC.FuelEnduranceMileInd",
    "Vehicle.Body.Light.LicLghtSts",
]

# ---------------------------------------------------------------- T2（配置档，1h）
T2_PATHS: list[str] = [
    "Vehicle.CarSettings.Privacy.PosService",
    "Vehicle.CarSettings.SceneMode.ModeState",
    "Vehicle.CarSettings.Xmode.ReserveFridge",
    "Vehicle.CarSettings.MoveOffOnTime.Xmode",
    "Vehicle.Cabin.RmtVirtualKeyAuthSts",
    "Vehicle.Sentry.SettingsStatus",
    "Vehicle.Body.SunShade.FrontPos",
    "Vehicle.Body.SunShade.RearPos",
]

# 观察期：全采（T1∪T2∪loc∪chg∪T0 去重）
OBSERVE_PATHS = sorted(set(T1_PATHS + T2_PATHS + T1_LOC_PATHS
                           + T1_CHARGE_PATHS + T0_PROBE_PATHS))


def batched(paths: list[str], size: int = 50) -> list[list[str]]:
    """分批（服务端对任一无效 path 整批 400 —— ha-lixiang 实测，批 ≤50）。"""
    return [paths[i:i + size] for i in range(0, len(paths), size)]
