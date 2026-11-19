"""VSS 路径清单生成器：从实测 169 路径 JSON 提取真实名称，替代手写猜测。

用法：python3 scripts/gen_paths.py <12_dynamic_vehicle_state.json> > liam/collector/paths_gen.json
观察期之前先跑一次，把手写 paths.py 中的猜测名（胎压 BL/BR 等）换成实测名。
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict


def main() -> int:
    src = sys.argv[1]
    data = json.load(open(src))
    groups: dict[str, list[str]] = defaultdict(list)
    for item in data:
        p = item.get("path", "")
        if not p:
            continue
        # 按三级域分组
        key = ".".join(p.split(".")[:3])
        groups[key].append(p)
    out = {k: sorted(v) for k, v in sorted(groups.items())}
    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
    print(file=sys.stdout)
    # stderr 打印我们关心的关键域
    for want in ("Vehicle.Chassis.Tire", "Vehicle.Body.DoorLockStatus",
                 "Vehicle.ConnectManager", "Vehicle.Location",
                 "Vehicle.Powertrain.Battery", "Vehicle.Cabin.AC"):
        print(f"-- {want}: {len(groups.get(want, []))} paths", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
