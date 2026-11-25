import json
import os
from pathlib import Path

hyperos_series = {
    "Xiaomi 17 系列": ["popsicle", "pudding", "pandora", "nezha", "byron", "chagall", "warhol"],
    "Xiaomi 15 系列": ["dada", "haotian", "xuanyuan", "dijun", "goya", "klimt"],
    "Xiaomi 14 系列": ["houji", "shennong", "aurora", "degas", "rothko", "chenfeng"],
    "REDMI K90 系列": ["annibale", "prague", "myron"],
    "REDMI K80 系列": ["zorn", "miro", "dali"],
    "REDMI Turbo 5 系列": ["klee", "dash"],
    "REDMI Turbo 4 系列": ["rodin", "onyx"],
}
samsung_series = {
    "s23": ["dm1q", "dm2q", "dm3q"],
    "s24": ["e1q", "e2q", "e3q"],
    "s25": ["pa1q", "pa2q", "pa3q"],
}


def select(target, family):
    if family:
        prefix = {"xiaomi": "Xiaomi ", "redmi": "REDMI "}[family]
        groups = {k: v for k, v in hyperos_series.items() if k.startswith(prefix)}
        if target in groups:
            return target, groups[target]
        code = target.rsplit(" (", 1)[-1].removesuffix(")")
        for group, devices in groups.items():
            if code in devices:
                return group, [code]
    else:
        group, _, code = target.partition("/")
        devices = samsung_series.get(group, [])
        if devices and (not code or code in devices):
            return group, [code] if code else devices
    raise ValueError(f"未知机型或系列 {target}")


if __name__ == "__main__":
    group, devices = select(os.environ["TARGET"], os.getenv("FAMILY"))
    if len(devices) != 1 and (os.getenv("CUSTOM_URL") or os.getenv("SUPER_SIZE")):
        raise ValueError("自定义固件链接或 super 大小需要选择单一机型")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        print(f"devices={json.dumps(devices)}", f"series={group}", sep="\n", file=output)
