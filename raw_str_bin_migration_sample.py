"""
历史 MessagePack raw -> str/bin 字段契约迁移样例
=================================================

背景
----
旧版 msgpack（pack 时 ``use_bin_type=False``）只有 raw 一种字节串类型，
列名、文本、二进制全部走 raw（wire type ``a*``）。新版 msgpack 区分
str（``a*``，合法 UTF-8）与 bin（``c4/c5/c6``）。本样例演示如何按
"字段契约" 把旧 raw 记录安全迁移到 str/bin，并对无法证明安全性的记录
原样保留。

四类迁移结论
------------
BYTE_PERFECT  原字节可还原：新包与旧包逐字节相同（全 str 记录天然成立）
VALUE_ONLY    仅业务值可还原：值完整恢复，但 raw->bin 改变了 wire 字节
PARTIAL       只剩普通 dict：仅各契约共同能证明的字段可恢复，其余列明损失
UNSAFE        不可安全迁移：保留原始 blob，附原因（撞键/未知版本/契约歧义/
              判别列缺失/UTF-8 不合法/目标 schema 不允许 bin 键）

两个关键事实（见文件末尾 demo 的断言）
--------------------------------------
1. ``unpackb(blob, raw=False)`` 走普通 dict 路径，map 重复键 last-wins，
   两条不同记录可能解包出 *完全相等* 的 dict，却应得到不同迁移结论。
2. ``unpackb(blob, raw=True, object_pairs_hook=list)`` 保留全部现场：
   重复键、键值对顺序、未解码字节——契约迁移必须基于这份现场，而不是 dict。

样例记录均为扁平 map；嵌套 map 下 object_pairs_hook 会递归生效，原则相同。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from msgpack import Packer, unpackb

legacy_pack = Packer(use_bin_type=False)  # 旧写法：bytes -> raw(a*)
modern_pack = Packer(use_bin_type=True)   # 新写法：bytes -> bin，str -> str


class Verdict(str, Enum):
    BYTE_PERFECT = "BYTE_PERFECT"
    VALUE_ONLY = "VALUE_ONLY"
    PARTIAL = "PARTIAL"
    UNSAFE = "UNSAFE"


# ---------------------------------------------------------------------------
# 契约定义
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FieldSpec:
    name: bytes               # 旧 raw 键
    value_type: str           # "str" | "bin" | "any"(非 raw，如 int)
    key_type: str = "str"     # 迁移后键类型："str" | "bin"
    versions: frozenset[int] = frozenset()  # 空=适用所有已知版本


@dataclass(frozen=True)
class TableContract:
    table: str
    version_field: bytes | None
    known_versions: frozenset[int]
    fields: tuple[FieldSpec, ...]
    # 判别列：(判别字段, {判别值: {被覆盖字段: 新值类型}})
    # 判别列允许排在受影响的 raw 值后面——引擎先全表扫描再定类型。
    discriminator: tuple[bytes, dict[Any, dict[bytes, str]]] | None = None
    allow_bin_keys: bool = False  # 目标 schema 是否允许 bin 类型 map 键

    def specs_for(self, key: bytes) -> list[FieldSpec]:
        return [f for f in self.fields if f.name == key]

    def version_of(self, pairs: list[tuple[Any, Any]]) -> Any:
        if self.version_field is None:
            return None
        vals = [v for k, v in pairs if k == self.version_field]
        return vals[0] if len(vals) == 1 else None


# events 表：body 的类型由判别列 type 决定；sig 仅 v2 存在且为二进制
EVENTS = TableContract(
    table="events",
    version_field=b"ver",
    known_versions=frozenset({1, 2}),
    fields=(
        FieldSpec(b"id", "str"),
        FieldSpec(b"name", "str"),
        FieldSpec(b"type", "str"),
        FieldSpec(b"body", "str"),  # 默认 str；type=image 时覆盖为 bin
        FieldSpec(b"sig", "bin", versions=frozenset({2})),
        FieldSpec(b"ver", "any"),
    ),
    discriminator=(b"type", {
        b"note": {b"body": "str"},
        b"image": {b"body": "bin"},
    }),
)

# 病态遗留表：文本键 name 与二进制键 b"name" 在旧 wire 上是同一串 raw 字节，
# 契约层声明为两个字段。默认目标 schema 只允许 str 键 -> 二者必然撞键。
SHADOWS = TableContract(
    table="shadows",
    version_field=None,
    known_versions=frozenset(),
    fields=(
        FieldSpec(b"name", "str", key_type="str"),
        FieldSpec(b"name", "bin", key_type="bin"),
    ),
)

# logs 表有两个互斥版本契约：body 在 v1 是 str、v2 是 bin。
LOGS_V1 = TableContract(
    table="logs",
    version_field=b"ver",
    known_versions=frozenset({1}),
    fields=(FieldSpec(b"id", "str"), FieldSpec(b"body", "str"),
            FieldSpec(b"ver", "any")),
)
LOGS_V2 = TableContract(
    table="logs",
    version_field=b"ver",
    known_versions=frozenset({2}),
    fields=(FieldSpec(b"id", "str"), FieldSpec(b"body", "bin"),
            FieldSpec(b"ver", "any")),
)

CONTRACTS: dict[str, list[TableContract]] = {
    "events": [EVENTS],
    "shadows": [SHADOWS],
    "logs": [LOGS_V1, LOGS_V2],
}


# ---------------------------------------------------------------------------
# 现场保全：raw=True + pairs hook，拒绝普通 dict 的 last-wins 折叠
# ---------------------------------------------------------------------------

def unpack_evidence(blob: bytes) -> list[tuple[Any, Any]]:
    """raw 键值一律还原为 bytes，map 以有序 (k, v) 列表返回，重复键不折叠。"""
    return unpackb(blob, raw=True, object_pairs_hook=list)


@dataclass
class MigrationResult:
    table: str
    verdict: Verdict
    original: bytes
    migrated: bytes | None = None
    reasons: list[str] = field(default_factory=list)
    recovered: dict[str, Any] | None = None      # PARTIAL 时的部分行
    losses: dict[str, list[str]] | None = None   # PARTIAL 时的不可恢复清单


class _Unsafe(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ---------------------------------------------------------------------------
# 契约选择与类型判定
# ---------------------------------------------------------------------------

def _pair_keys(pairs: list[tuple[Any, Any]]) -> list[bytes]:
    return [k for k, _ in pairs]


def _contract_explains(contract: TableContract,
                       pairs: list[tuple[Any, Any]],
                       version: Any) -> bool:
    for key in _pair_keys(pairs):
        specs = contract.specs_for(key)
        if not specs:
            return False  # 契约不认识的字段
        if version is not None and any(
            s.versions and version not in s.versions for s in specs
        ):
            return False
    return True


def select_contracts(table: str,
                     pairs: list[tuple[Any, Any]]) -> list[TableContract]:
    """返回所有能解释该记录的契约；0 个=未知版本，>1 个=契约歧义。"""
    candidates: list[TableContract] = []
    for contract in CONTRACTS[table]:
        version = contract.version_of(pairs)
        if contract.version_field is not None and version is not None:
            if version not in contract.known_versions:
                continue
        if _contract_explains(contract, pairs, version):
            candidates.append(contract)
    return candidates


def _discriminator_value(contract: TableContract,
                         pairs: list[tuple[Any, Any]]) -> Any:
    if contract.discriminator is None:
        return None
    dfield, table = contract.discriminator
    vals = [v for k, v in pairs if k == dfield]
    if len(vals) != 1:
        raise _Unsafe(f"判别列 {dfield!r} 缺失或重复，无法选择字段类型")
    if vals[0] not in table:
        raise _Unsafe(f"判别列 {dfield!r} 出现未知值 {vals[0]!r}")
    return vals[0]


def _effective_value_type(contract: TableContract, spec: FieldSpec,
                          disc_value: Any) -> str:
    if contract.discriminator is not None:
        _, overrides = contract.discriminator
        if disc_value in overrides and spec.name in overrides[disc_value]:
            return overrides[disc_value][spec.name]
    return spec.value_type


def _convert_value(raw_value: Any, value_type: str, where: bytes) -> Any:
    if value_type == "any":
        return raw_value
    if not isinstance(raw_value, bytes):
        raise _Unsafe(f"字段 {where!r} 契约要求 raw，实际类型 {type(raw_value)}")
    if value_type == "bin":
        return raw_value  # raw -> bin，业务字节不变，wire 类型改变
    try:
        return raw_value.decode("utf-8")  # str：严格 UTF-8，失败即 UNSAFE
    except UnicodeDecodeError:
        raise _Unsafe(f"字段 {where!r} 契约要求 str，但 raw 不是合法 UTF-8")


def _convert_key(raw_key: bytes, key_type: str) -> Any:
    if key_type == "bin":
        return raw_key
    return raw_key.decode("utf-8")  # 契约内声明的键本身保证合法 UTF-8


# ---------------------------------------------------------------------------
# 主迁移路径：基于完整现场（有序 raw pairs）
# ---------------------------------------------------------------------------

def migrate_record(table: str, blob: bytes) -> MigrationResult:
    pairs = unpack_evidence(blob)
    candidates = select_contracts(table, pairs)

    if not candidates:
        return MigrationResult(table, Verdict.UNSAFE, blob,
                               reasons=["没有任何已知契约能解释该记录（未知版本/未知字段），原样保留"])
    if len(candidates) > 1:
        return MigrationResult(table, Verdict.UNSAFE, blob,
                               reasons=[f"{len(candidates)} 个契约同时成立，无法唯一选择，原样保留"])

    contract = candidates[0]
    try:
        disc_value = _discriminator_value(contract, pairs)
        new_pairs: list[tuple[Any, Any]] = []
        for index, (raw_key, raw_value) in enumerate(pairs):
            specs = contract.specs_for(raw_key)
            if len(specs) > 1:
                # 同 raw 键字节对应多个字段（str 键 / bin 键），旧 wire 无类型信息，
                # 只能按契约声明顺序配对——确定性不足时直接拒绝。
                if index >= len(specs):
                    raise _Unsafe(f"键 {raw_key!r} 的配对超出契约声明，无法消歧")
                spec = specs[index]
            else:
                spec = specs[0]
            value_type = _effective_value_type(contract, spec, disc_value)
            new_key = _convert_key(raw_key, spec.key_type)
            new_value = _convert_value(raw_value, value_type, raw_key)
            new_pairs.append((new_key, new_value))

            if spec.key_type == "bin" and not contract.allow_bin_keys:
                raise _Unsafe(
                    f"字段 {raw_key!r} 迁移后是 bin 键，目标 schema 只允许 str 键；"
                    "与同名 str 键归一化后必然撞键"
                )

        # 撞键检查：Python 语义与 msgpack 一致——str 与 bytes 永不相等，
        # 同类型同值才撞键（即重复键迁成同一个新键）。
        seen: dict[Any, Any] = {}
        for new_key, _ in new_pairs:
            if new_key in seen:
                raise _Unsafe(
                    f"重复键迁移后撞在同一个新键 {new_key!r} 上，"
                    "普通 dict 会 last-wins 静默丢值，拒绝迁移"
                )
            seen[new_key] = True

        migrated = modern_pack.pack(dict(new_pairs))
    except _Unsafe as exc:
        return MigrationResult(table, Verdict.UNSAFE, blob, reasons=[exc.reason])

    if migrated == blob:
        return MigrationResult(table, Verdict.BYTE_PERFECT, blob, migrated)
    return MigrationResult(table, Verdict.VALUE_ONLY, blob, migrated)


# ---------------------------------------------------------------------------
# 降级路径：只剩普通 dict（raw=False 的产物，重复键已被折叠）
# ---------------------------------------------------------------------------

def migrate_plain_dict(table: str, plain: dict[str, Any]) -> MigrationResult:
    """
    仅凭 dict 能证明什么？
    - 只恢复在 *所有* 候选契约里类型一致、且当前值满足该类型的字段；
    - 跨契约类型不一致的字段（如 logs.body: str vs bin）不可证明，放弃；
    - 被 last-wins 覆盖的值、非法 UTF-8 的 bin 行（根本进不了 dict）、
      被折叠/丢弃键值对的位置，都无法推回。
    """
    contracts = CONTRACTS[table]
    recovered: dict[str, Any] = {}
    lost_keys: list[str] = []

    for key, value in plain.items():
        raw_key = key.encode("utf-8")
        specs_per_contract = [c.specs_for(raw_key) for c in contracts]
        if any(not specs for specs in specs_per_contract):
            lost_keys.append(f"{key!r}: 不是所有候选契约都认识该键")
            continue

        value_types = {specs[0].value_type for specs in specs_per_contract}
        if len(value_types) > 1:
            lost_keys.append(
                f"{key!r}: 各契约类型不一致 {sorted(value_types)}，无法证明该值应归 str 还是 bin"
            )
            continue

        value_type = next(iter(value_types))
        if value_type == "str":
            recovered[key] = value  # dict 里本就是 str
        elif value_type == "bin":
            # 能进 dict 说明原 raw 是合法 UTF-8，重新编码可拿回原字节；
            # 非法 UTF-8 的 bin 行在 unpackb(raw=False) 阶段就已抛错，无从恢复。
            recovered[key] = value.encode("utf-8")

    losses = {
        "lost_keys": lost_keys,
        "lost_values": [
            "map 重复键被 last-wins 折叠：被覆盖值的数量与内容仅凭 dict 不可知",
            "非法 UTF-8 的二进制值无法进入 raw=False 的 dict，对应整行不可达",
        ],
        "lost_order": [
            "被折叠/丢弃键值对的原始位置不可推回",
            "若 dict 曾被重建或经 JSON 等中转，幸存键的顺序也不再可证",
            "（直接由 msgpack 解包的 dict 仅保留幸存键的相对插入顺序）",
        ],
    }
    # dict-only 路径没有原始 blob，original 置空；产物只声称包含可证明的字段
    return MigrationResult(table, Verdict.PARTIAL, b"",
                           migrated=modern_pack.pack(recovered),
                           recovered=recovered, losses=losses)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

def _legacy(pairs: list[tuple[Any, Any]]) -> bytes:
    return legacy_pack.pack_map_pairs(pairs)


def main() -> None:
    print("=" * 72)
    print("一、基于完整现场（raw=True + object_pairs_hook）的契约迁移")
    print("=" * 72)

    # 1) 全 str 文本记录 -> 原字节可还原
    note = _legacy([(b"id", b"7"), (b"name", b"Ada"), (b"type", b"note"),
                    (b"body", b"hello"), (b"ver", 1)])

    # 2) 判别列 type 排在 body 之后；body/sig 是二进制 -> 仅业务值可还原
    image = _legacy([(b"id", b"8"), (b"body", b"\x89PNG\r\n\xff"),
                     (b"type", b"image"), (b"sig", b"\x00\x01"), (b"ver", 2)])

    # 3) 重复 raw 键 name（Ada 被 Bob 覆盖）-> 撞键，不可安全迁移
    dup_name = _legacy([(b"id", b"7"), (b"name", b"Ada"), (b"name", b"Bob"),
                        (b"type", b"note"), (b"body", b"hello"), (b"ver", 1)])

    # 4) 与 3) 解包成同一个普通 dict 的干净记录 -> 原字节可还原
    clean_same_dict = _legacy([(b"id", b"7"), (b"name", b"Bob"),
                               (b"type", b"note"), (b"body", b"hello"),
                               (b"ver", 1)])

    # 5) 未知版本 -> 原样保留
    unknown_ver = _legacy([(b"id", b"9"), (b"ver", 99)])

    # 6) 契约要求 str 的字段装了非法 UTF-8（判别列齐全，排除其他失败原因）
    bad_utf8 = _legacy([(b"id", b"9"), (b"name", b"\xff\xfe"),
                        (b"type", b"note"), (b"body", b"x"), (b"ver", 1)])

    # 7) 文本键与二进制键在旧 wire 上同为 raw b"name" -> 默认 str-only 目标撞键
    shadow = _legacy([(b"name", b"Ada"), (b"name", b"\x01\x02")])

    cases = [
        ("note(全 str)", "events", note),
        ("image(判别列在 raw 后, 含 bin)", "events", image),
        ("dup_name(重复键)", "events", dup_name),
        ("clean_same_dict", "events", clean_same_dict),
        ("unknown_ver(未知版本)", "events", unknown_ver),
        ("bad_utf8(str 字段非法 UTF-8)", "events", bad_utf8),
        ("shadow(文本键/字节键同名)", "shadows", shadow),
    ]

    results: dict[str, MigrationResult] = {}
    for label, table, blob in cases:
        result = migrate_record(table, blob)
        results[label] = result
        print(f"\n[{label}]  -> {result.verdict.value}")
        for reason in result.reasons:
            print(f"    原因: {reason}")
        if result.migrated is not None:
            same_bytes = result.migrated == blob
            print(f"    新旧 blob 逐字节相同: {same_bytes}")
            if not same_bytes:
                old_pairs = unpack_evidence(blob)
                new_map = unpackb(result.migrated, raw=False)
                print(f"    业务值核对: 旧现场 {old_pairs}")
                print(f"               新解包 {new_map}")

    print("\n" + "=" * 72)
    print("二、两份普通 dict 解包后相同，迁移结论却不同")
    print("=" * 72)
    dict_dup = unpackb(dup_name, raw=False)
    dict_clean = unpackb(clean_same_dict, raw=False)
    print(f"dup_name       普通 dict: {dict_dup}")
    print(f"clean_same     普通 dict: {dict_clean}")
    print(f"两个 dict 相等: {dict_dup == dict_clean}")
    print(f"dup_name 结论:   {results['dup_name(重复键)'].verdict.value}")
    print(f"clean_same 结论: {results['clean_same_dict'].verdict.value}")

    print("\n" + "=" * 72)
    print("三、只剩普通 dict 时的部分恢复")
    print("=" * 72)

    # 3a) events：单契约，幸存字段都可证明，但重复键丢失的值无从得知
    partial_events = migrate_plain_dict("events", dict_clean)
    print("\n[events dict-only]")
    print(f"  结论: {partial_events.verdict.value}")
    print(f"  可恢复字段: {partial_events.recovered}")
    print(f"  不可恢复: {partial_events.losses}")

    # 3b) logs：缺版本字段，v1/v2 两个契约都说得通 -> 现场路径 UNSAFE
    ambiguous_logs = _legacy([(b"id", b"9"), (b"body", b"plain text")])
    logs_verdict = migrate_record("logs", ambiguous_logs)
    print("\n[logs 完整现场] 两个契约同时成立:")
    print(f"  结论: {logs_verdict.verdict.value} / {logs_verdict.reasons[0]}")

    partial_logs = migrate_plain_dict("logs", unpackb(ambiguous_logs, raw=False))
    print("[logs dict-only]")
    print(f"  结论: {partial_logs.verdict.value}")
    print(f"  可恢复字段: {partial_logs.recovered}  (id 在两契约里都是 str)")
    print(f"  放弃的键: {partial_logs.losses['lost_keys']}")

    print("\n" + "=" * 72)
    print("四、断言自检")
    print("=" * 72)

    assert dict_dup == dict_clean, "两条记录的普通 dict 必须相等"
    assert results["dup_name(重复键)"].verdict is Verdict.UNSAFE
    assert results["clean_same_dict"].verdict is Verdict.BYTE_PERFECT
    assert results["note(全 str)"].migrated == note, "全 str 记录必须逐字节可还原"

    image_result = results["image(判别列在 raw 后, 含 bin)"]
    assert image_result.verdict is Verdict.VALUE_ONLY
    assert image_result.migrated != image, "raw->bin 必然改变 wire 字节"
    assert unpackb(image_result.migrated, raw=False) == {
        "id": "8", "body": b"\x89PNG\r\n\xff", "type": "image",
        "sig": b"\x00\x01", "ver": 2,
    }, "业务值必须完整还原"

    for label in ("unknown_ver(未知版本)", "bad_utf8(str 字段非法 UTF-8)",
                  "shadow(文本键/字节键同名)"):
        r = results[label]
        assert r.verdict is Verdict.UNSAFE and r.migrated is None
        assert r.original is not None and len(r.original) > 0, "原始记录必须原样保留"

    assert logs_verdict.verdict is Verdict.UNSAFE
    assert partial_logs.recovered == {"id": "9"}
    assert partial_events.recovered["name"] == "Bob"  # 只剩 last-wins 的值

    # 附：若显式允许 bin 键，str 'name' 与 bin b'name' 在新格式里可共存，
    # 这是 msgpack 类型系统提供的逃生舱，而非默认行为。
    typed_contract = TableContract(
        table="shadows", version_field=None, known_versions=frozenset(),
        fields=(FieldSpec(b"name", "str", key_type="str"),
                FieldSpec(b"name", "bin", key_type="bin")),
        allow_bin_keys=True,
    )
    CONTRACTS["shadows_typed"] = [typed_contract]
    rescued = migrate_record("shadows_typed", shadow)
    assert rescued.verdict is Verdict.VALUE_ONLY
    assert unpackb(rescued.migrated, raw=False) == {"name": "Ada", b"name": b"\x01\x02"}
    print("显式开启 bin 键后，shadow 记录可保留两个同名字段:",
          unpackb(rescued.migrated, raw=False))

    print("\n全部断言通过。")


if __name__ == "__main__":
    main()
