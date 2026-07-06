"""Opcode decoding helpers for the ``block_summary`` sparse parallel arrays.

Promoted verbatim from the original EDA notebook (since removed).

``block_summary`` stores ``opcode``, ``opcode_count``, ``opcode_gas_baseline``,
``opcode_gas_schedule`` as **sparse parallel arrays** — element *i* of all four
describes the same opcode, whose EVM byte is ``opcode[i]``. Two gotchas:

- Over the HTTP driver, ClickHouse ``Array`` columns come back as **string
  reprs** (``"[1,2,3]"``), not Python lists — parse with ``json.loads``
  (:func:`parse_arr`). Same applies to ``gas_delta_log2_hist`` /
  ``multiplier_log2_hist``.
- No opcode-name library is installed, so we ship a compact byte->mnemonic table
  (Cancun/Prague set; PUSH/DUP/SWAP/LOG generated; ``UNKNOWN_0x..`` fallback).
"""

import json

import pandas as pd

# EVM opcode byte -> mnemonic (canonical encodings). PUSH/DUP/SWAP/LOG ranges are
# generated; the rest come from this compact "hex name" table.
_OPCODE_TABLE = """
00 STOP 01 ADD 02 MUL 03 SUB 04 DIV 05 SDIV 06 MOD 07 SMOD 08 ADDMOD 09 MULMOD 0a EXP 0b SIGNEXTEND
10 LT 11 GT 12 SLT 13 SGT 14 EQ 15 ISZERO 16 AND 17 OR 18 XOR 19 NOT 1a BYTE 1b SHL 1c SHR 1d SAR
20 KECCAK256
30 ADDRESS 31 BALANCE 32 ORIGIN 33 CALLER 34 CALLVALUE 35 CALLDATALOAD 36 CALLDATASIZE 37 CALLDATACOPY 38 CODESIZE 39 CODECOPY 3a GASPRICE 3b EXTCODESIZE 3c EXTCODECOPY 3d RETURNDATASIZE 3e RETURNDATACOPY 3f EXTCODEHASH
40 BLOCKHASH 41 COINBASE 42 TIMESTAMP 43 NUMBER 44 PREVRANDAO 45 GASLIMIT 46 CHAINID 47 SELFBALANCE 48 BASEFEE 49 BLOBHASH 4a BLOBBASEFEE
50 POP 51 MLOAD 52 MSTORE 53 MSTORE8 54 SLOAD 55 SSTORE 56 JUMP 57 JUMPI 58 PC 59 MSIZE 5a GAS 5b JUMPDEST 5c TLOAD 5d TSTORE 5e MCOPY 5f PUSH0
f0 CREATE f1 CALL f2 CALLCODE f3 RETURN f4 DELEGATECALL f5 CREATE2 fa STATICCALL fd REVERT fe INVALID ff SELFDESTRUCT
"""
_toks = _OPCODE_TABLE.split()
OPCODES = {int(_toks[i], 16): _toks[i + 1] for i in range(0, len(_toks), 2)}
OPCODES.update({0x60 + i: f"PUSH{i + 1}" for i in range(32)})
OPCODES.update({0x80 + i: f"DUP{i + 1}" for i in range(16)})
OPCODES.update({0x90 + i: f"SWAP{i + 1}" for i in range(16)})
OPCODES.update({0xA0 + i: f"LOG{i}" for i in range(5)})


def parse_arr(v):
    """Decode a ClickHouse Array column, returned as a string repr over the HTTP driver."""
    return v if isinstance(v, (list, tuple)) else json.loads(v)


def opcode_name(b):
    """Map an EVM opcode byte to its mnemonic."""
    return OPCODES.get(int(b), f"UNKNOWN_0x{int(b):02x}")


def explode_opcodes(row):
    """Expand the parallel opcode arrays of one block_summary row into a tidy frame."""
    df = pd.DataFrame(
        {
            "opcode_byte": parse_arr(row["opcode"]),
            "count": parse_arr(row["opcode_count"]),
            "gas_baseline": parse_arr(row["opcode_gas_baseline"]),
            "gas_schedule": parse_arr(row["opcode_gas_schedule"]),
        }
    )
    df["opcode"] = df["opcode_byte"].map(opcode_name)
    return df
